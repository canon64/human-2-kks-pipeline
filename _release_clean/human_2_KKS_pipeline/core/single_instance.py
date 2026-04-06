from __future__ import annotations

import atexit
import ctypes


def acquire_single_instance(mutex_name: str) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool

    handle = create_mutex(None, False, mutex_name)
    if not handle:
        return False
    if ctypes.get_last_error() == 183:
        close_handle(handle)
        return False
    atexit.register(lambda: close_handle(handle))
    return True

