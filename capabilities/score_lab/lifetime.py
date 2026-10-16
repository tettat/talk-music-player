"""Own only a score-lab model tree, including Windows venv redirector children.

The worker joins a random named Job before importing torch; the server alone
keeps its handle. Closing/crashing the server therefore kills that worker tree,
not unrelated Python/player processes. No process-name based termination.
"""
import ctypes
from ctypes import wintypes
import os
import re
import secrets
import signal
import threading


def kernel_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                ("process_memory_limit", ctypes.c_size_t), ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class OwnedJob:
    def __init__(self):
        self.name = None
        self.handle = None
        self.process = None
        self._lock = threading.Lock()
        if os.name == "nt":
            self.name = "UsicScoreLab-" + secrets.token_hex(16)
            kernel = kernel_api()
            self.handle = kernel.CreateJobObjectW(None, self.name)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        with self._lock:
            if self.handle is not None:
                handle, self.handle = self.handle, None
                kernel_api().CloseHandle(handle)
            if os.name != "nt" and self.process is not None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def join_job(name):
    if not re.fullmatch(r"UsicScoreLab-[a-f0-9]{32}", name):
        raise ValueError("Invalid owned-job name")
    if os.name != "nt":
        raise ValueError("Named model jobs are Windows-only")
    kernel = kernel_api()
    handle = kernel.OpenJobObjectW(1, False, name)  # JOB_OBJECT_ASSIGN_PROCESS only
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        # Do not keep a second handle alive: server death must close the last one.
        kernel.CloseHandle(handle)


class DataLock:
    """One server per private data dir; the OS releases this lock on a crash."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                self.file.write(b"0"); self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.file.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.file.close()
