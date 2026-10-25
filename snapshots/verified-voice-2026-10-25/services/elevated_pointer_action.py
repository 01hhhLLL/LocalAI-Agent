"""One narrowly scoped elevated pointer action for an approved app window."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import sys
import time
import subprocess
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from application_registry import get_application  # noqa: E402
from approved_window_process import ApprovedProcessFamily, choose_window, focus_registered_window  # noqa: E402
from step_runtime import Deadline, hard_bounded_step


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


def _approved_window(executable_path: str, preferred: int | None = None) -> int:
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
    return choose_window(candidates, user32.GetForegroundWindow(), steam=bool(family.helper), preferred=preferred)


def _set_approved_window_topmost(executable_path: str, enabled: bool, preferred=None) -> int:
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    hwnd = _approved_window(executable_path, preferred)
    z_order = wintypes.HWND(-1 if enabled else -2)
    if not user32.SetWindowPos(hwnd, z_order, 0, 0, 0, 0, 0x0003):
        raise RuntimeError("Windows did not update the approved window's layer")
    return hwnd


def _prepare_approved_window(executable_path: str, preferred=None) -> dict:
    """Bring exactly one registered app window above the chat before capture."""
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = _approved_window(executable_path, preferred)
    user32.ShowWindow(hwnd, 9)
    _set_approved_window_topmost(executable_path, True, hwnd)
    focus_registered_window(executable_path, hwnd)
    time.sleep(0.1)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("approved window did not gain input focus")
    return {"window_handle": int(hwnd), "topmost": True, "focused": True}


def _send_click(x: int, y: int, count: int = 1, *, guard=lambda: None) -> None:
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.c_size_t]
    if not user32.SetCursorPos(x, y):
        raise RuntimeError("Windows did not set the pointer position")
    time.sleep(0.12)
    for _ in range(count):
        guard()
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        try:
            time.sleep(0.05)
        finally:
            user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.07)


def _send_pyautogui_click(x: int, y: int, count: int = 1, *, guard=lambda: None) -> None:
    import pyautogui

    pyautogui.moveTo(x, y, duration=0.2)
    actual = pyautogui.position()
    if abs(actual.x - x) > 2 or abs(actual.y - y) > 2:
        raise RuntimeError("PyAutoGUI pointer did not reach the requested point")
    for _ in range(count):
        guard()
        pyautogui.click()
        time.sleep(0.1)


def _check_window_proof(proof, registered, x, y, deadline):
    deadline.remaining()
    if proof.get("desktop_center") != [x, y] or proof.get("app_id") != registered["app_id"]:
        raise ValueError("pointer coordinates or application differ from the located target")
    hwnd = _approved_window(registered["executable_path"], proof.get("window_handle"))
    if hwnd != proof.get("window_handle"):
        raise RuntimeError("approved window identity changed after location")
    user = ctypes.WinDLL("user32.dll", use_last_error=True)
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetForegroundWindow.restype = wintypes.HWND
    rect = wintypes.RECT()
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)) or [rect.left, rect.top, rect.right, rect.bottom] != proof.get("window_rect"):
        raise RuntimeError("approved window bounds changed after location")
    if user.GetForegroundWindow() != hwnd:
        raise RuntimeError("approved window no longer has input focus")
    if not ApprovedProcessFamily(registered["executable_path"]).accepts(_window_process_at(x, y)):
        raise RuntimeError("target point is outside the approved application")


def _verify_target_pixels(proof, deadline, registered=None):
    capture = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
        str(ROOT / "scripts" / "capture-screen.ps1"), "-MaxImageSide", str(max(proof["geometry"][4:])),
        "-ForegroundWindowHandle", str(proof["window_handle"])],
        capture_output=True, timeout=deadline.remaining(30), creationflags=subprocess.CREATE_NO_WINDOW)
    if capture.returncode:
        raise RuntimeError("approved window could not be recaptured before click")
    payload = {"mode": "verify", "proof": proof, "capture": json.loads(capture.stdout.decode("utf-8-sig"))}
    checked = subprocess.run([str(ROOT / "runtime" / "ocr" / "Scripts" / "python.exe"),
        "-B", str(ROOT / "services" / "pointer_snapshot.py")],
        input=json.dumps(payload).encode("ascii"), capture_output=True,
        timeout=deadline.remaining(15), creationflags=subprocess.CREATE_NO_WINDOW)
    if not checked.returncode and json.loads(checked.stdout.decode("ascii")).get("fresh") is True:
        return {"method": "exact_target_pixels"}
    from pointer_snapshot import folia_ocr_refresh_allowed
    if (registered is None or not folia_ocr_refresh_allowed(proof, registered)
            or b"target pixels changed after location" not in checked.stderr):
        raise RuntimeError("target pixels changed; click abandoned")
    # Only re-observe. Window/PID/geometry guards remain binding and no mouse
    # input is sent until the same unique text and its bounds are reconfirmed.
    ocr = subprocess.run([str(ROOT / "runtime/ocr/Scripts/python.exe"), "-B",
        str(ROOT / "scripts/local-ocr.py")],
        input=base64.b64decode(payload["capture"]["image_base64"], validate=True),
        capture_output=True, timeout=deadline.remaining(30), creationflags=subprocess.CREATE_NO_WINDOW)
    if ocr.returncode:
        raise RuntimeError("fresh OCR failed; click abandoned")
    payload.update(mode="verify_ocr", lines=json.loads(ocr.stdout.decode("utf-8"))["lines"])
    refreshed = subprocess.run([str(ROOT / "runtime/ocr/Scripts/python.exe"), "-B",
        str(ROOT / "services/pointer_snapshot.py")], input=json.dumps(payload).encode("ascii"),
        capture_output=True, timeout=deadline.remaining(15), creationflags=subprocess.CREATE_NO_WINDOW)
    if refreshed.returncode:
        raise RuntimeError("fresh OCR target changed; click abandoned")
    result = json.loads(refreshed.stdout.decode("ascii"))
    if result.get("fresh") is not True:
        raise RuntimeError("fresh OCR did not verify the target; click abandoned")
    return result


@hard_bounded_step("elevated_pointer_action")
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
    parser.add_argument("--proof")
    parser.add_argument("--expires", type=float)
    parser.add_argument("--window", type=int)
    args = parser.parse_args()
    destination = _output_path(args.result)
    registered = get_application(args.app)
    result = {"status": "rejected", "app_id": args.app}
    try:
        deadline = Deadline("elevated_pointer_action", expires=args.expires)
        deadline.remaining()
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
            result.update(_prepare_approved_window(registered["executable_path"], args.window))
            result["status"] = "window_ready"
        elif args.release:
            if args.x is not None or args.y is not None:
                raise ValueError("release cannot include click coordinates")
            _set_approved_window_topmost(registered["executable_path"], False, args.window)
            result["status"] = "window_released"
        else:
            if args.x is None or args.y is None:
                raise ValueError("click requires x and y")
            if not args.proof:
                raise ValueError("click requires target-region proof")
            proof_file = _output_path(args.proof)
            if proof_file.stat().st_size > 65536:
                raise ValueError("target-region proof is too large")
            proof = json.loads(proof_file.read_text(encoding="utf-8"))
            deadline = Deadline("elevated_pointer_action", expires=proof["expires_monotonic"])
            deadline.remaining()
            _check_window_proof(proof, registered, args.x, args.y, deadline)
            result["target_validation"] = _verify_target_pixels(proof, deadline, registered)
            family = ApprovedProcessFamily(registered["executable_path"])
            if not family.accepts(_window_process_at(args.x, args.y)):
                raise RuntimeError("the requested point is not on the approved application window")
            try:
                result["status"] = "dispatch_started"
                guard = lambda: _check_window_proof(proof, registered, args.x, args.y, deadline)
                if args.backend == "pyautogui":
                    _send_pyautogui_click(args.x, args.y, args.click_count, guard=guard)
                else:
                    _send_click(args.x, args.y, args.click_count, guard=guard)
            finally:
                if args.release_after_click:
                    try:
                        _set_approved_window_topmost(registered["executable_path"], False, proof.get("window_handle"))
                        result["window_released"] = True
                    except RuntimeError as error:
                        result["window_release_error"] = str(error)[:150]
            result.update({"status": "click_sent", "backend": args.backend,
                           "click_count": args.click_count, "x": args.x, "y": args.y,
                           "execution_state": "executed_unverified"})
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if result["status"] == "dispatch_started":
            result["status"] = "executed_unverified"
        result["reason"] = str(error)[:300]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False)
    return 0 if result["status"] in {"click_sent", "window_ready", "window_released"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
