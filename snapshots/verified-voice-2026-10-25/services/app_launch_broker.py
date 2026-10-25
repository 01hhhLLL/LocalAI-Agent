"""Small loopback launcher outside Hermes' short-lived MCP process tree."""

from __future__ import annotations

import argparse
import json
import subprocess
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from application_registry import get_application, initialize as initialize_registry
from model_resources import FileLease
from step_runtime import hard_bounded_step
from approved_window_process import (canonical_path, process_image, verify_registered_window,
                                     focus_registered_window, is_foreground_window)

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "start-app-and-wait.ps1"
def _limited_process_observation(executable: Path) -> dict | None:
    """Keep elevated processes when .NET cannot read Process.Path."""
    name = executable.stem.replace("'", "''")
    script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "@(Get-Process -Name '" + name + "' -ErrorAction SilentlyContinue | ForEach-Object { "
        "[pscustomobject]@{pid=$_.Id; hwnd=$_.MainWindowHandle.ToInt64(); title=$_.MainWindowTitle} "
        "}) | ConvertTo-Json -Compress")
    try:
        query = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if query.returncode:
            return None
        rows = json.loads(query.stdout.decode("utf-8-sig")) if query.stdout.strip() else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        return None
    matched, visible = [], []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int:
            continue
        pid, hwnd = row["pid"], row.get("hwnd")
        if process_image(pid) != canonical_path(executable):
            continue
        matched.append(pid)
        if verify_registered_window(executable, pid, hwnd):
            visible.append({"process_pid": pid, "window_pid": pid,
                "window_handle": hwnd, "window_title": row.get("title")})
    if len(visible) == 1:
        return {**visible[0], "status": "window_visible", "already_running": True,
                "registered_window_verified": True, "identity_source": "limited_process_query"}
    if matched:
        return {"status": "process_running", "already_running": True,
                "process_pid": matched[0] if len(matched) == 1 else None,
                "registered_window_verified": False, "identity_source": "limited_process_query"}
    return None


def check(executable: Path, seconds: int) -> dict:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
         str(CHECKER), "-Executable", str(executable),
         "-TimeoutSeconds", str(seconds), "-CheckOnly"],
        stdin=subprocess.DEVNULL, capture_output=True,
        timeout=seconds + 8, creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise RuntimeError(detail or "application check failed")
    result = json.loads(completed.stdout.decode("utf-8-sig"))
    if result.get("status") not in {"window_visible", "process_running", "not_running"}:
        raise RuntimeError("application check returned invalid status")
    verified = (result["status"] == "window_visible" and verify_registered_window(
        executable, result.get("window_pid"), result.get("window_handle")))
    if not verified:
        fallback = _limited_process_observation(executable)
        if fallback is not None:
            # A process-only fallback must not erase the observed restore target.
            result = {**result, **fallback}
            verified = result.get("registered_window_verified") is True
    result["registered_window_verified"] = verified
    if result["status"] == "window_visible" and not verified:
        result["status"] = "process_running"
    if verified:
        result["window_foreground"] = is_foreground_window(result["window_handle"])
    return result


@hard_bounded_step("registered_application_launch")
def launch(app: str) -> dict:
    if not isinstance(app, str) or not 1 <= len(app) <= 100:
        raise ValueError("invalid application identity")
    name = hashlib.sha256(app.encode("utf-8")).hexdigest()
    with FileLease(name, ROOT / "runtime" / "application-launch-ownership"):
        return _launch_owned(app)


def _launch_owned(app: str) -> dict:
    registered = get_application(app)
    if registered is None:
        raise PermissionError("application is not approved in the registry")
    executable = Path(registered["executable_path"])
    if (not executable.is_absolute() or executable.suffix.lower() != ".exe"
            or str(executable).startswith("\\\\")):
        raise PermissionError("registered target must be a local absolute executable")
    if not executable.is_file() or not CHECKER.is_file():
        raise FileNotFoundError("application or checker is missing")
    if str(executable.resolve(strict=True)).casefold() != str(executable.absolute()).casefold():
        raise PermissionError("registered executable identity changed through redirection")
    before = check(executable, 1)
    if before["status"] in {"window_visible", "process_running"} and before.get("window_handle"):
        focused = focus_registered_window(str(executable), before["window_handle"],
                                          expected_pid=before.get("window_pid"))
        observed = check(executable, 1)
        return {**observed, "already_running": True, "new_launch": False,
                "activation_verified": focused and observed.get("window_foreground") is True}
    if before["status"] == "process_running":
        # An existing process may still be opening its window. Observe it;
        # launching a second copy cannot establish whether the first succeeded.
        return {**check(executable, 12), "already_running": True, "new_launch": False}
    if registered["requires_admin"]:
        from app_launch_broker_uac import _launch_elevated
        return _launch_elevated(executable)
    process = subprocess.Popen(
        [str(executable)], cwd=str(executable.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(0x00000200 | 0x08000000 | 0x01000000))
    result = check(executable, 12)
    result["already_running"] = before["status"] == "process_running"
    result["launch_pid"] = process.pid
    result["launcher_exit_code"] = process.poll()
    result["launcher_exited"] = result["launcher_exit_code"] is not None
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.reply(200, {"status": "ready"})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        self.connection.settimeout(180)
        if self.path != "/launch":
            self.reply(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 128:
                raise ValueError("invalid request length")
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict) or set(request) != {"app"}:
                raise ValueError("expected application name only")
            result = launch(request["app"])
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.reply(500, {"error": str(exc)})
        else:
            self.reply(200, result)


def main() -> None:
    import os
    if os.environ.get("LOCALAI_SERVICE_PARENT_PID"):
        from voice_process_lifetime import follow_parent
        follow_parent(int(os.environ["LOCALAI_SERVICE_PARENT_PID"]))
    initialize_registry()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"READY http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
