"""One narrowly scoped elevated pointer action for an approved app window."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from application_registry import get_application  # noqa: E402
from approved_window_process import ApprovedProcessFamily, choose_window  # noqa: E402


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _output_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT / "runtime" / "elevated-pointer-results") or path.suffix != ".json":
        raise ValueError("result path must be under the pointer results directory")
    return path


def _window_process_at(x: int, y: int) -> int:
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.WindowFromPoint.argtypes = [Point]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    hwnd = user32.WindowFromPoint(Point(x, y))
    if not hwnd:
        raise RuntimeError("no window is visible at the requested point")
    pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        raise RuntimeError("could not identify the window at the requested point")

    return int(pid.value)


def _approved_window(executable_path: str) -> int:
    """Resolve a visible window owned by a verified application process family."""
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    family = ApprovedProcessFamily(executable_path)
    candidates: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
            return True
        if family.accepts(pid.value):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            candidates.append((hwnd, title.value))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return choose_window(candidates, user32.GetForegroundWindow(), steam=bool(family.helper))


def _set_approved_window_topmost(executable_path: str, enabled: bool) -> int:
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    hwnd = _approved_window(executable_path)
    z_order = wintypes.HWND(-1 if enabled else -2)
    if not user32.SetWindowPos(hwnd, z_order, 0, 0, 0, 0, 0x0003):
        raise RuntimeError("Windows did not update the approved window's layer")
    return hwnd


def _prepare_approved_window(executable_path: str) -> dict:
    """Bring exactly one registered app window above the chat before capture."""
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = _approved_window(executable_path)
    user32.ShowWindow(hwnd, 9)
    _set_approved_window_topmost(executable_path, True)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("approved window did not gain input focus")
    return {"window_handle": int(hwnd), "topmost": True, "focused": True}


def _send_click(x: int, y: int, count: int = 1) -> None:
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.c_size_t]
    if not user32.SetCursorPos(x, y):
        raise RuntimeError("Windows did not set the pointer position")
    time.sleep(0.12)
    for _ in range(count):
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        try:
            time.sleep(0.05)
        finally:
            user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.07)


def _send_pyautogui_click(x: int, y: int, count: int = 1) -> None:
    import pyautogui

    pyautogui.moveTo(x, y, duration=0.2)
    actual = pyautogui.position()
    if abs(actual.x - x) > 2 or abs(actual.y - y) > 2:
        raise RuntimeError("PyAutoGUI pointer did not reach the requested point")
    pyautogui.click(clicks=count, interval=0.1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--release-after-click", action="store_true")
    parser.add_argument("--result", required=True)
    parser.add_argument("--backend", choices=("windows", "pyautogui"), default="windows")
    parser.add_argument("--click-count", choices=(1, 2), type=int, default=1)
    args = parser.parse_args()
    destination = _output_path(args.result)
    registered = get_application(args.app)
    result = {"status": "rejected", "app_id": args.app}
    try:
        if registered is None:
            raise ValueError("application is not approved in the registry")
        if args.prepare and args.release:
            raise ValueError("choose prepare or release")
        if args.release_after_click and (args.prepare or args.release):
            raise ValueError("release-after-click requires a click")
        # Screen captures and the caller use physical pixels; avoid DPI-virtualized
        # WindowFromPoint coordinates on displays with scaling enabled.
        ctypes.WinDLL("user32.dll", use_last_error=True).SetProcessDPIAware()
        if args.prepare:
            if args.x is not None or args.y is not None:
                raise ValueError("prepare cannot include click coordinates")
            result.update(_prepare_approved_window(registered["executable_path"]))
            result["status"] = "window_ready"
        elif args.release:
            if args.x is not None or args.y is not None:
                raise ValueError("release cannot include click coordinates")
            _set_approved_window_topmost(registered["executable_path"], False)
            result["status"] = "window_released"
        else:
            if args.x is None or args.y is None:
                raise ValueError("click requires x and y")
            family = ApprovedProcessFamily(registered["executable_path"])
            if not family.accepts(_window_process_at(args.x, args.y)):
                raise RuntimeError("the requested point is not on the approved application window")
            try:
                if args.backend == "pyautogui":
                    _send_pyautogui_click(args.x, args.y, args.click_count)
                else:
                    _send_click(args.x, args.y, args.click_count)
            finally:
                if args.release_after_click:
                    try:
                        _set_approved_window_topmost(registered["executable_path"], False)
                        result["window_released"] = True
                    except RuntimeError as error:
                        result["window_release_error"] = str(error)[:150]
            result.update({"status": "click_sent", "backend": args.backend,
                           "click_count": args.click_count, "x": args.x, "y": args.y})
    except (OSError, RuntimeError, ValueError) as error:
        result["reason"] = str(error)[:300]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False)
    return 0 if result["status"] in {"click_sent", "window_ready", "window_released"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
