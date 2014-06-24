"""Datasets implements some basic structures to deal with a dataset
"""
import glob
from iceberk import mpi
import logging
import numpy as np
import os
from PIL import Image
from scipy import misc
from skimage import transform


def imread_rgb(fname):
    '''This imread deals with occasional cases when scipy.misc.imread fails to
    load an image correctly.
    '''
    try:
        return np.asarray(Image.open(fname,'r').convert('RGB'))
    except Exception, e:
        logging.error("Error reading image filename: %s" % (fname))
        raise Exception, e

def lena():
    """Returns the lena image from iceberk's testing folder
    """
    fname = os.path.join(os.path.dirname(__file__), 'test', 'data', 'lena.png')
    return imread_rgb(fname)

def manipulate(img, target_size, max_size, min_size, center_crop):
    """Manipulates the input image. The following parameters are implemented:
    target_size: if provided, all images are resized to the size 
        specified. Should be a list of two integers, like [640,480].
        Note that this option may skew the images.
    max_size: if provided, any image that is larger than the max size
        is scaled so that its larger edge has max_size. If max_size is
        negative, all images are scaled (i.e. smaller images are scaled
        up). If target_size is set, max_size takes no effect.
    min_size: if provided, any image that is smaller than the min size
        is scaled so that its smaller edge has min_size. If min_size is
        negative, all images are scaled (i.e. larger images are scaled
        down). If target_size or max_size is set, min_size takes no 
        effect.
    center_crop: if given a number, the center of the image is cropped.
        Output image will be a square one with length being that of the
        shorter dimension of the original image. The image is then
        resized to the number given by center_crop.
    The order we carry out the manipulation is to check target size,
    then max_size, then min_size, then center_crop.
    """
    if target_size is not None:
        img = misc.imresize(img, target_size)
    elif max_size is not None:
        if max_size < 0 or max(img.shape[:2]) > max_size:
            newsize = np.asarray(img.shape[:2]) * np.abs(max_size) \
                    / max(img.shape[:2])
            img = transform.resize(img, newsize,\
                                   mode='nearest')
    elif min_size is not None:
        if min_size < 0 or min(img.shape[:2]) < min_size:
            newsize = np.asarray(img.shape[:2]) * np.abs(min_size) \
                    / min(img.shape[:2])
            img = transform.resize(img, newsize.astype(int),\
                                   mode='nearest')
    if center_crop is not None:
        # crop the center part of the image
        shorter_length = min(img.shape[:2])
        offset_y = int((img.shape[0] - shorter_length) / 2)
        offset_x = int((img.shape[1] - shorter_length) / 2)
        img = img[offset_y:offset_y + shorter_length,\
                  offset_x:offset_x + shorter_length]
        img = transform.resize(img, [center_crop, center_crop],
                               mode='nearest')
    return img


class ImageSet(object):
    """The basic structure that stores data. This class should be MPI ready.
    
    Each datum in this dataset would have to be a 3-dimensional image of size
    (height * width * num_channels) even if the number of channels is 1.
    """
    def __init__(self):
        """ You should write your own initialization code!
        """
        self._data = None
        self._label = None
        self._dim = False
        self._channels = False
        # we assume that the data is always prefetched
        self._prefetch = True
    
    def size(self):
        """Return the size of the dataset hosted on the current node
        """
        return len(self._data)
    
    def size_total(self):
        """Return the size of the dataset hosted on all nodes
        """
        return mpi.COMM.allreduce(self.size())
    
    def _read(self, idx):
        """reads a datum given by idx, if not prefetched.
        """
        raise NotImplementedError
    
    def image(self, idx):
        """ Returns datum 
        
        Note that you should almost never use data that is hosted on other
        nodes - every node should deal with its data only.
        """
        if self._prefetch:
            return self._data[idx]
        else:
            return self._read(idx)
    
    def raw_data(self):
        """ Returns the raw data
        
        Make sure you know what is stored in self._data if you use this
        """
        return self._data
    
    def label(self, idx):
        """ Returns the label for the corresponding datum
        """
        return self._label[idx]

    def labels(self):
        """ Returns the label vector for all the data I am hosting
        """
        return np.array(self._label)
    
    def dim(self):
        """Returns the dimension of the data if they have the same dimension
        Otherwise, return False
        """
        return self._dim
    
    def num_channels(self):
        """ Returns the number of channels
        """
        return self._channels
        
        
class NdarraySet(ImageSet):
    """Wraps an Ndarray using the dataset interface
    """
    def __init__(self, input_array, label = None, copy=False):
        """Initializtion
        
        If copy is true, copy the data
        """
        super(NdarraySet, self).__init__()
        if copy:
            self._data = input_array.copy()
        else:
            self._data = input_array
        if label is None:
            self._label = np.zeros(input_array.shape[0])
        elif len(label) != input_array.shape[0]:
            raise ValueError, \
                  "The number of input images and labels should be the same."
        else:
            self._label = label.copy()
        self._dim = self._data.shape[1:]
        # The number of channels. If the data has less than 4 dims, we
        # set the num of channels to 1 (in the case of e.g. grayscale images)
        if len(self._data.shape) < 4:
            self._channels = 1
        else:
            self._channels = self._dim[-1]

class MirrorSet(ImageSet):
    def __init__(self, original_set):
        """Create a mirrored dataset from the original data set.
        """
        super(MirrorSet, self).__init__()
        self._original = original_set

    def size(self):
        """Return the size of the dataset hosted on the current node
        """
        return self._original.size() * 2

    def image(self, idx):
        """ Returns datum 
        
        Note that you should almost never use data that is hosted on other
        nodes - every node should deal with its data only.
        """
        if idx < self._original.size():
            return self._original.image(idx)
        else:
            im = self._original.image(idx - self._original.size())
            return np.ascontiguousarray(im[:, ::-1])

    def label(self, idx):
        """ Returns the label for the corresponding datum
        """
        return self._original.label(idx % self._original.size())

    def labels(self):
        """ Returns the label vector for all the data I am hosting
        """
        return np.hstack((self._original.labels(), self._original.labels()))
    
    def dim(self):
        """Returns the dimension of the data if they have the same dimension
        Otherwise, return False
        """
        return self._original.dim()
    
    def num_channels(self):
        """ Returns the number of channels
        """
        return self._original.num_channels()

class ResizeSet(ImageSet):
    def __init__(self, original_set, target_size):
        """Create a mirrored dataset from the original data set. The original
        set should always contain images - i.e. they have to be either grayscale
        images or color images.
        Input:
            original_set: the original dataset
            target_size: if float, resize each image according to scale. if a
                tuple of 2 ints, resize each image to this fixed size.
        """
        super(ResizeSet, self).__init__()
        self._original = original_set
        self._target_size = target_size
        # decide the dimension
        if type(self._target_size) is not float:
            self._dim = self._target_size
        elif self._original.dim() != False:
            self._dim = self.image(0).shape[:2]
        else:
            self._dim = False

    def size(self):
        """Return the size of the dataset hosted on the current node
        """
        return self._original.size()

    def image(self, idx):
        """ Returns datum 
        
        Note that you should almost never use data that is hosted on other
        nodes - every node should deal with its data only.
        """
        return transform.resize(self._original.image(idx),
                                self._target_size,
                                mode='nearest')

    def label(self, idx):
        """ Returns the label for the corresponding datum
        """
        return self._original.label(idx)

    def labels(self):
        """ Returns the label vector for all the data I am hosting
        """
        return self._original.labels()
    
    def dim(self):
        """Returns the dimension of the data if they have the same dimension
        Otherwise, return False
        """
        return self._dim

    def num_channels(self):
        """ Returns the number of channels
        """
        return self._original.num_channels()

class TwoLayerDataset(ImageSet):
    """Builds a dataset composed of two-layer storage structures similar to
    Caltech-101 and ILSVRC
    """
    def __init__(self, root_folder, extensions, prefetch = False, 
                 target_size = None, max_size = None, min_size = None,
                 center_crop = None):
        """ Initialize from a two-layer storage
        Input:
            root_folder: the root that contains the data. Under root_folder
                there should be a list of folders, under which there should be
                a list of files
            extensions: the list of extensions that should be used to filter the
                files. Should be like ['png', 'jpg']. It's case insensitive.
            prefetch: if True, the images are prefetched to avoid disk read. If
                you have a large number of images, prefetch would require a lot
                of memory.
            target_size, max_size, min_size, center_crop: see manipulate() for
                details.
        """
        super(TwoLayerDataset, self).__init__()
        if mpi.agree(not os.path.exists(root_folder)):
            raise OSError, "The specified folder does not exist."
        logging.debug('Loading from %s' % (root_folder,))
        if type(extensions) is str:
            extensions = [extensions]
        extensions = set(extensions)
        if mpi.is_root():
            # get files first
            files = glob.glob(os.path.join(root_folder, '*', '*'))
            # select those that fits the extension
            files = [f for f in files  if any([
                            f.lower().endswith(ext) for ext in extensions])]
            logging.debug("A total of %d images." % (len(files)))
            # get raw labels
            labels = [os.path.split(os.path.split(f)[0])[1] for f in files]
            classnames = list(set(labels))
            # sort so we get a reasonable class order
            classnames.sort()
            name2val = dict(zip(classnames, range(len(classnames))))
            labels = [name2val[label] for label in labels]
        else:
            files = None
            classnames = None
            labels = None
        mpi.barrier()
        self._rawdata = mpi.distribute_list(files)
        self._data = self._rawdata
        self._prefetch = prefetch
        self._target_size = target_size
        self._max_size = max_size
        self._min_size = min_size
        self._center_crop = center_crop
        if target_size != None:
            self._dim = tuple(target_size) + (3,)
        else:
            self._dim = False
        self._channels = 3
        if prefetch:
            self._data = [self._read(idx) for idx in range(len(self._data))]
        self._label = mpi.distribute_list(labels)
        self._classnames = mpi.COMM.bcast(classnames)
    
    def _read(self, idx):
        img = imread_rgb(self._rawdata[idx])
        return manipulate(img, self._target_size,
                self._max_size, self._min_size, self._center_crop)


class SubImageSet(ImageSet):
    def __init__(self, original_set, new_dim, stride):
        """Create a dataset from the original data set by taking subimages from
        the original images. For example, you can have an input image size of
        256*256 and ask the code to produce images of 200*200 by densely taking
        the 200*200 subwindows in it.
        """
        if original_set.dim() == False:
            raise TypeError, \
                "The original dataset should have images of the same size!"
        super(SubImageSet, self).__init__()
        self._original = original_set
        self._dim = new_dim
        self._stride = int(stride)
        # compute how many images there are for each original image
        self._nrows = (original_set.dim()[0] - new_dim[0]) / self._stride + 1
        self._ncols = (original_set.dim()[1] - new_dim[1])/ self._stride + 1
        self._count = self._nrows * self._ncols
        # _last_call and _last_cache is used to store the original image of
        # the last image() call, since the call is often carried out in a
        # sequential way.
        self._last_call = -1
        self._last_cache = None
        
    def size(self):
        """Return the size of the dataset hosted on the current node
        """
        return self._original.size() * self._count

    def image(self, idx):
        """ Returns datum 
        
        Note that you should almost never use data that is hosted on other
        nodes - every node should deal with its data only.
        """
        if idx < 0 or idx >= self.size():
            raise ValueError, "The index is out of bound."
        original_idx = idx / self._count
        if original_idx != self._last_call:
            self._last_call = original_idx
            self._last_cache = self._original.image(original_idx)
        row_idx = (idx % self._count) / self._ncols * self._stride
        col_idx = (idx % self._count) % self._ncols * self._stride
        return self._last_cache[row_idx : row_idx + self._dim[0],
                                col_idx : col_idx + self._dim[1]].copy()

    def label(self, idx):
        """ Returns the label for the corresponding datum
        """
        return self._original.label(idx / self._count)

    def labels(self):
        """ Returns the label vector for all the data I am hosting
        """
        old_labels = self._original.labels()
        return np.tile(old_labels[:, np.newaxis], self._count).flatten()

    def num_channels(self):
        """ Returns the number of channels
        """
        return self._original.num_channels()

    def get_all_from_original(self, idx):
        """ Get all images from the original image of id given by idx
        """
        return [self.image(i) for i in range(idx * self._count,
                                             (idx+1) * self._count)]


class CenterRegionSet(ImageSet):
    def __init__(self, original_set, new_dim):
        """Create a dataset from the original data set by taking the center of
        the original images. For example, you can have an input image size of
        256*256 and ask the code to produce an output image size of 200*200.
        """
        super(CenterRegionSet, self).__init__()
        self._original = original_set
        self._dim = np.asarray(new_dim)
        # _last_call and _last_cache is used to store the original image of
        # the last image() call, since the call is often carried out in a
        # sequential way.
        self._last_call = -1
        self._last_cache = None
        
    def size(self):
        """Return the size of the dataset hosted on the current node
        """
        return self._original.size()

    def image(self, idx):
        """ Returns datum 
        
        Note that you should almost never use data that is hosted on other
        nodes - every node should deal with its data only.
        """
        if idx < 0 or idx >= self._original.size():
            raise ValueError, "The index is out of bound."
        img = self._original.image(idx)
        old_shape = np.asarray(img.shape[:2])
        offset = ((old_shape - self._dim) / 2).astype(np.int)
        return img[offset[0]:offset[0]+self._dim[0],
                   offset[1]:offset[1]+self._dim[1]].copy()

    def label(self, idx):
        """ Returns the label for the corresponding datum
        """
        return self._original.label(idx)

    def labels(self):
        """ Returns the label vector for all the data I am hosting
        """
        return self._original.labels()

    def num_channels(self):
        """ Returns the number of channels
        """
        return self._original.num_channels()