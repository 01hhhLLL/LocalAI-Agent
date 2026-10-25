"""Open an exact catalog image through Windows and verify its associated viewer."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from approved_window_process import canonical_path, focus_registered_window, process_image, verify_registered_window
from step_runtime import Deadline
from tuba_tool_catalog import entries


def association(extension):
    query = ctypes.WinDLL("shlwapi").AssocQueryStringW
    query.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR,
                      wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = ctypes.c_long
    result = {}
    for key, field in (("executable", 2), ("name", 4), ("app_id", 21)):
        buffer = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(len(buffer))
        result[key] = buffer.value if query(0, field, extension, "open", buffer, ctypes.byref(size)) == 0 else None
    return result


def process_app_id(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    query = kernel.GetApplicationUserModelId
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR]
    query.restype = ctypes.c_long
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(len(buffer))
        return buffer.value if query(handle, ctypes.byref(size), buffer) == 0 else None
    finally:
        kernel.CloseHandle(handle)


def matching_windows(path, associated):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    rows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _parameter):
        if not user.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(1024)
        user.GetWindowTextW(hwnd, title, len(title))
        if path.name.casefold() not in title.value.casefold():
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = process_image(pid.value)
        app_id = process_app_id(pid.value)
        same_exe = associated.get("executable") and image == canonical_path(associated["executable"])
        same_app = associated.get("app_id") and app_id == associated["app_id"]
        if (same_exe or same_app) and image and verify_registered_window(image, pid.value, int(hwnd)):
            rows.append({"window_handle": int(hwnd), "process_pid": pid.value,
                         "window_title": title.value, "executable_path": image, "app_id": app_id})
        return True

    user.EnumWindows(visit, 0)
    return rows


def open_image(value, deadline):
    path = Path(value).resolve(strict=True)
    if path.suffix.casefold() not in {".jpg", ".jpeg", ".png"} or not any(
            item["path"] == str(path) for item in entries()):
        raise ValueError("Only an exact image from the installed toolbox catalog can be opened")
    associated = association(path.suffix)
    if not associated.get("executable") and not associated.get("app_id"):
        return {"verified": False, "reason": "default_image_viewer_unavailable", "path": str(path)}
    os.startfile(str(path), "open")
    until = time.monotonic() + deadline.remaining(25)
    while time.monotonic() < until:
        matches = matching_windows(path, associated)
        if len(matches) == 1:
            window = matches[0]
            foreground = focus_registered_window(window["executable_path"], window["window_handle"])
            return {"verified": foreground, "path": str(path), "viewer": associated,
                    "window": window, "foreground_verified": foreground,
                    "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "reason": "native_image_opened" if foreground else "image_window_not_foreground",
                    "verification_scope": "exact_catalog_file_request_and_associated_viewer_filename"}
        time.sleep(.2)
    return {"verified": False, "reason": "native_image_window_not_verified", "path": str(path), "viewer": associated}


if __name__ == "__main__":
    print(json.dumps(open_image(sys.argv[1], Deadline.inherited("native_catalog_image")), ensure_ascii=True), flush=True)
