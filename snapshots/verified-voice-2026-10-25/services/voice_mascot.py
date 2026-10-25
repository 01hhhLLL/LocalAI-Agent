"""Frame-based mascot behaviors confined to the voice window client area.

Interaction patterns reference VPet and Shimeji (see docs/voice-appearance.md).
This native adapter does not install their desktop overlays or global hooks.
"""

import math
import random
import time
from tkinter import Canvas, Menu, PhotoImage, TclError, Toplevel


class MascotStage:
    def __init__(self, owner, assets, settings, *, status, on_interaction, on_frame, bottom_inset, on_hide):
        self.owner, self.settings, self.status = owner, settings, status
        self.scheduler = owner.master
        self.overlay = Toplevel(owner)
        self.overlay.withdraw()
        self.overlay.overrideredirect(True)
        self.overlay.transient(owner)
        self.overlay.attributes("-toolwindow", True, "-transparentcolor", "#ff00ff")
        self.overlay.configure(bg="#ff00ff")
        self.canvas = canvas = Canvas(self.overlay, width=500, height=500, bg="#ff00ff", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self.owner_bindings = [(event, owner.bind(event, self.owner_changed, add="+"))
                               for event in ("<Configure>", "<Map>", "<Unmap>", "<FocusIn>")]
        self.geometry = None
        self.on_frame = on_frame
        self.bottom_inset = bottom_inset
        self.on_hide = on_hide
        self.on_interaction = on_interaction
        self.frames = {kind: [PhotoImage(master=canvas, file=str(assets / f"{kind}-{i}.png")) for i in range(8)]
                       for kind in ("normal", "chibi")}
        self.animations = {kind: {action: [PhotoImage(master=canvas, file=str(assets / f"{kind}-{action}-{i}.png"))
                                          for i in range(count)]
                                 for action, count in (("walk", 8), ("climb", 4), ("sleep", 2))}
                           for kind in self.frames}
        self.left_frames = {kind: [frame.subsample(-1, 1) for frame in frames] for kind, frames in self.frames.items()}
        self.left_animations = {kind: {action: [frame.subsample(-1, 1) for frame in frames]
                                       for action, frames in actions.items()} for kind, actions in self.animations.items()}
        self.pets = []
        self.dragged = None
        self.context_pet = None
        self.timer = None
        self.closed = False
        self.last_tick = time.monotonic()
        self.last_status = None
        self.palette = {"panel": "#211f30", "fg": "#f2effb", "border": "#39334d"}
        self.accent = "#7c3aed"
        self.add_pet()
        self.menu = Menu(canvas, tearoff=False)
        self.menu.add_command(label="复制桌宠", command=self.add_pet)
        self.menu.add_command(label="眨眼", command=lambda: self.react(self.context_pet, "blink"))
        self.menu.add_command(label="打个招呼", command=lambda: self.react(self.context_pet, "wave"))
        self.menu.add_command(label="走一走", command=lambda: self.react(self.context_pet, "walk"))
        self.menu.add_command(label="攀爬", command=lambda: self.react(self.context_pet, "climb"))
        self.menu.add_command(label="休息一下", command=lambda: self.react(self.context_pet, "sleep"))
        self.menu.add_separator()
        self.menu.add_command(label="移除这只桌宠", command=self.remove_context_pet)
        self.menu.add_command(label="只保留一只", command=self.keep_one)
        self.menu.add_command(label="暂时隐藏桌宠", command=self.hide_pets)
        self.canvas.tag_bind("mascot", "<ButtonPress-1>", self.press)
        self.canvas.tag_bind("mascot", "<B1-Motion>", self.drag)
        self.canvas.tag_bind("mascot", "<ButtonRelease-1>", self.release)
        self.canvas.tag_bind("mascot", "<Button-3>", self.context)
        self.canvas.tag_bind("mascot", "<Enter>", lambda _event: self.canvas.configure(cursor="hand2"))
        self.canvas.tag_bind("mascot", "<Leave>", lambda _event: self.canvas.configure(cursor=""))
        self.tick()

    def add_pet(self):
        # ponytail: cap clones at six to keep the small voice window responsive and readable.
        # A larger mascot playground can later expose a configurable cap.
        if len(self.pets) >= 6:
            self.react(self.context_pet or self.pets[-1], "wave", "最多六只，留点空间给文字吧")
            return
        pet = {"x": 370 - len(self.pets) * 44, "y": 150., "vx": random.choice((-1, 1)) * 24,
               "vy": 0., "state": "idle", "until": time.monotonic() + 3, "blink": time.monotonic() + random.uniform(2, 5),
               "clicks": 0, "dragged": False, "moved": False, "bubble_until": 0., "placed": False,
               "facing": 1, "rest_pending": False}
        pet["image"] = self.canvas.create_image(pet["x"], pet["y"], anchor="s", image=self.frames[self.kind][0], tags="mascot")
        pet["bubble"] = self.canvas.create_text(pet["x"], 15, text="", font=("Microsoft YaHei UI", 9), tags="mascot")
        self.pets.append(pet)
        self.refresh()

    @property
    def kind(self):
        return "normal" if self.settings["character"] == "正常" else "chibi"

    @property
    def active_frames(self):
        return self.frames[self.kind]

    def owner_changed(self, event):
        if event.widget is self.owner and not self.closed:
            self.sync_overlay()

    def sync_overlay(self):
        if self.closed:
            return False
        visible = self.owner.winfo_viewable() and self.owner.state() != "iconic" and self.settings["pet"]
        if not visible:
            self.overlay.withdraw()
            return False
        geometry = (self.owner.winfo_width(), self.owner.winfo_height(),
                    self.owner.winfo_rootx(), self.owner.winfo_rooty())
        if geometry != self.geometry:
            width, height, x, y = geometry
            self.overlay.geometry(f"{width}x{height}+{x}+{y}")
            self.geometry = geometry
        for index, pet in enumerate(self.pets):
            if not pet["placed"]:
                pet.update(x=geometry[0] - 90 - index * 44, y=self.floor, placed=True)
        # Follow the owner's alpha, including invisible offline test fixtures.
        alpha = self.owner.attributes("-alpha")
        if self.overlay.attributes("-alpha") != alpha:
            self.overlay.attributes("-alpha", alpha)
        if self.overlay.state() == "withdrawn":
            self.overlay.deiconify()
        # A freshly restored tool layer may not be mapped until the next Tk event.
        if not self.overlay.winfo_viewable() or not self.scheduler.tk.call("wm", "stackorder", self.overlay, "isabove", self.owner):
            self.overlay.lift(self.owner)
        return True

    @property
    def floor(self):
        height = self.owner.winfo_height() if self.owner.winfo_height() > 2 else 500
        return max(self.active_frames[0].height(), height - self.bottom_inset() - 3)

    def hide_pets(self):
        self.on_hide()

    def selected(self):
        current = self.canvas.find_withtag("current")
        return next((p for p in self.pets if current and current[0] in (p["image"], p["bubble"])), None)

    def press(self, event):
        pet = self.selected()
        if pet is None:
            return
        self.on_interaction()
        pet.update(dragged=True, moved=False, origin=(event.x, event.y), grab=(event.x - pet["x"], event.y - pet["y"]))
        self.dragged = pet
        self.react(pet, "blink")
        return "break"

    def drag(self, event):
        pet = self.dragged
        if pet is None:
            return
        if math.hypot(event.x - pet["origin"][0], event.y - pet["origin"][1]) > 5:
            pet["moved"] = True
        pet["x"], pet["y"] = event.x - pet["grab"][0], event.y - pet["grab"][1]
        pet["state"] = "drag"
        self.clamp(pet)
        self.render(pet, time.monotonic())
        return "break"

    def release(self, _event):
        pet = self.dragged
        if pet is None:
            return
        pet["dragged"] = False
        self.dragged = None
        if pet["moved"]:
            self.react(pet, "fall", "落地啦")
            pet["vy"] = 0
        else:
            reactions = [("wave", "你好呀"), ("blink", "我在听呢"), ("bounce", "一起把文字检查好"), ("wave", "今天也要顺顺利利")]
            state, text = reactions[pet["clicks"] % len(reactions)]
            pet["clicks"] += 1
            self.react(pet, state, text)
        self.owner.focus_set()
        return "break"

    def context(self, event):
        self.on_interaction()
        self.context_pet = self.selected()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    def react(self, pet, state, text=""):
        if pet is None or pet not in self.pets:
            return
        now = time.monotonic()
        if state == "sleep":
            pet["rest_pending"] = True
            if pet["y"] >= self.floor - 1 or not self.settings["motion"]:
                pet["y"] = self.floor
                state = "sleep_ground"
            else:
                state = "fall"
                pet["vy"] = 0
        elif state not in ("fall", "bounce", "sleep_ground"):
            pet["rest_pending"] = False
        duration = .7 if state == "bounce" else 8 if state == "sleep_ground" else random.uniform(1, 3) if state == "hang" else 2
        if state == "fall":
            duration = math.inf
            pet["vy"] = 0
        if state in ("climb", "climb_down"):
            distance = pet["y"] - self.active_frames[0].height() if state == "climb" else self.floor - pet["y"]
            duration = max(2, distance / 45 + .5)
        pet.update(state=state, since=now, until=now + duration, bubble_until=now + 2)
        if state == "walk":
            pet["vx"] = random.choice((-1, 1)) * 38
            pet["facing"] = 1 if pet["vx"] > 0 else -1
        if state in ("climb", "climb_down"):
            width = max(1, self.owner.winfo_width())
            pet["facing"] = -1 if pet["x"] < width / 2 else 1
            half = self.animations[self.kind]["climb"][0].width() / 2
            pet["x"] = half if pet["facing"] < 0 else width - half
        self.canvas.itemconfigure(pet["bubble"], text=text or {"sleep_ground": "Z z z", "climb": "往上走", "hang": "歇一会儿", "walk": "散散步"}.get(state, ""))
        self.render(pet, now)

    def remove_context_pet(self):
        if self.context_pet in self.pets:
            for key in ("image", "bubble"):
                self.canvas.delete(self.context_pet[key])
            self.pets.remove(self.context_pet)
            self.context_pet = None
            if not self.pets:
                self.on_hide()

    def keep_one(self):
        for pet in self.pets[1:]:
            self.context_pet = pet
            self.remove_context_pet()

    def clamp(self, pet, frame=None):
        frame = frame or self.frame_for(pet, time.monotonic())
        width = max(frame.width(), self.owner.winfo_width() if self.owner.winfo_width() > 2 else 500)
        pet["x"] = max(frame.width() / 2, min(width - frame.width() / 2, pet["x"]))
        pet["y"] = max(frame.height(), min(self.floor, pet["y"]))

    def render(self, pet, now):
        frame = self.frame_for(pet, now)
        self.clamp(pet, frame)
        state = pet["state"]
        dy = 0
        if self.settings["motion"] and not pet["dragged"]:
            if state == "bounce":
                progress = min(1, max(0, (now - pet["since"]) / .7))
                dy = -abs(math.sin(progress * math.pi * 2)) * 12 * (1 - progress)
            elif state not in ("sleep_ground", "idle", "hang"):
                dy = -abs(math.sin(now * 3)) * 2
        self.canvas.itemconfigure(pet["image"], image=frame, state="normal" if self.settings["pet"] else "hidden")
        self.canvas.coords(pet["image"], pet["x"], max(frame.height(), pet["y"] + dy))
        self.canvas.coords(pet["bubble"], pet["x"], max(12, pet["y"] - frame.height() - 5))
        self.canvas.itemconfigure(pet["bubble"], fill=self.palette["fg"], width=150,
                                  state="normal" if self.settings["pet"] and now < pet["bubble_until"] else "hidden")
        self.canvas.tag_raise("mascot")

    def frame_for(self, pet, now):
        state = pet["state"]
        left = pet["facing"] < 0
        frames = self.left_frames[self.kind] if left else self.frames[self.kind]
        actions = self.left_animations[self.kind] if left else self.animations[self.kind]
        elapsed = max(0, now - pet.get("since", now)) if self.settings["motion"] else 0
        if state in ("walk", "climb", "climb_down", "hang", "sleep_ground"):
            action = "sleep" if state == "sleep_ground" else "climb" if state in ("climb", "climb_down", "hang") else "walk"
            rate = 1.5 if action == "sleep" else 8 if action == "walk" else 5
            index = int(elapsed * rate) % len(actions[action])
            if state == "hang":
                index = len(actions[action]) - 1
            elif state == "climb_down":
                index = len(actions[action]) - index - 1
            return actions[action][index]
        blink = self.settings["motion"] and now >= pet["blink"] and now < pet["blink"] + .18
        if now > pet["blink"] + .18:
            pet["blink"] = now + random.uniform(2, 5)
        index = 1 if blink or state == "blink" else 2 if state in ("wave", "bounce") else 3 if state == "drag" else 0
        if state == "blink" and now > pet["until"] - 1.75:
            index = 0
        return frames[index]

    def tick(self):
        if self.closed:
            return
        self.timer = None
        now = time.monotonic()
        dt = min(.1, max(0, now - self.last_tick))
        self.last_tick = now
        visible = self.sync_overlay()
        status = self.status()
        if status != self.last_status:
            self.last_status = status
            messages = {"recording": "我在听呢", "transcribing": "正在整理声音", "reviewing": "文字可以修改哦",
                        "candidate_review": "请选择正确的应用", "acting": "正在执行", "done": "完成啦", "failed": "请检查一下提示"}
            if self.pets and status in messages:
                self.pets[0]["bubble_until"] = now + 2
                self.canvas.itemconfigure(self.pets[0]["bubble"], text=messages[status])
        for pet in self.pets:
            if visible and self.settings["motion"] and not pet["dragged"]:
                if now > pet["until"]:
                    if pet["state"] == "bounce":
                        state = "sleep_ground" if pet["rest_pending"] else "idle"
                    elif pet["state"] == "hang":
                        state = random.choice(("climb_down", "fall"))
                    else:
                        state = random.choice(("idle", "walk", "walk", "blink", "wave", "sleep", "climb"))
                    self.react(pet, state)
                    if state in ("idle", "walk", "blink", "wave"):
                        pet["until"] = now + random.uniform(2, 5)
                state = pet["state"]
                if state == "walk":
                    pet["x"] += pet["vx"] * dt
                    pet["y"] = min(self.floor, pet["y"] + 35 * dt)
                    frame = self.frame_for(pet, now)
                    if pet["x"] <= frame.width() / 2 or pet["x"] >= self.canvas.winfo_width() - frame.width() / 2:
                        pet["vx"] *= -1
                    pet["facing"] = 1 if pet["vx"] > 0 else -1
                elif state == "climb":
                    pet["y"] -= 45 * dt
                    if pet["y"] <= self.active_frames[0].height():
                        pet["y"] = self.active_frames[0].height()
                        self.react(pet, random.choice(("hang", "climb_down", "fall")))
                elif state == "climb_down":
                    pet["y"] += 45 * dt
                    if pet["y"] >= self.floor:
                        pet["y"] = self.floor
                        self.react(pet, "idle")
                elif state == "fall":
                    pet["vy"] += 180 * dt
                    pet["y"] += pet["vy"] * dt
                    if pet["y"] >= self.floor:
                        pet["y"] = self.floor
                        self.react(pet, "bounce")
            if visible:
                self.render(pet, now)
        self.on_frame()
        self.timer = self.scheduler.after(60 if visible and self.settings["motion"] else 300, self.tick)

    def refresh(self):
        if self.settings["pet"] and not self.pets:
            self.add_pet()
            return
        self.sync_overlay()
        now = time.monotonic()
        for pet in self.pets:
            self.render(pet, now)

    def set_palette(self, palette, accent):
        self.palette, self.accent = palette, accent
        if hasattr(self, "menu"):
            self.menu.configure(bg=palette["panel"], fg=palette["fg"], activebackground=accent, activeforeground="#ffffff")
        self.refresh()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            try:
                self.scheduler.after_cancel(self.timer)
            except TclError:
                pass
            self.timer = None
        for event, binding in self.owner_bindings:
            try:
                self.owner.unbind(event, binding)
            except TclError:
                pass
        try:
            self.overlay.destroy()
        except TclError:
            pass
