"""Transcribe one local WAV with the already installed Qwen3-ASR-1.7B runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "asr-comparison" / "qwen3-asr-1.7b" / "source"
MIN_FREE_BEFORE_LOAD_MIB = 8192
PAUSE_FREE_MIB = 200
RESULT_PREFIX = "VOICE_ASR_RESULT="
PROGRESS_PREFIX = "VOICE_ASR_PROGRESS="


def _emit_progress(stage: str) -> None:
    print(PROGRESS_PREFIX + json.dumps({"stage": stage}, ensure_ascii=True), flush=True)


def _runner():
    source = ROOT / "scripts" / "run-asr-comparison-model.py"
    spec = importlib.util.spec_from_file_location("localai_asr_runner", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recognize(audio: Path) -> dict:
    resolved = audio.resolve(strict=True)
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        raise ValueError("WAV must be inside the LocalAI-Agent project") from None
    if resolved.suffix.lower() != ".wav" or not 44 <= resolved.stat().st_size <= 100 * 1024 * 1024:
        raise ValueError("Expected a nonempty WAV no larger than 100 MiB")
    if not (MODEL / "config.json").is_file():
        raise FileNotFoundError("Local Qwen3-ASR-1.7B model is missing")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(ROOT / "cache" / "huggingface")
    runner = _runner()
    free_before = runner.gpu_memory()[1]
    if free_before < MIN_FREE_BEFORE_LOAD_MIB:
        return {"status": "paused", "reason": "gpu_free_below_8192_mib",
                "gpu_free_mib": free_before}

    model = None
    try:
        _emit_progress("loading")
        started = time.perf_counter()
        model = runner.load_candidate("qwen3-asr-1.7b", MODEL)
        load_seconds = round(time.perf_counter() - started, 2)
        free_after_load = runner.gpu_memory()[1]
        if free_after_load <= PAUSE_FREE_MIB:
            return {"status": "paused", "reason": "gpu_free_at_200_mib",
                    "gpu_free_mib": free_after_load}
        _emit_progress("recognizing")
        started = time.perf_counter()
        transcript = runner.transcribe("qwen3-asr-1.7b", model, resolved).strip()
        recognition_seconds = round(time.perf_counter() - started, 2)
        if not transcript:
            return {"status": "failed", "reason": "empty_transcript",
                    "load_seconds": load_seconds,
                    "recognition_seconds": recognition_seconds}
        return {"status": "ok", "transcript": transcript,
                "model": "Qwen/Qwen3-ASR-1.7B", "load_seconds": load_seconds,
                "recognition_seconds": recognition_seconds,
                "gpu_free_before_mib": free_before,
                "gpu_free_after_load_mib": free_after_load}
    finally:
        del model
        try:
            runner.torch.cuda.empty_cache()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    args = parser.parse_args()
    try:
        _emit_progress("checking")
        result = recognize(args.audio)
    except (OSError, RuntimeError, ValueError) as error:
        result = {"status": "failed", "reason": type(error).__name__,
                  "detail": str(error)[:240]}
    # ASCII JSON survives Windows PowerShell's native-output code page; the UI
    # decodes the escaped Chinese text back to Unicode.
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
