'''dsift.py implements the dense sift feature extraction code.

The descriptors are defined in a similar way to the one used in
Svetlana Lazebnik's Matlab implementation, which could be found
at:

http://www.cs.unc.edu/~lazebnik/

Yangqing Jia, jiayq@eecs.berkeley.edu
'''

import numpy as np
from iceberk import pipeline, mpi, mathutil
import logging
from scipy.ndimage import filters

"""Default SIFT feature parameters
"""
_NUM_ANGLES = 8
_ANGLES = np.array(range(_NUM_ANGLES)) * np.pi / _NUM_ANGLES
_NUM_BINS = 4
_NUM_SAMPLES = _NUM_BINS**2
_ALPHA = 9.0
_TWOSIDE = True

def gen_dgauss(sigma,fwid=None):
    '''generating a derivative of Gauss filter on both the X and Y
    direction.
    '''
    if fwid is None:
        fwid = np.int(2*np.ceil(sigma))
    else:
        # in the code below, fwid is half the size of the returned filter
        # i.e. after the following line, if fwid is 2, the returned filter
        # will have size 5.
        fwid = int(fwid/2)
    sigma += np.finfo(np.float64).eps
    G = np.array(range(-fwid,fwid+1))**2
    G = G.reshape((G.size,1)) + G
    G = np.exp(- G / 2.0 / sigma / sigma)
    G /= np.sum(G)
    GH,GW = np.gradient(G)
    GH *= 2.0/np.sum(np.abs(GH))
    GW *= 2.0/np.sum(np.abs(GW))
    return GH, GW


class OrientedGradientExtractor(pipeline.Extractor):
    """The class that does oriented gradient extraction
    """
    def __init__(self, specs = {}):
        """
        specs:
            sigma_edge: the standard deviation for the gaussian smoothing
                before computing the gradient
            num_angles: the number of angles to use, default 8 (as in sift)
            alpha: the parameters used to compute the oriented gradient from
                x and y gradients. Default 9.0
            twoside: if true, compute the gradient evenly between 0 to 360
                degrees. if false, compute between 0 to 180 degrees (take the
                absolute value of the other side). Default true.
        """
        self.specs = specs
        self._GH, self._GW = gen_dgauss(specs.get('sigma_edge', 1.0))
        num_angles = self.specs.get('num_angles', _NUM_ANGLES)
        if specs.get('twoside', True):
            self._ANGLES = np.array(range(num_angles)) \
                    * 2.0 * np.pi / num_angles
        else:
            self._ANGLES = np.array(range(num_angles)) \
                    * np.pi / num_angles

    def process(self, image):
        image = image.astype(np.double)
        if image.max() > 1:
            # The image is between 0 and 255 - we need to convert it to [0,1]
            image /= 255;
        if image.ndim == 3:
            # we do not deal with color images.
            image = np.mean(image,axis=2)
        H,W = image.shape
        IH = filters.convolve(image, self._GH, mode='nearest')
        IW = filters.convolve(image, self._GW, mode='nearest')
        I_mag = np.sqrt(IH ** 2 + IW ** 2)
        I_theta = np.arctan2(IH, IW)
        
        alpha = self.specs.get('alpha', _ALPHA)
        num_angles = self.specs.get('num_angles', _NUM_ANGLES)
        I_orient = np.empty((H, W, num_angles))
        if self.specs.get('twoside', True):
            for i in range(num_angles):
                I_orient[:,:,i] = I_mag * np.maximum(
                        np.cos(I_theta - self._ANGLES[i]) ** alpha, 0)
        else:
            for i in range(num_angles):
                I_orient[:,:,i] = I_mag * np.abs(
                        np.cos(I_theta - self._ANGLES[i]) ** alpha)
        return I_orient
    
    
class DsiftExtractor(pipeline.Extractor):
    '''
    The class that does dense sift feature computation.
    Sample Usage:
        extractor = DsiftExtractor(gridSpacing,patchSize,[optional params])
        feat,positions = extractor.process_image(Image)
    '''
    def __init__(self, psize, stride, specs = {}):
        '''
        stride: the spacing for sampling dense descriptors
        psize: the size for each sift patch
        specs:
            nrml_thres: low contrast normalization threshold
            sigma_edge: the standard deviation for the gaussian smoothing
                before computing the gradient
            sift_thres: sift thresholding (0.2 works well based on
                Lowe's SIFT paper)
        '''
        self.gS = stride
        self.pS = psize
        self.nrml_thres = specs.get('nrml_thres', 1.0)
        self.sigma = specs.get('sigma_edge', 1.0)
        self.sift_thres = specs.get('sift_thres', 0.2)
        # precompute gradient filters
        self.GH, self.GW = gen_dgauss(self.sigma)
        # compute the weight contribution map
        sample_res = self.pS / np.double(_NUM_BINS)
        sample_p = np.array(range(self.pS))
        sample_ph, sample_pw = np.meshgrid(sample_p,sample_p)
        sample_ph.resize(sample_ph.size)
        sample_pw.resize(sample_pw.size)
        bincenter = np.array(range(1,_NUM_BINS*2,2)) \
                    / 2.0 / _NUM_BINS * self.pS - 0.5
        bincenter_h, bincenter_w = np.meshgrid(bincenter,bincenter)
        bincenter_h.resize((bincenter_h.size,1))
        bincenter_w.resize((bincenter_w.size,1))
        dist_ph = abs(sample_ph - bincenter_h)
        dist_pw = abs(sample_pw - bincenter_w)
        weights_h = dist_ph / sample_res
        weights_w = dist_pw / sample_res
        weights_h = (1-weights_h) * (weights_h <= 1)
        weights_w = (1-weights_w) * (weights_w <= 1)
        # weights is the contribution of each pixel to the corresponding bin
        # center
        self.weights = weights_h * weights_w
        
    def process(self, image):
        '''
        processes a single image, return the locations
        and the values of detected SIFT features.
        image: a M*N image which is a numpy 2D array. If you 
            pass a color image, it will automatically be converted
            to a grayscale image.
        
        Return values:
            feat
        '''
        image = image.astype(np.double)
        if image.max() > 1:
            # The image is between 0 and 255 - we need to convert it to [0,1]
            image /= 255;
        if image.ndim == 3:
            # we do not deal with color images.
            image = np.mean(image,axis=2)
        # compute the grids
        H,W = image.shape
        if H < self.pS or W < self.pS:
            logging.warning("Image size is smaller than patch size.")
            return np.zeros((0,0,_NUM_SAMPLES*_NUM_ANGLES))
        gS = self.gS
        pS = self.pS
        remH = np.mod(H-pS, gS)
        remW = np.mod(W-pS, gS)
        offsetH = remH/2
        offsetW = remW/2
        rangeH = np.arange(offsetH,H-pS+1,gS)
        rangeW = np.arange(offsetW, W-pS+1, gS)
        #logging.debug('Image: w {}, h {}, gs {}, ps {}, nFea {}'.\
        #              format(W,H,gS,pS,len(rangeH)*len(rangeW)))
        feat = self.calculate_sift_grid(image,rangeH,rangeW)
        feat = self.normalize_sift(feat)
        return feat

    def calculate_sift_grid(self,image,rangeH,rangeW):
        '''This function calculates the unnormalized sift features
        It is called by process_image().
        '''
        H,W = image.shape
        feat = np.zeros((len(rangeH), len(rangeW), _NUM_SAMPLES*_NUM_ANGLES))

        IH = filters.convolve(image, self.GH, mode='nearest')
        IW = filters.convolve(image, self.GW, mode='nearest')
        I_mag = np.sqrt(IH ** 2 + IW ** 2)
        I_theta = np.arctan2(IH, IW)
        
        I_orient = np.empty((H, W, _NUM_ANGLES))
        for i in range(_NUM_ANGLES):
            I_orient[:,:,i] = I_mag * np.maximum(
                    np.cos(I_theta - _ANGLES[i]) ** _ALPHA, 0)
        for i, hs in enumerate(rangeH):
            for j, ws in enumerate(rangeW):
                feat[i, j] = np.dot(self.weights,
                                    I_orient[hs:hs+self.pS, ws:ws+self.pS]\
                                        .reshape(self.pS**2, _NUM_ANGLES)
                                   ).flat
        return feat

    def normalize_sift(self, feat):
        '''
        This function does sift feature normalization
        following David Lowe's definition (normalize length ->
        thresholding at 0.2 -> renormalize length)
        '''
        siftlen = np.sqrt(np.sum(feat**2, axis=-1))
        hcontrast = (siftlen >= self.nrml_thres)
        siftlen[siftlen < self.nrml_thres] = self.nrml_thres
        # normalize with contrast thresholding
        feat /= siftlen[:, :, np.newaxis]
        # suppress large gradients
        feat[feat > self.sift_thres] = self.sift_thres
        # renormalize high-contrast ones
        feat[hcontrast] /= np.sqrt(np.sum(feat[hcontrast]**2, axis=-1))\
                [:, np.newaxis]
        return feat
