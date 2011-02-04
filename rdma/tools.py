#!/usr/bin/python

import rdma;
import os,os.path,stat,io;

def _IOC(dir,type,nr,size):
    """Emulate the C _IOC macro"""
    return (((dir)  << (0 + 8 + 8 + 14)) | # (_IOC_SIZESHIFT+_IOC_SIZEBITS)
            ((type) << (0 + 8)) | # (_IOC_NRSHIFT+_IOC_NRBITS)
            ((nr)   << 0) |
            ((size) << ((0 + 8) + 8))); # (_IOC_TYPESHIFT+_IOC_TYPEBITS)

class SysFSDevice(object):
    '''Handle to a /dev/ char device kernel interface. This supports the
    context manager protocol.'''
    def __init__(self,parent,sys_dir):
        self.parent = parent;
        self.dev,self.dev_fn = self._open_dev_from_sysfs(sys_dir);
        self._sys_dir = sys_dir;

    def _try_open(self,path,dev):
        '''Try to open a device node that should correspond to character
        device dev. Return None if it did not turn out.'''
        try:
            st = os.lstat(path);
            if not stat.S_ISCHR(st.st_mode) or st.st_rdev != dev:
                return None;
            F = io.FileIO(path,"r+b");
            st2 = os.fstat(F.fileno());
            if st.st_mode == st2.st_mode and st.st_rdev == st2.st_rdev:
                return (F,path);
            F.close();
        except IOError:
            pass;
        except OSError:
            pass;
        return None;

    def _open_dev_from_sysfs(self,sysfs):
        '''Given the sysfs path to a device node descriptor find that device
        in /dev/'''
        name = os.path.basename(sysfs);
        try:
            with open(sysfs + "/dev") as F:
                dev = F.read().strip().split(':');
                dev = os.makedev(int(dev[0]),int(dev[1]));
        except IOError:
            raise rdma.RDMAError("Unable to open device node for %s, bad dev file?"%(repr(name)));

        F = self._try_open("/dev/infiniband/%s"%(name),dev);
        if F:
            return F;
        F = self._try_open("/dev/%s"%(name),dev);
        if F:
            return F;
        for root,dirs,files in os.walk("/dev/"):
            for I in files:
                F = self._try_open("%s/%s"%(root,I),dev);
                if F:
                    return F;
        raise rdma.RDMAError("Unable to open device node for %s"%(repr(name)));

    def __enter__(self):
        return self;
    def __exit__(self,*exc_info):
        return self.dev.close();
    def close(self):
        return self.dev.close();

import ctypes;
class timespec(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long),('tv_nsec', ctypes.c_long)];
clock_gettime = None;

def clock_monotonic():
    """Return the value of CLOCK_MONOTONIC. Replace me if python ever
    gets this in the standard library. Only works on Linux."""
    global clock_gettime;
    if clock_gettime is None:
        librt = ctypes.CDLL('librt.so.1', use_errno=True);
        clock_gettime = librt.clock_gettime;
        clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(timespec)];

    t = timespec();
    if clock_gettime(1, ctypes.pointer(t)) != 0:
        errno_ = ctypes.get_errno()
        raise OSError(errno_, os.strerror(errno_))
    return t.tv_sec + t.tv_nsec * 1e-9
