"""Local model proposes names; the program owns one name-only evidence search."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
from application_registry import requested_application_name
from step_runtime import Deadline

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "runtime" / "hermes-home" / "bin" / "hermes.cmd"
HERMES_HOME = ROOT / "runtime" / "hermes-compat-home"
HERMES_STATE = HERMES_HOME / "state.db"
SEARCH_TIMEOUT_SECONDS = 110


def _answer(prompt: str, *, cancelled, timeout: int) -> dict:
    """Local inference has no tools and cannot initiate an external search."""
    if cancelled():
        return {"status": "cancelled"}
    body = json.dumps({"model": "local-qwen3-8b-nf4", "max_tokens": 256,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    request = urllib.request.Request("http://127.0.0.1:8765/v1/chat/completions",
                                     data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, 180)) as response:
            payload = json.load(response)
        return {"status": "answered", "text": payload["choices"][0]["message"]["content"]}
    except TimeoutError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {"status": "proposal_unavailable"}


def parse_answer(answer: dict, transcript: str, applications: list[dict]) -> dict:
    """Fail closed unless the searched answer selects one approved local app."""
    if answer.get("status") != "answered":
        return {"status": answer.get("status", "search_unavailable")}
    try:
        response = json.loads(answer.get("text", ""))
    except (TypeError, json.JSONDecodeError):
        return {"status": "invalid_answer"}
    if not isinstance(response, dict):
        return {"status": "invalid_answer"}
    name = response.get("canonical_name")
    spoken = response.get("spoken_name")
    if name is None:
        return {"status": "not_found"}
    if (not isinstance(name, str) or not isinstance(spoken, str) or
            not spoken.strip() or len(spoken) > 80 or
            spoken.casefold() not in transcript.casefold()):
        return {"status": "invalid_answer"}
    matches = [app for app in applications if app["display_name"].casefold() == name.casefold()]
    if len(matches) != 1:
        return {"status": "not_found"}
    return {"status": "resolved", "app_id": matches[0]["app_id"],
            "display_name": matches[0]["display_name"],
            "tool_name": matches[0]["tool_name"], "matched_aliases": [spoken]}


def _search_evidence(spoken: str, canonical: str, *, timeout=25) -> bool:
    """One program-controlled read-only search, restricted to an app-name span."""
    environments = list((HERMES_HOME / "installs").glob(
        "*/environments/*/venv/Scripts/python.exe"))
    if len(environments) != 1:
        return False
    code = (
        "import sys; sys.path.insert(0, '" + str(ROOT / "packages" / "hermes-agent").replace("\\", "/") + "'); "
        "from tools.web_tools import web_search_tool; "
        "query=sys.stdin.buffer.read(512).decode('utf-8'); "
        "print(web_search_tool(query, limit=5))"
    )
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(HERMES_HOME)
    try:
        result = subprocess.run(
            [str(environments[0]), "-I", "-c", code],
            input=(spoken + " 软件 官方名称").encode("utf-8"),
            capture_output=True, timeout=min(timeout, 25), env=environment, cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout.decode("utf-8"))
    except subprocess.TimeoutExpired:
        raise TimeoutError("name evidence search timed out") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    web = data.get("web", [])
    if not isinstance(web, list):
        return False
    hosts = set()
    for item in web:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("title", "")) + " " + str(item.get("description", ""))
        if canonical.casefold() not in snippet.casefold():
            continue
        url = str(item.get("url", ""))
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname:
            hosts.add(parsed.hostname.removeprefix("www."))
    return len(hosts) >= 2


def lookup(transcript: str, applications: list[dict], *, cancelled=lambda: False,
           runner=_answer, evidence_search=_search_evidence, deadline=None) -> dict:
    deadline = deadline or Deadline("web_name_evidence")
    if not applications:
        return {"status": "not_found"}
    names = sorted({app["display_name"] for app in applications})[:16]
    requested = requested_application_name(transcript)
    if (not 1 <= len(requested) <= 80 or re.search(r"[\r\n/\\:<>]", requested)
            or cancelled()):
        return {"status": "cancelled" if cancelled() else "invalid_name"}
    prompt = (
        "Propose the canonical local application name for this unfamiliar spoken name. "
        "You have no tools. The program separately controls all web evidence searches. "
        "Choose exactly one canonical_name from the provided local names; never invent "
        "a path, command, or app. If no unique well-supported match exists, use null. "
        "Return only one JSON object with canonical_name and spoken_name, no Markdown. "
        "spoken_name must be the exact span of the user's text naming the application. "
        f"Spoken application name: {json.dumps(requested, ensure_ascii=False)}. "
        f"Local application names: {json.dumps(names, ensure_ascii=False)}."
    )
    answer = runner(prompt, cancelled=cancelled, timeout=deadline.remaining(SEARCH_TIMEOUT_SECONDS))
    deadline.remaining()
    proposed = parse_answer(answer, requested, applications)
    if proposed["status"] != "resolved" or cancelled():
        return {"status": "cancelled" if cancelled() else proposed["status"]}
    spoken = proposed["matched_aliases"][0].strip()
    if (not re.fullmatch(r"[\w\s.+&()（）-]{1,80}", spoken)
            or re.search(r"\d{7,}", spoken)):
        return {"status": "invalid_name"}
    if not evidence_search(spoken, proposed["display_name"], timeout=deadline.remaining(25)):
        return {"status": "not_found"}
    deadline.remaining()
    if cancelled():
        return {"status": "cancelled"}
    return {**proposed, "executes": False, "evidence_source": "program_name_search"}
