"""End a voice-only worker when its owning Windows UI process exits."""

import ctypes
import os
import threading
from ctypes import wintypes


def follow_parent(parent_pid: int) -> None:
    if not 0 < parent_pid <= 0xFFFFFFFF or parent_pid == os.getpid():
        raise ValueError("Invalid voice UI process")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
    if not handle:
        raise OSError(ctypes.get_last_error(), "Voice UI no longer available")

    def wait():
        try:
            result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel.CloseHandle(handle)
        # A dead owner or failed ownership check must not leave an ASR model loaded.
        os._exit(1 if result == 0 else 2)

    threading.Thread(target=wait, daemon=True).start()
