"""One policy and execution entry point for typed text and saved ASR output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from application_registry import (  # noqa: E402
    DATABASE as REGISTRY_DATABASE,
    approved_applications,
    get_application,
    initialize as initialize_registry,
    lexical_candidates,
    normalized as normalized_command,
    pending_alias_reviews,
    resolve,
    review_alias,
)
from app_launch_broker import check  # noqa: E402
from modelscope_vision import VisionQuotaError  # noqa: E402
from visual_application_check import verify_visible_application  # noqa: E402
from web_alias_resolution import lookup as lookup_web_alias  # noqa: E402

TASK_DATABASE = ROOT / "data" / "task-journal.sqlite3"
HERMES = ROOT / "runtime" / "hermes-home" / "bin" / "hermes.cmd"
HERMES_HOME = ROOT / "runtime" / "hermes-compat-home"
HERMES_STATE = HERMES_HOME / "state.db"
RETRIEVAL_PYTHON = ROOT / "runtime" / "retrieval" / ".pixi" / "envs" / "default" / "python.exe"
SEMANTIC_SCRIPT = ROOT / "services" / "semantic_candidates.py"
MODEL_HEALTH_URL = "http://127.0.0.1:8765/healthz"
DEDUPLICATION_SECONDS = 60
HERMES_TIMEOUT_SECONDS = 150
PAUSE_GPU_FREE_MIB = 200
HERMES_MCP_PREFIX = "mcp__localai_vision__"

ACTIVE_STATES = {
    "accepted", "resolving", "authorizing", "running", "verifying",
}
DEDUPLICATION_STATES = ACTIVE_STATES | {"succeeded", "already_running"}
TERMINAL_STATES = {
    "succeeded", "already_running", "needs_confirmation", "ambiguous",
    "rejected", "failed", "paused", "cancelled", "cancelled_after_dispatch",
}
ALLOWED_TRANSITIONS = {
    "accepted": {"resolving", "cancelled", "failed"},
    "resolving": {"authorizing", "needs_confirmation", "ambiguous",
                  "rejected", "failed", "paused", "cancelled"},
    "authorizing": {"running", "already_running", "failed", "paused", "cancelled"},
    "running": {"running", "verifying", "failed", "cancelled",
                "cancelled_after_dispatch"},
    "verifying": {"running", "succeeded", "already_running", "failed",
                  "cancelled_after_dispatch"},
}


def initialize_journal(path: Path = TASK_DATABASE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as database:
        with database:
            database.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL CHECK (source IN ('text', 'asr')),
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    app_id TEXT,
                    error_code TEXT,
                    error_detail TEXT,
                    result_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (cancel_requested IN (0, 1)),
                    hermes_pid INTEGER
                )
            """)
            database.execute("""
                CREATE INDEX IF NOT EXISTS task_dedupe_idx
                ON tasks(input_sha256, created_epoch DESC)
            """)


def _now() -> tuple[float, str]:
    epoch = time.time()
    return epoch, datetime.now().astimezone().isoformat(timespec="milliseconds")


def _connect(path: Path = TASK_DATABASE) -> sqlite3.Connection:
    initialize_journal(path)
    return sqlite3.connect(path, timeout=10)


def _validated_transcript(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("transcript must be text")
    cleaned = " ".join(text.split())
    if not 1 <= len(cleaned) <= 500 or any(ord(char) < 32 for char in cleaned):
        raise ValueError("transcript must contain 1 to 500 printable characters")
    return cleaned


def load_asr_transcript(report_path: str | Path, clip: str,
                        root: Path = ROOT) -> str:
    voice_dir = (root / "声音").resolve()
    supplied = Path(report_path)
    if not supplied.is_absolute():
        supplied = root / supplied
    resolved = supplied.resolve(strict=True)
    try:
        resolved.relative_to(voice_dir)
    except ValueError:
        raise ValueError("ASR report must be inside the project 声音 folder") from None
    if resolved.suffix.lower() != ".json" or resolved.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("ASR report must be a JSON file no larger than 10 MiB")
    report = json.loads(resolved.read_text(encoding="utf-8"))
    clips = report.get("clips") if isinstance(report, dict) else None
    if not isinstance(clips, list):
        raise ValueError("ASR report does not contain a clips list")
    matches = [item for item in clips if isinstance(item, dict)
               and item.get("file") == clip]
    if len(matches) != 1 or not isinstance(matches[0].get("transcript"), str):
        raise ValueError("ASR clip was not found exactly once in the report")
    return _validated_transcript(matches[0]["transcript"])


def create_task(source: str, transcript: str, path: Path = TASK_DATABASE,
                dedupe_seconds: int = DEDUPLICATION_SECONDS) -> dict:
    if source not in {"text", "asr"}:
        raise ValueError("source must be text or asr")
    text = _validated_transcript(transcript)
    dedupe_text = normalized_command(text)
    for phrase in ("麻烦帮我", "请帮我", "请", "麻烦", "帮我", "一下"):
        dedupe_text = dedupe_text.replace(normalized_command(phrase), "")
    fingerprint = hashlib.sha256(dedupe_text.encode("utf-8")).hexdigest()
    epoch, timestamp = _now()
    task_id = uuid.uuid4().hex
    with closing(_connect(path)) as database:
        database.row_factory = sqlite3.Row
        # Serialize the dedupe check with insertion so concurrent text and ASR
        # submissions cannot both pass the same empty-window check.
        database.execute("BEGIN IMMEDIATE")
        try:
            existing = database.execute("""
                SELECT task_id, status, app_id, error_code, result_json
                FROM tasks
                WHERE input_sha256 = ? AND created_epoch >= ?
                  AND status IN ('accepted', 'resolving', 'authorizing', 'running',
                                 'verifying', 'succeeded', 'already_running')
                ORDER BY created_epoch DESC LIMIT 1
            """, (fingerprint, epoch - dedupe_seconds)).fetchone()
            if existing:
                database.commit()
                return {**dict(existing), "deduplicated": True}
            database.execute("""
                INSERT INTO tasks(task_id, source, input_sha256, status,
                                  created_epoch, created_at, updated_at)
                VALUES (?, ?, ?, 'accepted', ?, ?, ?)
            """, (task_id, source, fingerprint, epoch, timestamp, timestamp))
            database.commit()
        except Exception:
            database.rollback()
            raise
    return {"task_id": task_id, "source": source, "status": "accepted",
            "deduplicated": False}


def _task(path: Path, task_id: str) -> dict | None:
    with closing(_connect(path)) as database:
        database.row_factory = sqlite3.Row
        row = database.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def _update(path: Path, task_id: str, status: str, *, app_id: str | None = None,
            error_code: str | None = None, error_detail: str | None = None,
            result: dict | None = None, hermes_pid: int | None = None) -> None:
    _, timestamp = _now()
    with closing(_connect(path)) as database:
        database.row_factory = sqlite3.Row
        with database:
            current = database.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not current:
                raise KeyError("task was not found")
            previous = current["status"]
            if status != previous and status not in ALLOWED_TRANSITIONS.get(previous, set()):
                raise RuntimeError(f"invalid task state transition: {previous} -> {status}")
            database.execute("""
                UPDATE tasks SET status = ?, updated_at = ?,
                    app_id = COALESCE(?, app_id), error_code = ?, error_detail = ?,
                    result_json = COALESCE(?, result_json),
                    hermes_pid = COALESCE(?, hermes_pid)
                WHERE task_id = ?
            """, (status, timestamp, app_id, error_code, error_detail,
                  json.dumps(result, ensure_ascii=False) if result is not None else None,
                  hermes_pid, task_id))


def request_cancel(task_id: str, path: Path = TASK_DATABASE) -> dict:
    _, timestamp = _now()
    with closing(_connect(path)) as database:
        database.row_factory = sqlite3.Row
        with database:
            row = database.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise KeyError("task was not found")
            if row["status"] in TERMINAL_STATES:
                return {"task_id": task_id, "status": row["status"], "cancel": "too_late"}
            database.execute("""
                UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE task_id = ?
            """, (timestamp, task_id))
            if row["status"] == "accepted":
                database.execute("UPDATE tasks SET status = 'cancelled' WHERE task_id = ?",
                                 (task_id,))
                return {"task_id": task_id, "status": "cancelled", "cancel": "accepted"}
    return {"task_id": task_id, "status": row["status"], "cancel": "requested"}


def _cancel_requested(path: Path, task_id: str) -> bool:
    row = _task(path, task_id)
    return bool(row and row["cancel_requested"])


def gpu_free_mib() -> int:
    output = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"],
        text=True, timeout=5)
    value = output.split()[0]
    return int(value)


def _model_ready(url: str = MODEL_HEALTH_URL) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.load(response)
        return payload.get("status") == "ready"
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return False


def semantic_lookup(command: str, task_id: str,
                    journal_path: Path = TASK_DATABASE) -> dict:
    """Ask the optional CPU retrieval environment for candidates only."""
    if not RETRIEVAL_PYTHON.is_file() or not SEMANTIC_SCRIPT.is_file():
        return {"status": "semantic_unavailable", "candidates": [], "executes": False}
    environment = os.environ.copy()
    environment["CUDA_VISIBLE_DEVICES"] = ""
    environment["HF_HOME"] = str(ROOT / "cache" / "huggingface")
    environment["MODELSCOPE_CACHE"] = str(ROOT / "cache" / "modelscope")
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    try:
        process = subprocess.Popen(
            [str(RETRIEVAL_PYTHON), "-B", str(SEMANTIC_SCRIPT), "query", command],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return {"status": "semantic_unavailable", "candidates": [], "executes": False}
    deadline = time.monotonic() + 120
    while True:
        if _cancel_requested(journal_path, task_id):
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            return {"status": "cancelled", "candidates": [], "executes": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            return {"status": "semantic_timeout", "candidates": [], "executes": False}
        try:
            stdout, _stderr = process.communicate(timeout=min(0.5, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    if process.returncode:
        return {"status": "semantic_unavailable", "candidates": [], "executes": False}
    try:
        result = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {"status": "semantic_unavailable", "candidates": [], "executes": False}
    if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
        return {"status": "semantic_unavailable", "candidates": [], "executes": False}
    return result


def _hermes_call(task_id: str, prompt: str, tool_name: str, path: Path,
                 *, timeout: int = HERMES_TIMEOUT_SECONDS,
                 allowed_app_id: str | None = None) -> dict:
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(HERMES_HOME)
    environment["LOCALAI_MCP_ONLY_TOOL"] = tool_name
    if allowed_app_id:
        environment["LOCALAI_MCP_ALLOWED_APP"] = allowed_app_id
    command = [str(HERMES), "chat", "-Q", "-t", "localai_vision",
               "-m", "local-qwen3-8b-nf4", "--provider", "lmstudio",
               "--max-turns", "2", "--run-budget", "100", "-q", prompt]
    if not HERMES.is_file():
        raise FileNotFoundError("Hermes executable is missing")
    started = time.time()
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _update(path, task_id, "running", hermes_pid=process.pid)
    deadline = time.monotonic() + timeout
    cancelled = False
    timed_out = False
    while True:
        if _cancel_requested(path, task_id):
            cancelled = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            break
        try:
            stdout, stderr = process.communicate(timeout=min(0.5, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    combined = stdout_text + "\n" + stderr_text
    session = re.search(r"session_id:\s*([A-Za-z0-9_-]+)", combined)
    session_id = session.group(1) if session else None
    if not session_id:
        try:
            with closing(sqlite3.connect(HERMES_STATE, timeout=2)) as database:
                row = database.execute(
                    "SELECT session_id FROM messages WHERE role = 'user' AND content = ? "
                    "AND timestamp >= ? ORDER BY id DESC LIMIT 1",
                    (prompt, started - 2)).fetchone()
                session_id = row[0] if row else None
        except sqlite3.Error:
            session_id = None
    tools = []
    if session_id:
        try:
            with closing(sqlite3.connect(HERMES_STATE, timeout=2)) as database:
                tools = [name.removeprefix(HERMES_MCP_PREFIX) for (name,) in database.execute(
                    "SELECT tool_name FROM messages WHERE session_id = ? AND role = 'tool'",
                    (session_id,)) if name]
        except sqlite3.Error:
            tools = []
    return {"exit_code": process.returncode, "session_id": session_id,
            "called_tools": tools, "cancelled": cancelled, "timed_out": timed_out,
            "stderr_type": "present" if stderr_text.strip() else "empty"}


def _safe_result(result: dict) -> dict:
    return {key: result.get(key) for key in (
        "status", "already_running", "launch_pid", "process_pid", "window_title",
        "elapsed_seconds", "elevation_requested") if key in result}


def _post_launch_steps(text: str, matched_aliases: list[str]) -> list[str]:
    """Take only explicit actions following the named application."""
    found = [re.search(re.escape(alias), text, re.I) for alias in matched_aliases]
    found = [match for match in found if match is not None]
    if not found:
        return []
    tail = text[max(found, key=lambda match: match.end()).end():]
    clauses = re.split(r"(?:然后|接着|随后|并且|再|[,，。;；])", tail)
    actions = []
    for clause in clauses:
        match = re.search(r"(?:选择|点击|双击|点开|进入|打开|找到|切换到|选第|选中).+", clause)
        if match:
            action = match.group(0).strip()
            # A follow-up such as "点击并打开" restates the preceding choice;
            # without an object it must not become a second blind click.
            if actions and re.fullmatch(r"(?:点击|点开|打开|进入)(?:并|再)?(?:打开|进入)?", action):
                continue
            actions.append(action)
    return actions


def _verify_screen(task_id: str, app_id: str, name: str, journal_db: Path,
                   screen_verify, settle, *, expected_action: str | None = None) -> dict | None:
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled_after_dispatch", app_id=app_id,
                error_code="cancelled_before_visual_check")
        return None
    settle(5 if expected_action is None else 2)
    try:
        result = screen_verify(name, expected_action=expected_action)
        if not isinstance(result, dict) or type(result.get("visible")) is not bool:
            raise ValueError("screen verifier returned an invalid result")
        if expected_action is not None and type(result.get("action_completed")) is not bool:
            raise ValueError("screen verifier did not verify the action")
        return result
    except VisionQuotaError:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="vision_quota_or_rate_limit")
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="visual_verification_unavailable")
    return None


def _finish_visible_task(task_id: str, text: str, application: dict,
                         matched_aliases: list[str], process: dict, *,
                         was_running: bool, journal_db: Path, model_ready,
                         hermes_call, screen_verify, settle) -> dict:
    app_id = application["app_id"]
    if process.get("status") not in {"window_visible", "process_running"}:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="launch_process_not_verified", result={"window": _safe_result(process)})
        return task_status(task_id, journal_db)
    _update(journal_db, task_id, "verifying", app_id=app_id)
    visual = _verify_screen(task_id, app_id, application["display_name"], journal_db,
                            screen_verify, settle)
    if visual is None:
        return task_status(task_id, journal_db)
    if not visual["visible"]:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="application_not_visible",
                result={"process": _safe_result(process), "screen": visual})
        return task_status(task_id, journal_db)
    steps = _post_launch_steps(text, matched_aliases)
    if not steps:
        _update(journal_db, task_id, "already_running" if was_running else "succeeded",
                app_id=app_id, result={"process": _safe_result(process), "screen": visual,
                                       "new_launch": not was_running})
        return task_status(task_id, journal_db)
    if len(steps) > 3:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="too_many_window_actions")
        return task_status(task_id, journal_db)
    if not model_ready():
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="local_text_model_not_ready")
        return task_status(task_id, journal_db)
    for index, step in enumerate(steps, 1):
        if _cancel_requested(journal_db, task_id):
            _update(journal_db, task_id, "cancelled_after_dispatch", app_id=app_id,
                    error_code="cancelled_before_window_action")
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "running", app_id=app_id)
        prompt = (
            "应用已启动并经过画面核验。现在只完成用户的这一项窗口动作："
            f"{step}。目标应用：{application['display_name']}；应用标识：{app_id}。"
            "只调用已提供的 Windows 点击工具一次，参数 app_id 必须是上述标识；"
            "先用本地 OCR 定位文字，读不到再用视觉定位。"
            "若画面上没有明确目标，不得猜测或点击。不要执行后续步骤。"
        )
        try:
            call = hermes_call(task_id, prompt, "click_visible_target", journal_db,
                               timeout=175, allowed_app_id=app_id)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            _update(journal_db, task_id, "failed", app_id=app_id,
                    error_code="window_action_tool_failed",
                    result={"completed_steps": index - 1})
            return task_status(task_id, journal_db)
        if call.get("cancelled"):
            _update(journal_db, task_id, "cancelled_after_dispatch", app_id=app_id,
                    error_code="cancelled_during_window_action")
            return task_status(task_id, journal_db)
        if call.get("timed_out") or call.get("exit_code") != 0 or (
                "click_visible_target" not in call.get("called_tools", [])):
            _update(journal_db, task_id, "failed", app_id=app_id,
                    error_code="window_action_not_completed",
                    result={"completed_steps": index - 1})
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "verifying", app_id=app_id)
        visual = _verify_screen(task_id, app_id, application["display_name"], journal_db,
                                screen_verify, settle, expected_action=step)
        if visual is None:
            return task_status(task_id, journal_db)
        if not visual["action_completed"]:
            _update(journal_db, task_id, "failed", app_id=app_id,
                    error_code="window_action_not_visually_verified",
                    result={"completed_steps": index - 1, "screen": visual})
            return task_status(task_id, journal_db)
    _update(journal_db, task_id, "succeeded", app_id=app_id,
            result={"process": _safe_result(process), "screen": visual,
                    "completed_window_steps": len(steps)})
    return task_status(task_id, journal_db)


def process_task(task_id: str, transcript: str, *, source: str | None = None,
                 registry_db: Path = REGISTRY_DATABASE,
                 journal_db: Path = TASK_DATABASE,
                 model_ready=_model_ready,
                 free_vram=gpu_free_mib,
                 semantic_search=semantic_lookup,
                 web_lookup=lookup_web_alias,
                 hermes_call=_hermes_call,
                 app_check=check,
                 screen_verify=verify_visible_application,
                 settle=time.sleep) -> dict:
    text = _validated_transcript(transcript)
    task = _task(journal_db, task_id)
    if task is None:
        raise KeyError("task was not found")
    if task["status"] == "cancelled" or _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled", error_code="cancelled_before_start")
        return task_status(task_id, journal_db)
    _update(journal_db, task_id, "resolving")
    if any(term in text for term in ("卸载", "删除", "清理残留", "格式化")):
        _update(journal_db, task_id, "rejected",
                error_code="destructive_action_needs_separate_flow")
        return task_status(task_id, journal_db)
    resolution = resolve(text, registry_db)
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled", error_code="cancelled_during_resolution")
        return task_status(task_id, journal_db)
    if resolution["status"] in {"blocked_negation", "unsupported_action"}:
        _update(journal_db, task_id, "rejected", error_code=resolution["status"])
        return task_status(task_id, journal_db)
    if resolution["status"] == "ambiguous":
        _update(journal_db, task_id, "ambiguous", error_code="multiple_applications",
                result={"candidate_app_ids": resolution["candidates"]})
        return task_status(task_id, journal_db)
    if resolution["status"] != "confirmed_alias":
        lexical = lexical_candidates(text, path=registry_db)
        semantic = semantic_search(text, task_id, journal_db)
        if _cancel_requested(journal_db, task_id) or semantic.get("status") == "cancelled":
            _update(journal_db, task_id, "cancelled", error_code="cancelled_during_candidate_search")
            return task_status(task_id, journal_db)
        combined: dict[str, dict] = {}
        for source_name, source_candidates in (
                ("sqlite_fts5_trigram", lexical["candidates"]),
                ("bge_faiss_cpu", semantic.get("candidates", []))):
            for item in source_candidates:
                app_id = item.get("app_id")
                if not isinstance(app_id, str):
                    continue
                current = combined.setdefault(app_id, {
                    "app_id": app_id,
                    "display_name": item.get("display_name"),
                    "tool_name": item.get("tool_name"),
                    "sources": [], "executes": False,
                })
                if source_name not in current["sources"]:
                    current["sources"].append(source_name)
                for field in ("matched_alias", "score", "rank"):
                    if field in item:
                        current[field] = item[field]
        candidates = list(combined.values())
        if not model_ready():
            _update(journal_db, task_id, "failed", error_code="local_text_model_not_ready",
                    result={"candidates": candidates, "executes": False})
            return task_status(task_id, journal_db)
        try:
            free = free_vram()
        except (OSError, ValueError, subprocess.SubprocessError):
            _update(journal_db, task_id, "paused", error_code="gpu_status_unavailable")
            return task_status(task_id, journal_db)
        if free <= PAUSE_GPU_FREE_MIB:
            _update(journal_db, task_id, "paused", error_code="gpu_pause_threshold")
            return task_status(task_id, journal_db)
        try:
            web_result = web_lookup(
                text, approved_applications(registry_db),
                cancelled=lambda: _cancel_requested(journal_db, task_id))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            web_result = {"status": "search_unavailable"}
        if web_result["status"] == "cancelled" or _cancel_requested(journal_db, task_id):
            _update(journal_db, task_id, "cancelled", error_code="cancelled_during_web_search")
            return task_status(task_id, journal_db)
        if web_result["status"] != "resolved":
            unavailable = web_result["status"] in {"search_unavailable", "timeout", "not_searched"}
            _update(journal_db, task_id, "failed" if unavailable else "rejected",
                    error_code="web_search_unavailable" if unavailable else "application_not_resolved",
                    result={"candidates": candidates, "executes": False,
                            "web_lookup_status": web_result["status"]})
            return task_status(task_id, journal_db)
        resolution = {**web_result, "status": "confirmed_alias",
                      "resolution_source": "web"}

    app_id = resolution["app_id"]
    application = get_application(app_id, registry_db)
    if application is None:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="registry_approval_missing")
        return task_status(task_id, journal_db)
    if not re.fullmatch(r"launch_[a-z0-9_]{1,64}", application["tool_name"]):
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="invalid_registered_tool")
        return task_status(task_id, journal_db)

    _update(journal_db, task_id, "authorizing", app_id=app_id,
            result={"app_id": app_id, "display_name": application["display_name"],
                    "resolution_source": resolution.get("resolution_source", "registry")})
    executable = Path(application["executable_path"])
    if not executable.is_file():
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="registered_target_missing")
        return task_status(task_id, journal_db)
    try:
        before = app_check(executable, 1)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="prelaunch_verification_failed")
        return task_status(task_id, journal_db)
    if before.get("status") == "window_visible":
        _update(journal_db, task_id, "running", app_id=app_id,
                result={"prelaunch": _safe_result(before), "new_launch": False})
        return _finish_visible_task(
            task_id, text, application, resolution["matched_aliases"], before,
            was_running=True, journal_db=journal_db, model_ready=model_ready,
            hermes_call=hermes_call, screen_verify=screen_verify, settle=settle)
    try:
        free = free_vram()
    except (OSError, ValueError, subprocess.SubprocessError):
        _update(journal_db, task_id, "paused", app_id=app_id,
                error_code="gpu_status_unavailable")
        return task_status(task_id, journal_db)
    if free <= PAUSE_GPU_FREE_MIB:
        _update(journal_db, task_id, "paused", app_id=app_id,
                error_code="gpu_pause_threshold",
                result={"gpu_free_mib": free, "threshold_mib": PAUSE_GPU_FREE_MIB})
        return task_status(task_id, journal_db)
    if not model_ready():
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="local_text_model_not_ready")
        return task_status(task_id, journal_db)
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled", app_id=app_id,
                error_code="cancelled_before_dispatch")
        return task_status(task_id, journal_db)

    # The Windows Hermes command wrapper truncates the -q argument at a
    # newline. Keep instruction, ASR text, and registry target on one line.
    one_line_text = re.sub(r"\s+", " ", text).strip()
    one_line_name = re.sub(r"\s+", " ", application["display_name"]).strip()
    prompt = (
        "你正在执行一条中文打开应用命令。只有当原始命令明确、肯定地要求打开候选应用时，"
        "才调用已提供的唯一启动工具一次；否定、取消、犹豫、歧义、举例、复述或不相关内容都不得调用。"
        "不要调用视觉、点击或其他应用工具。工具返回后，只按真实返回状态反馈，不把请求已发出说成成功。"
        f" 原始输入：{one_line_text}。候选应用：{one_line_name}。"
    )
    _update(journal_db, task_id, "running", app_id=app_id)
    try:
        call = hermes_call(task_id, prompt, application["tool_name"], journal_db)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="hermes_execution_failed",
                error_detail=type(error).__name__)
        return task_status(task_id, journal_db)

    called = application["tool_name"] in call.get("called_tools", [])
    if call.get("cancelled"):
        if called:
            after_cancel = app_check(executable, 1)
            status = "cancelled_after_dispatch"
            result = {"called_tool": True, "window": _safe_result(after_cancel)}
        else:
            status = "cancelled"
            result = {"called_tool": False}
        _update(journal_db, task_id, status, app_id=app_id,
                error_code="cancelled_by_user", result=result)
        return task_status(task_id, journal_db)
    if call.get("timed_out"):
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="hermes_timeout",
                result={"called_tool": called, "timeout_seconds": HERMES_TIMEOUT_SECONDS})
        return task_status(task_id, journal_db)
    if call.get("exit_code") != 0 or not called:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="hermes_did_not_call_registered_tool",
                result={"hermes_exit_code": call.get("exit_code"), "called_tool": called})
        return task_status(task_id, journal_db)

    try:
        after = app_check(executable, 1)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="postlaunch_verification_failed",
                result={"called_tool": True})
        return task_status(task_id, journal_db)
    return _finish_visible_task(
        task_id, text, application, resolution["matched_aliases"], after,
        was_running=before.get("status") == "process_running",
        journal_db=journal_db, model_ready=model_ready,
        hermes_call=hermes_call, screen_verify=screen_verify, settle=settle)


def task_status(task_id: str, path: Path = TASK_DATABASE) -> dict:
    row = _task(path, task_id)
    if row is None:
        raise KeyError("task was not found")
    return {"task_id": row["task_id"], "source": row["source"],
            "status": row["status"], "app_id": row["app_id"],
            "error_code": row["error_code"], "error_detail": row["error_detail"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified local text/ASR application task entry")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="submit typed text or a saved ASR transcript")
    source = submit.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--asr-report")
    submit.add_argument("--clip")
    status = commands.add_parser("status")
    status.add_argument("task_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("task_id")
    commands.add_parser("alias-reviews")
    propose = commands.add_parser("propose-alias")
    propose.add_argument("app_id")
    propose.add_argument("alias")
    propose.add_argument("--source", default="manual")
    review = commands.add_parser("review-alias")
    review.add_argument("review_id", type=int)
    decision = review.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    try:
        initialize_registry()
        initialize_journal()
        if args.command == "status":
            _emit(task_status(args.task_id))
            return 0
        if args.command == "cancel":
            _emit(request_cancel(args.task_id))
            return 0
        if args.command == "alias-reviews":
            _emit({"pending": pending_alias_reviews()})
            return 0
        if args.command == "propose-alias":
            from application_registry import propose_alias
            _emit(propose_alias(args.app_id, args.alias, args.source))
            return 0
        if args.command == "review-alias":
            _emit(review_alias(args.review_id, args.approve))
            return 0
        if args.asr_report:
            if not args.clip:
                parser.error("--clip is required with --asr-report")
            transcript = load_asr_transcript(args.asr_report, args.clip)
            source_name = "asr"
        else:
            if args.clip:
                parser.error("--clip is only valid with --asr-report")
            transcript = _validated_transcript(args.text)
            source_name = "text"
        accepted = create_task(source_name, transcript)
        _emit({key: accepted[key] for key in accepted if key not in {"result_json"}})
        if accepted.get("deduplicated"):
            return 0 if accepted.get("status") in {"succeeded", "already_running"} else 1
        result = process_task(accepted["task_id"], transcript, source=source_name)
        _emit(result)
        return 0 if result["status"] in {"succeeded", "already_running"} else 1
    except (ValueError, OSError, RuntimeError, KeyError, sqlite3.Error) as error:
        _emit({"status": "error", "error_type": type(error).__name__,
               "error": str(error)[:400]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
