"""Bind a visible window to an approved application and its known UI child."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def canonical_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def process_image(pid: int) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        return (canonical_path(buffer.value) if kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)) else None)
    finally:
        kernel32.CloseHandle(handle)


def process_parents() -> dict[int, int]:
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        item = ProcessEntry32W()
        item.dwSize = ctypes.sizeof(item)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(item)):
            raise ctypes.WinError(ctypes.get_last_error())
        parents: dict[int, int] = {}
        while True:
            parents[int(item.th32ProcessID)] = int(item.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(item)):
                break
        return parents
    finally:
        kernel32.CloseHandle(snapshot)


class ApprovedProcessFamily:
    def __init__(self, registered_executable: str,
                 *, image_lookup=process_image, parent_lookup=process_parents):
        self.main = canonical_path(registered_executable)
        self.helper = (canonical_path(Path(registered_executable).parent /
                                      "bin/cef/cef.win64/steamwebhelper.exe")
                       if Path(registered_executable).name.casefold() == "steam.exe" else None)
        self.image_lookup = image_lookup
        self.parent_lookup = parent_lookup
        self._images: dict[int, str | None] = {}
        self._parents: dict[int, int] | None = None

    def image(self, pid: int) -> str | None:
        if pid not in self._images:
            self._images[pid] = self.image_lookup(pid)
        return self._images[pid]

    def accepts(self, pid: int) -> bool:
        image = self.image(pid)
        if image == self.main:
            return True
        if not self.helper or image != self.helper:
            return False
        if self._parents is None:
            self._parents = self.parent_lookup()
        seen = {pid}
        current = pid
        for _ in range(16):
            current = self._parents.get(current, 0)
            if not current or current in seen:
                return False
            seen.add(current)
            if self.image(current) == self.main:
                return True
        return False


def choose_window(candidates: list[tuple[int, str]], foreground: int,
                  *, steam: bool) -> int:
    if len(candidates) == 1:
        return candidates[0][0]
    if steam:
        login = [hwnd for hwnd, title in candidates if
                 any(word in title.casefold() for word in
                     ("登录 steam", "sign in to steam", "steam login", "choose an account"))]
        if len(login) == 1:
            return login[0]
        foreground_candidates = [hwnd for hwnd, _ in candidates if hwnd == foreground]
        if len(foreground_candidates) == 1:
            return foreground_candidates[0]
    raise RuntimeError("expected exactly one visible window for the approved app")
