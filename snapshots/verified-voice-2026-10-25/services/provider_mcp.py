"""Forward one installed provider to an MCP host without copying Codex plugins."""
from __future__ import annotations
import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from step_runtime import Deadline
from tool_integrations import RUNTIME, ensure_browser, provider_command


async def serve(provider):
    deadline = Deadline("provider_startup", 60)
    command = provider_command(provider)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / (provider + "-host-" + uuid.uuid4().hex + ".log")).open("x", encoding="utf-8") as log:
        params = StdioServerParameters(command=command[0], args=command[1:], cwd=str(ROOT))
        async with stdio_client(params, errlog=log) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as client:
                await asyncio.wait_for(client.initialize(), timeout=deadline.remaining())
                server = Server("localai-" + provider)

                @server.list_tools()
                async def list_tools():
                    return (await asyncio.wait_for(client.list_tools(), timeout=30)).tools

                @server.call_tool()
                async def call_tool(name, arguments):
                    step = Deadline("provider_" + name)
                    if provider == "browser":
                        await asyncio.to_thread(ensure_browser, step)
                    return await asyncio.wait_for(client.call_tool(name, arguments), timeout=step.remaining())

                async with stdio_server() as (read, write):
                    await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("browser", "scrapling", "rea"))
    asyncio.run(serve(parser.parse_args().provider))
