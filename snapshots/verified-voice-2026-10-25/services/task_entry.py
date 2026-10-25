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
    visible_applications,
    get_application,
    initialize as initialize_registry,
    lexical_candidates,
    normalized as normalized_command,
    pending_alias_reviews,
    ranked_candidates,
    resolve,
    resolve_exact_name,
    review_alias,
)
from app_launch_broker import check, launch  # noqa: E402
from modelscope_vision import VisionQuotaError  # noqa: E402
from visual_application_check import verify_visible_application  # noqa: E402
from web_alias_resolution import lookup as lookup_web_alias  # noqa: E402
from candidate_resolution import qualify_candidates, review_candidates
from step_runtime import Deadline, call_with_deadline, stop_owned
from launch_intent import affirmative_open, blocked_context, simple_open
from integration_intents import explicit_tool_request, search_query
from application_lookup import get_target, search_name


def _integrations_enabled():
    from tool_integrations import enabled
    return enabled()


def _integration_execute(tool, arguments, *, deadline, original_text, cancelled=lambda: False):
    from integration_agent import run
    return run(original_text, tool, expected_arguments=arguments, deadline=deadline, cancelled=cancelled)


def _run_integration_task(task_id, text, tool, arguments, journal_db, *, model_ready, app_id=None, checkpoint=lambda _step: None):
    state = _task(journal_db, task_id)["status"]
    if state == "resolving":
        _update(journal_db, task_id, "authorizing", app_id=app_id)
    _update(journal_db, task_id, "running", app_id=app_id,
            result={"workflow": tool, "execution_state": "dispatch_pending"})
    try:
        if not model_ready():
            raise RuntimeError("Local text model is unavailable")
        result = _integration_execute(tool, arguments, deadline=Deadline("integration_" + tool),
                                      original_text=text, cancelled=lambda: _cancel_requested(journal_db, task_id))
    except Exception as error:
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="integration_" + ("deadline_exceeded" if isinstance(error, TimeoutError) else "failed"),
                error_detail=type(error).__name__, result={"workflow": tool})
        return task_status(task_id, journal_db)
    _update(journal_db, task_id, "verifying", app_id=app_id)
    verified = result.get("verified") is True
    if verified:
        checkpoint("integration_result_verified:" + tool)
    _update(journal_db, task_id, "succeeded" if verified else "executed_unverified", app_id=app_id,
            error_code=None if verified else "integration_result_not_verified",
            result={"workflow": tool, "original_text": text, **result})
    return task_status(task_id, journal_db)


def _open_catalog_target(task_id, text, application, journal_db, *, checkpoint):
    """A resolved catalog identity uses the existing verified opener, not a new guess."""
    from tool_integrations import execute
    from application_registry import requested_application_name
    app_id = application["app_id"]
    _update(journal_db, task_id, "authorizing", app_id=app_id)
    aliases = [application["display_name"], requested_application_name(text)]
    if (_post_launch_steps(text, aliases) or re.search(r"启动|运行|测试|烤机|压力|benchmark|导出|读取",
                                                    _application_tail(text, aliases), re.I)):
        _update(journal_db, task_id, "failed", error_code="unsupported_toolbox_followup_action")
        return task_status(task_id, journal_db)
    if not _integrations_enabled():
        _update(journal_db, task_id, "failed", error_code="toolbox_opener_disabled")
        return task_status(task_id, journal_db)
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled", error_code="cancelled_before_dispatch")
        return task_status(task_id, journal_db)
    _update(journal_db, task_id, "running", result={"workflow": "toolbox_open",
            "original_text": text, "selected_name": application["display_name"],
            "selected_entry": application["entry_path"], "execution_state": "dispatch_pending"})
    try:
        observed = execute("toolbox_open", {"name": application["display_name"], "entry_id": app_id},
                           deadline=Deadline("selected_toolbox_entry"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError) as error:
        _update(journal_db, task_id, "executed_unverified", error_code="toolbox_open_failed",
                error_detail=type(error).__name__)
        return task_status(task_id, journal_db)
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled_after_dispatch", error_code="cancelled_during_toolbox_open", result=observed)
        return task_status(task_id, journal_db)
    verified = (observed.get("verified") is True and isinstance(observed.get("path"), str)
                and Path(observed["path"]).resolve() == Path(application["entry_path"]).resolve())
    observed = {**observed, "verified": verified, "workflow": "toolbox_open", "original_text": text,
                "selected_name": application["display_name"], "selected_entry": application["entry_path"]}
    _update(journal_db, task_id, "verifying", result=observed)
    if verified:
        checkpoint("toolbox_entry_window_verified")
    _update(journal_db, task_id, "succeeded" if verified else "executed_unverified",
            error_code=None if verified else "toolbox_result_not_verified", result=observed)
    return task_status(task_id, journal_db)


def _task_database():
    value = os.environ.get("LOCALAI_TASK_JOURNAL")
    if not value:
        return ROOT / "data/task-journal.sqlite3"
    path = Path(value).resolve()
    if not path.is_relative_to((ROOT / "runtime").resolve()) or path.suffix != ".sqlite3":
        raise ValueError("isolated task journal must be inside project runtime")
    return path


TASK_DATABASE = _task_database()
HERMES = ROOT / "runtime" / "hermes-home" / "bin" / "hermes.cmd"
HERMES_HOME = ROOT / "runtime" / "hermes-compat-home"
HERMES_STATE = HERMES_HOME / "state.db"
RETRIEVAL_PYTHON = ROOT / "runtime" / "retrieval" / ".pixi" / "envs" / "default" / "python.exe"
SEMANTIC_SCRIPT = ROOT / "services" / "semantic_candidates.py"
MODEL_HEALTH_URL = "http://127.0.0.1:8765/healthz"
DEDUPLICATION_SECONDS = 60
HERMES_TIMEOUT_SECONDS = 150
HERMES_MCP_PREFIX = "mcp__localai_vision__"

ACTIVE_STATES = {
    "accepted", "resolving", "authorizing", "running", "verifying",
}
DEDUPLICATION_STATES = ACTIVE_STATES | {"succeeded", "already_running"}
TERMINAL_STATES = {
    "succeeded", "already_running", "needs_confirmation", "ambiguous",
    "rejected", "failed", "paused", "cancelled", "cancelled_after_dispatch", "executed_unverified",
}
ALLOWED_TRANSITIONS = {
    "accepted": {"resolving", "cancelled", "failed"},
    "resolving": {"authorizing", "needs_confirmation", "ambiguous",
                  "rejected", "failed", "paused", "cancelled"},
    "authorizing": {"running", "already_running", "failed", "paused", "cancelled"},
    "running": {"running", "verifying", "failed", "cancelled",
                "cancelled_after_dispatch", "executed_unverified"},
    "verifying": {"running", "succeeded", "already_running", "needs_confirmation", "failed",
                  "cancelled_after_dispatch", "executed_unverified"},
}


def initialize_journal(path: Path = TASK_DATABASE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as database:
        with database:
            database.execute("BEGIN IMMEDIATE")
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
            columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)")}
            if "review_of" not in columns:
                database.execute("ALTER TABLE tasks ADD COLUMN review_of TEXT")
            database.execute("CREATE UNIQUE INDEX IF NOT EXISTS task_review_once ON tasks(review_of) WHERE review_of IS NOT NULL")
            database.execute("""CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                phase TEXT NOT NULL, recorded_at TEXT NOT NULL, result_json TEXT)""")


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


def _input_fingerprint(text: str) -> str:
    dedupe_text = normalized_command(text)
    for phrase in ("麻烦帮我", "请帮我", "请", "麻烦", "帮我", "一下"):
        dedupe_text = dedupe_text.replace(normalized_command(phrase), "")
    return hashlib.sha256(dedupe_text.encode("utf-8")).hexdigest()


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
                dedupe_seconds: int = DEDUPLICATION_SECONDS, *, review_of=None) -> dict:
    if source not in {"text", "asr"}:
        raise ValueError("source must be text or asr")
    text = _validated_transcript(transcript)
    fingerprint = _input_fingerprint(text)
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
                WHERE (input_sha256 = ? AND (created_epoch >= ? OR status IN
                       ('running','verifying','executed_unverified','cancelled_after_dispatch'))
                  AND status IN ('accepted', 'resolving', 'authorizing', 'running',
                                 'verifying', 'succeeded', 'already_running', 'executed_unverified', 'cancelled_after_dispatch'))
                   OR (review_of IS NOT NULL AND review_of = ?)
                ORDER BY created_epoch DESC LIMIT 1
            """, (fingerprint, epoch - dedupe_seconds, review_of)).fetchone()
            if existing:
                database.commit()
                return {**dict(existing), "deduplicated": True}
            database.execute("""
                INSERT INTO tasks(task_id, source, input_sha256, status,
                                  created_epoch, created_at, updated_at, review_of)
                VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)
            """, (task_id, source, fingerprint, epoch, timestamp, timestamp, review_of))
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
                "SELECT status, result_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not current:
                raise KeyError("task was not found")
            previous = current["status"]
            if result is not None and "name_lookup" not in result:
                lookup_trace = json.loads(current["result_json"] or "{}").get("name_lookup")
                if lookup_trace is not None:
                    result = {**result, "name_lookup": lookup_trace}
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
            database.execute("INSERT INTO task_events(task_id, phase, recorded_at, result_json) VALUES (?,?,?,?)",
                             (task_id, status, timestamp, json.dumps(
                                 {"error_code": error_code, "result": result}, ensure_ascii=False)))


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
                    journal_path: Path = TASK_DATABASE, *, deadline=None) -> dict:
    """Ask the optional CPU retrieval environment for candidates only."""
    if not RETRIEVAL_PYTHON.is_file() or not SEMANTIC_SCRIPT.is_file():
        return {"status": "semantic_unavailable", "candidates": [], "executes": False}
    step = deadline or Deadline("semantic_name_lookup", 120)
    environment = step.environment()
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
    cutoff = time.monotonic() + step.remaining(120)
    while True:
        if _cancel_requested(journal_path, task_id):
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
            return {"status": "cancelled", "candidates": [], "executes": False}
        remaining = cutoff - time.monotonic()
        if remaining <= 0:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
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
                 allowed_app_id: str | None = None, window_handle=None, deadline=None) -> dict:
    if tool_name == "click_visible_target":
        return _compact_call(task_id, prompt, allowed_app_id, path,
                             window_handle=window_handle, deadline=deadline or Deadline("window_action"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("bounded_hermes_runtime", ROOT / "scripts" / "run-hermes-bounded.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    step = deadline or Deadline("hermes_action", min(timeout, 180))
    environment = step.environment()
    environment["LOCALAI_HELPER_PARENT_PID"] = str(os.getpid())
    environment["HERMES_HOME"] = str(HERMES_HOME)
    environment["LOCALAI_MCP_ONLY_TOOL"] = tool_name
    if allowed_app_id:
        environment["LOCALAI_MCP_ALLOWED_APP"] = allowed_app_id
    command = [*runtime.hermes_python_command(), "chat", "-Q", "-t", "localai_vision",
               "-m", "local-qwen3-8b-nf4", "--provider", "lmstudio",
               "--max-turns", "2", "--run-budget", str(int(step.remaining(100))), "--query-file", "-"]
    if not HERMES.is_file():
        raise FileNotFoundError("Hermes executable is missing")
    started = time.time()
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdin.write(prompt.encode("utf-8"))
    process.stdin.close()
    process.stdin = None
    _update(path, task_id, "running", hermes_pid=process.pid)
    cutoff = time.monotonic() + step.remaining(min(timeout, 180))
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
                stdout, stderr = process.communicate(timeout=3)
            break
        remaining = cutoff - time.monotonic()
        if remaining <= 0:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=3)
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


def _compact_call(task_id, prompt, app_id, journal, *, deadline, window_handle=None):
    environment = deadline.environment()
    environment["LOCALAI_HELPER_PARENT_PID"] = str(os.getpid())
    environment["LOCALAI_WINDOW_ACTION_TASK_ID"] = task_id
    python = ROOT / "runtime/foundation/Scripts/python.exe"
    process = subprocess.Popen([str(python), "-B", str(ROOT / "services/compact_window_action.py")],
        cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    process.stdin.write(json.dumps({"action": prompt, "app_id": app_id,
        "window_handle": window_handle}, ensure_ascii=False).encode("utf-8"))
    process.stdin.close()
    process.stdin = None
    try:
        while True:
            if _cancel_requested(journal, task_id):
                return {"cancelled": True, "called_tools": []}
            try:
                output, _ = process.communicate(timeout=deadline.remaining(.5))
                break
            except subprocess.TimeoutExpired:
                continue
        result = json.loads(output.decode("utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("invalid compact action result")
        # This route has already made a local verification attempt. Do not turn
        # a failed local result into an unsolicited external screenshot request.
        result.setdefault("screen", {"visible": True, "state": "main_window", "action_completed": False})
        return result
    finally:
        if process.poll() is None:
            # Foundation has no psutil dependency. The owned Popen handle is
            # still live; /T only stops this worker's locator/pointer children.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stop_owned(process)


def _safe_result(result: dict) -> dict:
    return {key: result.get(key) for key in (
        "status", "already_running", "launch_pid", "process_pid", "window_title",
        "elapsed_seconds", "elevation_requested", "window_handle", "registered_window_verified",
        "window_pid", "window_foreground", "activation_verified", "execution_state") if key in result}


def _application_tail(text, matched_aliases):
    from application_registry import primary_launch_clause
    clause = primary_launch_clause(text)
    hierarchy = re.search(r"(?:里面的|里的|中的|内的)(?=.{1,60}(?:页面|界面|分类|菜单))", clause)
    if hierarchy:
        return text[text.find(clause) + hierarchy.end():]
    found = [re.search(re.escape(alias), text, re.I) for alias in matched_aliases]
    found = [match for match in found if match is not None]
    if not found:
        tail = text[text.find(clause) + len(clause):]
    else:
        tail = text[max(found, key=lambda match: match.end()).end():]
    return tail


def _post_launch_steps(text: str, matched_aliases: list[str]) -> list[str]:
    """Take only explicit actions following the named application."""
    tail = _application_tail(text, matched_aliases)
    child = re.fullmatch(r"\s*(?:里面的|里的|中的|内的)(.{1,100}?)\s*[。.]?", tail)
    if child:
        return ["打开" + child[1]]
    location = re.fullmatch(r"\s*(.{1,40}?)(?:页面|界面|分类|菜单)(?:下|里面|中|内|里)?\s*[,，:：]\s*(.{1,80}?)\s*[。.]?", tail)
    if location:
        return ["进入" + location[1], "打开" + location[2].rstrip("。.")]
    clauses = re.split(r"(?:然后|接着|随后|并且|再|[,，。;；])", tail)
    actions = []
    for clause in clauses:
        match = re.search(r"(?:选择|点击|双击|点开|进入|打开|找到|切换到|选第|选中).+", clause)
        if match:
            action = match.group(0).strip()
            nested = re.fullmatch(r"(?:打开|进入|找到|选择)(.{1,40}?)(?:里面的|里的|中的|内的)(.{1,80})", action)
            if nested:
                actions.extend(["进入" + nested[1], "打开" + nested[2]])
                continue
            # A follow-up such as "点击并打开" restates the preceding choice;
            # without an object it must not become a second blind click.
            if actions and re.fullmatch(r"(?:点击|点开|打开|进入)(?:并|再)?(?:打开|进入)?", action):
                continue
            actions.append(action)
    return actions


def _verify_screen(task_id: str, app_id: str, name: str, journal_db: Path,
                   screen_verify, settle, *, expected_action: str | None = None,
                   deadline=None) -> dict | None:
    deadline = deadline or Deadline("result_verification")
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled_after_dispatch", app_id=app_id,
                error_code="cancelled_before_visual_check")
        return None
    try:
        settle(min(5 if expected_action is None else 2, deadline.remaining()))
        result = call_with_deadline(screen_verify, name, expected_action=expected_action, deadline=deadline)
        if not isinstance(result, dict) or type(result.get("visible")) is not bool:
            raise ValueError("screen verifier returned an invalid result")
        if expected_action is not None and type(result.get("action_completed")) is not bool:
            raise ValueError("screen verifier did not verify the action")
        return result
    except VisionQuotaError:
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="vision_quota_or_rate_limit")
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="visual_verification_unavailable")
    return None


def _finish_visible_task(task_id: str, text: str, application: dict,
                         matched_aliases: list[str], process: dict, *,
                         was_running: bool, journal_db: Path, model_ready,
                         hermes_call, screen_verify, settle, checkpoint=lambda _step: None, app_check=check) -> dict:
    app_id = application["app_id"]
    if process.get("status") not in {"window_visible", "process_running"}:
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="launch_process_not_verified", result={"window": _safe_result(process)})
        return task_status(task_id, journal_db)
    _update(journal_db, task_id, "verifying", app_id=app_id)
    if re.search(r"正在(?:打开|启动|加载)|\b(?:opening|starting|loading)\b", process.get("window_title") or "", re.I):
        ready_deadline = Deadline("application_ready_window", 25)
        while True:
            if _cancel_requested(journal_db, task_id):
                _update(journal_db, task_id, "cancelled_after_dispatch", error_code="cancelled_while_loading")
                return task_status(task_id, journal_db)
            try:
                settle(ready_deadline.remaining(.3))
                process = app_check(Path(application["executable_path"]), 1)
                ready_deadline.remaining()
                if process.get("status") == "window_visible" and not re.search(
                        r"正在(?:打开|启动|加载)|\b(?:opening|starting|loading)\b", process.get("window_title") or "", re.I):
                    break
            except (TimeoutError, OSError, RuntimeError, subprocess.SubprocessError):
                _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                        error_code="application_ready_window_not_verified", result={"process": _safe_result(process)})
                return task_status(task_id, journal_db)
    if (process.get("registered_window_verified") is True
            and type(process.get("window_handle")) is int and process["window_handle"] > 0):
        if process.get("window_foreground") is False and not _post_launch_steps(text, matched_aliases):
            from approved_window_process import focus_registered_window
            try:
                focused = focus_registered_window(application["executable_path"], process["window_handle"],
                                                  expected_pid=process.get("window_pid"))
            except (OSError, RuntimeError, ValueError):
                focused = False
            if not focused:
                _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                        error_code="application_activation_not_verified", result={"process": _safe_result(process)})
                return task_status(task_id, journal_db)
            process = {**process, "window_foreground": True, "activation_verified": True}
        visual = {"visible": True, "state": "local_window", "evidence": "registered_window_identity"}
    else:
        visual = _verify_screen(task_id, app_id, application["display_name"], journal_db,
                                screen_verify, settle)
    if visual is None:
        return task_status(task_id, journal_db)
    if not visual["visible"]:
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="application_not_visible",
                result={"process": _safe_result(process), "screen": visual})
        return task_status(task_id, journal_db)
    checkpoint("application_window_verified")
    steps = _post_launch_steps(text, matched_aliases)
    if app_id == "tuba" and len(steps) == 1 and _integrations_enabled():
        target = re.sub(r"^(?:打开|点开|进入|找到|选择)", "", steps[0]).strip(" 。.")
        selection = search_name(target, parent="tuba")
        if selection["candidates"]:
            _update(journal_db, task_id, "needs_confirmation",
                    error_code="candidate_selection_required", result={**selection, "name_lookup": selection})
            return task_status(task_id, journal_db)
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
    current_window = None
    for index, step in enumerate(steps, 1):
        action_deadline = Deadline("window_action_" + str(index))
        if _cancel_requested(journal_db, task_id):
            _update(journal_db, task_id, "cancelled_after_dispatch", app_id=app_id,
                    error_code="cancelled_before_window_action")
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "running", app_id=app_id,
                result={"step": "window_action", "action_index": index, "completed_steps": index - 1})
        prompt = step
        try:
            call = call_with_deadline(hermes_call, task_id, prompt, "click_visible_target", journal_db,
                                     timeout=175, allowed_app_id=app_id, window_handle=current_window,
                                     deadline=action_deadline)
        except TimeoutError:
            _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                    error_code="window_action_deadline_exceeded",
                    result={"completed_steps": index - 1})
            return task_status(task_id, journal_db)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            call = {"exit_code": -1, "called_tools": [], "error_type": type(error).__name__}
        if call.get("cancelled"):
            _update(journal_db, task_id, "cancelled_after_dispatch", app_id=app_id,
                    error_code="cancelled_during_window_action")
            return task_status(task_id, journal_db)
        if call.get("timed_out"):
            _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                    error_code="window_action_not_completed",
                    result={"completed_steps": index - 1})
            return task_status(task_id, journal_db)
        if type(call.get("window_handle")) is int and call["window_handle"] > 0:
            current_window = call["window_handle"]
        _update(journal_db, task_id, "verifying", app_id=app_id)
        visual = call.get("screen")
        if visual is None:
            visual = _verify_screen(task_id, app_id, application["display_name"], journal_db,
                                    screen_verify, settle, expected_action=step, deadline=action_deadline)
        else:
            _update(journal_db, task_id, "verifying", app_id=app_id,
                    result={"action_index": index, "action_evidence": call})
        if visual is None:
            return task_status(task_id, journal_db)
        if not visual["action_completed"]:
            _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                    error_code="window_action_not_visually_verified" if call.get("exit_code") == 0 else "window_action_acknowledgement_missing",
                    result={"completed_steps": index - 1, "screen": visual, "action_evidence": call})
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "verifying", app_id=app_id,
                result={"step": "window_action", "action_index": index, "completed_steps": index,
                        "verified": True})
        checkpoint(f"window_action_verified:{index}")
    _update(journal_db, task_id, "succeeded", app_id=app_id,
            result={"process": _safe_result(process), "screen": visual,
                    "completed_window_steps": len(steps), "action_evidence": call})
    return task_status(task_id, journal_db)


def _reviewed_resolution(review_task_id: str, transcript: str,
                         registry_db: Path, journal_db: Path,
                         selected_app_id: str | None,
                         manual_name: str | None, selection_origin=None, literal_name=False) -> dict:
    if type(literal_name) is not bool:
        raise ValueError("literal name option must be boolean")
    if literal_name and selected_app_id is not None:
        raise ValueError("literal name option requires manual input")
    if selection_origin not in (None, "user_confirmed", "experimental_countdown", "candidate_countdown"):
        raise ValueError("invalid candidate selection origin")
    if (selected_app_id is None) == (manual_name is None):
        raise ValueError("choose one candidate or enter one exact application name")
    reviewed = _task(journal_db, review_task_id)
    if (reviewed is None or reviewed["status"] != "needs_confirmation"
            or reviewed["input_sha256"] != _input_fingerprint(transcript)):
        raise ValueError("matching candidate review was not found")
    if selected_app_id is not None:
        result = json.loads(reviewed["result_json"] or "{}")
        allowed = {item.get("app_id") for item in result.get("candidates", [])
                   if isinstance(item, dict)}
        if selected_app_id not in allowed:
            raise ValueError("selected application was not offered for this task")
        if selection_origin == "experimental_countdown":
            from candidate_experiment import validate_experimental_choice
            validate_experimental_choice(result.get("candidates", []), selected_app_id)
        elif selection_origin == "candidate_countdown":
            from candidate_experiment import validate_countdown_choice
            from application_registry import requested_application_name
            target = result.get("requested_name") or requested_application_name(transcript)
            qualified = qualify_candidates(target, result.get("candidates", []), registry_db)
            if not qualified or qualified[0]["app_id"] != selected_app_id:
                raise ValueError("automatic candidate no longer matches the requested application")
            validate_countdown_choice(result.get("candidates", []), selected_app_id)
        application = get_target(selected_app_id, registry_db)
        if application is None:
            raise ValueError("selected application is no longer approved")
        offered_item = next(item for item in result.get("candidates", []) if item.get("app_id") == selected_app_id)
        if application.get("target_kind") == "toolbox" and normalized_command(
                offered_item.get("canonical_name", offered_item.get("display_name", ""))) != normalized_command(application["display_name"]):
            raise ValueError("selected toolbox entry has changed identity; search again")
        return {"status": "confirmed_alias", "app_id": selected_app_id,
                "matched_aliases": [application["display_name"]], "target_kind": application.get("target_kind"),
                "name_lookup": result.get("name_lookup", result),
                "resolution_source": selection_origin or "user_selected_candidate"}
    if selection_origin in ("experimental_countdown", "candidate_countdown"):
        raise ValueError("automatic selection cannot submit a manual name")
    previous = json.loads(reviewed["result_json"] or "{}").get("name_lookup", {})
    attempts = [*previous.get("manual_attempts", []), manual_name]
    repeated = sum(normalized_command(item) == normalized_command(manual_name) for item in attempts) >= 3
    resolution = search_name(manual_name, registry_db, literal=literal_name or repeated,
                             parent=previous.get("parent_scope"))
    trace = {**resolution, "manual_name": manual_name, "manual_attempts": attempts}
    return {**resolution, "name_lookup": trace,
            "resolution_source": "user_entered_name"}


def process_task(task_id: str, transcript: str, *, source: str | None = None,
                 registry_db: Path = REGISTRY_DATABASE,
                 journal_db: Path = TASK_DATABASE,
                 review_task_id: str | None = None,
                 selected_app_id: str | None = None,
                 manual_name: str | None = None,
                 selection_origin=None,
                 literal_name=False,
                 model_ready=_model_ready,
                 free_vram=gpu_free_mib,
                 semantic_search=semantic_lookup,
                 web_lookup=lookup_web_alias,
                 hermes_call=_hermes_call,
                 app_launch=launch,
                 app_check=check,
                 screen_verify=verify_visible_application,
                 settle=time.sleep,
                 checkpoint=lambda _step: None) -> dict:
    text = _validated_transcript(transcript)
    task = _task(journal_db, task_id)
    if task is None:
        raise KeyError("task was not found")
    if task["input_sha256"] != _input_fingerprint(text):
        raise ValueError("task transcript does not match its submitted input")
    if review_task_id is None and (selected_app_id is not None or manual_name is not None):
        raise ValueError("candidate selection requires a prior review task")
    reviewed_resolution = (
        _reviewed_resolution(review_task_id, text, registry_db, journal_db,
                             selected_app_id, manual_name, selection_origin, literal_name)
        if review_task_id is not None else None)
    if task["status"] == "cancelled" or _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled", error_code="cancelled_before_start")
        return task_status(task_id, journal_db)
    _update(journal_db, task_id, "resolving")
    if any(term in text for term in ("卸载", "删除", "清理残留", "格式化")):
        _update(journal_db, task_id, "rejected",
                error_code="destructive_action_needs_separate_flow")
        return task_status(task_id, journal_db)
    integration = explicit_tool_request(text)
    if integration and _integrations_enabled():
        return _run_integration_task(task_id, text, integration["tool"], integration["arguments"],
                                     journal_db, model_ready=model_ready, checkpoint=checkpoint)
    resolution = reviewed_resolution or resolve(text, registry_db)
    if reviewed_resolution is None and resolution["status"] not in {"blocked_negation", "unsupported_action"}:
        from application_registry import requested_application_name
        name = requested_application_name(text)
        # Preserve an explicit parent-app + later action; its child is resolved
        # after the parent window is verified. Literal names share one lookup.
        nested = resolution["status"] == "confirmed_alias" and bool(_post_launch_steps(text, resolution.get("matched_aliases", [])))
        if name and len(name) <= 100 and not nested:
            shared = search_name(name, registry_db)
            if shared["candidates"] or shared["literal_name"]:
                resolution = {**shared, "name_lookup": shared}
            else:
                _update(journal_db, task_id, "resolving", result={"name_lookup": shared})
    if resolution["status"] == "confirmed_alias":
        selected = get_target(resolution["app_id"], registry_db)
        if selected is not None:
            trace = resolution.get("name_lookup") or {}
            evidence = next((item for item in trace.get("candidates", []) if item.get("app_id") == resolution["app_id"]), {})
            selected = {**selected, **evidence}
            selected["entry_path"] = selected.get("entry_path") or selected["executable_path"]
            choice = {key: selected[key] for key in ("app_id", "display_name", "entry_path", "product_version",
                      "description", "parent_name", "category", "folder") if key in selected}
            resolution["name_lookup"] = {**trace, "original_input": text, "selected_target": choice,
                "selection_origin": resolution.get("resolution_source", "registry_exact_name")}
    if resolution.get("name_lookup") is not None:
        _update(journal_db, task_id, "resolving", result={"name_lookup": resolution["name_lookup"]})
    if _cancel_requested(journal_db, task_id):
        _update(journal_db, task_id, "cancelled", error_code="cancelled_during_resolution")
        return task_status(task_id, journal_db)
    if resolution["status"] in {"blocked_negation", "unsupported_action"}:
        _update(journal_db, task_id, "rejected", error_code=resolution["status"])
        return task_status(task_id, journal_db)
    if blocked_context(text):
        _update(journal_db, task_id, "rejected", error_code="affirmative_launch_required")
        return task_status(task_id, journal_db)
    from application_registry import primary_launch_clause
    followup = text[text.find(primary_launch_clause(text)) + len(primary_launch_clause(text)):]
    if resolution["status"] == "confirmed_alias":
        followup = _application_tail(text, resolution.get("matched_aliases", []))
        application = get_target(resolution["app_id"], registry_db)
        query = search_query(followup)
        if (application and Path(application["executable_path"]).name.casefold() == "chrome.exe"
                and query and affirmative_open(text) and _integrations_enabled()):
            return _run_integration_task(task_id, text, "browser_search", {"query": query}, journal_db,
                                         model_ready=model_ready, app_id=resolution["app_id"], checkpoint=checkpoint)
    if re.search(r"输入|发送|保存|下载|安装|写入|搜索", followup):
        _update(journal_db, task_id, "rejected", error_code="unsupported_followup_action",
                result={"execution_state": "not_executed"})
        return task_status(task_id, journal_db)
    if resolution["status"] != "confirmed_alias":
        try:
            review = review_candidates(
                text, resolution=resolution, registry_db=registry_db, task_id=task_id,
                journal_db=journal_db, semantic_search=semantic_search,
                web_lookup=web_lookup, model_ready=model_ready, free_vram=free_vram,
                cancelled=lambda: _cancel_requested(journal_db, task_id))
        except TimeoutError:
            _update(journal_db, task_id, "paused", error_code="name_resolution_timeout")
            return task_status(task_id, journal_db)
        if review.get("cancelled"):
            _update(journal_db, task_id, "cancelled", error_code="cancelled_during_web_search")
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "needs_confirmation",
                error_code="candidate_selection_required" if review["candidates"]
                else "manual_name_required",
                result=review)
        return task_status(task_id, journal_db)

    app_id = resolution["app_id"]
    application = get_target(app_id, registry_db)
    if application is None:
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="registry_approval_missing")
        return task_status(task_id, journal_db)
    if application.get("target_kind") == "toolbox":
        return _open_catalog_target(task_id, text, application, journal_db, checkpoint=checkpoint)
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
            hermes_call=hermes_call, screen_verify=screen_verify, settle=settle, checkpoint=checkpoint, app_check=app_check)
    steps = _post_launch_steps(text, resolution["matched_aliases"])
    if len(steps) > 3:
        _update(journal_db, task_id, "failed", app_id=app_id, error_code="too_many_window_actions")
        return task_status(task_id, journal_db)
    if (simple_open(text, app_id, registry_db, reviewed=reviewed_resolution is not None)
            or (affirmative_open(text) and bool(steps))):
        # Stage the serial text loader before a heavy application's startup.
        # The launch itself is still deterministic and does not use a model.
        if steps and not model_ready():
            _update(journal_db, task_id, "failed", app_id=app_id,
                    error_code="local_text_model_not_ready")
            return task_status(task_id, journal_db)
        if _cancel_requested(journal_db, task_id):
            _update(journal_db, task_id, "cancelled", error_code="cancelled_before_dispatch")
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "running", app_id=app_id,
                result={"dispatch": "registered_broker", "execution_state": "dispatch_pending"})
        try:
            after = app_launch(app_id)
        except (FileNotFoundError, PermissionError) as error:
            _update(journal_db, task_id, "failed", app_id=app_id,
                    error_code="registered_launch_rejected",
                    result={"execution_state": "not_executed", "error_type": type(error).__name__})
            return task_status(task_id, journal_db)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            # Dispatch may have succeeded before its return failed. Observe once;
            # never repeat a side effect because its acknowledgement was lost.
            try:
                observed = app_check(executable, 1)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                observed = {"status": "unknown"}
            if observed.get("registered_window_verified") is True:
                return _finish_visible_task(
                    task_id, text, application, resolution["matched_aliases"], observed,
                    was_running=before.get("status") == "process_running", journal_db=journal_db,
                    model_ready=model_ready, hermes_call=hermes_call, screen_verify=screen_verify,
                    settle=settle, checkpoint=checkpoint, app_check=app_check)
            _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                    error_code="launch_acknowledgement_missing",
                    result={"execution_state": "dispatch_unknown", "observed": _safe_result(observed),
                            "error_type": type(error).__name__})
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "running", app_id=app_id,
                result={"launch": _safe_result(after), "launch_reply_epoch": time.time()})
        return _finish_visible_task(
            task_id, text, application, resolution["matched_aliases"], after,
            was_running=bool(after.get("already_running")), journal_db=journal_db,
            model_ready=model_ready, hermes_call=hermes_call, screen_verify=screen_verify,
            settle=settle, checkpoint=checkpoint, app_check=app_check)
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
    reviewed_choice = (f"用户已明确选择主目标：{one_line_name}。"
                       if reviewed_resolution else "")
    prompt = (
        "你正在执行一条中文打开应用命令。只有当原始命令明确、肯定地要求打开应用，"
        "且目标已精确匹配或由用户明确选定时，"
        "才调用已提供的唯一启动工具一次；否定、取消、犹豫、歧义、举例、复述或不相关内容都不得调用。"
        "不要调用视觉、点击或其他应用工具。工具返回后，只按真实返回状态反馈，不把请求已发出说成成功。"
        f" {reviewed_choice}原始输入：{one_line_text}。候选应用：{one_line_name}。"
    )
    _update(journal_db, task_id, "running", app_id=app_id)
    try:
        call = hermes_call(task_id, prompt, application["tool_name"], journal_db)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
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
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="hermes_timeout",
                result={"called_tool": called, "timeout_seconds": HERMES_TIMEOUT_SECONDS})
        return task_status(task_id, journal_db)
    if call.get("exit_code") != 0 or not called:
        if call.get("exit_code") != 0:
            try:
                observed = app_check(executable, 1)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                observed = {"status": "unknown"}
            if observed.get("registered_window_verified"):
                return _finish_visible_task(
                    task_id, text, application, resolution["matched_aliases"], observed,
                    was_running=before.get("status") == "process_running", journal_db=journal_db,
                    model_ready=model_ready, hermes_call=hermes_call, screen_verify=screen_verify,
                    settle=settle, checkpoint=checkpoint, app_check=app_check)
            _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                    error_code="hermes_dispatch_outcome_unknown",
                    result={"observed": _safe_result(observed), "called_tool": called})
            return task_status(task_id, journal_db)
        _update(journal_db, task_id, "failed", app_id=app_id,
                error_code="hermes_did_not_call_registered_tool",
                result={"hermes_exit_code": call.get("exit_code"), "called_tool": called})
        return task_status(task_id, journal_db)

    try:
        after = app_check(executable, 1)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        _update(journal_db, task_id, "executed_unverified", app_id=app_id,
                error_code="postlaunch_verification_failed",
                result={"called_tool": True})
        return task_status(task_id, journal_db)
    return _finish_visible_task(
        task_id, text, application, resolution["matched_aliases"], after,
        was_running=before.get("status") == "process_running",
        journal_db=journal_db, model_ready=model_ready,
        hermes_call=hermes_call, screen_verify=screen_verify, settle=settle, checkpoint=checkpoint, app_check=app_check)


def resume_review(review_task_id: str, transcript: str, *,
                  selected_app_id: str | None = None,
                  manual_name: str | None = None,
                  selection_origin=None,
                  literal_name=False,
                  registry_db: Path = REGISTRY_DATABASE,
                  journal_db: Path = TASK_DATABASE,
                  **execution_options) -> dict:
    """Run a new task only after a user-selected candidate or exact typed name."""
    text = _validated_transcript(transcript)
    _reviewed_resolution(review_task_id, text, registry_db, journal_db,
                         selected_app_id, manual_name, selection_origin, literal_name)
    reviewed = _task(journal_db, review_task_id)
    accepted = create_task(reviewed["source"], text, journal_db, dedupe_seconds=0, review_of=review_task_id)
    if accepted["deduplicated"]:
        return {**task_status(accepted["task_id"], journal_db), "deduplicated": True}
    try:
        return process_task(accepted["task_id"], text, registry_db=registry_db,
                            journal_db=journal_db, review_task_id=review_task_id,
                            selected_app_id=selected_app_id, manual_name=manual_name,
                            selection_origin=selection_origin, literal_name=literal_name, **execution_options)
    except ValueError:
        # Configuration may be disabled between claiming the review and the
        # second authorization check. Release only a still-unexecuted claim,
        # preserving its failed task and events so human confirmation can resume.
        current = _task(journal_db, accepted["task_id"])
        if current and current["status"] == "accepted":
            _update(journal_db, accepted["task_id"], "failed",
                    error_code="review_authorization_changed",
                    result={"execution_state": "not_executed"})
            with closing(_connect(journal_db)) as database, database:
                database.execute("UPDATE tasks SET review_of=NULL WHERE task_id=? AND status='failed'",
                                 (accepted["task_id"],))
        raise


def task_status(task_id: str, path: Path = TASK_DATABASE) -> dict:
    row = _task(path, task_id)
    if row is None:
        raise KeyError("task was not found")
    with closing(_connect(path)) as database:
        events = [{"phase": phase, "recorded_at": at, "detail": json.loads(detail or "{}")}
                  for phase, at, detail in database.execute(
                      "SELECT phase, recorded_at, result_json FROM task_events WHERE task_id=? ORDER BY event_id",
                      (task_id,))]
    dispatched = any(e["phase"] in {"running", "verifying", "executed_unverified"} for e in events)
    execution_state = ("verified" if row["status"] in {"succeeded", "already_running"} else
                       "executed_unverified" if dispatched else "not_executed")
    final_result = json.loads(row["result_json"]) if row["result_json"] else {}
    if final_result.get("execution_state") == "not_executed" or (
            row["status"] == "failed" and final_result.get("called_tool") is False):
        execution_state = "not_executed"
    return {"task_id": row["task_id"], "source": row["source"],
            "status": row["status"], "app_id": row["app_id"],
            "error_code": row["error_code"], "error_detail": row["error_detail"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "cancel_requested": bool(row["cancel_requested"]),
            "execution_state": execution_state, "events": events,
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
    choose = commands.add_parser("choose-application")
    choose.add_argument("review_task_id")
    choose.add_argument("--text", required=True)
    choice = choose.add_mutually_exclusive_group(required=True)
    choice.add_argument("--app-id")
    choice.add_argument("--name")
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
        if args.command == "choose-application":
            result = resume_review(args.review_task_id, args.text,
                                   selected_app_id=args.app_id,
                                   manual_name=args.name)
            _emit(result)
            return 0 if result["status"] in {"succeeded", "already_running"} else 1
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
