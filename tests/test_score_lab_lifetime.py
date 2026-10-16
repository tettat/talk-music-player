"""Real owned sleeping processes; no model, music, or production process touched."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from capabilities.score_lab.lifetime import OwnedJob, join_job


@unittest.skipUnless(os.name == "nt", "Windows Job lifecycle")
class LifetimeTests(unittest.TestCase):
    def test_named_job_rejects_unrelated_names(self):
        with self.assertRaises(ValueError):
            join_job("not-our-job")

    def test_job_close_kills_registered_python_and_its_child(self):
        # Use a venv redirector when present: killing only its Popen PID would
        # orphan the actual Python model process. This tests that exact case.
        python = ROOT / ".venv/Scripts/python.exe"
        command = """
import json,os,subprocess,sys,time
from capabilities.score_lab.lifetime import join_job
join_job(sys.argv[1])
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],stdin=subprocess.DEVNULL)
print(json.dumps({'worker':os.getpid(),'child':child.pid}),flush=True)
time.sleep(20)
"""
        process = None
        with OwnedJob() as job:
            try:
                process = subprocess.Popen([str(python) if python.is_file() else sys.executable,
                                            "-c", command, job.name], cwd=ROOT,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                data = json.loads(process.stdout.readline())
                self.assertNotEqual(data["worker"], os.getpid())
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handles = [kernel.OpenProcess(0x100000, False, data[key]) for key in ("worker", "child")]
                self.assertTrue(all(handles))
                try:
                    job.close()
                    for handle in handles:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                    # KILL_ON_JOB_CLOSE may use exit code 0 on Windows. The
                    # captured real process handles, not its code, prove exit.
                    process.wait(timeout=5)
                finally:
                    for handle in handles:
                        kernel.CloseHandle(handle)
            finally:
                job.close()
                if process:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
                    process.stdout.close(); process.stderr.close()


if __name__ == "__main__":
    unittest.main()
