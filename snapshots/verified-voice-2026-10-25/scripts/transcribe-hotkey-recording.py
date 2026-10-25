"""Transcribe one local WAV with the already installed Qwen3-ASR-1.7B runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from voice_process_lifetime import follow_parent
from asr_flow import inspect_wav, failure_message
from step_runtime import Deadline, failure_code
from model_resources import (ModelLease, ResourceError, require_admission, require_headroom)
MODEL = ROOT / "models" / "asr-comparison" / "qwen3-asr-1.7b" / "source"
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
    with ModelLease("qwen3-asr-1.7b") as lease:
        require_admission("qwen3-asr-1.7b")
        return _recognize_owned(audio, lease)


def _recognize_owned(audio: Path, lease) -> dict:
    deadline = Deadline.inherited("automatic_asr")
    deadline.remaining()
    resolved = audio.resolve(strict=True)
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        raise ValueError("WAV must be inside the LocalAI-Agent project") from None
    if resolved.suffix.lower() != ".wav" or not 44 <= resolved.stat().st_size <= 100 * 1024 * 1024:
        raise ValueError("Expected a nonempty WAV no larger than 100 MiB")
    inspect_wav(resolved)
    if not (MODEL / "config.json").is_file():
        raise FileNotFoundError("Local Qwen3-ASR-1.7B model is missing")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(ROOT / "cache" / "huggingface")
    runner = _runner()
    model = None
    try:
        _emit_progress("loading")
        started = time.perf_counter()
        model = runner.load_candidate("qwen3-asr-1.7b", MODEL)
        deadline.remaining()
        load_seconds = round(time.perf_counter() - started, 2)
        resident_resources = require_headroom("qwen3-asr-1.7b")
        resident_memory = runner.psutil.Process().memory_info()
        load_peak_rss = runner.peak_rss_mib()
        lease.loaded()
        _emit_progress("recognizing")
        started = time.perf_counter()
        transcript = runner.transcribe("qwen3-asr-1.7b", model, resolved).strip()
        deadline.remaining()
        recognition_seconds = round(time.perf_counter() - started, 2)
        inference_resources = require_headroom("qwen3-asr-1.7b")
        if not transcript:
            return {"status": "failed", "reason": "empty_transcript",
                    "load_seconds": load_seconds,
                    "recognition_seconds": recognition_seconds}
        return {"status": "ok", "transcript": transcript,
                "model": "Qwen/Qwen3-ASR-1.7B", "load_seconds": load_seconds,
                "recognition_seconds": recognition_seconds,
                "resource_checks_enabled": False,
                "gpu_free_before_mib": None,
                "gpu_free_after_load_mib": None,
                "resource_measurements": {
                    "load_peak_rss_mib": load_peak_rss,
                    "resident_rss_mib": resident_memory.rss / 1024**2,
                    "peak_rss_mib": runner.peak_rss_mib(),
                    "resident_resources": resident_resources,
                    "inference_resources": inference_resources,
                    "cuda_peak_allocated_mib": runner.torch.cuda.max_memory_allocated() / 1024**2,
                    "cuda_peak_reserved_mib": runner.torch.cuda.max_memory_reserved() / 1024**2}}
    finally:
        import gc
        del model
        gc.collect()
        try:
            runner.torch.cuda.empty_cache()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    args = parser.parse_args()
    try:
        parent_pid = os.environ.get("LOCALAI_VOICE_PARENT_PID")
        if parent_pid is not None:
            follow_parent(int(parent_pid))
        _emit_progress("checking")
        result = recognize(args.audio)
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        result = {"status": "failed", "reason": failure_code(error),
                  "error_type": type(error).__name__}
        if isinstance(error, ResourceError):
            if error.report:
                result["resource_report"] = error.report
            elif str(error) == "owned_operation_already_running":
                result["reason"] = "model_busy"
        result["detail"] = failure_message(result)
    # ASCII JSON survives Windows PowerShell's native-output code page; the UI
    # decodes the escaped Chinese text back to Unicode.
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
