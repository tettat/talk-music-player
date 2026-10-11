"""Real Windows child processes: clean exit and abrupt exit both kill descendants."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import time
import unittest

SRC = Path(__file__).resolve().parents[1] / 'src'


@unittest.skipUnless(sys.platform == 'win32', 'Windows job objects')
class ProcessLifetimeTests(unittest.TestCase):
    def check_exit(self, abrupt):
        code = (f'import sys;sys.path.insert(0,{str(SRC)!r});'
                'from process_lifetime import own_process_tree;own_process_tree();'
                'import subprocess,os;'
                'child=subprocess.Popen([sys.executable,"-I","-c",'
                '"import os,time;print(os.getpid(),flush=True);time.sleep(60)"],'
                'stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW);'
                'print(child.stdout.readline().decode().strip(),flush=True);'
                'sys.stdin.readline()')
        parent = subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        child_pid = int(parent.stdout.readline())
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, child_pid)  # SYNCHRONIZE only.
        self.assertTrue(handle)
        try:
            if abrupt: parent.kill()
            parent.communicate(b'exit\n', timeout=10)
            self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0)
        finally:
            kernel.CloseHandle(handle)
            if parent.poll() is None:
                parent.kill(); parent.communicate(timeout=5)

    def test_clean_owner_exit_kills_inference_descendant(self): self.check_exit(False)
    def test_abrupt_owner_exit_kills_inference_descendant(self): self.check_exit(True)


if __name__ == '__main__': unittest.main(verbosity=2)
