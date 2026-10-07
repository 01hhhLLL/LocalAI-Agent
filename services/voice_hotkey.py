"""Windows Ctrl+Alt+X voice capture, countdown, and local ASR UI."""

from __future__ import annotations

import ctypes
import audioop
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import wave
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import Button, Canvas, Label, Text, Tk, Toplevel

import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "runtime" / "voice-hotkey" / "status.json"
TEMP_AUDIO = ROOT / "cache" / "temp" / "voice-hotkey"
ASR_PYTHON = ROOT / "runtime" / "asr-compare-qwen" / "Scripts" / "python.exe"
ASR_SCRIPT = ROOT / "scripts" / "transcribe-hotkey-recording.py"
TASK_PYTHON = ROOT / "runtime" / "foundation" / "Scripts" / "python.exe"
COMMAND_SCRIPT = ROOT / "scripts" / "run-hotkey-command.py"
RESULT_PREFIX = "VOICE_ASR_RESULT="
PROGRESS_PREFIX = "VOICE_ASR_PROGRESS="
COMMAND_RESULT_PREFIX = "VOICE_COMMAND_RESULT="
COMMAND_PROGRESS_PREFIX = "VOICE_COMMAND_PROGRESS="
PROGRESS_TEXT = {
    "checking": ("正在检查录音", "声音已保存，正在核对识别环境。"),
    "loading": ("正在载入语音模型", "本地模型正在准备，马上开始处理声音。"),
    "recognizing": ("正在生成文字", "模型已经就绪，正在转写刚才的录音。"),
    "loading_text": ("正在准备本地文本模型", "语音已经转成文字，正在顺序加载文本模型。"),
    "executing": ("正在执行并核验", "本地模型和 Hermes 正在处理这条明确的启动指令。"),
}
SAMPLE_RATE = 16000
MAX_RECORDING_SECONDS = 180
METER_BARS = 48
METER_WIDTH = 460
METER_HEIGHT = 60
DARK = "#101114"
PANEL = "#1b1d22"
FOREGROUND = "#f2f3f5"
RED = "#c93636"
GREEN = "#4ade80"
MIN_FREE_BEFORE_LOAD_MIB = 8192
PAUSE_FREE_MIB = 200
HOTKEY_ID = 0x4C41
QUIT_ID = 0x4C42
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_QUIT = 0x0012
VK_X = 0x58
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gpu_free_mib() -> int:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=6, check=True,
        creationflags=CREATE_NO_WINDOW)
    return int(result.stdout.split()[0])


def parse_asr_output(output: bytes) -> dict:
    for line in reversed(output.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(RESULT_PREFIX):
            result = json.loads(line[len(RESULT_PREFIX):])
            if not isinstance(result, dict) or result.get("status") not in {
                    "ok", "failed", "paused"}:
                raise ValueError("ASR result has an invalid status")
            return result
    raise ValueError("ASR process returned no structured result")


def parse_asr_progress(line: bytes) -> str | None:
    if not line.startswith(PROGRESS_PREFIX.encode("ascii")):
        return None
    try:
        message = json.loads(line[len(PROGRESS_PREFIX):].decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    stage = message.get("stage") if isinstance(message, dict) else None
    return stage if isinstance(stage, str) and stage in PROGRESS_TEXT else None


def read_asr_messages(stream, events: queue.SimpleQueue) -> bytes:
    """Drain child output continuously so progress arrives before final text."""
    results = []
    for line in stream:
        stage = parse_asr_progress(line)
        if stage:
            events.put(stage)
        elif line.startswith(RESULT_PREFIX.encode("ascii")):
            results.append(line)
    return b"".join(results)


def is_launch_command(transcript: str) -> bool:
    """Route short action mentions to the shared task entry for intent review."""
    # ponytail: the short-utterance cap avoids loading the text model for a
    # long casual recording; a lighter intent model can replace this gate.
    return (isinstance(transcript, str) and len(transcript) <= 160 and
            any(verb in transcript for verb in ("打开", "启动", "运行")))


def read_command_messages(stream, events: queue.SimpleQueue) -> bytes:
    results = []
    for line in stream:
        if line.startswith(COMMAND_PROGRESS_PREFIX.encode("ascii")):
            try:
                value = json.loads(line[len(COMMAND_PROGRESS_PREFIX):].decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            stage = value.get("stage") if isinstance(value, dict) else None
            if isinstance(stage, str) and stage in PROGRESS_TEXT:
                events.put(stage)
        elif line.startswith(COMMAND_RESULT_PREFIX.encode("ascii")):
            results.append(line)
    return b"".join(results)


def parse_command_output(output: bytes) -> dict:
    for line in reversed(output.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(COMMAND_RESULT_PREFIX):
            result = json.loads(line[len(COMMAND_RESULT_PREFIX):])
            if isinstance(result, dict) and isinstance(result.get("status"), str):
                return result
    raise ValueError("Command runner returned no structured result")


def meter_fraction(rms: int) -> float:
    """Scale microphone RMS to a visible bar height without auto-gain noise."""
    if rms < 50:
        return 0.0
    return min(1.0, (rms / 12000) ** 0.55)


def write_pcm_wav(chunks: list[bytes], path: Path) -> Path:
    frames = b"".join(chunks)
    if len(frames) < SAMPLE_RATE * 2 // 4:
        raise ValueError("录音过短，请至少说半秒。")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as raw_output:
        with wave.open(raw_output, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            output.writeframes(frames)
    return path


def _write_status(state: str, *, pid: int | None = None, thread_id: int | None = None) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, "pid": pid or os.getpid(),
               "thread_id": thread_id,
               "start_nonce": os.environ.get("LOCALAI_VOICE_HOTKEY_NONCE"),
               "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    temporary = STATUS.with_name(f"status-{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATUS)


class VoiceHotkey:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.withdraw()
        self.dialog = Toplevel(self.root)
        self.dialog.title("本地语音输入")
        self.dialog.geometry("500x310")
        self.dialog.resizable(False, False)
        self.dialog.configure(bg=DARK)
        self.dialog.protocol("WM_DELETE_WINDOW", self._close_dialog)
        self.label = Label(self.dialog, text="", font=("Microsoft YaHei UI", 13),
                           wraplength=455, justify="left", bg=DARK, fg=FOREGROUND)
        self.label.pack(padx=16, pady=(18, 8), anchor="w")
        self.meter_label = Label(self.dialog, text="麦克风声音", bg=DARK,
                                 fg="#aeb4bf", font=("Microsoft YaHei UI", 10))
        self.meter_label.pack(padx=16, anchor="w")
        self.meter = Canvas(self.dialog, width=METER_WIDTH, height=METER_HEIGHT,
                            bg=PANEL, highlightthickness=0)
        self.meter.pack(padx=16, pady=(4, 8))
        self.meter_bars = []
        for index in range(METER_BARS):
            x = 14 + index * 9
            self.meter_bars.append(self.meter.create_rectangle(
                x, METER_HEIGHT // 2 - 1, x + 5, METER_HEIGHT // 2 + 1,
                fill=GREEN, outline=""))
        self.meter_history = [0.0] * METER_BARS
        self.latest_rms = 0
        self.meter_level = 0.0
        self.progress_events = queue.SimpleQueue()
        self.processing_stage = "checking"
        self.processing_frame = 0
        self.text = Text(self.dialog, height=4, width=54, wrap="word",
                         font=("Microsoft YaHei UI", 11), bg=PANEL, fg=FOREGROUND,
                         insertbackground=FOREGROUND, relief="flat", padx=8, pady=6)
        self.text.pack(padx=16, pady=5)
        self.text.configure(state="disabled")
        self.button = Button(self.dialog, text="关闭", width=18,
                             command=self._close_dialog, bg="#343943",
                             fg=FOREGROUND, activebackground="#4a505b",
                             activeforeground=FOREGROUND, relief="flat")
        self.button.pack(pady=8)
        self.dialog.withdraw()
        self.state = "idle"
        self.chunks: list[bytes] = []
        self.stream = None
        self.audio_path: Path | None = None
        self.asr_process: subprocess.Popen | None = None
        self.recording_started = 0.0
        self.capture_warning = ""
        self.countdown_nonce = 0
        self.alive = True
        self.hotkey_events = queue.SimpleQueue()
        self.hotkey_ready = threading.Event()
        self.hotkey_error = ""
        self.hotkey_thread_id: int | None = None
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                               wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                            wintypes.HWND, wintypes.UINT,
                                            wintypes.UINT]
        self.user32.GetMessageW.restype = ctypes.c_int
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD,
                                                   wintypes.UINT, wintypes.WPARAM,
                                                   wintypes.LPARAM]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL

    def _set_state(self, state: str) -> None:
        self.state = state
        _write_status(state, thread_id=self.hotkey_thread_id)

    def _show(self, label: str, button: str, action) -> None:
        self.label.configure(text=label)
        is_cancel = button == "取消"
        self.button.configure(text=button, command=action,
                              bg=RED if is_cancel else "#343943",
                              activebackground="#ed5555" if is_cancel else "#4a505b")
        if not self.dialog.winfo_viewable():
            self.dialog.deiconify()
            self.dialog.lift()

    def _reset_meter(self) -> None:
        self.latest_rms = 0
        self.meter_level = 0.0
        self.meter_history = [0.0] * METER_BARS
        self._draw_meter()

    def _draw_meter(self) -> None:
        center = METER_HEIGHT // 2
        for bar, level in zip(self.meter_bars, self.meter_history):
            half_height = 1 + round(level * (center - 4))
            x1, _, x2, _ = self.meter.coords(bar)
            self.meter.coords(bar, x1, center - half_height,
                              x2, center + half_height)
            self.meter.itemconfigure(bar, fill=GREEN)

    def _tick_meter(self) -> None:
        if self.state != "recording":
            return
        self.meter_level = max(meter_fraction(self.latest_rms),
                               self.meter_level * 0.7)
        self.meter_history.pop(0)
        self.meter_history.append(self.meter_level)
        self._draw_meter()
        self.root.after(80, self._tick_meter)

    def _tick_processing(self) -> None:
        if self.state not in {"transcribing", "acting"}:
            return
        stage_changed = False
        while not self.progress_events.empty():
            stage = self.progress_events.get_nowait()
            if stage in PROGRESS_TEXT and stage != self.processing_stage:
                self.processing_stage = stage
                stage_changed = True
        heading, detail = PROGRESS_TEXT[self.processing_stage]
        if stage_changed:
            self._set_text(detail)
        self.label.configure(text=f"{heading}{'·' * (self.processing_frame % 4)}\n请稍候，结果会出现在这里。")
        self.meter_label.configure(text="声音正在变成文字")
        colors = {"checking": "#60a5fa", "loading": "#c084fc",
                  "recognizing": "#4ade80", "loading_text": "#fbbf24",
                  "executing": "#4ade80"}
        color = colors[self.processing_stage]
        center = METER_HEIGHT // 2
        focus = (self.processing_frame % (METER_BARS + 16)) - 8
        for index, bar in enumerate(self.meter_bars):
            distance = abs(index - focus)
            shimmer = 0.5 + 0.5 * math.sin(index * 0.42 + self.processing_frame * 0.26)
            height = 2 + round((0.06 + max(0.0, 1 - distance / 9) * 0.75
                                + shimmer * 0.12) * (center - 4))
            x1, _, x2, _ = self.meter.coords(bar)
            self.meter.coords(bar, x1, center - height, x2, center + height)
            self.meter.itemconfigure(bar, fill=color if distance < 8 else "#375061")
        self.processing_frame += 1
        self.root.after(90, self._tick_processing)

    def _set_text(self, value: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.configure(state="disabled")

    def _close_dialog(self) -> None:
        if self.state == "countdown":
            self.countdown_nonce += 1
            self._set_state("idle")
        elif self.state == "recording":
            self._stop_recording()
            return
        self.dialog.withdraw()

    def _begin_countdown(self) -> None:
        try:
            sd.check_input_settings(channels=1, dtype="int16", samplerate=SAMPLE_RATE)
            free = gpu_free_mib()
            if free < MIN_FREE_BEFORE_LOAD_MIB:
                raise RuntimeError(f"语音模型需要至少 8192 MiB 空闲显存；现在为 {free} MiB。")
        except Exception as error:
            self._set_state("failed")
            self._set_text("")
            self._show(f"暂时无法开始录音：{error}", "关闭", self._close_dialog)
            return
        self.chunks.clear()
        self.capture_warning = ""
        self._reset_meter()
        self.meter_label.configure(text="麦克风声音")
        self._set_text("")
        self.countdown_nonce += 1
        nonce = self.countdown_nonce
        self._set_state("countdown")
        self._countdown(3, nonce)

    def _countdown(self, remaining: int, nonce: int) -> None:
        if nonce != self.countdown_nonce or self.state != "countdown":
            return
        if remaining == 0:
            self._start_recording()
            return
        self._show(f"{remaining} 秒后开始录音。\n再次按 Ctrl+Alt+X 可取消。",
                   "取消", self._close_dialog)
        self.root.after(1000, self._countdown, remaining - 1, nonce)

    def _audio_callback(self, indata, _frames, _time_info, status) -> None:
        if status:
            self.capture_warning = str(status)
        payload = bytes(indata)
        self.chunks.append(payload)
        self.latest_rms = audioop.rms(payload, 2)

    def _start_recording(self) -> None:
        try:
            self.stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=SAMPLE_RATE // 10, callback=self._audio_callback)
            self.stream.start()
        except Exception as error:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self._set_state("failed")
            self._show(f"麦克风未能打开：{error}", "关闭", self._close_dialog)
            return
        self.recording_started = time.monotonic()
        self._set_state("recording")
        self._tick_recording()
        self._tick_meter()

    def _tick_recording(self) -> None:
        if self.state != "recording":
            return
        elapsed = int(time.monotonic() - self.recording_started)
        self._show(f"正在录音：{elapsed} 秒\n再按 Ctrl+Alt+X，或点下方按钮结束并识别。",
                   "结束录音并识别", self._stop_recording)
        if elapsed >= MAX_RECORDING_SECONDS:
            self._stop_recording()
        else:
            self.root.after(500, self._tick_recording)

    def _stop_recording(self) -> None:
        if self.state != "recording":
            return
        try:
            self.stream.stop()
            self.stream.close()
            self.stream = None
            path = TEMP_AUDIO / f"voice-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.wav"
            self.audio_path = write_pcm_wav(self.chunks, path)
            self.chunks.clear()
        except Exception as error:
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
                finally:
                    self.stream = None
            self._set_state("failed")
            self._show(f"录音没有完成：{error}", "关闭", self._close_dialog)
            return
        self._set_state("transcribing")
        self.progress_events = queue.SimpleQueue()
        self.processing_stage = "checking"
        self.processing_frame = 0
        self._set_text(PROGRESS_TEXT["checking"][1])
        self._show("正在处理录音…", "隐藏窗口", self._close_dialog)
        self._tick_processing()
        threading.Thread(target=self._transcribe_worker, args=(self.audio_path,),
                         daemon=True).start()

    def _transcribe_worker(self, audio_path: Path) -> None:
        result = {"status": "failed", "reason": "asr_process_failed"}
        try:
            environment = os.environ.copy()
            environment.update({"CUDA_VISIBLE_DEVICES": "0", "HF_HUB_OFFLINE": "1",
                                "TRANSFORMERS_OFFLINE": "1",
                                "HF_HOME": str(ROOT / "cache" / "huggingface")})
            process = subprocess.Popen(
                [str(ASR_PYTHON), "-B", str(ASR_SCRIPT), str(audio_path)],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
            self.asr_process = process
            output = []
            reader = threading.Thread(
                target=lambda: output.append(read_asr_messages(
                    process.stdout, self.progress_events)), daemon=True)
            reader.start()
            while True:
                try:
                    process.wait(timeout=1)
                    reader.join(timeout=5)
                    if reader.is_alive():
                        raise RuntimeError("ASR output reader did not finish")
                    result = parse_asr_output(b"".join(output))
                    break
                except subprocess.TimeoutExpired:
                    try:
                        free = gpu_free_mib()
                    except Exception:
                        free = -1
                    if free < 0 or free <= PAUSE_FREE_MIB:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        reader.join(timeout=5)
                        result = {"status": "paused", "reason": "gpu_limit_or_status_unavailable",
                                  "gpu_free_mib": free}
                        break
        except Exception as error:
            result = {"status": "failed", "reason": type(error).__name__,
                      "detail": str(error)[:160]}
        finally:
            self.asr_process = None
        if self.alive:
            self.root.after(0, self._asr_finished, result, audio_path)

    def _asr_finished(self, result: dict, audio_path: Path) -> None:
        if result.get("status") == "ok":
            transcript = result["transcript"]
            try:
                audio_path.unlink()
                retention_note = ""
            except OSError:
                retention_note = f"\n录音仍保存在：{audio_path}"
            self._set_text(transcript)
            self._set_state("done")
            self.meter_label.configure(text="识别完成")
            warning = "\n录音时检测到丢帧，文字可能不完整。" if self.capture_warning else ""
            self._show("语音已转成文字。" + warning + retention_note,
                       "关闭", self._close_dialog)
            if is_launch_command(transcript):
                self._set_state("acting")
                self.progress_events = queue.SimpleQueue()
                self.processing_stage = "loading_text"
                self.processing_frame = 0
                self._show("正在处理启动指令…", "隐藏窗口", self._close_dialog)
                self._tick_processing()
                threading.Thread(target=self._command_worker, args=(transcript,),
                                 daemon=True).start()
        else:
            reason = result.get("detail") or result.get("reason") or "未知错误"
            self._set_state("failed")
            self.meter_label.configure(text="识别未完成")
            self._show(f"语音识别未完成：{reason}\n录音保存在：{audio_path}",
                       "关闭", self._close_dialog)

    def _command_worker(self, transcript: str) -> None:
        result = {"status": "failed", "error_code": "command_runner_failed"}
        try:
            process = subprocess.Popen(
                [str(TASK_PYTHON), "-B", str(COMMAND_SCRIPT)],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            process.stdin.write(transcript.encode("utf-8"))
            process.stdin.close()
            output = []
            reader = threading.Thread(
                target=lambda: output.append(read_command_messages(
                    process.stdout, self.progress_events)), daemon=True)
            reader.start()
            # Each launch, visual check, and click has its own bounded step.
            # The complete multi-step task can legitimately exceed five minutes.
            process.wait(timeout=1500)
            reader.join(timeout=5)
            if reader.is_alive():
                raise RuntimeError("Command output reader did not finish")
            result = parse_command_output(b"".join(output))
        except subprocess.TimeoutExpired:
            result = {"status": "failed", "error_code": "command_timeout"}
        except Exception as error:
            result = {"status": "failed", "error_code": type(error).__name__}
        if self.alive:
            self.root.after(0, self._command_finished, result, transcript)

    def _command_finished(self, result: dict, transcript: str) -> None:
        status = result.get("status")
        messages = {
            "succeeded": "应用已打开，窗口已核验。",
            "already_running": "应用已在运行，画面已核验。",
            "running": "相同指令正在处理中，没有重复启动。",
            "needs_confirmation": "找到可能的应用，但还需要确认，未执行。",
            "ambiguous": "识别出多个应用，未执行。",
            "rejected": "这条语音未构成可执行的启动命令。",
            "paused": "本次操作已暂停。",
            "cancelled": "本次操作已取消。",
        }
        self._set_state("done" if status in {"succeeded", "already_running"} else "failed")
        self.meter_label.configure(text="启动结果")
        self._set_text(transcript)
        failures = {
            "application_not_visible": "检测到应用进程，但屏幕上没确认到窗口，未继续点击。",
            "window_action_not_completed": "应用已打开，但界面点击没有完成。",
            "window_action_not_visually_verified": "已尝试界面点击，但画面未确认到结果。",
            "window_action_tool_failed": "应用已打开，但界面操作工具运行失败。",
            "visual_verification_unavailable": "画面核验暂不可用，未继续点击。",
            "vision_quota_or_rate_limit": "视觉接口额度或频率受限，已停止后续操作。",
            "local_text_model_not_ready": "本地文本模型未就绪，未继续操作。",
        }
        self._show(messages.get(status, failures.get(result.get("error_code"),
                                                 "任务未完成；请查看诊断记录。")),
                   "关闭", self._close_dialog)

    def _on_hotkey(self) -> None:
        if self.state in {"idle", "done", "failed"}:
            self._begin_countdown()
        elif self.state == "countdown":
            self._close_dialog()
        elif self.state == "recording":
            self._stop_recording()

    def _poll_hotkeys(self) -> None:
        if not self.alive:
            return
        while not self.hotkey_events.empty():
            hotkey_id = self.hotkey_events.get_nowait()
            if hotkey_id == HOTKEY_ID:
                self._on_hotkey()
            elif hotkey_id == QUIT_ID:
                self._quit()
                return
        self.root.after(50, self._poll_hotkeys)

    def _hotkey_loop(self) -> None:
        self.hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        flags = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        registered = bool(self.user32.RegisterHotKey(None, HOTKEY_ID, flags, VK_X))
        if not registered:
            self.hotkey_error = "Ctrl+Alt+X is already registered by another program"
            self.hotkey_ready.set()
            return
        quit_registered = bool(self.user32.RegisterHotKey(
            None, QUIT_ID, flags | MOD_SHIFT, VK_X))
        if not quit_registered:
            self.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.hotkey_error = "Ctrl+Alt+Shift+X is already registered by another program"
            self.hotkey_ready.set()
            return
        self.hotkey_ready.set()
        try:
            message = wintypes.MSG()
            while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    self.hotkey_events.put(int(message.wParam))
        finally:
            self.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.user32.UnregisterHotKey(None, QUIT_ID)

    def _quit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        if self.stream is not None:
            self.stream.abort()
            self.stream.close()
            self.stream = None
        if self.asr_process is not None and self.asr_process.poll() is None:
            self.asr_process.terminate()
        if self.hotkey_thread_id:
            self.user32.PostThreadMessageW(self.hotkey_thread_id, WM_QUIT, 0, 0)
        _write_status("stopped", thread_id=self.hotkey_thread_id)
        self.root.destroy()

    def run(self) -> int:
        thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        thread.start()
        if not self.hotkey_ready.wait(timeout=5) or self.hotkey_error:
            self.root.destroy()
            return 1
        self._set_state("idle")
        self._poll_hotkeys()
        try:
            self.root.mainloop()
        finally:
            self._quit()
        return 0


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Windows is required")
    raise SystemExit(VoiceHotkey().run())
