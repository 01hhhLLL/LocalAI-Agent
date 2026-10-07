"""Small loopback launcher outside Hermes' short-lived MCP process tree."""

from __future__ import annotations

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from application_registry import get_application, initialize as initialize_registry

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "start-app-and-wait.ps1"
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
    return result


def launch(app: str) -> dict:
    registered = get_application(app)
    if registered is None:
        raise PermissionError("application is not approved in the registry")
    executable = Path(registered["executable_path"])
    if not executable.is_file() or not CHECKER.is_file():
        raise FileNotFoundError("application or checker is missing")
    before = check(executable, 1)
    if before["status"] == "window_visible":
        return {**before, "already_running": True}
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
    initialize_registry()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"READY http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
