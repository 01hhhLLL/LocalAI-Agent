"""Expose the project's existing ModelScope visual locator to Hermes over MCP."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import re
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

ROOT = Path(__file__).resolve().parents[1]
LOCATOR = ROOT / "scripts" / "vision-locate.py"
CLOSE_GEEK = ROOT / "scripts" / "close-tuba-geek.ps1"
GEEK_EXE = Path("D:/图吧工具箱202601/tools/其他工具/Geek Uninstaller/Geek Uninstaller.exe")
LAUNCH_BROKER = "http://127.0.0.1:8766/launch"
ONLY_TOOL = os.environ.get("LOCALAI_MCP_ONLY_TOOL", "").strip()
ALLOWED_APP_ID = os.environ.get("LOCALAI_MCP_ALLOWED_APP", "").strip()
CLICK_TOOLS = {"click_visible_target", "click_visible_target_pyautogui"}
sys.path.insert(0, str(ROOT / "services"))
from application_registry import (  # noqa: E402
    approved_applications,
    get_application,
    get_application_by_tool,
    initialize,
)

server = Server("localai-vision-desktop")
_completed_clicks: dict[tuple[str, str, str, str | None, int], dict] = {}
_completed_closes: dict[str, dict] = {}
_click_lock = asyncio.Lock()


def _enabled(name: str) -> bool:
    # One action is selected before Hermes starts. Missing or malformed scope
    # leaves every tool unavailable instead of exposing the entire catalog.
    if not ONLY_TOOL or name != ONLY_TOOL:
        return False
    return (name not in CLICK_TOOLS or
            bool(re.fullmatch(r"[a-z][a-z0-9_]*", ALLOWED_APP_ID)))


async def _launch_app(app: str) -> dict:
    def request_launch() -> dict:
        request = urllib.request.Request(
            LAUNCH_BROKER, data=json.dumps({"app": app}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)

    result = await asyncio.to_thread(request_launch)
    if result.get("status") not in {"window_visible", "process_running", "not_running"}:
        raise RuntimeError("launch broker returned an invalid status")
    return result


def _close_geek_elevated() -> dict:
    """Close only Geek's verified child window in a one-shot elevated helper."""
    result_file = ROOT / "runtime" / "elevated-close-results" / f"{uuid.uuid4().hex}.json"
    script = (
        "$ErrorActionPreference='Stop'; "
        "try { & '" + str(CLOSE_GEEK) + "' -Executable '" + str(GEEK_EXE) +
        "' -ResultFile '" + str(result_file) + "' } "
        "catch { if (-not (Test-Path -LiteralPath '" + str(result_file) + "')) { "
        "@{status='error';reason=$_.Exception.Message} | ConvertTo-Json -Compress | "
        "Set-Content -LiteralPath '" + str(result_file) + "' -Encoding UTF8 } }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    execute = ctypes.WinDLL("shell32.dll", use_last_error=True).ShellExecuteW
    execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    execute.restype = ctypes.c_void_p
    code = execute(None, "runas", "powershell.exe",
                   f"-NoProfile -NonInteractive -EncodedCommand {encoded}", str(ROOT), 0)
    if not code or code <= 32:
        raise RuntimeError("Windows did not authorize Geek's scoped close helper")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not result_file.is_file():
        time.sleep(0.15)
    if not result_file.is_file():
        raise RuntimeError("Geek close helper did not report a result")
    result = json.loads(result_file.read_text(encoding="utf-8-sig"))
    if result.get("status") != "closed":
        raise RuntimeError("Geek was not verified closed: " + str(result.get("status")))
    return result


@server.list_tools()
async def list_tools() -> list[Tool]:
    initialize()
    locate_schema = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "minLength": 1, "maxLength": 500},
            "ocr_text": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        "required": ["target"], "additionalProperties": False,
    }
    click_schema = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "minLength": 1, "maxLength": 500},
            "ocr_text": {"type": "string", "minLength": 1, "maxLength": 80},
            "app_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "click_count": {"type": "integer", "enum": [1, 2], "default": 1},
        },
        "required": ["target", "app_id"], "additionalProperties": False,
    }
    tools = [
        Tool(name="close_tuba_geek",
             description="Normally close only the verified Geek Uninstaller window from TuBa Toolbox, then wait for its process to exit. Does not uninstall anything.",
             inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        Tool(name="locate_visible_target",
             description="Find exact visible text with local CPU OCR first; use the configured ModelScope vision API when OCR cannot identify it. Returns coordinates without clicking. Pass ocr_text when known.",
             inputSchema=locate_schema),
        Tool(name="click_visible_target",
             description="First locate exact visible text with project-local CPU OCR; if absent or ambiguous, use the configured ModelScope vision API. Then request one scoped elevated click inside the approved app. Pass ocr_text when the exact visible label is known.",
             inputSchema=click_schema),
        Tool(name="click_visible_target_pyautogui",
             description="Try exact visible text with local CPU OCR first, then the configured vision API if needed; use one scoped elevated PyAutoGUI click inside the approved app. Pass ocr_text when known.",
             inputSchema=click_schema),
    ]
    no_arguments = {"type": "object", "properties": {}, "additionalProperties": False}
    for application in approved_applications():
        tool_name = application["tool_name"]
        if not tool_name.startswith("launch_"):
            continue
        tools.append(Tool(
            name=tool_name,
            description=(f"Open the approved registered application {application['display_name']} "
                         "and wait for its process and window. Returns observed startup status."),
            inputSchema=no_arguments,
        ))
    return [tool for tool in tools if _enabled(tool.name)]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if not _enabled(name):
        raise ValueError("tool is not enabled for this run")
    application = get_application_by_tool(name)
    if application is not None:
        if arguments:
            raise ValueError(f"{name} takes no arguments")
        result = await _launch_app(application["app_id"])
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    if name == "close_tuba_geek":
        if arguments:
            raise ValueError("close_tuba_geek takes no arguments")
        async with _click_lock:
            result = _completed_closes.get(name)
            if result is None:
                result = await asyncio.to_thread(_close_geek_elevated)
                _completed_closes[name] = result
            else:
                result = {**result, "reused": True}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    if name not in {"locate_visible_target", *CLICK_TOOLS}:
        raise ValueError("unknown tool")
    is_click = name in CLICK_TOOLS
    required = {"target", "app_id"} if is_click else {"target"}
    allowed = required | ({"click_count", "ocr_text"} if is_click else {"ocr_text"})
    if (not isinstance(arguments, dict) or not required <= set(arguments)
            or not set(arguments) <= allowed):
        raise ValueError(f"expected {', '.join(sorted(required))} and optional click_count")
    target = arguments["target"]
    if not isinstance(target, str) or not 1 <= len(target.strip()) <= 500:
        raise ValueError("target must be 1 to 500 characters")
    command = [sys.executable, "-B", str(LOCATOR), target.strip()]
    ocr_text = arguments.get("ocr_text")
    if ocr_text is not None:
        if not isinstance(ocr_text, str) or not 1 <= len(ocr_text.strip()) <= 80:
            raise ValueError("ocr_text must be 1 to 80 characters")
        command.extend(("--ocr-text", ocr_text.strip()))
    if is_click:
        app_id = arguments["app_id"]
        if not isinstance(app_id, str) or not 1 <= len(app_id) <= 80:
            raise ValueError("app_id must be a registered application ID")
        if get_application(app_id) is None:
            raise ValueError("app_id is not approved in the registry")
        if app_id != ALLOWED_APP_ID:
            raise ValueError("app_id is outside this Hermes invocation's scope")
        command.extend(("--click", "--elevated-app", app_id))
        click_count = arguments.get("click_count", 1)
        if type(click_count) is not int or click_count not in (1, 2):
            raise ValueError("click_count must be 1 or 2")
        command.extend(("--click-count", str(click_count)))
        if name == "click_visible_target_pyautogui":
            command.extend(("--pointer-backend", "pyautogui"))
    async def run_locator() -> dict:
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(ROOT), env=child_env,
            # The MCP transport owns stdin; passing it to the child can stall Python
            # startup before the locator reaches screenshot capture.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), timeout=160)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("vision locator timed out") from None
        if process.returncode:
            detail = error.decode("utf-8", errors="replace").strip()[-500:]
            raise RuntimeError(f"vision locator failed: {detail or 'unknown error'}")
        return json.loads(output.decode("utf-8-sig"))

    if is_click:
        # ponytail: one identical click per short Hermes session prevents a model
        # retry from double-opening a tool; a future long-lived agent needs task IDs.
        click_key = (name, app_id, target.strip(), ocr_text.strip() if ocr_text else None,
                     click_count)
        async with _click_lock:
            if _completed_clicks and click_key not in _completed_clicks:
                raise RuntimeError("a click already completed in this Hermes invocation")
            previous = _completed_clicks.get(click_key)
            if previous is not None:
                result = {**previous, "action": "previous click result reused; no new click"}
            else:
                result = await run_locator()
                if result.get("action", "").startswith("click sent"):
                    _completed_clicks[click_key] = result
    else:
        result = await run_locator()
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def main() -> None:
    initialize()
    async with stdio_server() as (incoming, outgoing):
        await server.run(incoming, outgoing, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
