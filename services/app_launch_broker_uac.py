"""Experimental broker entry point for apps that request Windows UAC."""

from __future__ import annotations

import ctypes
from pathlib import Path

import app_launch_broker as broker


_standard_launch = broker.launch


def launch(app: str) -> dict:
    registered = broker.get_application(app)
    if registered is None:
        raise PermissionError("application is not approved in the registry")
    executable = Path(registered["executable_path"])
    before = broker.check(executable, 1)
    if before["status"] != "not_running":
        return {**before, "already_running": True}
    if registered["requires_admin"]:
        return _launch_elevated(executable)
    try:
        return _standard_launch(app)
    except OSError as error:
        if app != "tuba" or getattr(error, "winerror", None) != 740:
            raise
    return _launch_elevated(executable)


def _launch_elevated(executable):
    shell_execute = ctypes.WinDLL("shell32", use_last_error=True).ShellExecuteW
    shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_int]
    shell_execute.restype = ctypes.c_void_p
    result_code = shell_execute(None, "runas", str(executable), None,
                                str(executable.parent), 1)
    if result_code is None or result_code <= 32:
        raise OSError(f"Windows UAC launch declined or failed ({result_code})")

    result = broker.check(executable, 12)
    result["already_running"] = False
    result["elevation_requested"] = True
    return result


if __name__ == "__main__":
    broker.launch = launch
    broker.main()
