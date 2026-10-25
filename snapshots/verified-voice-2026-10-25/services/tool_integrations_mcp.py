"""Shared integration bridge for the local program and Codex; no Codex-private API."""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from tool_integrations import execute
from tuba_tool_catalog import lookup

server = Server("localai-project-integrations")
SCHEMAS = {
    "browser_search": ("Search in a visible project-owned Chrome window; verify the results page.",
        {"query": {"type": "string", "minLength": 1, "maxLength": 300}}, ["query"]),
    "scrapling_fetch": ("Read a web page with the installed Scrapling MCP service; GET only.",
        {"url": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["url"]),
    "rea_inspect": ("Use REA for static analysis of an explicit project JavaScript file, or discover a selected website target. Returns evidence and scope; target discovery does not prove full analysis.",
        {"path": {"type": "string"}, "url": {"type": "string"}}, []),
    "browser_action": ("Perform one visible-browser action with Playwright MCP. Obtain current element targets from a snapshot before typing or clicking.",
        {"action": {"type": "string", "enum": ["navigate", "snapshot", "type", "click", "press"]},
         "arguments": {"type": "object"}}, ["action"]),
    "toolbox_lookup": ("Find installed TuBa tools or content by name. File paths and verified UI click paths are distinct. Multiple matches require selection.",
        {"name": {"type": "string", "minLength": 1, "maxLength": 100}}, ["name"]),
    "toolbox_open": ("Open exactly one named tool/content from the installed TuBa catalog, then verify the actual result. Multiple matches stop for selection; does not operate the opened tool.",
        {"name": {"type": "string", "minLength": 1, "maxLength": 100},
         "entry_id": {"type": "string", "pattern": "^tuba-entry-[a-f0-9]{24}$"}}, ["name"]),
}


def tools():
    return [Tool(name=name, description=description,
                 inputSchema={"type": "object", "properties": properties,
                              "required": required, "additionalProperties": False})
            for name, (description, properties, required) in SCHEMAS.items()]


@server.list_tools()
async def list_tools():
    return tools()


async def dispatch(name, arguments):
    if name not in SCHEMAS:
        raise ValueError("Unknown integration tool")
    properties, required = SCHEMAS[name][1:]
    if not isinstance(arguments, dict) or not set(required) <= set(arguments) or not set(arguments) <= set(properties):
        raise ValueError("Invalid integration arguments")
    if name == "rea_inspect" and ("path" in arguments) == ("url" in arguments):
        raise ValueError("Select one explicit source path or URL")
    result = await asyncio.to_thread(lookup, arguments["name"]) if name == "toolbox_lookup" else await asyncio.to_thread(execute, name, arguments)
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


@server.call_tool()
async def call_tool(name, arguments):
    return await dispatch(name, arguments)


async def main():
    async with stdio_server() as (incoming, outgoing):
        await server.run(incoming, outgoing, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
