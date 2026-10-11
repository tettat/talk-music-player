"""User-bound Windows DPAPI storage; never fall back to plaintext."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile

MAGIC = b'TALK-QQ-DPAPI\x01'
ENTROPY = b'talk.qqmusic.independent-session.v1'
MAX_SECRET_BYTES = 65536


class Blob(ctypes.Structure):
    """Windows DATA_BLOB ABI."""
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def transform(data, encrypt):
    """Protect or unprotect for the current Windows user with UI forbidden."""
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_DPAPI_REQUIRED')
    if not data or len(data) > MAX_SECRET_BYTES + 4096:
        raise ValueError('SECRET_SIZE_REJECTED')
    system = Path(os.environ['SystemRoot']) / 'System32'
    crypt = ctypes.WinDLL(str(system / 'crypt32.dll'), use_last_error=True)
    kernel = ctypes.WinDLL(str(system / 'kernel32.dll'), use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(ENTROPY)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    method = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    try:
        # CRYPTPROTECT_UI_FORBIDDEN only; no machine-wide protection flag.
        if not method(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1,
                      ctypes.byref(output)):
            raise OSError(ctypes.get_last_error(), 'DPAPI_OPERATION_FAILED')
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        if output.pbData:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def save(path, text):
    """Atomically save ciphertext in the caller's already validated private directory."""
    data = text.encode('utf-8')
    if not data or len(data) > MAX_SECRET_BYTES:
        raise ValueError('SECRET_SIZE_REJECTED')
    encoded = MAGIC + transform(data, True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='credential-', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path):
    """Reject unprotected/oversized files before user-bound decryption."""
    path = Path(path)
    if path.stat().st_size > MAX_SECRET_BYTES + 8192:
        raise ValueError('SECRET_SIZE_REJECTED')
    encoded = path.read_bytes()
    if not encoded.startswith(MAGIC) or len(encoded) <= len(MAGIC):
        raise ValueError('PROTECTED_SECRET_FORMAT_REQUIRED')
    return transform(encoded[len(MAGIC):], False).decode('utf-8')
