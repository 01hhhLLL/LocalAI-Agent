"""Run one Hermes UI-test step with a hard timeout and a scoped process cleanup."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "runtime" / "hermes-home" / "bin" / "hermes.cmd"
TEST_ROOT = ROOT / "runtime" / "ui-three-runs"


def hermes_python_command() -> list[str]:
    """Use Hermes' installed environment and bootstrap without cmd.exe."""
    launcher = HERMES.read_text(encoding="utf-8")
    match = re.search(r'^"([^"]+python\.exe)" -I -c "([^"]+)" %\*\s*$',
                      launcher, re.MULTILINE)
    if match is None:
        raise RuntimeError("Hermes launcher format changed")
    runtime = (ROOT / "runtime" / "hermes-home").resolve()
    launcher_python = Path(match.group(1)).resolve()
    if not launcher_python.is_file() or not launcher_python.is_relative_to(runtime):
        raise RuntimeError("Hermes launcher interpreter is outside its project runtime")
    candidates = list(runtime.glob("installs/*/environments/*/venv/Scripts/python.exe"))
    if len(candidates) != 1:
        raise RuntimeError("expected exactly one installed Hermes environment")
    interpreter = candidates[0].resolve()
    if not interpreter.is_relative_to(runtime):
        raise RuntimeError("Hermes environment is outside its project runtime")
    return [str(interpreter), "-I", "-c", match.group(2)]


def run_step(prompt: str, name: str, output: Path, timeout: int) -> dict:
    output = output.resolve()
    if not output.is_relative_to(TEST_ROOT.resolve()) or not name.isascii() or not name.replace("-", "").isalnum():
        raise ValueError("invalid test output or step name")
    if not HERMES.is_file():
        raise FileNotFoundError("Hermes launcher is missing")
    output.mkdir(parents=True, exist_ok=True)
    log = output / f"{name}.log"
    result = output / f"{name}.json"
    prompt_file = output / f"{name}.prompt.txt"
    if log.exists() or result.exists() or prompt_file.exists():
        raise FileExistsError("step output already exists")
    if not prompt.strip() or len(prompt) > 20000:
        raise ValueError("prompt must be nonempty and at most 20000 characters")
    only_tool = os.environ.get("LOCALAI_MCP_ONLY_TOOL", "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", only_tool):
        raise ValueError("select exactly one MCP tool before starting Hermes")
    allowed_app = os.environ.get("LOCALAI_MCP_ALLOWED_APP", "").strip()
    if only_tool in {"click_visible_target", "click_visible_target_pyautogui"} and not re.fullmatch(
            r"[a-z][a-z0-9_]*", allowed_app):
        raise ValueError("select one approved target application before a click step")
    # Hermes reads UTF-8 itself; the Windows batch wrapper must never interpret Chinese.
    with prompt_file.open("x", encoding="utf-8") as handle:
        handle.write(prompt)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(ROOT / "runtime" / "hermes-compat-home")
    env["PYTHONIOENCODING"] = "utf-8"
    env["LOCALAI_MCP_ONLY_TOOL"] = only_tool
    if allowed_app:
        env["LOCALAI_MCP_ALLOWED_APP"] = allowed_app
    args = [*hermes_python_command(), "chat", "-Q", "-t", "localai_vision", "-m",
            "local-qwen3-8b-nf4", "--provider", "lmstudio", "--max-turns", "2",
            "--run-budget", "100", "--query-file", str(prompt_file)]
    start = time.monotonic()
    with log.open("x", encoding="utf-8") as sink:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=sink, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP |
                                   subprocess.CREATE_NO_WINDOW)
        status = "finished"
        while process.poll() is None:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                status = "step_timeout"
                break
            try:
                gpu = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5, check=True)
                free = int(gpu.stdout.strip().splitlines()[0])
                if free <= 200:
                    status = "low_vram"
                    break
            except (OSError, ValueError, subprocess.SubprocessError, IndexError):
                pass
            time.sleep(2)
        if status != "finished":
            # The process tree belongs solely to this bounded Hermes invocation.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
        try:
            exit_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exit_code = None
    payload = {"status": status if status != "finished" else
               ("completed" if exit_code == 0 else "command_failed"),
               "elapsed_seconds": round(time.monotonic() - start, 2),
               "exit_code": exit_code, "step": name, "allowed_tool": only_tool,
               "log": str(log)}
    with result.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    prompt_source = parser.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--prompt")
    prompt_source.add_argument("--prompt-file", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=165)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 180:
        parser.error("timeout must be 1 to 180 seconds")
    prompt = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else args.prompt
    report = run_step(prompt, args.name, args.output, args.timeout)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
