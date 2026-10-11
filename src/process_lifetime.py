"""Windows desktop-owned process tree: children die even after an abrupt UI exit."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

_job_handle = None


def own_process_tree():
    global _job_handle
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_DESKTOP_REQUIRED')
    if _job_handle is not None:
        return

    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                    ('maximum_working_set', ctypes.c_size_t), ('active_process_limit', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', IoCounters),
                    ('process_memory_limit', ctypes.c_size_t), ('job_memory_limit', ctypes.c_size_t),
                    ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError('DESKTOP_JOB_CREATE_FAILED')
    if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or
            not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        kernel.CloseHandle(handle)
        raise OSError('DESKTOP_JOB_ASSIGN_FAILED')
    # OS teardown closes this unshared handle and kills remaining inference children.
    _job_handle = handle
