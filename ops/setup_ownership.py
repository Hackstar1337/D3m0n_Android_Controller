"""Dependency-free local installation/process ownership primitives."""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path

@contextmanager
def checkout_lock():
    """Held by launcher for its lifetime, or installer while replacing builds."""
    path=Path(__file__).resolve().parents[1]/'.d3m0n-service.lock'
    with path.open('a+b') as handle:
        from ops.platform_config import ensure_private_file
        ensure_private_file(path)
        handle.seek(0)
        if not handle.read(1):handle.write(b'0');handle.flush()
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:raise RuntimeError('This checkout is running or being installed. Stop it before updating.') from exc
            try:yield
            finally:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:raise RuntimeError('This checkout is running or being installed. Stop it before updating.') from exc
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

class WindowsChildJob:
    """Closing this private Job handle kills only the assigned child tree."""
    def __init__(self):
        self.handle=None
        if os.name!='nt':return
        import ctypes
        from ctypes import wintypes as w
        self.ctypes=ctypes;self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        class BASIC(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ['ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount']]
        class EXTENDED(ctypes.Structure):
            _fields_=[('BasicLimitInformation',BASIC),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        self.k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.k.CreateJobObjectW.restype=w.HANDLE
        self.k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD];self.k.SetInformationJobObject.restype=w.BOOL
        self.k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];self.k.AssignProcessToJobObject.restype=w.BOOL
        self.k.CloseHandle.argtypes=[w.HANDLE];self.k.CloseHandle.restype=w.BOOL
        self.handle=self.k.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        limits=EXTENDED();limits.BasicLimitInformation.LimitFlags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error=ctypes.get_last_error();self.close();raise ctypes.WinError(error)
    def assign(self,process):
        if self.handle and not self.k.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None
