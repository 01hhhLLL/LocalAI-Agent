"""Local appearance preferences and native styling for the voice workbench."""

import json
import math
import re
import time
import uuid
from pathlib import Path
from tkinter import BooleanVar, Button, Canvas, Checkbutton, Frame, Label, StringVar, TclError, colorchooser, ttk

from voice_mascot import MascotStage

ROOT = Path(__file__).resolve().parents[1]
PREFERENCES = ROOT / "runtime" / "voice-hotkey" / "appearance.json"
COLORS = {"鸢尾紫": "#7c3aed", "湖水蓝": "#0891b2", "薄荷绿": "#059669", "玫瑰粉": "#db2777"}
DEFAULTS = {"theme": "dark", "color": "#7c3aed", "wave": "音柱", "character": "Q版", "motion": True, "pet": True}
PALETTES = {
    "dark": {"bg": "#13121c", "panel": "#211f30", "fg": "#f2effb", "muted": "#b4acc8", "border": "#39334d"},
    "light": {"bg": "#f4f1fa", "panel": "#ffffff", "fg": "#252036", "muted": "#655c78", "border": "#d8d0e5"},
}


def load_preferences(path=PREFERENCES):
    settings = DEFAULTS.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(data, dict):
        return settings
    if data.get("wave") == "流线":
        data["wave"] = "粒子"
    for key, values in {"theme": PALETTES, "wave": ("音柱", "粒子", "光点"), "character": ("正常", "Q版")}.items():
        if isinstance(data.get(key), str) and data[key] in values:
            settings[key] = data[key]
    if isinstance(data.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", data["color"]):
        settings["color"] = data["color"]
    for key in ("motion", "pet"):
        if isinstance(data.get(key), bool):
            settings[key] = data[key]
    return settings


def contrast_text(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    luminance = sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
    return "#ffffff" if 1.05 / (luminance + .05) >= 4.5 else "#17121f"


class ThemeSwitch(Canvas):
    """Sun/moon switch with a sliding thumb and Space/Enter keyboard access."""

    def __init__(self, parent, command, reduced_motion):
        super().__init__(parent, width=126, height=38, highlightthickness=1, takefocus=True, cursor="hand2")
        self.scheduler = parent.winfo_toplevel().master
        self.command, self.reduced_motion = command, reduced_motion
        self.position = self.target = 1.
        self.timer = None
        self.palette = PALETTES["dark"]
        self.accent = DEFAULTS["color"]
        self.bind("<ButtonRelease-1>", self.toggle)
        self.bind("<space>", self.toggle)
        self.bind("<Return>", self.toggle)
        self.bind("<FocusIn>", lambda _event: self.configure(highlightbackground=self.accent))
        self.bind("<FocusOut>", lambda _event: self.configure(highlightbackground=self.palette["bg"]))

    def toggle(self, _event=None):
        if _event is not None and _event.state & 4:
            return None  # Keep Ctrl+Enter available to confirm the transcript.
        self.focus_set()
        self.command()
        return "break"

    def set_theme(self, theme, palette, accent):
        self.palette, self.accent = palette, accent
        self.configure(bg=palette["bg"], highlightbackground=palette["bg"], highlightcolor=accent)
        target = 1. if theme == "dark" else 0.
        if target != self.target:
            self.target = target
            self.started, self.origin = time.monotonic(), self.position
            self.close()
            self.animate()
        else:
            self.draw()

    def animate(self):
        self.timer = None
        progress = 1. if self.reduced_motion() else min(1., (time.monotonic() - self.started) / .16)
        eased = 1 - (1 - progress) ** 3
        self.position = self.origin + (self.target - self.origin) * eased
        self.draw()
        if progress < 1:
            self.timer = self.scheduler.after(16, self.animate)

    def draw(self):
        self.delete("all")
        track = "#302846" if self.target else "#e4edf7"
        for x in (2, 92):
            self.create_oval(x, 3, x + 32, 35, fill=track, outline="")
        self.create_rectangle(18, 3, 108, 35, fill=track, outline="")
        x = 3 + self.position * 62
        thumb = "#5b457d" if self.target else "#ffffff"
        self.create_oval(x, 5, x + 28, 33, fill=thumb, outline="")
        self.create_rectangle(x + 14, 5, x + 44, 33, fill=thumb, outline="")
        self.create_oval(x + 30, 5, x + 58, 33, fill=thumb, outline="")
        self.create_text(28, 19, text="☀ 白天", fill="#66500b" if not self.target else "#c8bdde",
                         font=("Microsoft YaHei UI", 9, "bold"))
        self.create_text(94, 19, text="☾ 黑夜", fill="#ffffff" if self.target else "#526172",
                         font=("Microsoft YaHei UI", 9, "bold"))

    def close(self):
        if self.timer is not None:
            try:
                self.scheduler.after_cancel(self.timer)
            except TclError:
                pass
            self.timer = None


class VoiceAppearance:
    def __init__(self, window, path=PREFERENCES):
        self.window, self.path = window, path
        self.settings = load_preferences(path)
        self.p = PALETTES[self.settings["theme"]]
        self.header = Frame(window.dialog)
        self.header.pack(fill="x", padx=18, pady=(14, 4), before=window.label)
        self.title = Label(self.header, text="语音工作台", font=("Microsoft YaHei UI", 18, "bold"))
        self.title.pack(side="left", anchor="w")
        self.theme_button = ThemeSwitch(self.header, self.toggle_theme, lambda: not self.settings["motion"])
        self.theme_button.pack(side="right")
        self.options_button = Button(self.header, text="外观 ▾", command=self.toggle_options, padx=8, pady=6, relief="flat")
        self.options_button.pack(side="right", padx=(0, 6))
        self.badge = Label(window.dialog, font=("Microsoft YaHei UI", 9), padx=10, pady=4, anchor="w")
        self.badge.pack(fill="x", padx=18, pady=(0, 5), before=window.label)
        self.controls = Frame(window.dialog)
        self.color_buttons = []
        self.labels = []
        first, second = Frame(self.controls), Frame(self.controls)
        first.pack(fill="x")
        second.pack(fill="x", pady=(6, 0))
        self.rows = [first, second]
        label = Label(first, text="颜色", font=("Microsoft YaHei UI", 9))
        label.pack(side="left", padx=(0, 7))
        self.labels.append(label)
        for name, color in COLORS.items():
            button = Button(first, text=name, command=lambda color=color: self.set_color(color),
                            bg=color, fg=contrast_text(color), relief="flat", padx=8, pady=4,
                            font=("Microsoft YaHei UI", 9), takefocus=True)
            button.pack(side="left", padx=(0, 5))
            self.color_buttons.append(button)
        self.custom_button = Button(first, text="自选", command=self.choose_color, relief="flat", padx=8, pady=4)
        self.custom_button.pack(side="left")
        self.wave_var = StringVar(window.dialog, self.settings["wave"])
        self.character_var = StringVar(window.dialog, self.settings["character"])
        self.combos = []
        for caption, var, values in (("波形", self.wave_var, ("音柱", "粒子", "光点")),
                                     ("角色", self.character_var, ("Q版", "正常"))):
            label = Label(second, text=caption, font=("Microsoft YaHei UI", 9))
            label.pack(side="left", padx=(0, 5))
            self.labels.append(label)
            combo = ttk.Combobox(second, textvariable=var, values=values, state="readonly", width=5,
                                 font=("Microsoft YaHei UI", 9), style="Voice.TCombobox")
            combo.pack(side="left", padx=(0, 10))
            combo.bind("<<ComboboxSelected>>", self.change_options)
            self.combos.append(combo)
        self.motion_var = BooleanVar(window.dialog, self.settings["motion"])
        self.pet_var = BooleanVar(window.dialog, self.settings["pet"])
        self.checks = []
        for caption, var in (("动效", self.motion_var), ("桌宠", self.pet_var)):
            check = Checkbutton(second, text=caption, variable=var, command=self.change_options,
                                font=("Microsoft YaHei UI", 9), relief="flat")
            check.pack(side="left")
            self.checks.append(check)
        self.note = Label(window.dialog, text="点击互动 · 拖拽移动 · 右键更多", font=("Microsoft YaHei UI", 9))
        self.note.pack(anchor="w", padx=18, pady=(0, 5), before=window.auto_check)
        self.dots = [window.meter.create_oval(0, 0, 1, 1, tags="wave-dots") for _ in window.meter_bars]
        self.stage = MascotStage(window.dialog, ROOT / "assets" / "voice-mascot", self.settings,
                                 status=lambda: window.state, on_interaction=window._pause_review,
                                 on_frame=self.animate_particles, bottom_inset=lambda: window.actions.winfo_height() + 12,
                                 on_hide=self.hide_pet)
        self.style = ttk.Style(window.dialog)
        self.style.theme_use("clam")
        self.apply_theme()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"appearance-{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(self.settings, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            self.note.configure(text="外观已切换，但保存失败；下次打开可能恢复默认。")

    def toggle_options(self):
        if self.controls.winfo_manager():
            self.controls.pack_forget()
            self.options_button.configure(text="外观 ▾")
        else:
            self.controls.pack(fill="x", padx=16, pady=(8, 5), before=self.note)
            self.options_button.configure(text="外观 ▴")
        self.window._queue_dialog_layout()

    def toggle_theme(self):
        self.settings["theme"] = "light" if self.settings["theme"] == "dark" else "dark"
        self.apply_theme()
        self.save()

    def set_color(self, color):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return
        self.settings["color"] = color
        self.apply_theme()
        self.save()

    def choose_color(self):
        color = colorchooser.askcolor(self.settings["color"], parent=self.window.dialog, title="选择波形与按钮颜色")[1]
        if color:
            self.set_color(color)

    def change_options(self, _event=None):
        self.settings.update(wave=self.wave_var.get(), character=self.character_var.get(),
                             motion=self.motion_var.get(), pet=self.pet_var.get())
        self.stage.refresh()
        self.draw_meter(self.window.meter_history)
        self.save()

    def hide_pet(self):
        self.pet_var.set(False)
        self.change_options()

    def style_buttons(self):
        w, p, color = self.window, self.p, self.settings["color"]
        primary = w.button.cget("text") not in ("取消", "关闭", "最小化（后台继续）")
        w.button.configure(bg=color if primary else p["panel"], fg=contrast_text(color) if primary else p["fg"],
                           activebackground=color, activeforeground=contrast_text(color),
                           disabledforeground=p["muted"], font=("Microsoft YaHei UI", 10, "bold"),
                           padx=10, pady=7, highlightthickness=1, highlightbackground=p["border"])
        w.cancel_button.configure(bg=p["panel"], fg=p["fg"], activebackground=p["border"],
                                  activeforeground=p["fg"], font=("Microsoft YaHei UI", 10),
                                  padx=8, pady=7, highlightthickness=1, highlightbackground=p["border"])
        w.recovery_button.configure(bg=p["panel"], fg=p["fg"], activebackground=p["border"],
                                    activeforeground=p["fg"], font=("Microsoft YaHei UI", 10),
                                    padx=8, pady=7)
        states = {"idle": "等待录音", "countdown": "即将录音", "recording": "正在聆听", "transcribing": "正在识别",
                  "reviewing": "文字审查", "candidate_review": "选择应用", "recovery": "重录或输入文字", "acting": "正在执行", "done": "处理完成", "failed": "需要留意"}
        self.badge.configure(text=f"●  {states.get(w.state, '语音工作台')}     ·     Ctrl+Alt+X 唤醒")

    def apply_theme(self):
        w = self.window
        self.p = p = PALETTES[self.settings["theme"]]
        color = self.settings["color"]
        for widget in (w.dialog, w.actions, self.header, self.controls, *self.rows):
            widget.configure(bg=p["bg"])
        for widget in (self.title, w.label):
            widget.configure(bg=p["bg"], fg=p["fg"])
        for widget in (w.meter_label, self.note, *self.labels):
            widget.configure(bg=p["bg"], fg=p["muted"])
        self.badge.configure(bg=p["panel"], fg=p["fg"])
        for widget in (self.custom_button, self.options_button):
            widget.configure(bg=p["panel"], fg=p["fg"], activebackground=p["border"], activeforeground=p["fg"])
        self.theme_button.set_theme(self.settings["theme"], p, color)
        w.meter.configure(bg=p["panel"], highlightthickness=1, highlightbackground=p["border"])
        w.text.configure(bg=p["panel"], fg=p["fg"], insertbackground=p["fg"],
                         selectbackground=color, selectforeground=contrast_text(color),
                         highlightthickness=1, highlightbackground=p["border"], highlightcolor=color)
        for widget in (w.auto_check, w.candidate_auto_check, *self.checks):
            widget.configure(bg=p["bg"], fg=p["fg"], selectcolor=p["panel"], activebackground=p["bg"], activeforeground=p["fg"])
        self.style.configure("Voice.TCombobox", fieldbackground=p["panel"], background=p["panel"],
                             foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["border"], padding=4)
        self.style.map("Voice.TCombobox", fieldbackground=[("readonly", p["panel"])],
                       foreground=[("readonly", p["fg"])], selectbackground=[("readonly", p["panel"])],
                       selectforeground=[("readonly", p["fg"])])
        w.candidate_panel.set_theme(bg=p["bg"], panel=p["panel"], foreground=p["fg"], accent=color, muted=p["muted"], border=p["border"])
        self.stage.set_palette(p, color)
        self.style_buttons()
        self.draw_meter(w.meter_history)
        w._queue_dialog_layout()

    def draw_meter(self, levels):
        canvas = self.window.meter
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        if width <= 2:
            width, height = 460, 160
        usable = max(1, width - 32)
        mode, color = self.settings["wave"], self.settings["color"]
        phase = time.monotonic() * 1.8 if self.settings["motion"] else 0.
        for index, (bar, dot, level) in enumerate(zip(self.window.meter_bars, self.dots, levels)):
            x = 16 + index * usable / max(1, len(levels) - 1)
            amplitude = 1 + max(0, min(1, level)) * min(40, height / 3)
            center = height / 2
            canvas.coords(bar, x - 2, center - amplitude, x + 2, center + amplitude)
            canvas.itemconfigure(bar, fill=color, state="normal" if mode == "音柱" else "hidden")
            y = center + math.sin(index * .6 + (phase if mode == "粒子" else 0)) * (max(12, amplitude * 1.4) if mode == "粒子" else amplitude)
            radius = 2 + max(0, min(1, level)) * 2
            if mode == "粒子":
                radius = 1.5 + max(0, min(1, level)) * 3
            canvas.coords(dot, x - radius, y - radius, x + radius, y + radius)
            canvas.itemconfigure(dot, fill=color, outline="", state="normal" if mode in ("光点", "粒子") else "hidden")

    def animate_particles(self):
        if self.settings["wave"] == "粒子" and self.window.meter.winfo_viewable():
            self.draw_meter(self.window.meter_history)

    def close(self):
        self.theme_button.close()
        self.stage.close()
