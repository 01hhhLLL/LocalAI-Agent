"""Windows Ctrl+Alt+X voice capture, countdown, and local ASR UI."""

from __future__ import annotations

import ctypes
import audioop
import io
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
import winsound
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, Button, Canvas, Checkbutton, Frame, Label, Text, Tk, Toplevel, TclError

import sounddevice as sd
from voice_candidates import CandidatePanel
from voice_appearance import VoiceAppearance
from asr_flow import inspect_wav, run_asr_flow
from step_runtime import Deadline, failure_code, stop_owned, hard_bounded_step
from candidate_experiment import CandidatePreview
from integration_intents import explicit_tool_request

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "runtime" / "voice-hotkey" / "status.json"
TEMP_AUDIO = ROOT / "cache" / "temp" / "voice-hotkey"
ASR_PYTHON = ROOT / "runtime" / "asr-compare-qwen" / "Scripts" / "python.exe"
ASR_SCRIPT = ROOT / "scripts" / "transcribe-hotkey-recording.py"
# Only replace this slot with a separately verified SecondaryASR adapter.
# No experimental ASR is activated from disk discovery or configuration alone.
SECONDARY_ASR = None
TASK_PYTHON = ROOT / "runtime" / "foundation" / "Scripts" / "python.exe"
COMMAND_SCRIPT = ROOT / "scripts" / "run-hotkey-command.py"
RESULT_PREFIX = "VOICE_ASR_RESULT="
PROGRESS_PREFIX = "VOICE_ASR_PROGRESS="
COMMAND_RESULT_PREFIX = "VOICE_COMMAND_RESULT="
COMMAND_PROGRESS_PREFIX = "VOICE_COMMAND_PROGRESS="
COMMAND_CHECKPOINT_PREFIX = "VOICE_COMMAND_CHECKPOINT="
PROGRESS_TEXT = {
    "checking": ("正在准备识别", "声音已保存，正在准备语音识别。"),
    "loading": ("正在载入语音模型", "本地模型正在准备，马上开始处理声音。"),
    "recognizing": ("正在生成文字", "模型已经就绪，正在转写刚才的录音。"),
    "loading_text": ("正在准备本地文本模型", "语音已经转成文字，正在顺序加载文本模型。"),
    "executing": ("正在执行并核验", "正在打开已确认的应用，并核对实际结果。"),
}
SAMPLE_RATE = 16000
MAX_RECORDING_SECONDS = 180
REVIEW_SECONDS = 5
METER_BARS = 48
METER_WIDTH = 460
METER_HEIGHT = 160
DARK = "#13121c"
PANEL = "#211f30"
FOREGROUND = "#f2effb"
RED = "#c93636"
GREEN = "#4ade80"
HOTKEY_ID = 0x4C41
QUIT_ID = 0x4C42
SHOW_ID = 0x4C43
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_QUIT = 0x0012
VK_X = 0x58
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def execution_target_text(target, origin=None):
    lines = ["执行目标：" + target["display_name"]]
    if target.get("product_version"):
        lines.append("版本：" + str(target["product_version"]))
    if target.get("description"):
        lines.append("程序说明：" + str(target["description"]))
    parent = " → ".join(str(target[key]) for key in ("parent_name", "category", "folder") if target.get(key))
    if parent:
        lines.append("所属：" + parent)
    path = target.get("entry_path") or target.get("executable_path")
    if path:
        lines.append("打开路径：" + str(path))
    labels = {"candidate_countdown": "十秒倒计时自动选择", "user_selected_candidate": "人工选择",
              "user_confirmed": "人工选择", "user_entered_name": "手动输入匹配",
              "unified_exact_name": "精确匹配", "registry_exact_name": "已登记名称匹配"}
    if origin in labels:
        lines.append("选择方式：" + labels[origin])
    return "\n".join(lines)


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


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


def read_asr_messages(stream, events: queue.SimpleQueue, on_stage=lambda _stage: None) -> bytes:
    """Drain child output continuously so progress arrives before final text."""
    results = []
    for line in stream:
        stage = parse_asr_progress(line)
        if stage:
            events.put(stage)
            on_stage(stage)
        elif line.startswith(RESULT_PREFIX.encode("ascii")):
            results.append(line)
    return b"".join(results)


def is_launch_command(transcript: str) -> bool:
    """Route short action mentions to the shared task entry for intent review."""
    # ponytail: the short-utterance cap avoids loading the text model for a
    # long casual recording; a lighter intent model can replace this gate.
    return (isinstance(transcript, str) and len(transcript) <= 160 and
            (any(verb in transcript for verb in ("打开", "启动", "运行"))
             or explicit_tool_request(transcript) is not None))


def read_command_messages(stream, events: queue.SimpleQueue, verified=None) -> bytes:
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
        elif line.startswith(COMMAND_CHECKPOINT_PREFIX.encode("ascii")) and verified is not None:
            try:
                message = json.loads(line[len(COMMAND_CHECKPOINT_PREFIX):].decode("ascii"))
                sequence, step = message.get("sequence"), message.get("step")
                if (message.get("verified") is True and type(sequence) is int and sequence > 0
                        and isinstance(step, str) and step in {"text_model_ready", "launch_broker_ready",
                             "application_window_verified", "window_action_verified:1",
                             "window_action_verified:2", "window_action_verified:3"}):
                    verified((sequence, step, time.monotonic()))
            except (ValueError, AttributeError):
                continue
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as raw_output:
        with wave.open(raw_output, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            output.writeframes(frames)
    inspect_wav(path)
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
        self.dialog.geometry("500x390")
        self.dialog.resizable(False, False)
        self.dialog.configure(bg=DARK)
        self.dialog_exit_after = None
        self.dialog.protocol("WM_DELETE_WINDOW", self._quit)
        self.dialog.bind("<Destroy>", self._on_dialog_destroyed)
        self.root.bind("<Destroy>", self._on_root_destroyed)
        self.label = Label(self.dialog, text="", font=("Microsoft YaHei UI", 13),
                           wraplength=455, justify="left", bg=DARK, fg=FOREGROUND)
        self.label.pack(padx=16, pady=(18, 8), anchor="w")
        self.meter_label = Label(self.dialog, text="麦克风声音", bg=DARK,
                                 fg="#aeb4bf", font=("Microsoft YaHei UI", 10))
        self.meter_label.pack(padx=16, anchor="w")
        self.meter = Canvas(self.dialog, width=METER_WIDTH, height=METER_HEIGHT,
                            bg=PANEL, highlightthickness=0)
        self.meter.pack(fill="x", padx=16, pady=(4, 8))
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
        self.text.pack(fill="x", padx=16, pady=5)
        self.text.configure(state="disabled")
        self.review_after: str | None = None
        self.review_deadline: float | None = None
        self.review_nonce = 0
        self.review_note = ""
        self.auto_execute = BooleanVar(self.dialog, value=True)
        self.auto_check = Checkbutton(
            self.dialog, text=f"自动等待后继续（文字 {REVIEW_SECONDS} 秒；候选 10 秒）", variable=self.auto_execute,
            command=self._on_auto_execute_changed, bg=DARK, fg=FOREGROUND,
            selectcolor=PANEL, activebackground=DARK, activeforeground=FOREGROUND,
            font=("Microsoft YaHei UI", 10))
        self.auto_check.pack(padx=16, anchor="w")
        self.candidate_preview = CandidatePreview()
        self.candidate_preview_after = None
        self.candidate_auto = self.auto_execute
        experiment_caption = (f"候选 {self.candidate_preview.settings['countdown_seconds']} 秒无人选择后自动执行第一项"
            if self.candidate_preview.settings.get("mode") == "ranked" else
            "实验：倒计时自动选择并执行（交互会暂停）"
            if self.candidate_preview.settings.get("mode") == "execute"
            and self.candidate_preview.settings.get("thresholds_calibrated") is True else
            "实验：倒计时预览最高名称匹配（仍须确认）")
        self.candidate_auto_check = Checkbutton(
            self.dialog, text=experiment_caption,
            variable=self.candidate_auto, command=self._candidate_auto_changed,
            bg=DARK, fg=FOREGROUND, selectcolor=PANEL)
        # Keep the legacy preview control for compatibility, but expose only the
        # shared switch. Candidate countdown status appears beside that switch.
        self.dialog_layout_after = None
        self.fitting_dialog = False
        self.candidate_task_id = None
        self.candidate_transcript = ""
        self.candidate_panel = CandidatePanel(
            self.dialog, bg=DARK, panel=PANEL, foreground=FOREGROUND,
            on_change=self._candidate_selection_changed,
            on_layout=self._queue_dialog_layout)
        self.actions = Frame(self.dialog, bg=DARK)
        self.actions.pack(side="bottom", pady=8, before=self.label)
        self.cancel_button = Button(
            self.actions, text="取消", width=10, command=self._cancel_review,
            bg=RED, fg=FOREGROUND, activebackground="#ed5555",
            activeforeground=FOREGROUND, relief="flat")
        self.dialog.bind("<KeyPress>", self._pause_review)
        self.dialog.bind("<ButtonPress>", self._pause_review)
        self.dialog.bind("<MouseWheel>", self._pause_review)
        self.dialog.bind("<Control-Return>", self._confirm_review)
        self.dialog.bind("<Escape>", self._cancel_review_key)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.button = Button(self.actions, text="关闭", width=18,
                             command=self._close_dialog, bg="#343943",
                             fg=FOREGROUND, activebackground="#4a505b",
                             activeforeground=FOREGROUND, relief="flat")
        self.button.pack(side="left", padx=5)
        self.recovery_button = Button(self.actions, text="重新录音", command=self._begin_countdown)
        self.dialog.withdraw()
        self.state = "idle"
        self.completion_notified = False
        self.chunks: list[bytes] = []
        self.capture_accepting = True
        self.input_source = "asr"
        self.stream = None
        self.audio_path: Path | None = None
        self.asr_process: subprocess.Popen | None = None
        self.recording_started = 0.0
        self.capture_warning = ""
        self.countdown_nonce = 0
        self.alive = True
        self.lifecycle_lock = threading.Lock()
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
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.appearance = VoiceAppearance(self)

    def _set_state(self, state: str) -> None:
        self.state = state
        _write_status(state, thread_id=self.hotkey_thread_id)
        if state in {"done", "failed", "recovery"}:
            self._notify_completion()

    def _notify_completion(self) -> None:
        if self.completion_notified:
            return
        self.completion_notified = True
        try:
            # MessageBeep queues the system sound without blocking this UI.
            winsound.MessageBeep(winsound.MB_OK)
        except (RuntimeError, OSError):
            pass  # Audio device failure must not change the task result.

    def _show(self, label: str, button: str, action) -> None:
        self.label.configure(text=label)
        wait_status = {
            "recording": "正在录音；后续确认按自动等待设置继续",
            "transcribing": "正在识别；文字就绪后再倒计时",
            "acting": "已提交，正在执行；当前没有确认倒计时",
            "recovery": "本轮已停止，等待手动处理",
            "done": "本轮已结束",
            "failed": "本轮已结束",
        }.get(self.state)
        if wait_status:
            self.auto_check.configure(text=f"自动等待后继续（{wait_status}）")
        is_cancel = button == "取消"
        self.button.configure(text=button, command=action,
                              state="normal",
                              bg=RED if is_cancel else "#343943",
                              activebackground="#ed5555" if is_cancel else "#4a505b")
        if self.state in {"reviewing", "candidate_review"}:
            self.cancel_button.pack(side="left", padx=5)
        else:
            self.cancel_button.pack_forget()
        self.auto_check.configure(state="disabled" if self.state == "candidate_review" else "normal")
        if self.state == "recovery":
            self.recovery_button.pack(side="left", padx=5)
        else:
            self.recovery_button.pack_forget()
        self.appearance.style_buttons()
        self._fit_dialog()
        if not self.dialog.winfo_viewable():
            self.dialog.deiconify()
            self.dialog.lift()

    def _work_area(self):
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        monitor = self.user32.MonitorFromWindow(self.dialog.winfo_id(), 2)
        if monitor and self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, self.dialog.winfo_screenwidth(), self.dialog.winfo_screenheight()

    def _queue_dialog_layout(self):
        if not self.fitting_dialog and self.dialog_layout_after is None:
            self.dialog_layout_after = self.root.after_idle(self._fit_dialog)

    def _fit_dialog(self):
        if self.fitting_dialog:
            return
        self.fitting_dialog = True
        try:
            if self.dialog_layout_after is not None:
                self.root.after_cancel(self.dialog_layout_after)
                self.dialog_layout_after = None
            left, top, right, bottom = self._work_area()
            max_height = max(1, bottom - top - 64)
            text_review = self.state in {"reviewing", "candidate_review"}
            target_details = self.state in {"acting", "done", "failed"} and bool(getattr(self, "execution_choice", None))
            width = min(640 if text_review or target_details else 500, max(1, right - left - 16))
            self.meter.configure(height=100 if max_height < 700 or text_review else METER_HEIGHT)
            self.text.configure(height=8 if (self.state == "reviewing" or target_details) and max_height >= 700 else 4)
            self.label.configure(wraplength=max(1, width - 40))
            self.dialog.geometry(f"{width}x{min(max_height, self.dialog.winfo_height())}")
            self.dialog.update_idletasks()
            requested = self.dialog.winfo_reqheight()
            if requested > max_height and self.appearance.controls.winfo_manager():
                self.appearance.controls.pack_forget()
                self.appearance.options_button.configure(text="外观 ▾")
                self.dialog.update_idletasks()
                requested = self.dialog.winfo_reqheight()
            if self.candidate_panel.winfo_manager():
                base_height = requested - self.candidate_panel.winfo_reqheight() - 12
                capacity = max(1, max_height - base_height - 12)
                self.candidate_panel.set_viewport_height(
                    min(capacity, self.candidate_panel.content_height()))
                self.dialog.update_idletasks()
                requested = self.dialog.winfo_reqheight()
            height = min(max_height, max(390, requested))
            x = min(max(left + 8, self.dialog.winfo_x()), right - width - 8)
            y = min(max(top + 8, self.dialog.winfo_y()), bottom - height - 56)
            self.dialog.geometry(f"{width}x{height}+{x}+{y}")
        finally:
            self.fitting_dialog = False

    def _clear_candidates(self):
        self._cancel_candidate_preview()
        self.candidate_panel.clear()
        self.candidate_panel.pack_forget()
        self.candidate_task_id = None
        self.candidate_transcript = ""

    def _reset_meter(self) -> None:
        self.latest_rms = 0
        self.meter_level = 0.0
        self.meter_history = [0.0] * METER_BARS
        self._draw_meter()

    def _draw_meter(self) -> None:
        if hasattr(self, "appearance"):
            self.appearance.draw_meter(self.meter_history)
            return
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
        if stage_changed and self.state == "transcribing":
            self._set_text(detail)
        self.label.configure(text=f"{heading}{'·' * (self.processing_frame % 4)}\n请稍候，结果会出现在这里。")
        self.meter_label.configure(text="声音正在变成文字")
        phase = self.processing_frame if self.appearance.settings["motion"] else 0
        self.appearance.draw_meter([.1 + .45 * (1 + math.sin(index * .42 + phase * .26))
                                    for index in range(METER_BARS)])
        self.processing_frame += 1
        self.root.after(90, self._tick_processing)

    def _set_text(self, value: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.edit_modified(False)
        self.text.configure(state="disabled")

    def _cancel_review_timer(self) -> None:
        self.review_nonce += 1
        self.review_deadline = None
        if self.review_after is not None:
            self.root.after_cancel(self.review_after)
            self.review_after = None

    def _begin_review(self, transcript: str, note: str = "") -> None:
        self._cancel_review_timer()
        self._clear_candidates()
        self.review_note = note
        self._set_text(transcript)
        self._set_state("reviewing")
        self.text.configure(state="normal")
        self.meter_label.configure(text="审查识别文字，可直接修改")
        if self.auto_execute.get():
            self.review_deadline = time.monotonic() + REVIEW_SECONDS
            self._tick_review(self.review_nonce)
        else:
            self._pause_review()

    def _tick_review(self, nonce: int) -> None:
        if (not self.alive or self.state != "reviewing" or
                nonce != self.review_nonce or self.review_deadline is None):
            return
        self.review_after = None
        # Read the edit flag before timeout, including edits whose Tk event is queued.
        if self.text.edit_modified():
            self._pause_review()
            return
        remaining = self.review_deadline - time.monotonic()
        self.auto_check.configure(
            text=f"{REVIEW_SECONDS} 秒无操作自动执行（剩余 {max(0, math.ceil(remaining))} 秒）")
        self._show(
            f"请审查文字：{max(0, math.ceil(remaining))} 秒无操作后自动执行。\n"
            "点击或按键可暂停，修改后点确认执行。" + self.review_note,
            "确认执行", self._submit_review)
        if remaining <= 0:
            # Let Tk paint zero before changing state; recheck edits and the
            # review identity after yielding so a stale timer cannot submit.
            self.review_after = self.root.after(100, self._finish_review_countdown, nonce)
            return
        self.review_after = self.root.after(100, self._tick_review, nonce)

    def _finish_review_countdown(self, nonce: int) -> None:
        if (not self.alive or self.state != "reviewing" or
                nonce != self.review_nonce or self.review_deadline is None):
            return
        self.review_after = None
        if self.text.edit_modified() or not self.auto_execute.get():
            self._pause_review()
            return
        self._submit_review()

    def _pause_review(self, _event=None) -> None:
        if self.state == "candidate_review":
            if _event is None or self.candidate_preview.settings.get("mode") != "ranked":
                self._cancel_candidate_preview()
            return
        if self.state != "reviewing":
            return
        self._cancel_review_timer()
        self.auto_check.configure(text=f"{REVIEW_SECONDS} 秒无操作自动执行（已暂停）")
        self._show("自动执行已暂停，可修改文字后确认。\n"
                   "Ctrl+Enter 确认执行，取消或 Esc 只暂停自动执行。" + self.review_note,
                   "确认执行", self._submit_review)

    def _cancel_review(self) -> None:
        if self.state == "reviewing":
            self._pause_review()
        elif self.state == "candidate_review":
            self._cancel_candidate_preview()
            self._cancel_review_timer()
            self.label.configure(text="已暂停，候选和输入内容已保留。\n选择后点确认继续，右上角关闭可退出。")
            self._fit_dialog()

    def _on_text_modified(self, _event=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._pause_review()

    def _on_auto_execute_changed(self) -> None:
        # Future confirmation stages read this switch; enabling it never submits
        # the current paused edit without confirmation.
        self.candidate_preview.set_enabled(self.auto_execute.get())
        self._pause_review()

    def _confirm_review(self, _event=None):
        if self.state == "candidate_review":
            self._submit_candidates()
            return "break"
        if self.state == "reviewing":
            self._submit_review()
            return "break"

    def _cancel_review_key(self, _event=None):
        if self.state in {"reviewing", "candidate_review"}:
            self._cancel_review()
            return "break"

    def _submit_review(self) -> None:
        if not self.alive or self.state != "reviewing":
            return
        self._cancel_review_timer()
        transcript = self.text.get("1.0", "end-1c").strip()
        if not transcript or not is_launch_command(transcript):
            self.auto_check.configure(text="自动等待后继续（等待有效输入，倒计时已暂停）")
            self._show("文字为空或不是当前支持的启动指令，未执行。\n"
                       "请修改后确认；取消只暂停自动执行。" + self.review_note,
                       "确认执行", self._submit_review)
            return
        self._start_command(transcript)

    def _start_command(self, transcript: str, review: dict | None = None):
        self.completion_notified = False
        self.execution_choice = (next((item.copy() for item in self.candidate_panel.items
            if item["app_id"] == review["selected_app_id"]), None)
            if review and review.get("selected_app_id") else None)
        self.execution_origin = (review or {}).get("selection_origin", "user_selected_candidate")
        self._clear_candidates()
        self._set_state("acting")
        self._set_text(execution_target_text(self.execution_choice, self.execution_origin)
                       if self.execution_choice else transcript)
        self.progress_events = queue.SimpleQueue()
        self.processing_stage = "executing"
        self.processing_frame = 0
        self._show("正在处理启动指令…", "最小化（后台继续）", self._close_dialog)
        self._tick_processing()
        arguments = (transcript,) if review is None else (transcript, review)
        threading.Thread(target=self._command_worker, args=arguments,
                         daemon=True).start()

    def _begin_candidate_review(self, result: dict, transcript: str):
        self._cancel_review_timer()
        self._clear_candidates()
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 100:
            self._set_state("failed")
            self._set_text(transcript)
            self._show("候选确认信息不完整，请重新录音。", "关闭", self._close_dialog)
            return
        self.candidate_task_id = task_id
        self.candidate_transcript = transcript
        self.execution_choice = None
        self._set_text(transcript)
        self._set_state("candidate_review")
        self.auto_check.configure(text="自动等待后继续（文字已提交；候选另行倒计时）")
        self.meter_label.configure(text="等待选择应用")
        self.candidate_panel.pack(fill="both", expand=True, padx=16, pady=(4, 8))
        details = result.get("result")
        details = details if isinstance(details, dict) else {}
        self.candidate_panel.show_candidates(
            details.get("candidates", []), complete_matches=details.get("complete_matches") is True)
        message = "请选择一个应用，或选“都不是”手动输入。"
        self.candidate_correction = ""
        if details.get("corrected_name"):
            message = f"名称纠错：按“{details['corrected_name']}”查找（原输入：{details.get('requested_name', '')}）。"
            self.candidate_correction = message + "\n"
        if details.get("same_name_entries"):
            message += f"\n找到 {len(self.candidate_panel.items)} 个同名入口，已全部列出；请选择具体入口。"
        if not self.candidate_panel.items:
            message = "未找到匹配的应用，未启动其他程序。\n请修改应用名称，或取消本轮。"
        if result.get("error_code") == "candidate_choice_rejected":
            message = "这个选择尚未确认，请重新选择或修改名称。\n只有确认后才继续。"
        self._show(message, "确认选择", self._submit_candidates)
        previous_manual = details.get("manual_name")
        if isinstance(previous_manual, str) and not self.candidate_panel.items:
            self.candidate_panel.manual_name.set(previous_manual)
            self.candidate_panel.literal_name.set(details.get("literal_name") is True)
            if not self.candidate_panel.items:
                self.candidate_panel._select(0)
        self.appearance.apply_theme()
        self._candidate_selection_changed()
        self.candidate_preview.set_enabled(self.auto_execute.get())
        if result.get("error_code") != "candidate_choice_rejected":
            self.candidate_preview.arm(self.candidate_panel.items)
        self._tick_candidate_preview()

    def _cancel_candidate_preview(self):
        active = self.candidate_preview.expires is not None
        self.candidate_preview.cancel()
        if self.candidate_preview_after is not None:
            self.root.after_cancel(self.candidate_preview_after)
            self.candidate_preview_after = None
        if active and self.state == "candidate_review":
            self._candidate_timer_text("候选自动执行已暂停；选择后点确认继续")

    def _candidate_timer_text(self, text):
        self.candidate_auto_check.configure(text=text)
        if self.state == "candidate_review":
            self.auto_check.configure(text=f"自动等待后继续（{text}）")

    def _candidate_auto_changed(self):
        self._cancel_candidate_preview()
        self.candidate_preview.set_enabled(self.candidate_auto.get())
        if self.state == "candidate_review":
            self.label.configure(text="自动选择开关已更新；本轮保持人工确认，下轮生效。")

    def _tick_candidate_preview(self):
        self.candidate_preview_after = None
        if not self.alive or self.state != "candidate_review":
            return
        preview = self.candidate_preview.poll()
        if preview is None:
            if self.candidate_panel.choice.get() == -1:
                self._candidate_timer_text("候选自动执行未启动；请手动选择并确认")
            return
        if "remaining" in preview:
            if self.candidate_preview.settings.get("mode") == "ranked":
                name = self.candidate_preview.decision["display_name"]
                self._candidate_timer_text(f"候选自动执行：剩余 {preview['remaining']} 秒")
                self.label.configure(text=getattr(self, "candidate_correction", "") + f"{preview['remaining']} 秒无人选择后自动执行第一项：{name}。\n选择任一项或输入名称会暂停，随后点确认继续。")
                self.candidate_preview_after = self.root.after(100, self._tick_candidate_preview)
                return
            real_choice = (self.candidate_preview.settings.get("mode") == "execute"
                           and self.candidate_preview.settings.get("thresholds_calibrated") is True
                           and self.candidate_preview.decision.get("eligible") is True)
            mode = "符合实验阈值，届时自动选择并执行" if real_choice else "仅预览，仍需手动确认"
            self.label.configure(text=f"实验剩余 {preview['remaining']} 秒：{mode}；交互会暂停。")
            self.candidate_preview_after = self.root.after(200, self._tick_candidate_preview)
        else:
            decision = preview["decision"]
            if (preview.get("dispatch") is True and self.candidate_auto.get()
                    and self.candidate_panel.choice.get() == -1
                    and not self.candidate_panel.manual_name.get().strip()):
                index = next((i for i, item in enumerate(self.candidate_panel.items)
                              if item["app_id"] == decision.get("app_id")), None)
                if index is not None:
                    self.candidate_panel._select(index)
                    self._candidate_timer_text("候选自动执行：剩余 0 秒，已提交第一项")
                    self._submit_candidates(selection_origin=("candidate_countdown"
                        if self.candidate_preview.settings.get("mode") == "ranked" else "experimental_countdown"))
                    return
            if self.candidate_preview.settings.get("mode") == "ranked":
                self._candidate_timer_text("候选未自动提交；请手动选择并确认")
                self.label.configure(text="请核对候选或输入应用名称，点确认继续。")
                return
            name = decision.get("display_name", "无可靠候选")
            reason = "符合实验阈值" if decision.get("eligible") else "证据不足或阈值待校准"
            self.label.configure(text=f"实验预览：最高字面匹配为 {name}，{reason}。\n仍需手动选择并确认。")

    def _candidate_selection_changed(self):
        if self.state != "candidate_review":
            return
        selected = self.candidate_panel.choice.get() != -1 or self.candidate_panel.manual_name.get().strip()
        if selected or self.candidate_preview.settings.get("mode") != "ranked":
            self._cancel_candidate_preview()
        if selected:
            self.label.configure(text="已暂停自动选择；请核对选项或输入名称，点确认继续。")
        try:
            self.candidate_panel.selection()
            valid = True
        except ValueError:
            valid = False
        self.button.configure(state="normal" if valid else "disabled")

    def _submit_candidates(self, selection_origin=None):
        if not self.alive or self.state != "candidate_review":
            return
        try:
            selection = self.candidate_panel.selection()
        except ValueError as error:
            self.label.configure(text=str(error))
            self._fit_dialog()
            return
        request = {"transcript": self.candidate_transcript,
                   "review_task_id": self.candidate_task_id, **selection}
        if selection_origin in ("experimental_countdown", "candidate_countdown"):
            request["selection_origin"] = selection_origin
        self._start_command(self.candidate_transcript, request)

    def _restore_dialog(self) -> None:
        if not self.alive:
            return
        if self.state in {"idle", "done", "failed"}:
            self._begin_countdown()
        elif self.state == "reviewing":
            self._pause_review()
        self.dialog.deiconify()
        self.dialog.lift()

    def _close_dialog(self) -> None:
        if self.state in {"reviewing", "candidate_review"}:
            self._cancel_review()
            return
        elif self.state == "countdown":
            self.countdown_nonce += 1
            self._set_state("idle")
            self._show("录音倒计时已暂停。\n点击继续录音，或按 Ctrl+Alt+X 重新开始。",
                       "继续录音", self._begin_countdown)
            return
        elif self.state == "recording":
            self._stop_recording()
            return
        elif self.state in {"transcribing", "acting"}:
            self.dialog.iconify()
            return
        elif self.state in {"done", "failed"}:
            self._quit()
            return
        self.dialog.iconify()

    @hard_bounded_step("microphone_device_check")
    def _begin_countdown(self) -> None:
        self.completion_notified = False
        self._clear_candidates()
        if self.stream is not None:
            self._show_recovery("录音设备尚未确认释放，暂不再次打开设备。")
            return
        try:
            sd.check_input_settings(channels=1, dtype="int16", samplerate=SAMPLE_RATE)
        except Exception as error:
            self._show_recovery("暂时无法开始录音：" + type(error).__name__)
            return
        self.chunks.clear()
        self.audio_path = None
        self.capture_accepting = True
        self.capture_warning = ""
        self._reset_meter()
        self.auto_check.configure(text=f"自动等待后继续（文字 {REVIEW_SECONDS} 秒；候选 10 秒）")
        self.meter_label.configure(text="准备录音 · 麦克风尚未开启")
        self._set_text("")
        self.countdown_nonce += 1
        nonce = self.countdown_nonce
        self._set_state("countdown")
        self._countdown(3, nonce)

    def _countdown(self, remaining: int, nonce: int) -> None:
        if nonce != self.countdown_nonce or self.state != "countdown":
            return
        self.auto_check.configure(text=f"自动等待后继续（准备录音：剩余 {remaining} 秒；麦克风尚未开启）")
        if remaining == 0:
            self._start_recording()
            return
        self._show(f"{remaining} 秒后开始录音。\n再次按 Ctrl+Alt+X 可取消。",
                   "取消", self._close_dialog)
        self.root.after(1000, self._countdown, remaining - 1, nonce)

    def _audio_callback(self, indata, _frames, _time_info, status) -> None:
        if not self.capture_accepting:
            return
        if status:
            self.capture_warning = str(status)
        payload = bytes(indata)
        self.chunks.append(payload)
        self.latest_rms = audioop.rms(payload, 2)

    @hard_bounded_step("microphone_open")
    def _start_recording(self) -> None:
        try:
            self.stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=SAMPLE_RATE // 10, callback=self._audio_callback)
            self.stream.start()
        except Exception as error:
            self.capture_accepting = False
            if self.chunks:
                try:
                    self._preserve_captured_audio()
                except (OSError, ValueError):
                    pass
            if self.stream is not None:
                try:
                    self.stream.close()
                    self.stream = None
                except Exception:
                    pass  # Keep the handle; rerecording is blocked until released.
            self._show_recovery("麦克风未能打开：" + type(error).__name__)
            return
        self.recording_started = time.monotonic()
        self.meter_label.configure(text="正在录音 · 麦克风声音")
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

    def _preserve_captured_audio(self):
        path = TEMP_AUDIO / f"voice-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.wav"
        self.audio_path = path
        write_pcm_wav(self.chunks, path)
        self.chunks.clear()
        return path

    @hard_bounded_step("microphone_finalize")
    def _stop_recording(self) -> None:
        if self.state != "recording":
            return
        self.capture_accepting = False
        capture_error = None
        try:
            # Preserve all data already collected before a driver stop/close
            # can fail or stall. No silence trimming or content-based rejection.
            self._preserve_captured_audio()
        except Exception as error:
            capture_error = error
        try:
            try:
                self.stream.stop()
            finally:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
        except Exception as error:
            capture_error = capture_error or error
        if capture_error is not None:
            self._show_recovery(f"录音没有完成：{capture_error}\n录音位置：{self.audio_path}")
            return
        self._set_state("transcribing")
        self.progress_events = queue.SimpleQueue()
        self.processing_stage = "checking"
        self.processing_frame = 0
        self._set_text(PROGRESS_TEXT["checking"][1])
        self._show("正在处理录音…", "最小化（后台继续）", self._close_dialog)
        self._tick_processing()
        threading.Thread(target=self._transcribe_worker, args=(self.audio_path,),
                         daemon=True).start()

    def _transcribe_worker(self, audio_path: Path) -> None:
        result = run_asr_flow(audio_path, self._attempt_asr, deadline=Deadline("automatic_asr"),
                              secondary=SECONDARY_ASR, cancelled=lambda: not self.alive)
        if self.alive:
            self.root.after(0, self._asr_finished, result, audio_path)

    def _attempt_asr(self, audio_path: Path, deadline: Deadline) -> dict:
        result = {"status": "failed", "reason": "asr_process_failed"}
        process = None
        try:
            environment = deadline.environment()
            environment.update({"CUDA_VISIBLE_DEVICES": "0", "HF_HUB_OFFLINE": "1",
                                "TRANSFORMERS_OFFLINE": "1",
                                "LOCALAI_VOICE_PARENT_PID": str(os.getpid()),
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
                    process.wait(timeout=deadline.remaining(1))
                    reader.join(timeout=5)
                    if reader.is_alive():
                        raise RuntimeError("ASR output reader did not finish")
                    result = parse_asr_output(b"".join(output))
                    break
                except subprocess.TimeoutExpired:
                    deadline.remaining()
        except Exception as error:
            result = {"status": "failed", "reason": failure_code(error)}
        finally:
            stop_owned(process)
            self.asr_process = None
        return result

    def _show_recovery(self, message):
        self._cancel_review_timer()
        self._set_state("recovery")
        self._set_text("")
        self._show(message + "\n可重新录音，或直接输入文字。", "文字输入", self._begin_manual_input)

    def _begin_manual_input(self):
        self.input_source = "text"
        self._begin_review("", "\n手动输入内容后点确认执行。")
        self._pause_review()
        self.text.focus_set()

    def _asr_finished(self, result: dict, audio_path: Path) -> None:
        if result.get("status") == "ok":
            self.input_source = "asr"
            transcript = result["transcript"]
            retention_note = f"\n完整录音保存在：{audio_path}"
            warning = "\n录音时检测到丢帧，文字可能不完整。" if self.capture_warning else ""
            if result.get("quality", {}).get("warnings"):
                warning += "\n录音音量偏低，请核对文字。"
            self._begin_review(transcript, warning + retention_note)
        else:
            from asr_flow import failure_message
            reason = failure_message(result)
            self.meter_label.configure(text="识别未完成")
            self._show_recovery(f"语音识别未完成：{reason}\n完整录音保存在：{audio_path}")

    def _command_worker(self, transcript: str, review: dict | None = None) -> None:
        result = {"status": "failed", "error_code": "command_runner_failed"}
        log_path = ROOT / "runtime" / "voice-hotkey" / f"command-{uuid.uuid4().hex}.log"
        process = None
        checkpoints = queue.SimpleQueue()
        deadline = Deadline("voice_command")
        last_sequence = 0
        verified_steps = set()
        try:
            arguments = [str(TASK_PYTHON), "-B", str(COMMAND_SCRIPT)]
            if review is not None:
                arguments.append("--review")
            elif self.input_source == "text":
                arguments.append("--text")
            payload = transcript if review is None else json.dumps(review, ensure_ascii=False)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # A confirmed task owns its remaining work; its output must survive UI exit.
            with self.lifecycle_lock, log_path.open("xb") as output_log:
                if not self.alive:
                    return
                process = subprocess.Popen(
                    arguments, cwd=ROOT, stdin=subprocess.PIPE, stdout=output_log,
                    stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
                from approved_window_process import allow_child_foreground
                allow_child_foreground(process.pid)
                process.stdin.write(payload.encode("utf-8"))
                process.stdin.close()
            output = []

            def read_output():
                with log_path.open("rb") as stream:
                    while True:
                        line = stream.readline()
                        if line:
                            output.append(line)
                            read_command_messages(io.BytesIO(line), self.progress_events, checkpoints.put)
                        elif process.poll() is not None:
                            output.append(stream.read())
                            break
                        else:
                            time.sleep(0.05)

            reader = threading.Thread(
                target=read_output, daemon=True)
            reader.start()
            while True:
                while not checkpoints.empty():
                    sequence, step, observed = checkpoints.get_nowait()
                    if sequence > last_sequence and step not in verified_steps and observed <= deadline.expires:
                        last_sequence = sequence
                        verified_steps.add(step)
                        deadline = Deadline(step, started=observed)
                try:
                    process.wait(timeout=deadline.remaining(0.5))
                    break
                except subprocess.TimeoutExpired:
                    continue
            reader.join(timeout=5)
            if reader.is_alive():
                raise RuntimeError("Command output reader did not finish")
            result = parse_command_output(b"".join(output))
        except (subprocess.TimeoutExpired, TimeoutError):
            stop_owned(process)
            result = {"status": "executed_unverified", "error_code": "command_timeout",
                      "execution_state": "requires_local_reconciliation", "log_path": str(log_path),
                      **deadline.report()}
        except Exception as error:
            result = {"status": "failed", "error_code": type(error).__name__}
        if self.alive:
            self.root.after(0, self._command_finished, result, transcript)
        else:
            self._notify_completion()

    def _command_finished(self, result: dict, transcript: str) -> None:
        status = result.get("status")
        if status == "needs_confirmation":
            self._begin_candidate_review(result, transcript)
            return
        self.auto_check.configure(text=f"{REVIEW_SECONDS} 秒无操作自动执行（本轮已结束）")
        messages = {
            "succeeded": "应用已打开，窗口已核验。",
            "already_running": "应用已在运行，窗口已核验。",
            "running": "相同指令正在处理中，没有重复启动。",
            "needs_confirmation": "找到可能的应用，但还需要确认，未执行。",
            "ambiguous": "识别出多个应用，未执行。",
            "rejected": "这条语音未构成可执行的启动命令。",
            "paused": "本次操作已暂停。",
            "cancelled": "本次操作已取消。",
            "executed_unverified": "操作可能已经生效，但尚未验收；已停止，请先核对结果再继续。",
        }
        if result.get("deduplicated") and status == "executed_unverified":
            messages[status] = "同一指令已有未核验结果；为避免重复操作，本轮没有再次执行。\n请先核对原任务结果。"
        workflow = (result.get("result") or {}).get("workflow")
        if status == "succeeded" and workflow:
            messages[status] = {"browser_search": "搜索已提交，搜索结果页已核验。",
                                "toolbox_open": "已从图吧目录找到目标，打开结果已核验。",
                                "scrapling_fetch": "网页内容已提取，结果已保存。",
                                "rea_inspect": "REA 调用已完成；分析范围请查看结果记录。"}.get(workflow, "任务结果已核验。")
        self._set_state("done" if status in {"succeeded", "already_running"} else "failed")
        self.meter_label.configure(text="任务结果" if workflow else "启动结果")
        trace = (result.get("result") or {}).get("name_lookup", {})
        target = trace.get("selected_target") or getattr(self, "execution_choice", None)
        self.execution_choice = target
        self._set_text(execution_target_text(target, trace.get("selection_origin", getattr(self, "execution_origin", None)))
                       if target else transcript)
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
        trace = (result.get("result") or {}).get("name_lookup", {})
        if trace.get("corrected_name"):
            self.label.configure(text=self.label.cget("text") + f"\n名称纠错：{trace['requested_name']} → {trace['corrected_name']}")
            self._fit_dialog()

    def _on_hotkey(self) -> None:
        if self.state in {"idle", "done", "failed", "recovery"}:
            self._begin_countdown()
        elif self.state == "countdown":
            self._close_dialog()
        elif self.state == "recording":
            self._stop_recording()
        elif self.state == "reviewing":
            self._pause_review()
        elif self.state == "candidate_review":
            self._restore_dialog()
        elif self.state in {"transcribing", "acting"}:
            self._restore_dialog()

    def _poll_hotkeys(self) -> None:
        if not self.alive:
            return
        while not self.hotkey_events.empty():
            hotkey_id = self.hotkey_events.get_nowait()
            if hotkey_id == HOTKEY_ID:
                self._on_hotkey()
            elif hotkey_id == SHOW_ID:
                self._restore_dialog()
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

    def _on_dialog_destroyed(self, event) -> None:
        if event.widget is self.dialog and self.alive:
            self.dialog_exit_after = self.root.after_idle(self._quit)

    def _on_root_destroyed(self, event) -> None:
        if event.widget is self.root:
            if hasattr(self, "appearance"):
                self.appearance.close()
            if self.dialog_exit_after is not None:
                self.root.after_cancel(self.dialog_exit_after)
                self.dialog_exit_after = None

    @hard_bounded_step("voice_ui_cleanup")
    def _quit(self) -> None:
        self.dialog_exit_after = None
        with self.lifecycle_lock:
            if not self.alive:
                return
            self.alive = False
        self._cancel_review_timer()
        self.appearance.close()
        self._cancel_candidate_preview()
        if self.stream is not None:
            self.capture_accepting = False
            if self.chunks:
                try:
                    self._preserve_captured_audio()
                except (OSError, ValueError):
                    pass
            try:
                self.stream.abort()
            except Exception:
                pass
            finally:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
        if self.asr_process is not None and self.asr_process.poll() is None:
            stop_owned(self.asr_process)
        if self.state in {"countdown", "recording", "transcribing", "reviewing", "candidate_review"}:
            self._notify_completion()
        if self.hotkey_thread_id:
            self.user32.PostThreadMessageW(self.hotkey_thread_id, WM_QUIT, 0, 0)
        try:
            _write_status("stopped", thread_id=self.hotkey_thread_id)
        finally:
            try:
                self.root.destroy()
            except TclError:
                pass  # The root may already be gone after external destruction.

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
