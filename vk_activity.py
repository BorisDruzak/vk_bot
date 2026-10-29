"""Shared login leases compatible with the worker's exclusive activity guard."""
from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path

import vk_unified as app


@contextmanager
def _windows_shared(stream):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        # The offset pair occupies the same union as PVOID in OVERLAPPED.
        # ULONG_PTR fields must remain pointer-sized on 64-bit Windows.
        _fields_ = [('Internal', ctypes.c_size_t),
                    ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    lock = kernel32.LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    lock.restype = wintypes.BOOL
    unlock = kernel32.UnlockFileEx
    unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                       wintypes.DWORD, ctypes.POINTER(Overlapped)]
    unlock.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    overlapped = Overlapped()
    # FAIL_IMMEDIATELY without EXCLUSIVE_LOCK: shared byte zero, length one.
    if not lock(handle, 0x00000001, 0, 1, 0, ctypes.byref(overlapped)):
        code = ctypes.get_last_error()
        if code == 33:  # ERROR_LOCK_VIOLATION
            raise app.RunAlreadyActive('Activity is already in use') from None
        raise ctypes.WinError(code)
    try:
        yield
    finally:
        if not unlock(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def shared_activity(root):
    """Nonblocking OS reader lease; preserve the file and release on crash.

    Readers may coexist on distinct account profiles. The existing worker
    singleton locks this same byte exclusively, so no batch overlaps a login.
    Per-account profile ownership and disabled-account checks remain required.
    """
    path = Path(root) / '.vk-activity.guard'
    # Unbuffered initialization avoids a delayed write under a shared lock.
    with path.open('a+b', buffering=0) as stream:
        if stream.seek(0, 2) == 0:
            try:
                stream.write(b'0')
            except OSError as exc:
                # A concurrent first reader may have seeded and locked byte 0
                # between our size check and write. Do not write through it.
                if not (os.name == 'nt' and exc.errno == errno.EACCES
                        and os.fstat(stream.fileno()).st_size > 0):
                    raise
        stream.seek(0)
        if os.name == 'nt':
            with _windows_shared(stream):
                yield
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise app.RunAlreadyActive('Activity is already in use') from None
                raise
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
