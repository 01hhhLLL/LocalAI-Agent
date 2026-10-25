"""Recording validation and bounded ASR recovery; no model imports or downloads."""
from __future__ import annotations

import audioop
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from step_runtime import Deadline, StepTimeout, failure_code

MIN_AUDIO_SECONDS = 0.5
MAX_AUDIO_SECONDS = 180
RECOVERABLE = {"empty_transcript", "transient_recognition_failure", "worker_interrupted"}


def failure_message(result):
    """Present only allowlisted diagnostics; retain the machine reason separately."""
    report = result.get("resource_report")
    if isinstance(report, dict):
        labels = (("ram_free_mib", "required_free_ram_mib", "内存"),
                  ("gpu_free_mib", "required_free_vram_mib", "显存"),
                  ("commit_free_mib", "required_free_commit_mib", "系统提交额度"))
        details = []
        for key, required, label in labels:
            available, need = report.get("resources", {}).get(key), report.get(required)
            if (isinstance(available, (int, float)) and isinstance(need, (int, float))
                    and available < need):
                details.append(f"可用{label} {available / 1024:.1f} GiB，本阶段预算需 {need / 1024:.1f} GiB（含余量）")
        if details:
            return "；".join(details) + "。请先关闭暂时不用的应用后再试；本次操作已停止。"
    return {
        "resource_unavailable": "可用资源不足或模型正被占用，请查看资源检查记录后再试。",
        "model_busy": "模型正被另一个任务占用，请稍后再试。",
        "dependency_missing": "语音识别运行环境缺少依赖，录音已保留。",
        "model_or_runtime_missing": "未找到本地语音模型或运行程序，录音已保留。",
        "out_of_memory": "加载或识别时内存/显存不足，录音已保留。",
        "deadline_exceeded": "语音识别超过本次总时限，已停止；录音已保留。",
        "empty_transcript": "没有识别出文字，请核对录音或重新录音。",
    }.get(result.get("reason"), result.get("detail") or result.get("reason") or "未知错误")


def inspect_wav(path: Path):
    """Validate data and format, never authenticate a speaker or judge meaning."""
    try:
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("recording exceeds the 100 MiB file limit")
        with path.open("rb") as source:
            header = source.read(12)
        if (len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE"
                or struct.unpack("<I", header[4:8])[0] + 8 != path.stat().st_size):
            raise ValueError("recording WAV is incomplete")
        with wave.open(str(path), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(),
                    source.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("recording must be mono 16 kHz PCM16 WAV")
            count = source.getnframes()
            if count > MAX_AUDIO_SECONDS * 16000:
                raise ValueError("recording exceeds 180 seconds")
            frames = source.readframes(count)
            if len(frames) != count * 2:
                raise ValueError("recording WAV data is truncated")
            duration = count / 16000
            if duration < MIN_AUDIO_SECONDS:
                raise ValueError("录音过短，请至少说半秒；完整录音已保留。")
    except (wave.Error, EOFError) as error:
        raise ValueError("recording WAV is damaged") from error
    rms = audioop.rms(frames, 2)
    # ponytail: an uncalibrated level is advisory only; device-specific
    # calibration may replace this hint, never the structural WAV checks.
    return {"duration_seconds": duration, "frames": count, "rms": rms,
            "warnings": ["low_recording_level"] if rms < 50 else []}


@dataclass(frozen=True)
class SecondaryASR:
    name: str
    recognize: object
    verified: bool = False

    def usable(self):
        return (self.verified is True and isinstance(self.name, str)
                and "moss" not in self.name.casefold() and callable(self.recognize))


def run_asr_flow(audio, attempt, *, deadline=None, secondary=None, cancelled=lambda: False):
    deadline = deadline or Deadline("automatic_asr")
    attempts = []
    secondary_state = "disabled_unselected"
    result = {"status": "failed", "reason": "invalid_recording"}
    try:
        deadline.remaining()
        quality = inspect_wav(audio)
    except (OSError, ValueError) as error:
        return {**result, "reason": failure_code(error), "recording_preserved": True,
                "secondary_asr": secondary_state, "attempts": attempts}
    routes = [("primary", attempt), ("primary_retry", attempt)]
    if secondary is not None and secondary.usable():
        secondary_state = "verified_available"
        routes.append((secondary.name, secondary.recognize))
    try:
        for route, recognize in routes:
            deadline.remaining()
            if cancelled():
                result = {"status": "paused", "reason": "cancelled"}
                break
            result = recognize(audio, deadline)
            deadline.remaining()
            if not isinstance(result, dict):
                result = {"status": "failed", "reason": "invalid_worker_result"}
            if result.get("status") == "ok" and not (
                    isinstance(result.get("transcript"), str) and result["transcript"].strip()):
                result = {"status": "failed", "reason": "empty_transcript"}
            attempts.append({"route": route, "status": result.get("status"),
                             "reason": result.get("reason")})
            if result.get("status") == "ok" or result.get("reason") not in RECOVERABLE:
                break
    except (StepTimeout, TimeoutError):
        result = {"status": "paused", "reason": "deadline_exceeded"}
    except Exception as error:
        result = {"status": "failed", "reason": failure_code(error)}
    return {**result, "quality": quality, "attempts": attempts,
            "secondary_asr": secondary_state, "recording_preserved": True,
            "requires_text_review": result.get("status") == "ok", **deadline.report()}
