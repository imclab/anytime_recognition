'''
mpiclassify
====
Provides an MPI interface that trains linear classifiers that can be represented
by
    \min_w     1/N * sum_n L(y_n,w'x_n+b) + gamma * Reg(w)

This algorithm only deals with the primal case (no dual), assuming that there 
are more data points than the number of feature dimension (if not, you might 
want to look for dual solvers to your problem). We use L-BFGS as the default
solver, and if the loss function or regularizer is not differentiable everywhere
(like the v-style L1 regularizer), we will use the subgradient methods.
'''

from iceberk import cpputil, mathutil, mpi, util
import inspect
import logging
import numpy as np
# The inner1d function is imported here to do more memory-efficient sum of
# squares. For example, if a.size = [300,100], inner1d(a,a) is equivalent to
# (a**2).sum(axis=1) but does not create additional space.
from numpy.core.umath_tests import inner1d
from scipy import optimize
from sklearn import metrics

_FMIN = optimize.fmin_l_bfgs_b

def to_one_of_k_coding(Y, fill = -1, K = None):
    '''Convert the vector Y into one-of-K coding. The element will be either
    fill (-1 in default) or 1. If K is None, the number of classes is 
    determined by Y.max().
    '''
    if Y.ndim > 1:
        raise ValueError, "The input Y should be a vector."
    if K is None:
        K = mpi.COMM.allreduce(Y.max(), op=max) + 1
    Yout = np.ones((len(Y), K)) * fill
    valid = (Y >= 0) & (Y < K)
    Yout[np.arange(len(Y))[valid], Y.astype(int)[valid]] = 1
    return Yout

def feature_meanstd(mat, reg = None):
    '''
    Utility function that does distributed mean and std computation
    Input:
        mat: the local data matrix, each row is a feature vector and each 
             column is a feature dim
        reg: if reg is not None, the returned std is computed as
            std = np.sqrt(std**2 + reg)
    Output:
        m:      the mean for each dimension
        std:    the standard deviation for each dimension
    
    The implementation is actually moved to iceberk.mathutil now, we leave the
    code here just for backward compatibility
    '''
    m, std = mathutil.mpi_meanstd(mat)

    if reg is not None:
        std = np.sqrt(std**2 + reg)
    return m, std


class Solver(object):
    '''
    Solver is the general solver to deal with bookkeeping stuff
    '''
    def __init__(self, gamma, loss, reg,
                 args = {}, lossargs = {}, regargs = {}, fminargs = {}):
        '''
        Initializes the solver.
        Input:
            gamma: the regularization parameter
            loss: the loss function. Should accept three variables Y, X and W,
                where Y is a vector in {labels}^(num_data), X is a matrix of size
                [num_data,nDim], and W is a vector of size nDim. It returns
                the loss function value and the gradient with respect to W.
            reg: the regularizaiton func. Should accept a vector W of
                shape nDim and returns the regularization term value and
                the gradient with respect to W.
            args: the arguments for the solver in general.
            lossargs: the arguments that should be passed to the loss function
            regargs: the arguments that should be passed to the regularizer
            fminargs: additional arguments that you may want to pass to fmin.
                you can check the fmin function to see what arguments can be
                passed (like display options: {'disp':1}).
        '''
        self._gamma = gamma
        self.loss = loss
        self.reg = reg
        self._args = args.copy()
        self._lossargs = lossargs.copy()
        self._regargs = regargs.copy()
        self._fminargs = fminargs.copy()
        self._add_default_fminargs()
    
    def _add_default_fminargs(self):
        '''
        This function adds some default args to fmin, if we have not explicitly
        specified them.
        '''
        self._fminargs['maxfun'] = self._fminargs.get('maxfun', 1000)
        self._fminargs['disp'] = self._fminargs.get('disp', 1)
        # even when fmin displays outputs, we set non-root display to none
        if not mpi.is_root():
            self._fminargs['disp'] = 0
            
    @staticmethod
    def obj(wb, solver):
        """The objective function to be used by fmin
        """
        raise NotImplementedError
    
    def presolve(self, X, Y, weight, param_init, K = None):
        """This function is called before we call lbfgs. It should return a
        vector that is the initialization of the lbfgs, and does any preparation
        (such as creating caches) for the optimization.
        """
        raise NotImplementedError
    
    def postsolve(self, lbfgs_result):
        """This function deals with the post-processing of the lbfgs result. It
        should return the optimal parameter for the classifier.
        """
        raise NotImplementedError
    
    def solve(self, X, Y, weight = None, param_init = None, presolve = True, K = None):
        """The solve function
        """
        if presolve:
            param_init = self.presolve(X, Y, weight, param_init, K)
        else:
            # if no presolve is needed, we simply replace the training data
            self._X = X
            self._Y = Y
            self._weight = weight
        logging.debug('Solver: running lbfgs...')
        result = _FMIN(self.__class__.obj, param_init, 
                       args=[self], **self._fminargs)
        return self.postsolve(result)


class SolverMC(Solver):
    '''SolverMC is a multi-dimensional wrapper
    For the input Y, it could be either a vector of the labels
    (starting from 0), or a matrix whose values are -1 or 1. You 
    need to manually make sure that the input Y format is consistent
    with the loss function though.
    '''
    def __init__(self, *args, **kwargs):
        super(SolverMC, self).__init__(*args, **kwargs)
        self._pred = None
        self._glocal = None
        self._g = None
        self._gpred = None
        self._gpredcache = []

    @staticmethod
    def flatten_params(params):
        if type(params) is np.array:
            return params
        elif type(params) is list or type(params) is tuple:
            return np.hstack((p.flatten() for p in params))
        else:
            raise TypeError, "Unknown input type: %s." % (repr(type(params)))

    def presolve(self, X, Y, weight, param_init, K = None):
        self._iter = 0
        self._X = X.reshape((X.shape[0],np.prod(X.shape[1:])))
        # determine the number of classes.
        if K is not None:
            self._K = K
        elif len(Y.shape) == 1:
            self._K = mpi.COMM.allreduce(Y.max(), op=max) + 1
        else:
            # We treat Y as a two-dimensional matrix
            Y = Y.reshape((Y.shape[0],np.prod(Y.shape[1:])))
            self._K = Y.shape[1]
        self._Y = Y
        self._weight = weight
        # compute the number of data
        if weight is None:
            self._num_data = mpi.COMM.allreduce(X.shape[0])
        else:
            self._num_data = mpi.COMM.allreduce(weight.sum())
        self._dim = self._X.shape[1]
        if self._pred is None:
            self._pred = np.empty((X.shape[0], self._K), dtype = X.dtype)
        else:
            self._pred.resize(X.shape[0], self._K)
        if param_init is None:
            param_init = np.zeros(self._K * (self._dim+1))
        else:
            # the initialization is w and b
            param_init = SolverMC.flatten_params(param_init) 
        # gradient cache
        if self._glocal is None:
            self._glocal = np.empty(param_init.shape)
            self._g = np.empty(param_init.shape)
        else:
            self._glocal.resize(param_init.shape)
            self._g.resize(param_init.shape)
        # depending on the loss function, we choose whether we want to do
        # gpred cache
        if len(inspect.getargspec(self.loss)[0]) == 5:
            #logging.debug('Using gpred cache')
            self.gpredcache = True
            if self._gpred is None:
                self._gpred = np.empty((X.shape[0], self._K))
            else:
                self._gpred.resize(X.shape[0], self._K)
        else:
            self.gpredcache = False
        # just to make sure every node is on the same page
        mpi.COMM.Bcast(param_init)
        # for debugging, we report the initial function value.
        #f = SolverMC.obj(param_init, self)[0]
        #logging.debug("Initial function value: %f." % f)
        return param_init
    
    def unflatten_params(self, wb):
        K = self._K
        w = wb[: K * self._dim].reshape(self._dim, K).copy()
        b = wb[K * self._dim :].copy()
        return w, b
    
    def postsolve(self, lbfgs_result):
        wb = lbfgs_result[0]
        logging.debug("Final function value: %f." % lbfgs_result[1])
        return self.unflatten_params(wb)
    
    @staticmethod
    def obj(wb,solver):
        '''
        The objective function used by fmin
        '''
        # obtain w and b
        K = solver._K
        dim = solver._dim
        w = wb[:K*dim].reshape((dim, K))
        b = wb[K*dim:]
        # pred is a matrix of size [num_datalocal, K]
        mathutil.dot(solver._X, w, out = solver._pred)
        solver._pred += b
        # compute the loss function
        if solver.gpredcache:
            flocal,gpred = solver.loss(solver._Y, solver._pred, solver._weight,
                                       solver._gpred, solver._gpredcache,
                                       **solver._lossargs)
        else:
            flocal,gpred = solver.loss(solver._Y, solver._pred, solver._weight,
                                       **solver._lossargs)
        mathutil.dot(solver._X.T, gpred,
                     out = solver._glocal[:K*dim].reshape(dim, K))
        solver._glocal[K*dim:] = gpred.sum(axis=0)
        # we should normalize them with the number of data
        flocal /= solver._num_data
        solver._glocal /= solver._num_data
        # add regularization term, but keep in mind that we have multiple nodes
        # so we only carry it out on root to make sure we only added one 
        # regularization term
        if mpi.is_root():
            freg, greg = solver.reg(w, **solver._regargs)
            flocal += solver._gamma * freg
            solver._glocal[:K*dim] += solver._gamma * greg.ravel()
        # do mpi reduction
        mpi.barrier()
        f = mpi.COMM.allreduce(flocal)
        mpi.COMM.Allreduce(solver._glocal, solver._g)
        ######### DEBUG PART ##############
        if np.isnan(f):
            # check all the components to see what went wrong.
            print 'rank %s: isnan X: %d' % (mpi.RANK,np.any(np.isnan(solver._X)))
            print 'rank %s: isnan Y: %d' % (mpi.RANK,np.any(np.isnan(solver._Y)))
            print 'rank %s: isnan flocal: %d' % (mpi.RANK,np.any(np.isnan(flocal)))
            print 'rank %s: isnan pred: %d' % (mpi.RANK,np.any(np.isnan(solver._pred)))
            print 'rank %s: isnan w: %d' % (mpi.RANK,np.any(np.isnan(w)))
            print 'rank %s: isnan b: %d' % (mpi.RANK,np.any(np.isnan(b)))
        return f, solver._g


class SolverStochastic(Solver):
    """A stochastic solver following existing papers in the literature. The
    method creates minibatches and runs LBFGS (using SolverMC) or Adagrad for
    a few iterations, then moves on to the next minibatch.
    
    The solver should have the following args:
        'mode': the basic solver. Currently 'LBFGS' or 'Adagrad', with LBFGS
            as default.
        'base_lr': the base learning rate (if using Adagrad as the solver).
        'eta': the initial gradient accumulation regularization term (if 
            using Adagrad as the solver)
        'minibatch': the batch size
        'num_iter': the number of iterations to carry out. Note that if you
            use LBFGS, how many iterations to carry out on one minibatch is
            defined in the max_iter parameter defined in fminargs. If you use
            Adagrad, each minibatch will be used once to compute the function
            value and the gradient, and then discarded.
        'callback': the callback function after each LBFGS iteration. It
            should take the result output by the solver.solve() function and
            return whatever that can be converted to a string by str(). If 
            callback is a list, then every entry in the list is a callback 
            function, and they will be carried out sequentially.
        'dump_every': the interval between two parameter dumps.
        'dump_name': the name for the dump file. The format will be cPickle.
    """
    def solve(self, sampler, param_init = None, K = None,
             resume = None, new_lr = None):
        """The solve function.
        Input:
            sampler: the data sampler. sampler.sample() should return a list
                of training data, either (X, Y, weight) or (X, Y, None)
                depending on whether weight is enforced.
            param_init: the initial parameter. See SolverMC for details.
        """
        mode = self._args.get('mode', 'lbfgs').lower()
        # even when we use Adagrad we create a solver_basic to deal with
        # function value and gradient computation, etc.
        solver_basic = SolverMC(self._gamma, self.loss, self.reg,
                self._args, self._lossargs, self._regargs,
                self._fminargs)
        param = param_init
        iter_start = 0
        if resume is not None:
            # load data from
            logging.debug('Resuming from %s' % resume)
            npzdata = np.load(resume)
            param = (npzdata['w'], npzdata['b'])
            iter_start = npzdata['iter'] + 1
            if 'accum_grad' in npzdata:
                accum_grad = npzdata['accum_grad']
            if 'base_lr' in npzdata:
                self._args['base_lr'] = npzdata['base_lr']
            if new_lr is not None:
                self._args['base_lr'] = new_lr
        timer = util.Timer()
        for iter in range(iter_start, self._args['num_iter']):
            Xbatch, Ybatch, weightbatch = sampler.sample(self._args['minibatch'])
            # carry out the computation
            if mode == 'lbfgs':
                accum_grad = None
                param = solver_basic.solve(Xbatch, Ybatch, weightbatch, param, K = K)
                logging.debug('iter %d time = %s' % \
                        (iter, str(timer.total(False))))
            else:
                # adagrad: compute gradient and update
                if iter == iter_start:
                    logging.debug("Adagrad: Initializing")
                    param_flat = solver_basic.presolve(\
                            Xbatch, Ybatch, weightbatch, param, K = K)
                    # we need to build the cache in solver_basic as well as
                    # the accumulated gradients
                    if iter == 0:
                        accum_grad = np.ones_like(param_flat) * \
                                (self._args.get('eta', 0.) ** 2) + \
                                np.finfo(np.float64).eps
                    if 'base_lr' not in self._args or self._args['base_lr'] < 0:
                        logging.debug("Adagrad: Performing line search")
                        # do a line search to get the value
                        self._args['base_lr'] = \
                                mathutil.wolfe_line_search_adagrad(param_flat,
                                lambda x: SolverMC.obj(x, solver_basic),
                                alpha = np.abs(self._args.get('base_lr', 1.)),
                                eta = self._args.get('eta', 0.))
                        # reset the timer to exclude the base learning rate tuning
                        # time
                        timer.reset()
                else:
                    solver_basic._X = Xbatch
                    solver_basic._Y = Ybatch
                    solver_basic._weight = weightbatch
                logging.debug("Adagrad: Computing func and grad")
                f0, g = SolverMC.obj(param_flat, solver_basic)
                logging.debug('gradient max/min: %f/%f' % (g.max(), g.min()))
                accum_grad += g * g
                # we are MINIMIZING, so go against the gradient direction
                param_flat -= g / np.sqrt(accum_grad) * self._args['base_lr']
                # the below code could be used to debug, but is commented out
                # currently for speed considerations.
                if False:
                    f = SolverMC.obj(param_flat, solver_basic)[0] 
                    logging.debug('iter %d f0 = %f f = %f time = %s' % \
                            (iter, f0, f,\
                            str(timer.total(False))))
                else:
                    logging.debug('iter %d f0 = %f time = %s' % \
                            (iter, f0, str(timer.total(False))))
                param = solver_basic.unflatten_params(param_flat)
            callback = self._args.get('callback', None)
            if callback is None:
                pass
            elif type(callback) is not list:
                cb_val = callback(param)
                logging.debug('cb: ' + str(cb_val))
            else:
                cb_val = [cb_func(param) for cb_func in callback]
                logging.debug('cb: ' + ' '.join([str(v) for v in cb_val]))
            if 'dump_every' in self._args and \
                    (iter + 1) % self._args['dump_every'] == 0:
                logging.debug('dumping param...')
                mpi.root_savez(self._args['dump_name'],\
                        iter=iter, w = param[0], b = param[1], \
                        accum_grad = accum_grad, base_lr = self._args['base_lr'])
        return param


class Loss(object):
    """LOSS defines commonly used loss functions
    For all loss functions:
    Input:
        Y:    a vector or matrix of true labels
        pred: prediction, has the same shape as Y.
    Return:
        f: the loss function value
        g: the gradient w.r.t. pred, has the same shape as pred.
    """
    def __init__(self):
        """All functions in Loss should be static
        """
        raise NotImplementedError, "Loss should not be instantiated!"
     
    @staticmethod
    def loss_l2(Y, pred, weight, **kwargs):
        '''
        The l2 loss: f = ||Y - pred||_{fro}^2
        '''
        diff = pred - Y
        if weight is None:
            return np.dot(diff.flat, diff.flat), 2.*diff 
        else:
            return np.dot((diff**2).sum(1), weight), \
                   2.*diff*weight[:,np.newaxis]
         
    @staticmethod
    def loss_hinge(Y, pred, weight, **kwargs):
        '''The SVM hinge loss. Input vector Y should have values 1 or -1
        '''
        margin = np.maximum(0., 1. - Y * pred)
        if weight is None:
            f = margin.sum()
            g = - Y * (margin>0)
        else:
            f = np.dot(weight, margin).sum()
            g = - Y * weight[:, np.newaxis] * (margin>0)
        return f, g
     
    @staticmethod
    def loss_squared_hinge(Y,pred,weight,**kwargs):
        ''' The squared hinge loss. Input vector Y should have values 1 or -1
        '''
        margin = np.maximum(0., 1. - Y * pred)
        if weight is None:
            return np.dot(margin.flat, margin.flat), -2. * Y * margin
        else:
            wm = weight[:, np.newaxis] * margin
            return np.dot(wm.flat, margin.flat), -2. * Y * wm
 
    @staticmethod
    def loss_bnll(Y,pred,weight,**kwargs):
        '''
        the BNLL loss: f = log(1 + exp(-y * pred))
        '''
        # expnyp is exp(-y * pred)
        expnyp = mathutil.exp(-Y*pred)
        expnyp_plus = 1. + expnyp
        if weight is None:
            return np.sum(np.log(expnyp_plus)), -Y * expnyp / expnyp_plus
        else:
            return np.dot(weight, np.log(expnyp_plus)).sum(), \
                   - Y * weight * expnyp / expnyp_plus
 
    @staticmethod
    def loss_multiclass_logistic(Y, pred, weight, **kwargs):
        """The multiple class logistic regression loss function
         
        The input Y should be a 0-1 matrix 
        """
        # normalized prediction and avoid overflowing
        prob = pred - pred.max(axis=1)[:,np.newaxis]
        mathutil.exp(prob, out=prob)
        prob /= prob.sum(axis=1)[:, np.newaxis]
        g = prob - Y
        # take the log
        mathutil.log(prob, out=prob)
        return -np.dot(prob.flat, Y.flat), g


class Loss2(object):
    """LOSS2 defines commonly used loss functions, rewritten with the gradient
    value cached (provided by the caller) for large-scale problems to save
    memory allocation / deallocation time.
    
    For all loss functions:
    Input:
        Y:    a vector or matrix of true labels
        pred: prediction, has the same shape as Y.
        weight: the weight for each data point.
        gpred: the pre-assigned numpy array to store the gradient. We force
            gpred to be preassigned to save memory allocation time in large
            scales.
        cache: a list (initialized with []) containing any misc cache that
            the loss function computation uses.
    Return:
        f: the loss function value
        gpred: the gradient w.r.t. pred, has the same shape as pred.
    """
    def __init__(self):
        """All functions in Loss should be static
        """
        raise NotImplementedError, "Loss should not be instantiated!"
    
    @staticmethod
    def loss_l2(Y, pred, weight, gpred, cache, **kwargs):
        '''
        The l2 loss: f = ||Y - pred||_{fro}^2
        '''
        if weight is None:
            gpred[:] = pred
            gpred -= Y
            f = np.dot(gpred.flat, gpred.flat)
            gpred *= 2.
        else:
            # we aim to minimize memory usage and avoid re-allocating large 
            # matrices.
            gpred[:] = pred
            gpred -= Y
            gpred **= 2
            f = np.dot(gpred.sum(1), weight)
            gpred[:] = pred
            gpred -= Y
            gpred *= 2. * weight[:, np.newaxis]
        return f, gpred
    
    @staticmethod
    def loss_hinge(Y, pred, weight, gpred, cache, **kwargs):
        '''The SVM hinge loss. Input vector Y should have values 1 or -1
        '''
        gpred[:] = pred
        gpred *= Y
        gpred *= -1
        gpred += 1.
        np.clip(gpred, 0, np.inf, out=gpred)
        if weight is None:
            f = gpred.sum()
            gpred[:] = (gpred > 0)
            gpred *= Y
            gpred *= -1
        else:
            f = np.dot(weight, gpred.sum(axis=1))
            gpred[:] = (gpred > 0)
            gpred *= Y
            gpred *= - weight[:, np.newaxis]
        return f, gpred
    
    @staticmethod
    def loss_squared_hinge(Y, pred, weight, gpred, cache, **kwargs):
        ''' The squared hinge loss. Input vector Y should have values 1 or -1
        '''
        gpred[:] = pred
        gpred *= Y
        gpred *= -1
        gpred += 1.
        np.clip(gpred, 0, np.inf, out=gpred)
        if weight is None:
            f = np.dot(gpred.flat, gpred.flat)
            gpred *= Y
            gpred *= -2
        else:
            gprednorm = inner1d(gpred,gpred)
            f = np.dot(gprednorm, weight)
            gpred *= Y
            gpred *= (-2 * weight[:, np.newaxis])
        return f, gpred

    @staticmethod
    def loss_multiclass_logistic(Y, pred, weight, gpred, cache, **kwargs):
        """The multiple class logistic regression loss function
        
        The input Y should be a 0-1 matrix 
        """
        if len(cache) == 0:
            cache.append(np.empty_like(pred))
        cache[0].resize(pred.shape)
        prob = cache[0]
        # normalize prediction to avoid overflowing
        prob[:] = pred
        prob -= pred.max(axis=1)[:,np.newaxis]
        mathutil.exp(prob, out=prob)
        prob /= prob.sum(axis=1)[:, np.newaxis]
        gpred[:] = prob
        gpred -= Y
        # take the log
        mathutil.log(prob, out=prob)
        return -np.dot(prob.flat, Y.flat), gpred

    @staticmethod
    def loss_multiclass_logistic_yvector(Y, pred, weight, gpred, cache, **kwargs):
        """The multiple class logistic regression loss function, where the
        input Y is a vector of indices, instead of a 0-1 matrix.
        """
        if len(cache) == 0:
            cache.append(np.empty_like(pred))
        cache[0].resize(pred.shape)
        prob = cache[0]
        # normalize prediction to avoid overflowing
        prob[:] = pred
        prob -= pred.max(axis=1)[:,np.newaxis]
        mathutil.exp(prob, out=prob)
        prob /= prob.sum(axis=1)[:, np.newaxis]
        gpred[:] = prob
        # instead of carrying out gpred-=Y, we need to convert it to indices
        gpred[np.arange(len(Y)), Y] -= 1.
        mathutil.log(prob, out=prob)
        return - prob[np.arange(len(Y)), Y].sum(), gpred
        


class Reg(object):
    '''
    REG defines commonly used regularization functions
    For all regularization functions:
    Input:
        w: the weight vector, or the weight matrix in the case of multiple classes
    Return:
        f: the regularization function value
        g: the gradient w.r.t. w, has the same shape as w.
    '''
    @staticmethod
    def reg_l2(w,**kwargs):
        '''
        l2 regularization: ||w||_2^2
        '''
        return np.dot(w.flat, w.flat), 2.*w

    @staticmethod
    def reg_l1(w,**kwargs):
        '''
        l1 regularization: ||w||_1
        '''
        g = np.sign(w)
        # subgradient
        g[g==0] = 0.5
        return np.abs(w).sum(), g

    @staticmethod
    def reg_elastic(w, **kwargs):
        '''
        elastic net regularization: (1-alpha) * ||w||_2^2 + alpha * ||w||_1
        kwargs['alpha'] is the balancing weight, default 0.5
        '''
        alpha1 = kwargs.get('alpha', 0.5)
        alpha2 = 1. - alpha1
        f1, g1 = Reg.reg_l1(w, **kwargs)
        f2, g2 = Reg.reg_l2(w, **kwargs)
        return f1 * alpha1 + f2 * alpha2, g1 * alpha1 + g2 * alpha2

class Evaluator(object):
    """Evaluator implements some commonly-used criteria for evaluation
    """
    @staticmethod
    def mse(Y, pred, axis=None):
        """Return the mean squared error of the true value and the prediction
        Input:
            Y, pred: the true value and the prediction
            axis: (optional) if Y and pred are matrices, you can specify the
                axis along which the mean is carried out.
        """
        return ((Y - pred) ** 2).mean(axis=axis)
    
    @staticmethod
    def accuracy(Y, pred):
        """Computes the accuracy
        Input: 
            Y, pred: two vectors containing discrete labels. If either is a
            matrix instead of a vector, then argmax is used to get the discrete
            labels.
        """
        if pred.ndim == 2:
            pred = pred.argmax(axis=1)
        if Y.ndim == 2:
            Y = Y.argmax(axis=1)
        correct = mpi.COMM.allreduce((Y==pred).sum())
        num_data = mpi.COMM.allreduce(len(Y))
        return float(correct) / num_data
    
    @staticmethod
    def confusion_table(Y, pred):
        """Computes the confusion table
        Input:
            Y, pred: two vectors containing discrete labels
        Output:
            table: the confusion table. table[i,j] is the number of data points
                that belong to i but predicted as j
        """
        if pred.ndim == 2:
            pred = pred.argmax(axis=1)
        if Y.ndim == 2:
            Y = Y.argmax(axis=1)
        num_classes = Y.max() + 1
        table = np.zeros((num_classes, num_classes))
        for y, p in zip(Y, pred):
            table[y,p] += 1
        return table
    
    @staticmethod
    def accuracy_class_averaged(Y, pred):
        """Computes the accuracy, but averaged over classes instead of averaged
        over data points.
        Input:
            Y: the ground truth vector
            pred: a vector containing the predicted labels. If pred is a matrix
            instead of a vector, then argmax is used to get the discrete label.
        """
        if pred.ndim == 2:
            pred = pred.argmax(axis=1)
        num_classes = Y.max() + 1
        accuracy = 0.0
        correct = (Y == pred).astype(np.float)
        for i in range(num_classes):
            idx = (Y == i)
            accuracy += correct[idx].mean()
        accuracy /= num_classes
        return accuracy

    @staticmethod
    def top_k_accuracy(Y, pred, k):
        """Computes the top k accuracy
        Input:
            Y: a vector containing the discrete labels of each datum
            pred: a matrix of size len(Y) * num_classes, each row containing the
                real value scores for the corresponding label. The classes with
                the highest k scores will be considered.
        """
        if k > pred.shape[1]:
            logging.warning("Warning: k is larger than the number of classes"
                            "so the accuracy would always be one.")
        top_k_id = np.argsort(pred, axis=1)[:, -k:]
        match = (top_k_id == Y[:, np.newaxis])
        correct = mpi.COMM.allreduce(match.sum())
        num_data = mpi.COMM.allreduce(len(Y))
        return float(correct) / num_data
    
    @staticmethod
    def average_precision(Y, pred):
        """Average Precision for binary classification
        """
        # since we need to compute the precision recall curve, we have to
        # compute this on the root node.
        Y = mpi.COMM.gather(Y)
        pred = mpi.COMM.gather(pred)
        if mpi.is_root():
            Y = np.hstack(Y)
            pred = np.hstack(pred)
            precision, recall, _ = metrics.precision_recall_curve(
                    Y == 1, pred)
            ap = metrics.auc(recall, precision)
        else:
            ap = None
        mpi.barrier()
        return mpi.COMM.bcast(ap)
    
    @staticmethod
    def average_precision_multiclass(Y, pred):
        """Average Precision for multiple class classification
        """
        K = pred.shape[1]
        aps = [Evaluator.average_precision(Y==k, pred[:,k]) for k in range(K)]
        return np.asarray(aps).mean()

'''
Utility functions that wraps often-used functions
'''

def svm_onevsall(X, Y, gamma, weight = None, **kwargs):
    if Y.ndim == 1:
        Y = to_one_of_k_coding(Y)
    solver = SolverMC(gamma, Loss.loss_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def l2svm_onevsall(X, Y, gamma, weight = None, **kwargs):
    if Y.ndim == 1:
        Y = to_one_of_k_coding(Y)
    solver = SolverMC(gamma, Loss.loss_squared_hinge, Reg.reg_l2, **kwargs)
    return solver.solve(X, Y, weight)

def elasticnet_svm_onevsall(X, Y, gamma, weight = None, alpha = 0.5, **kwargs):
    if Y.ndim == 1:
        Y = to_one_of_k_coding(Y)
    solver = SolverMC(gamma, Loss.loss_squared_hinge, Reg.reg_elastic, 
                      lossargs = {'alpha': alpha}, **kwargs)
    return solver.solve(X, Y, weight)
