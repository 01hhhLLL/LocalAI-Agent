"""Project-owned MCP clients for visible browsing, Scrapling and REA.

The browser is an explicit Chrome profile with a loopback debugging endpoint.
Closing an MCP connection disconnects it; the result window is kept visible.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from step_runtime import Deadline

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/tool-integrations.json"
RUNTIME = ROOT / "runtime/tool-integrations"


def _focus_owned_browser(owner):
    """Focus only a window of the Chrome process launched for this profile."""
    from ctypes import wintypes
    pid = owner.get("launch_pid")
    user32 = ctypes.windll.user32
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _parameter):
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        raise RuntimeError("The owned Chrome window is not visible")
    if len(found) != 1:
        raise RuntimeError("The owned Chrome window is ambiguous")
    from approved_window_process import focus_registered_window
    if not focus_registered_window(owner.get("executable"), found[0], expected_pid=pid):
        raise RuntimeError("The owned Chrome window could not be restored and activated")


def settings():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def enabled():
    return os.environ.get("LOCALAI_DISABLE_INTEGRATIONS") != "1" and settings()["enabled"] is True


def public_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("URL is invalid")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) URL without credentials")
    return value


def node_command():
    node = shutil.which("node") or "C:/Program Files/nodejs/node.exe"
    if not Path(node).is_file():
        raise FileNotFoundError("Node.js is missing")
    return node


def provider_command(provider):
    config = settings()
    item = config["providers"][provider]
    entry = (ROOT / item["entry"]).resolve(strict=True)
    if not entry.is_relative_to(RUNTIME):
        raise ValueError("Provider runtime escaped the project integration directory")
    if provider == "scrapling":
        return [str(entry)]
    if provider == "rea":
        return [node_command(), str(entry), "--mcp"]
    if provider == "browser":
        return [node_command(), str(entry), "--cdp-endpoint", config["browser_endpoint"],
                "--timeout-action", "15000", "--timeout-navigation", "45000",
                "--output-dir", str(RUNTIME / "browser-output")]
    raise ValueError("Unknown provider")


def ensure_browser(deadline):
    from application_registry import approved_applications
    config = settings()
    endpoint = config["browser_endpoint"]
    if endpoint != "http://127.0.0.1:8767":
        raise ValueError("The integration browser endpoint must be the project loopback endpoint")
    profile = RUNTIME / "chrome-profile"
    owner = RUNTIME / "chrome-owner.json"

    def connected():
        try:
            with urllib.request.urlopen(endpoint + "/json/version", timeout=1) as response:
                value = json.load(response)
            return isinstance(value.get("Browser"), str) and "Chrome" in value["Browser"]
        except (OSError, ValueError):
            return False

    if connected():
        receipt = json.loads(owner.read_text(encoding="utf-8")) if owner.is_file() else {}
        if receipt.get("profile") != str(profile):
            raise RuntimeError("Debugging port is already in use by an unowned browser")
        _focus_owned_browser(receipt)
        return
    applications = [app for app in approved_applications()
                    if Path(app["executable_path"]).name.casefold() == "chrome.exe"]
    if len(applications) != 1:
        raise ValueError("One approved Google Chrome executable is required")
    executable = Path(applications[0]["executable_path"]).resolve(strict=True)
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([str(executable), "--remote-debugging-port=8767",
        "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + str(profile),
        "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    temporary = RUNTIME / ("chrome-owner-" + uuid.uuid4().hex + ".json")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump({"profile": str(profile), "launch_pid": process.pid,
                   "executable": str(executable)}, stream)
    temporary.replace(owner)
    until = min(deadline.expires, time.monotonic() + 25)
    while time.monotonic() < until:
        if connected():
            _focus_owned_browser(json.loads(owner.read_text(encoding="utf-8")))
            return
        time.sleep(.15)
    raise TimeoutError("The owned Chrome window did not become ready")


@asynccontextmanager
async def provider_session(provider, deadline, folder):
    command = provider_command(provider)
    with (folder / (provider + "-stderr.log")).open("a", encoding="utf-8") as log:
        params = StdioServerParameters(command=command[0], args=command[1:],
            cwd=str(ROOT), env=deadline.environment())
        async with stdio_client(params, errlog=log) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as session:
                await asyncio.wait_for(session.initialize(), timeout=deadline.remaining(30))
                yield session


async def call(session, name, arguments, deadline, folder, events):
    started = time.monotonic()
    reply = await asyncio.wait_for(session.call_tool(name, arguments), timeout=deadline.remaining())
    payload = reply.model_dump(mode="json")
    path = folder / f"{len(events) + 1:02d}-{name}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    events.append({"operation": name, "elapsed_seconds": round(time.monotonic() - started, 3),
                   "is_error": bool(reply.isError), "evidence": str(path)})
    if name.startswith("browser_") and not reply.isError:
        await capture_fullscreen(name, deadline, folder, events)
    text = "\n".join(part.text for part in reply.content if getattr(part, "type", None) == "text")
    if reply.isError:
        raise RuntimeError("Provider rejected " + name + "; inspect its saved evidence")
    value = getattr(reply, "structuredContent", None)
    if value is None:
        try:
            value = json.loads(text)
        except ValueError:
            value = {"text": text}
    return value


async def capture_fullscreen(name, deadline, folder, events):
    captured = await asyncio.create_subprocess_exec("powershell.exe", "-NoProfile", "-NonInteractive",
        "-File", str(ROOT / "scripts/capture-screen.ps1"), "-MaxImageSide", "8192",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        output, _error = await asyncio.wait_for(captured.communicate(), timeout=deadline.remaining(15))
        if captured.returncode:
            raise RuntimeError("Full-screen capture failed")
        screen = json.loads(output.decode("utf-8-sig"))
        image = folder / f"{len(events):02d}-{name}.full.png"
        with image.open("xb") as stream:
            stream.write(base64.b64decode(screen.pop("image_base64")))
        events[-1]["fullscreen"] = str(image)
    except (OSError, RuntimeError, ValueError) as error:
        events[-1]["screenshot_error"] = type(error).__name__
    finally:
        if captured.returncode is None:
            captured.kill()
            await asyncio.wait_for(captured.communicate(), timeout=3)


def _snapshot_text(value):
    return value.get("text", "") if isinstance(value, dict) else str(value)


def search_field_reference(snapshot):
    # Snapshot metadata (active, expanded, box) can precede the ref token.
    matches = re.findall(r'^\s*-\s+(?:combobox|searchbox|textbox) "(?:Search|搜索|搜尋)[^"\r\n]*"'
                         r'[^\r\n]*?\[ref=([A-Za-z0-9_-]+)\]', snapshot, re.I | re.M)
    return matches[0] if len(matches) == 1 else None


def search_observation(snapshot, query):
    url_match = re.search(r"Page URL: (\S+)", snapshot)
    url = url_match[1] if url_match else ""
    parsed = urllib.parse.urlsplit(url)
    blocked = bool(re.search(r"/sorry/|unusual traffic|异常流量|不是机器人|captcha", snapshot, re.I))
    submitted = (parsed.scheme == "https" and parsed.hostname in {"google.com", "www.google.com"}
                 and parsed.path == "/search" and urllib.parse.parse_qs(parsed.query).get("q") == [query])
    has_results = bool(re.search(r'heading .+\[level=3\]', snapshot, re.I))
    return {"verified": submitted and not blocked and has_results, "query": query, "url": url,
            "submitted": submitted, "reason": "search_blocked" if blocked else
            "results_verified" if submitted and has_results else "results_not_verified"}


async def wait_search_result(session, query, deadline, folder, events):
    # ponytail: verify the result-page structure, not article correctness. A
    # separate content-reading step must assess source relevance and claims.
    until = min(deadline.expires, time.monotonic() + 12)
    while True:
        deadline.remaining()
        snapshot = _snapshot_text(await call(session, "browser_snapshot", {}, deadline, folder, events))
        deadline.remaining()
        observed = search_observation(snapshot, query)
        if observed["verified"] or observed["reason"] == "search_blocked":
            return observed
        if observed["submitted"] is False and "/search?" in observed["url"]:
            return {**observed, "reason": "search_query_mismatch"}
        if time.monotonic() >= until:
            return observed
        await asyncio.sleep(min(.4, max(0, until - time.monotonic()), deadline.remaining()))


async def _execute(tool, arguments, deadline, folder, events, checkpoint):
    if tool in {"browser_search", "browser_action"} or tool == "rea_inspect" and "url" in arguments:
        await asyncio.to_thread(ensure_browser, deadline)
    if tool == "browser_search":
        query = arguments.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            raise ValueError("Search text must contain 1 to 300 characters")
        async with provider_session("browser", deadline, folder) as session:
            await call(session, "browser_navigate", {"url": "https://www.google.com/"}, deadline, folder, events)
            snapshot = _snapshot_text(await call(session, "browser_snapshot", {}, deadline, folder, events))
            reference = search_field_reference(snapshot)
            if reference is None:
                return {"verified": False, "reason": "search_field_unavailable"}
            await call(session, "browser_type", {"target": reference, "text": query, "submit": True}, deadline, folder, events)
            observed = await wait_search_result(session, query, deadline, folder, events)
            await call(session, "browser_take_screenshot", {"filename": str(folder / "browser-result.png"),
                       "scale": "css", "fullPage": False}, deadline, folder, events)
            return observed
    if tool == "native_open_image":
        started = time.monotonic()
        worker = await asyncio.create_subprocess_exec(str(ROOT / "runtime/foundation/Scripts/python.exe"),
            "-B", str(ROOT / "services/native_catalog_image.py"), arguments["path"],
            env=deadline.environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            output, error = await asyncio.wait_for(worker.communicate(), timeout=deadline.remaining(35))
            if worker.returncode:
                with (folder / "native-viewer-error.log").open("xb") as stream:
                    stream.write(error)
                raise RuntimeError("The native image viewer worker failed; inspect its evidence")
            result = json.loads(output)
        finally:
            if worker.returncode is None:
                worker.kill()
                await asyncio.wait_for(worker.communicate(), timeout=3)
        events.append({"operation": "native_open_image", "elapsed_seconds": round(time.monotonic() - started, 3),
                       "verified": result.get("verified") is True})
        await capture_fullscreen("native_open_image", deadline, folder, events)
        return result
    if tool == "scrapling_fetch":
        url = public_url(arguments.get("url"))
        async with provider_session("scrapling", deadline, folder) as session:
            result = await call(session, "make_request", {"url": url, "method": "GET", "retries": 1,
                "timeout": min(30, deadline.remaining()), "extraction_type": "markdown"}, deadline, folder, events)
            return {"verified": 200 <= result.get("status", 0) < 300,
                    "url": result.get("url"), "content": result.get("content"), "status": result.get("status")}
    if tool == "rea_inspect":
        if "url" in arguments:
            url = public_url(arguments["url"])
            async with provider_session("browser", deadline, folder) as session:
                await call(session, "browser_navigate", {"url": url}, deadline, folder, events)
            async with provider_session("rea", deadline, folder) as session:
                endpoint = settings()["browser_endpoint"]
                targets = await call(session, "list_browser_targets", {"cdp_endpoint": endpoint}, deadline, folder, events)
                return {"verified": True, "browser_targets": targets, "scope": "target_discovery_only"}
        path = Path(arguments.get("path", "")).resolve(strict=True)
        if any(re.search(r"(?i)(?:^\.env|^credentials\.|private.*key|token|secret)", part) for part in path.parts):
            raise ValueError("Sensitive paths cannot be inspected")
        if not path.is_relative_to(ROOT) or path.suffix.casefold() not in {".js", ".mjs", ".cjs"}:
            raise ValueError("This route accepts an explicit public JavaScript file in the project")
        staged = folder / "source"
        staged.mkdir()
        with (staged / path.name).open("xb") as stream:
            stream.write(path.read_bytes())
        async with provider_session("rea", deadline, folder) as session:
            result = await call(session, "analyze_javascript_application", {"input_path": str(staged),
                "detail": "summary", "analysis_timeout_ms": int(deadline.remaining(120) * 1000)}, deadline, folder, events)
            return {"verified": bool(result.get("evidence_id")), "analysis": result,
                    "scope": "static_javascript", "source": str(path)}
    if tool == "browser_action":
        operations = {"snapshot": "browser_snapshot", "navigate": "browser_navigate", "type": "browser_type",
                      "click": "browser_click", "press": "browser_press_key"}
        operation = operations.get(arguments.get("action"))
        if not operation or not isinstance(arguments.get("arguments", {}), dict):
            raise ValueError("Unknown browser action")
        values = arguments.get("arguments", {})
        if operation == "browser_navigate":
            public_url(values.get("url"))
        async with provider_session("browser", deadline, folder) as session:
            result = await call(session, operation, values, deadline, folder, events)
            return {"verified": True, "result": result, "scope": "one_browser_action"}
    if tool == "toolbox_open":
        from tuba_tool_catalog import lookup, normalized
        from app_launch_broker import check
        from approved_window_process import focus_registered_window
        if "entry_id" in arguments:
            from application_lookup import get_target
            target = get_target(arguments["entry_id"])
            if (not target or target.get("target_kind") != "toolbox"
                    or normalized(target["display_name"]) != normalized(arguments.get("name", ""))):
                raise ValueError("Selected toolbox entry no longer matches the requested name")
            item = {**target, "path": target["entry_path"]}
            selection = {"status": "matched", "candidates": [item]}
        else:
            selection = lookup(arguments.get("name", ""))
            if selection["status"] != "matched":
                return {"verified": False, "reason": "tool_name_not_unique", "selection": selection}
            item = selection["candidates"][0]
        if item["kind"] == "content":
            if Path(item["path"]).suffix.casefold() not in {".jpg", ".jpeg", ".png"}:
                return {"verified": False, "reason": "content_type_not_yet_enabled", "selection": selection}
            return await _execute("native_open_image", {"path": item["path"]}, deadline, folder, events, checkpoint)
        path = Path(item["path"])
        if path.suffix.casefold() != ".exe":
            return {"verified": False, "reason": "launcher_type_not_yet_enabled", "selection": selection}
        before = await asyncio.to_thread(check, path, 1)
        if before.get("registered_window_verified") is True and not re.search(
                r"正在(?:打开|启动|加载)|\b(?:opening|starting|loading)\b", before.get("window_title") or "", re.I):
            focused = focus_registered_window(path, before.get("window_handle"))
            return {"verified": focused is True, "window": before, "path": str(path), "already_running": True}
        if before.get("status") not in {"process_running", "window_visible"}:
            subprocess.Popen([str(path)], cwd=path.parent, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        until = min(deadline.expires, time.monotonic() + 30)
        while time.monotonic() < until:
            observed = await asyncio.to_thread(check, path, 1)
            if observed.get("registered_window_verified") is True and not re.search(
                    r"正在(?:打开|启动|加载)|\b(?:opening|starting|loading)\b", observed.get("window_title") or "", re.I):
                focused = focus_registered_window(path, observed.get("window_handle"))
                return {"verified": focused is True, "window": observed, "path": str(path)}
            await asyncio.sleep(.4)
        return {"verified": False, "reason": "tool_window_not_verified", "path": str(path)}
    raise ValueError("Unknown integration tool")


def execute(tool, arguments, *, deadline=None, checkpoint=lambda _step: None):
    if not enabled():
        raise RuntimeError("Project integrations are disabled")
    deadline = deadline or Deadline.inherited("integration_" + tool)
    folder = RUNTIME / "runs" / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    events = []
    try:
        result = asyncio.run(asyncio.wait_for(_execute(tool, arguments, deadline, folder, events, checkpoint),
                                             timeout=deadline.remaining()))
        result.update(tool=tool, evidence_dir=str(folder), operations=events)
    except BaseException as error:
        with (folder / "failure.json").open("x", encoding="utf-8") as stream:
            json.dump({"tool": tool, "error": type(error).__name__, "operations": events,
                       **deadline.report()}, stream, ensure_ascii=False, indent=2)
        raise
    with (folder / "result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result
