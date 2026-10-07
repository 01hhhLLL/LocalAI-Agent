"""Run one short recognized launch command through the shared task entry.

The ASR child has exited before this script starts. Services created here are
stopped after Hermes finishes; an already running service is left untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from application_registry import initialize as initialize_registry, resolve  # noqa: E402
from task_entry import (  # noqa: E402
    TASK_DATABASE, _update, create_task, gpu_free_mib, process_task,
    request_cancel, task_status,
)

API_PYTHON = ROOT / "runtime" / "fast" / "Scripts" / "python.exe"
TASK_PYTHON = ROOT / "runtime" / "foundation" / "Scripts" / "python.exe"
API_SCRIPT = ROOT / "services" / "local_openai_api.py"
BROKER_SCRIPT = ROOT / "services" / "app_launch_broker.py"
API_HEALTH = "http://127.0.0.1:8765/healthz"
BROKER_HEALTH = "http://127.0.0.1:8766/health"
EXPECTED_MODEL = "local-qwen3-8b-nf4"
RESULT_PREFIX = "VOICE_COMMAND_RESULT="
PROGRESS_PREFIX = "VOICE_COMMAND_PROGRESS="
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def emit(prefix: str, payload: dict) -> None:
    print(prefix + json.dumps(payload, ensure_ascii=True), flush=True)


def health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.load(response)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def stop_owned(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def ensure_service(python: Path, script: Path, url: str, *, model: str | None = None,
                   timeout: int = 120, watch_gpu: bool = False) -> subprocess.Popen | None:
    current = health(url)
    if current is not None:
        if current.get("status") != "ready" or (model and current.get("model") != model):
            raise RuntimeError("an incompatible local service is already listening")
        return None
    if not python.is_file() or not script.is_file():
        raise FileNotFoundError("a project runtime or service script is missing")
    environment = os.environ.copy()
    environment.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(ROOT / "cache" / "huggingface"),
        "TORCH_HOME": str(ROOT / "cache" / "torch"),
        "CUDA_VISIBLE_DEVICES": "0", "PYTHONIOENCODING": "utf-8",
    })
    process = subprocess.Popen(
        [str(python), "-B", str(script)], cwd=ROOT, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            current = health(url)
            if current and current.get("status") == "ready":
                if model and current.get("model") != model:
                    raise RuntimeError("the local model service reports the wrong model")
                return process
            if process.poll() is not None:
                raise RuntimeError("local service exited before becoming ready")
            if watch_gpu and gpu_free_mib() <= 200:
                raise RuntimeError("GPU free memory reached the pause threshold")
            time.sleep(0.5)
        raise TimeoutError("local service startup timed out")
    except Exception:
        stop_owned(process)
        raise


def run(transcript: str) -> dict:
    initialize_registry()
    accepted = create_task("asr", transcript)
    if accepted.get("deduplicated"):
        return {"status": accepted["status"], "deduplicated": True,
                "app_id": accepted.get("app_id")}
    task_id = accepted["task_id"]
    owned_broker = None
    owned_api = None
    stop_monitor = threading.Event()
    watcher = None
    try:
        resolution = resolve(transcript)
        if resolution["status"] in {"confirmed_alias", "no_match"}:
            free = gpu_free_mib()
            if free > 200:
                emit(PROGRESS_PREFIX, {"stage": "loading_text"})
                owned_broker = ensure_service(
                    TASK_PYTHON, BROKER_SCRIPT, BROKER_HEALTH, timeout=20)
                if health(API_HEALTH) is None and free < 8192:
                    result = process_task(task_id, transcript, free_vram=lambda: free)
                    return result
                owned_api = ensure_service(
                    API_PYTHON, API_SCRIPT, API_HEALTH, model=EXPECTED_MODEL,
                    timeout=120, watch_gpu=True)

                def monitor_gpu() -> None:
                    while not stop_monitor.wait(1):
                        try:
                            unsafe = gpu_free_mib() <= 200
                        except (OSError, ValueError, subprocess.SubprocessError):
                            unsafe = True
                        if unsafe:
                            try:
                                request_cancel(task_id)
                            except (OSError, ValueError, RuntimeError):
                                pass
                            stop_owned(owned_api)
                            return

                watcher = threading.Thread(target=monitor_gpu, daemon=True)
                watcher.start()
        emit(PROGRESS_PREFIX, {"stage": "executing"})
        return process_task(task_id, transcript)
    except Exception as error:
        try:
            _update(TASK_DATABASE, task_id, "failed",
                    error_code="voice_command_runtime_failed",
                    error_detail=type(error).__name__)
            return task_status(task_id)
        except Exception:
            return {"status": "failed", "error_code": "voice_command_runtime_failed",
                    "error_type": type(error).__name__}
    finally:
        stop_monitor.set()
        if watcher is not None:
            watcher.join(timeout=3)
        stop_owned(owned_api)
        stop_owned(owned_broker)


def main() -> int:
    raw = sys.stdin.buffer.read(4096)
    if not 1 <= len(raw) <= 4000:
        emit(RESULT_PREFIX, {"status": "rejected", "error_code": "invalid_voice_input"})
        return 1
    try:
        transcript = raw.decode("utf-8", errors="strict").strip()
        result = run(transcript)
    except (UnicodeDecodeError, ValueError) as error:
        result = {"status": "rejected", "error_code": type(error).__name__}
    emit(RESULT_PREFIX, result)
    return 0 if result.get("status") in {"succeeded", "already_running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
