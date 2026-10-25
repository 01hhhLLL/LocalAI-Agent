"""Run one short recognized launch command through the shared task entry.

The ASR child has exited before this script starts. Services created here are
stopped after Hermes finishes; an already running service is left untouched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from application_registry import (  # noqa: E402
    initialize as initialize_registry, resolve, resolve_exact_name,
)
from application_discovery import refresh as refresh_app_discovery  # noqa: E402
from task_entry import (  # noqa: E402
    TASK_DATABASE, _update, create_task, gpu_free_mib, process_task,
    resume_review, task_status, _hermes_call,
)
from step_runtime import Deadline, StepSupervisor, call_with_deadline, stop_owned
from model_resources import FileLease, POLICY_VERSION

API_PYTHON = ROOT / "runtime" / "fast" / "Scripts" / "python.exe"
TASK_PYTHON = ROOT / "runtime" / "foundation" / "Scripts" / "python.exe"
API_SCRIPT = ROOT / "services" / "local_openai_api.py"
BROKER_SCRIPT = ROOT / "services" / "app_launch_broker.py"
API_HEALTH = "http://127.0.0.1:8765/healthz"
BROKER_HEALTH = "http://127.0.0.1:8766/health"
EXPECTED_MODEL = "local-qwen3-8b-nf4"
RESULT_PREFIX = "VOICE_COMMAND_RESULT="
PROGRESS_PREFIX = "VOICE_COMMAND_PROGRESS="
CHECKPOINT_PREFIX = "VOICE_COMMAND_CHECKPOINT="
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


def ensure_service(python: Path, script: Path, url: str, *, model: str | None = None,
                   timeout: int = 120, watch_gpu: bool = False, deadline=None) -> subprocess.Popen | None:
    import hashlib
    with FileLease(hashlib.sha256(url.encode()).hexdigest(), ROOT / "runtime" / "service-start-ownership"):
        return _ensure_owned_service(python, script, url, model=model, timeout=timeout,
                                     watch_gpu=watch_gpu, deadline=deadline)


def _ensure_owned_service(python, script, url, *, model=None, timeout=120, watch_gpu=False, deadline=None):
    # Keep watch_gpu accepted for old callers; resource monitoring is disabled.
    step = deadline or Deadline("local_service_start", min(timeout, 180))
    step.remaining()
    current = health(url)
    if current is not None:
        if current.get("status") != "ready" or (model and (
                current.get("model") != model or current.get("resource_policy") != POLICY_VERSION)):
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
        "LOCALAI_SERVICE_PARENT_PID": str(os.getpid()),
    })
    process = subprocess.Popen(
        [str(python), "-B", str(script)], cwd=ROOT, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    cutoff = time.monotonic() + step.remaining(min(timeout, 180))
    try:
        while time.monotonic() < cutoff:
            current = health(url)
            if current and current.get("status") == "ready":
                if model and (current.get("model") != model or current.get("resource_policy") != POLICY_VERSION):
                    raise RuntimeError("the local model service reports the wrong model")
                return process
            if process.poll() is not None:
                raise RuntimeError("local service exited before becoming ready")
            time.sleep(0.5)
        raise TimeoutError("local service startup timed out")
    except Exception:
        stop_owned(process)
        raise


def run(transcript: str, review: dict | None = None, *, source="asr", on_application_complete=None) -> dict:
    def cutoff(report):
        emit(RESULT_PREFIX, {"status": "executed_unverified", "error_code": "step_deadline_exceeded",
                             "execution_state": "requires_local_reconciliation", **report})
    with StepSupervisor("voice_command", on_timeout=cutoff) as supervisor:
        if review is not None:
            return _run(transcript, review, supervisor=supervisor, source=source)
        from voice_task_plan import split_plan
        initialize_registry()
        segments = split_plan(transcript, resolve)
        if len(segments) == 1:
            result = _run(transcript, review, supervisor=supervisor, source=source)
            if on_application_complete is not None:
                on_application_complete(result)
            return result
        results = []
        for index, segment in enumerate(segments, 1):
            result = _run(segment, supervisor=supervisor, source=source,
                          checkpoint_prefix=f"application_{index}:")
            results.append(result)
            if on_application_complete is not None:
                on_application_complete(result)
            if result.get("status") not in {"succeeded", "already_running"} or result.get("deduplicated"):
                return {"status": "executed_unverified", "error_code": "application_sequence_stopped",
                        "completed_applications": index - 1, "tasks": results}
        return {"status": "succeeded", "completed_applications": len(results), "tasks": results}


def _run(transcript, review=None, *, supervisor, source="asr", checkpoint_prefix=""):
    initialize_registry()
    try:
        discovery = refresh_app_discovery()
        if discovery["new_pending"]:
            emit(PROGRESS_PREFIX, {"stage": "new_apps_pending_review",
                                   "count": len(discovery["new_pending"])})
    except (OSError, RuntimeError, ValueError, sqlite3.Error, TimeoutError):
        emit(PROGRESS_PREFIX, {"stage": "app_discovery_unavailable"})
    if review is None:
        accepted = create_task(source, transcript)
        if accepted.get("deduplicated"):
            return {"status": accepted["status"], "deduplicated": True,
                    "app_id": accepted.get("app_id")}
        task_id = accepted["task_id"]
    else:
        task_id = None
        try:
            reviewed = task_status(review["review_task_id"])
        except (KeyError, OSError, sqlite3.Error):
            return {"status": "failed", "error_code": "candidate_review_expired"}
        if reviewed["status"] != "needs_confirmation":
            return {"status": "failed", "error_code": "candidate_review_expired"}
        candidates = reviewed.get("result") or {}
        offered = {item.get("app_id") for item in candidates.get("candidates", [])
                   if isinstance(item, dict)}
        if review.get("selected_app_id") is not None:
            valid_choice = review["selected_app_id"] in offered
        else:
            valid_choice = isinstance(review.get("manual_name"), str) and bool(review["manual_name"].strip())
        if not valid_choice:
            return {**reviewed, "error_code": "candidate_choice_rejected"}

    def execute(**options) -> dict:
        if review is None:
            return process_task(task_id, transcript, **options)
        literal_options = {"literal_name": True} if review.get("literal_name") is True else {}
        return resume_review(review["review_task_id"], transcript,
                             selected_app_id=review.get("selected_app_id"),
                             manual_name=review.get("manual_name"),
                             selection_origin=review.get("selection_origin"), **literal_options, **options)

    owned_broker = None
    owned_api = None
    ready_checked = False
    sequence = 0

    def checkpoint(step):
        nonlocal sequence
        step = checkpoint_prefix + step
        if not supervisor.verified_step(step):
            return
        sequence += 1
        emit(CHECKPOINT_PREFIX, {"verified": True, "sequence": sequence, "step": step})

    def model_ready(*, deadline=None):
        nonlocal owned_api, ready_checked
        if ready_checked:
            current = health(API_HEALTH)
            return bool(current and current.get("status") == "ready" and
                        current.get("resource_policy") == POLICY_VERSION)
        emit(PROGRESS_PREFIX, {"stage": "loading_text"})
        owned_api = ensure_service(API_PYTHON, API_SCRIPT, API_HEALTH,
                                   model=EXPECTED_MODEL, timeout=120, deadline=deadline)
        ready_checked = True
        checkpoint("text_model_ready")
        return True

    def hermes_call(*args, deadline=None, **options):
        nonlocal owned_broker
        if args[2].startswith("launch_"):
            owned_broker = ensure_service(TASK_PYTHON, BROKER_SCRIPT, BROKER_HEALTH,
                                          timeout=20, deadline=deadline)
            checkpoint("launch_broker_ready")
        return call_with_deadline(_hermes_call, *args, deadline=deadline or Deadline("hermes_action"), **options)

    try:
        emit(PROGRESS_PREFIX, {"stage": "executing"})
        return execute(model_ready=model_ready, hermes_call=hermes_call, checkpoint=checkpoint)
    except ValueError:
        if review is not None:
            return {**reviewed, "error_code": "candidate_choice_rejected"}
        _update(TASK_DATABASE, task_id, "failed", error_code="invalid_task_input")
        return task_status(task_id)
    except Exception as error:
        if task_id is None:
            return {"status": "failed", "error_code": "candidate_resume_failed",
                    "error_type": type(error).__name__}
        try:
            current = task_status(task_id)
            state = "executed_unverified" if current["status"] in {"running", "verifying"} else "failed"
            _update(TASK_DATABASE, task_id, state,
                    error_code="voice_command_runtime_failed",
                    error_detail=type(error).__name__)
            return task_status(task_id)
        except Exception:
            return {"status": "failed", "error_code": "voice_command_runtime_failed",
                    "error_type": type(error).__name__}
    finally:
        stop_owned(owned_api)
        stop_owned(owned_broker)


def parse_request(raw: bytes, review_mode: bool) -> tuple[str, dict | None]:
    if not 1 <= len(raw) <= 4000:
        raise ValueError("invalid voice input length")
    if review_mode:
        request = json.loads(raw.decode("utf-8", errors="strict"))
        if not isinstance(request, dict) or set(request) - {
                "transcript", "review_task_id", "selected_app_id", "manual_name", "selection_origin", "literal_name"}:
            raise ValueError("invalid candidate review fields")
        transcript = request.get("transcript")
        review_id = request.get("review_task_id")
        selected = request.get("selected_app_id")
        manual = request.get("manual_name")
        origin = request.get("selection_origin")
        if (not isinstance(transcript, str) or not 1 <= len(transcript.strip()) <= 500
                or not isinstance(review_id, str) or not 1 <= len(review_id) <= 100
                or (selected is None) == (manual is None)
                or (selected is not None and (not isinstance(selected, str)
                                               or not 1 <= len(selected) <= 100))
                or origin not in (None, "user_confirmed", "experimental_countdown", "candidate_countdown")
                or (origin in ("experimental_countdown", "candidate_countdown") and selected is None)
                or ("literal_name" in request and (type(request["literal_name"]) is not bool or manual is None))
                or (manual is not None and (not isinstance(manual, str)
                                            or not 1 <= len(manual.strip()) <= 100))):
            raise ValueError("invalid candidate review values")
        return transcript.strip(), request
    transcript = raw.decode("utf-8", errors="strict").strip()
    if not transcript:
        raise ValueError("empty voice input")
    return transcript, None


def main() -> int:
    raw = sys.stdin.buffer.read(4096)
    try:
        if sys.argv[1:] not in ([], ["--review"], ["--text"]):
            raise ValueError("unsupported command arguments")
        transcript, review = parse_request(raw, sys.argv[1:] == ["--review"])
        result = run(transcript, review=review, source="text" if sys.argv[1:] == ["--text"] else "asr")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        result = {"status": "rejected", "error_code": type(error).__name__}
    emit(RESULT_PREFIX, result)
    return 0 if result.get("status") in {"succeeded", "already_running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
