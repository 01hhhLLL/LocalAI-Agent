"""Scrollable candidate choices for the existing Tk voice window."""

from tkinter import BooleanVar, Button, Canvas, Checkbutton, Entry, Frame, IntVar, Radiobutton, Scrollbar, StringVar, Text


class CandidatePanel(Frame):
    def __init__(self, parent, *, bg, panel, foreground, on_change, on_layout):
        super().__init__(parent, bg=bg)
        self.bg, self.panel, self.foreground = bg, panel, foreground
        self.on_change, self.on_layout = on_change, on_layout
        self.choice = IntVar(self, value=-1)
        self.rows = []
        self.items = []
        self.layout_after = None
        self.canvas = Canvas(self, height=1, bg=bg, highlightthickness=0)
        self.scrollbar = Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = Frame(self.canvas, bg=bg)
        self.body_window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._resize_body)
        self.body.bind("<Configure>", self._body_changed)
        self.canvas.bind("<MouseWheel>", self._scroll)
        self.manual_entry = None
        self.manual_name = StringVar(self, value="")
        self.literal_name = BooleanVar(self, value=False)
        self.manual_name.trace_add("write", lambda *_args: self.on_change())

    def _scroll(self, event):
        self.on_change()
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            steps = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(steps, "units")
        return "break"

    def _bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self._scroll)

    def _resize_body(self, event):
        self.canvas.itemconfigure(self.body_window, width=max(1, event.width))
        self._queue_layout()

    def _body_changed(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.on_layout()

    def _queue_layout(self):
        if self.layout_after is None:
            self.layout_after = self.after_idle(self._measure_rows)

    def _measure_rows(self):
        self.layout_after = None
        for row in self.rows:
            count = row["text"].count("1.0", "end-1c", "displaylines")
            lines = 1 + (count[0] if count else 0)
            row["lines"] = lines
            row["text"].configure(height=lines if row["expanded"] else min(5, lines))
            if lines > 5:
                row["toggle"].configure(text="收起" if row["expanded"] else f"展开全部（{lines} 行）")
                row["toggle"].pack(anchor="e", padx=8, pady=(0, 6))
            else:
                row["toggle"].pack_forget()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.on_layout()

    def _toggle(self, row):
        self.on_change()
        row["expanded"] = not row["expanded"]
        self._queue_layout()

    def clear(self):
        if self.layout_after is not None:
            self.after_cancel(self.layout_after)
            self.layout_after = None
        for child in self.body.winfo_children():
            child.destroy()
        self.rows.clear()
        self.items.clear()
        self.manual_entry = None
        self.choice.set(-1)
        self.manual_name.set("")
        self.literal_name.set(False)
        self.canvas.yview_moveto(0)

    def show_candidates(self, candidates, *, complete_matches=False):
        self.clear()
        seen = set()
        if isinstance(candidates, list):
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                app_id, name = item.get("app_id"), item.get("display_name")
                if (not isinstance(app_id, str) or not 1 <= len(app_id) <= 100
                        or app_id in seen or not isinstance(name, str) or not name.strip()):
                    continue
                seen.add(app_id)
                self.items.append(item.copy())
                if not complete_matches and len(self.items) == 5:
                    break
        for index, item in enumerate(self.items):
            frame = Frame(self.body, bg=self.panel)
            frame.pack(fill="x", pady=(0, 6))
            radio = Radiobutton(
                frame, text=f"候选 {index + 1}", variable=self.choice, value=index,
                command=self._selection_changed, anchor="w", bg=self.panel,
                fg=self.foreground, selectcolor=self.bg, activebackground=self.panel,
                activeforeground=self.foreground, font=("Microsoft YaHei UI", 10))
            radio.pack(fill="x", padx=6, pady=(4, 0))
            text = Text(frame, height=1, width=1, wrap="word", relief="flat",
                        bg=self.panel, fg=self.foreground, padx=8, pady=4,
                        font=("Microsoft YaHei UI", 10), takefocus=False)
            value = item["display_name"]
            if item.get("product_version") and item["product_version"] != "0.0.0.0":
                value += "\n版本：" + str(item["product_version"])
            if item.get("description"):
                value += "\n程序说明：" + str(item["description"])
            if item.get("parent_name"):
                value += "\n所属：" + " → ".join(str(item[key]) for key in ("parent_name", "category", "folder") if item.get(key))
            if item.get("entry_path"):
                value += "\n入口：" + str(item["entry_path"])
            alias = item.get("matched_alias")
            if isinstance(alias, str) and alias.strip() and alias != value:
                value += f"\n匹配名称：{alias}"
            similarity = item.get("name_similarity")
            if type(similarity) in (int, float) and 0 <= similarity <= 1:
                value += f"\n名称字面相似度：{similarity:.1%}（非成功概率）"
            labels = {"local_alias": "本地批准别名", "exact_alias": "精确名称",
                      "sqlite_fts5_trigram": "全文检索", "bge_faiss_cpu": "语义检索",
                      "web_name_evidence": "联网名称证据", "installed_toolbox_catalog": "本机工具箱目录"}
            sources = item.get("sources", [])
            if isinstance(sources, list) and sources:
                value += "\n来源：" + "、".join(labels.get(s, s) for s in sources if isinstance(s, str))
            text.insert("1.0", value)
            text.configure(state="disabled")
            text.pack(fill="x")
            text.bind("<Button-1>", lambda _event, i=index: self._select(i))
            row = {"frame": frame, "radio": radio, "text": text,
                   "expanded": False, "lines": 1}
            row["toggle"] = Button(
                frame, command=lambda row=row: self._toggle(row), relief="flat",
                bg=self.panel, fg="#93c5fd", activebackground=self.panel,
                activeforeground=self.foreground, takefocus=True)
            for widget in (frame, radio, text, row["toggle"]):
                self._bind_scroll(widget)
            self.rows.append(row)
        manual = Frame(self.body, bg=self.bg)
        manual.pack(fill="x", pady=(2, 6))
        self.manual_radio = Radiobutton(
            manual, text="都不是，手动输入应用名称", variable=self.choice,
            value=len(self.items), command=self._selection_changed,
            anchor="w", bg=self.bg, fg=self.foreground, selectcolor=self.panel,
            activebackground=self.bg, activeforeground=self.foreground,
            font=("Microsoft YaHei UI", 10))
        self.manual_radio.pack(fill="x")
        self.manual_entry = Entry(
            manual, state="disabled", textvariable=self.manual_name,
            bg=self.panel, fg=self.foreground,
            insertbackground=self.foreground, disabledbackground=self.panel,
            disabledforeground="#aeb4bf", font=("Microsoft YaHei UI", 11))
        self.manual_entry.pack(fill="x", padx=8, pady=(3, 4))
        self.literal_check = Checkbutton(manual, text="按这个原名称查找，不自动纠错", variable=self.literal_name,
                              command=self.on_change, bg=self.bg, fg=self.foreground,
                              state="disabled",
                              selectcolor=self.panel, activebackground=self.bg,
                              activeforeground=self.foreground, font=("Microsoft YaHei UI", 10))
        self.literal_check.pack(anchor="w", padx=8)
        for widget in (manual, self.manual_radio, self.manual_entry):
            self._bind_scroll(widget)
        self._queue_layout()

    def _select(self, index):
        self.choice.set(index)
        self._selection_changed()

    def _selection_changed(self):
        manual = self.choice.get() == len(self.items)
        self.manual_entry.configure(state="normal" if manual else "disabled")
        self.literal_check.configure(state="normal" if manual else "disabled")
        if manual:
            self.manual_entry.focus_set()
        self.on_change()

    def selection(self):
        index = self.choice.get()
        if 0 <= index < len(self.items):
            return {"selected_app_id": self.items[index]["app_id"]}
        if index == len(self.items) and self.manual_entry is not None:
            name = self.manual_entry.get().strip()
            if 1 <= len(name) <= 100:
                return {"manual_name": name, **({"literal_name": True} if self.literal_name.get() else {})}
            raise ValueError("请输入 1 到 100 字的应用名称。")
        raise ValueError("请先选择一个候选，或选择“都不是”后输入名称。")

    def content_height(self):
        return self.body.winfo_reqheight()

    def set_viewport_height(self, height):
        self.canvas.configure(height=max(1, height))

    def set_theme(self, *, bg, panel, foreground, accent, muted, border):
        old_panel = self.panel
        self.bg, self.panel, self.foreground = bg, panel, foreground
        self.configure(bg=bg)
        self.canvas.configure(bg=bg)
        self.body.configure(bg=bg)

        def recolor(widget):
            kind = widget.winfo_class()
            if kind == "Frame":
                widget.configure(bg=panel if widget.cget("bg") == old_panel else bg)
            elif kind in ("Text", "Entry"):
                widget.configure(bg=panel, fg=foreground, insertbackground=foreground,
                                 highlightbackground=border, highlightcolor=accent, highlightthickness=1)
                if kind == "Entry":
                    widget.configure(disabledbackground=panel, disabledforeground=muted)
            elif kind == "Radiobutton":
                widget.configure(bg=panel, fg=foreground, selectcolor=panel,
                                 activebackground=panel, activeforeground=foreground)
            elif kind == "Checkbutton":
                widget.configure(bg=bg, fg=foreground, selectcolor=panel,
                                 activebackground=bg, activeforeground=foreground)
            elif kind == "Button":
                widget.configure(bg=panel, fg=foreground, activebackground=border, activeforeground=foreground)
            for child in widget.winfo_children():
                recolor(child)

        recolor(self.body)
        self.scrollbar.configure(bg=panel, activebackground=border, troughcolor=bg)
