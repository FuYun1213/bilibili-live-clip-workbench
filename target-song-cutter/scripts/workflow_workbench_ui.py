"""Shared visual theme and live execution queue for the Tk workbench."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PureWindowsPath
import tkinter as tk
from tkinter import font as tkfont, ttk

import workflow_auto as auto


UI_FONT = "Microsoft YaHei UI"


def configure_windows_dpi() -> bool:
    """Declare system DPI awareness before creating Tk windows.

    Tk uses the real system DPI to render its text instead of Windows stretching
    an already rasterized 96-DPI window. System awareness matches Tk's scaling.
    """
    import os
    if os.name != "nt":
        return False
    try:
        import ctypes
        value = ctypes.c_int()
        if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0 and value.value:
            return True
        result = ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return result == 0
    except (AttributeError, OSError):
        try:
            import ctypes
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except (AttributeError, OSError):
            return False

COLORS = {
    "background": "#F3F6FB", "surface": "#FFFFFF", "ink": "#1E293B",
    "muted": "#64748B", "border": "#DCE4EF", "accent": "#2563EB",
    "accent_hover": "#1D4ED8", "accent_soft": "#EAF1FF", "green": "#16836A",
}
ACTION_LABELS = {
    "scan": "扫描录播", "watch": "自动监控", "batch": "历史批处理",
    "run-job": "执行任务", "run-jobs": "批量执行任务", "run-queue": "执行自动队列",
    "retry-codex": "重试选片", "continue-late-job": "继续处理后续分段",
    "quick-clip": "快速切片", "pause-job": "暂停任务", "resume-job": "恢复任务",
    "prioritize-job": "优先处理", "redo-job": "重做流程", "cleanup-job": "清理任务",
    "cleanup-jobs": "批量清理", "replace-published-job": "重新发布",
    "retry-publish-job": "重试发布", "retry-publish-jobs": "批量重试发布",
    "status": "刷新任务状态", "doctor": "检查环境", "step1": "转写与弹幕",
    "prompt": "生成选片提示", "import-selection": "导入选片结果",
    "step3": "生成切片与字幕", "step4": "烧录与审计", "preview": "生成投稿预览",
    "upload": "发布投稿", "expected-confirmation": "检查投稿确认",
}


def configure_workbench_style(root=None) -> ttk.Style:
    """Apply one readable, consistent theme to existing and new Tk controls."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    base_font = (UI_FONT, 11)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont"):
        try:
            tkfont.nametofont(name, root=root).configure(family=UI_FONT, size=11)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkFixedFont", root=root).configure(family="Consolas", size=11)
    except tk.TclError:
        pass
    rowheight = tkfont.nametofont("TkDefaultFont", root=root).metrics("linespace") + 6
    if root is not None:
        root.configure(background=COLORS["background"])
        root.option_add("*Font", base_font)
        root.option_add("*Text.background", COLORS["surface"])
        root.option_add("*Text.foreground", COLORS["ink"])
        root.option_add("*Text.selectBackground", COLORS["accent"])
        root.option_add("*Listbox.background", COLORS["surface"])
        root.option_add("*Listbox.foreground", COLORS["ink"])
        root.option_add("*Canvas.background", COLORS["background"])
    style.configure(".", font=base_font, background=COLORS["background"], foreground=COLORS["ink"])
    style.configure("TFrame", background=COLORS["background"])
    style.configure("TLabel", background=COLORS["background"], foreground=COLORS["ink"])
    style.configure("Title.TLabel", font=(UI_FONT, 19, "bold"))
    style.configure("Subtitle.TLabel", foreground=COLORS["muted"])
    style.configure("TLabelframe", background=COLORS["background"], bordercolor=COLORS["border"], relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=COLORS["background"], foreground=COLORS["ink"], font=(UI_FONT, 10, "bold"))
    for name in ("Auto.TLabelframe.Label", "Step.TLabelframe.Label"):
        style.configure(name, font=(UI_FONT, 11, "bold"))
    style.configure("TButton", font=base_font, padding=(8, 4), background=COLORS["surface"], foreground=COLORS["ink"], bordercolor=COLORS["border"], lightcolor=COLORS["surface"], darkcolor=COLORS["surface"], relief="flat", focusthickness=1, focuscolor=COLORS["accent"])
    style.map("TButton", background=[("disabled", "#F0F3F7"), ("pressed", "#DFE8F5"), ("active", "#EAF0F8")], foreground=[("disabled", "#9AA8B8")], bordercolor=[("focus", COLORS["accent"]), ("active", "#A8BDD8")])
    style.configure("Accent.TButton", background=COLORS["accent"], foreground="white", bordercolor=COLORS["accent"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"])
    style.map("Accent.TButton", background=[("disabled", "#C8D6F1"), ("pressed", "#1E40AF"), ("active", COLORS["accent_hover"])], foreground=[("disabled", "#F8FAFC"), ("!disabled", "white")], bordercolor=[("!disabled", COLORS["accent"])])
    style.configure("TMenubutton", padding=(10, 6), background=COLORS["surface"], bordercolor=COLORS["border"], relief="flat")
    style.configure("TNotebook", background=COLORS["background"], borderwidth=0, tabmargins=(0, 0, 0, 6))
    style.configure("TNotebook.Tab", font=(UI_FONT, 10, "bold"), padding=(20, 9), background="#E7EDF6", borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", COLORS["surface"]), ("active", "#EDF3FC")], foreground=[("selected", COLORS["accent"]), ("!selected", COLORS["muted"])])
    style.configure("Treeview", font=base_font, rowheight=rowheight, background=COLORS["surface"], fieldbackground=COLORS["surface"], foreground=COLORS["ink"], bordercolor=COLORS["border"], borderwidth=1, relief="flat")
    style.configure("Treeview.Heading", font=(UI_FONT, 10, "bold"), padding=(6, 4), background="#EDF2F8", foreground="#475569", bordercolor=COLORS["border"], relief="flat")
    style.map("Treeview", background=[("selected", "#DCEAFF")], foreground=[("selected", "#173D7A")])
    style.map("Treeview.Heading", background=[("active", "#E2EAF5")])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=(7, 5), fieldbackground=COLORS["surface"], bordercolor=COLORS["border"], lightcolor=COLORS["surface"], darkcolor=COLORS["surface"])
        style.map(name, bordercolor=[("focus", COLORS["accent"])])
    style.configure("TCheckbutton", padding=(2, 3), background=COLORS["background"])
    style.configure("Horizontal.TProgressbar", background=COLORS["accent"], troughcolor="#E5ECF5", borderwidth=0)
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background="#C6D3E3", troughcolor=COLORS["background"], borderwidth=0, arrowsize=12, relief="flat")
    style.configure("Card.TFrame", background=COLORS["surface"])
    style.configure("Card.TLabel", background=COLORS["surface"], foreground=COLORS["ink"])
    style.configure("CardTitle.TLabel", background=COLORS["surface"], foreground=COLORS["ink"], font=(UI_FONT, 12, "bold"))
    style.configure("CardMuted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
    style.configure("QueueHeading.TLabel", background=COLORS["surface"], foreground=COLORS["ink"], font=(UI_FONT, 10, "bold"))
    style.configure("QueueCount.TLabel", background=COLORS["accent_soft"], foreground=COLORS["accent"], padding=(7, 1), font=(UI_FONT, 9, "bold"))
    style.configure("QueueItem.TFrame", background="#F6F8FC")
    style.configure("QueueItem.TLabel", background="#F6F8FC", foreground=COLORS["ink"])
    style.configure("QueueAction.TLabel", background="#F6F8FC", foreground=COLORS["accent"], font=(UI_FONT, 9, "bold"))
    style.configure("QueueDetail.TLabel", background="#F6F8FC", foreground=COLORS["muted"], font=(UI_FONT, 9))
    return style


def _job_list(jobs) -> list[dict]:
    if isinstance(jobs, Mapping):
        if "jobs" in jobs:
            jobs = jobs["jobs"]
        if isinstance(jobs, Mapping):
            jobs = jobs.values()
    return [dict(job) for job in (jobs or ()) if isinstance(job, Mapping)]


def command_arguments(command: Iterable, option: str) -> list[str]:
    """Read repeated command options without exposing the full command line."""
    parts = [str(part) for part in command]
    values = []
    for index, part in enumerate(parts):
        if part == option and index + 1 < len(parts):
            values.append(parts[index + 1])
        elif part.startswith(option + "="):
            values.append(part[len(option) + 1:])
    return values


def command_action(command: Iterable) -> str:
    parts = [str(part) for part in command]
    script_index = next((i for i, part in enumerate(parts) if PureWindowsPath(part).name in {"workflow_auto.py", "workflow_app_core.py"}), -1)
    tail = parts[script_index + 1:]
    return next((part for part in tail if part in ACTION_LABELS), "")


def execution_command_item(record: Mapping, jobs_by_id: Mapping, *, pending: bool = False) -> dict:
    command = record.get("command") or ()
    action = command_action(command)
    job_ids = command_arguments(command, "--job-id")
    jobs = [jobs_by_id[job_id] for job_id in job_ids if job_id in jobs_by_id]
    titles = [str(jobs_by_id.get(job_id, {}).get("title") or job_id) for job_id in job_ids]
    if not titles:
        projects = command_arguments(command, "--project")
        if projects:
            titles = [PureWindowsPath(project).name or project for project in projects]
        elif action in {"run-queue", "watch", "scan", "batch", "run-jobs"}:
            titles = ["自动任务队列"]
        else:
            titles = [str(record.get("flow_key") or "工作台任务")]
    label = str(record.get("label") or ACTION_LABELS.get(action) or "执行操作")
    if action == "redo-job":
        flows = command_arguments(command, "--from-flow")
        if flows and not record.get("label"):
            label = f"从流程 {flows[0].removeprefix('flow')} 重做"
    publishes = action in {"replace-published-job", "retry-publish-job", "retry-publish-jobs", "upload"} or (
        action in {"run-job", "run-jobs", "run-queue"} and "--allow-upload" in command
    )
    # The child announces priority before acquiring the auto queue's state lock.
    # Until that lock is available, its persisted job still describes the old
    # failure. Other child output means execution has advanced, so retain that
    # attempt's real status (including a new platform failure).
    output_lines = str(record.get("output_tail") or "").splitlines()
    waiting_for_state = not any(
        line.strip() and not line.strip().startswith("手动操作已进入优先队列；")
        for line in output_lines
    )
    stages = []
    for job in jobs:
        stage = str(job.get("current_stage") or "").strip()
        if publishes and job.get("status") == "failed":
            if pending:
                stage = "等待再次投递"
            elif waiting_for_state:
                stage = "准备再次投递，等待任务状态更新"
        if stage and stage not in stages:
            stages.append(stage)
    return {"id": record.get("id"), "job_ids": job_ids, "action": label, "title": "\n".join(titles), "detail": "；".join(stages), "flow_key": str(record.get("flow_key") or "")}


def build_execution_snapshot(active_commands, pending_commands, jobs=None) -> dict:
    """Describe actual dispatcher records and persistent jobs without Tk or I/O."""
    job_list = _job_list(jobs)
    jobs_by_id = {str(job.get("id")): job for job in job_list if job.get("id") is not None}
    active_records = list(active_commands.values()) if isinstance(active_commands, Mapping) else list(active_commands or ())
    running = [execution_command_item(record, jobs_by_id) for record in active_records]
    represented = {job_id for item in running for job_id in item["job_ids"]}
    for job in job_list:
        job_id = str(job.get("id") or "")
        if job.get("status") == "running" and job_id not in represented:
            running.append({"id": f"auto:{job_id}", "job_ids": [job_id], "action": "自动任务运行中", "title": str(job.get("title") or job_id), "detail": str(job.get("current_stage") or "正在处理"), "flow_key": ""})
    pending = [execution_command_item(record, jobs_by_id, pending=True) for record in (pending_commands or ())]
    # Persistent status can still be queued while a local command starts or
    # waits. Show that job in its local section until the request leaves it.
    represented.update(job_id for item in pending for job_id in item["job_ids"])
    queued = [
        {"id": str(job.get("id") or ""), "job_ids": [str(job.get("id") or "")], "action": "自动队列接续", "title": str(job.get("title") or job.get("id") or "未命名任务"), "detail": str(job.get("current_stage") or "等待自动处理"), "flow_key": ""}
        for job in sorted(
            (job for job in job_list if job.get("status") == "queued"
             and str(job.get("id") or "") not in represented),
            key=auto.queue_order_key,
        )
    ]
    return {"running": running, "pending": pending, "queued": queued}


class WorkbenchUiMixin:
    """UI-only integration; the host owns scheduling and persists queue state."""

    def build_review_scroll_page(self, notebook, title):
        page = ttk.Frame(notebook)
        notebook.add(page, text=title)
        page.rowconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)
        canvas = tk.Canvas(page, width=300, height=180, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(page, orient="horizontal", command=canvas.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        content = ttk.Frame(canvas, padding=8)
        window = canvas.create_window(0, 0, window=content, anchor="nw")
        def resize(_event=None):
            canvas.itemconfigure(window, width=max(canvas.winfo_width(), content.winfo_reqwidth()))
            canvas.configure(scrollregion=canvas.bbox("all"))
        content.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)
        def scroll(event):
            widget = event.widget
            if isinstance(widget, (tk.Text, ttk.Treeview, ttk.Combobox)):
                return None
            while widget is not None:
                if widget == page:
                    if event.delta:
                        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                    return "break"
                widget = getattr(widget, "master", None)
            return None
        self.bind("<MouseWheel>", scroll, add="+")
        page.scroll_canvas = canvas
        page.scroll_content = content
        return page, content

    def configure_workbench_style(self) -> ttk.Style:
        return configure_workbench_style(self)

    def build_execution_panel(self, parent) -> ttk.Frame:
        panel = ttk.Frame(parent, width=318, style="Card.TFrame", padding=(14, 14))
        panel.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        self.execution_panel = panel
        header = ttk.Frame(panel, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="执行队列", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(header, text="运行进度与接下来要做的任务", style="CardMuted.TLabel", font=(UI_FONT, 9)).pack(anchor="w", pady=(3, 0))
        self.execution_summary_var = tk.StringVar(master=panel, value="")
        ttk.Label(panel, textvariable=self.execution_summary_var, style="CardMuted.TLabel", font=(UI_FONT, 9)).grid(row=1, column=0, sticky="w", pady=(10, 8))
        viewport = ttk.Frame(panel, style="Card.TFrame")
        viewport.grid(row=2, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, width=268, height=200, background=COLORS["surface"], borderwidth=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        canvas.configure(yscrollcommand=scrollbar.set)
        content = ttk.Frame(canvas, style="Card.TFrame")
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.columnconfigure(0, weight=1)
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: self._resize_execution_panel(event.width, window_id))
        self.execution_canvas = canvas
        self.execution_content = content
        self._execution_label_widgets = []
        self._execution_rendered_snapshot = None
        canvas.bind("<MouseWheel>", self._scroll_execution_panel)
        self.refresh_execution_panel()
        return panel

    def _resize_execution_panel(self, width: int, window_id: int) -> None:
        self.execution_canvas.itemconfigure(window_id, width=width)
        wrap = max(120, width - 24)
        for label in self._execution_label_widgets:
            label.configure(wraplength=wrap)

    def _scroll_execution_panel(self, event):
        canvas = self.execution_canvas
        if canvas.yview() != (0.0, 1.0):
            delta = int(getattr(event, "delta", 0))
            if delta:
                canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def refresh_execution_panel(self, jobs=None) -> dict:
        if jobs is not None:
            self._execution_jobs = _job_list(jobs)
        data = self.__dict__
        snapshot = build_execution_snapshot(data.get("active_commands", {}), data.get("pending_commands", ()), data.get("_execution_jobs", ()))
        self._execution_snapshot = snapshot
        if not data.get("execution_panel") or snapshot == data.get("_execution_rendered_snapshot"):
            return snapshot
        self._execution_rendered_snapshot = snapshot
        self.execution_summary_var.set(f"运行 {len(snapshot['running'])}  ·  待执行 {len(snapshot['pending'])}  ·  自动接续 {len(snapshot['queued'])}")
        old_y = self.execution_canvas.yview()[0]
        for widget in self.execution_content.winfo_children():
            widget.destroy()
        self._execution_label_widgets = []
        sections = (
            ("running", "正在执行", "当前没有正在执行的任务", ""),
            ("pending", "接下来", "本地执行队列为空", "按提交顺序排列，空位可用时自动接续"),
            ("queued", "自动队列", "没有等待自动处理的任务", "按优先级排列，由自动监控或队列执行器接续"),
        )
        width = max(240, self.execution_canvas.winfo_width())
        for index, (key, title, empty_text, hint) in enumerate(sections):
            section = ttk.Frame(self.execution_content, style="Card.TFrame")
            section.grid(row=index, column=0, sticky="ew", pady=(0 if index == 0 else 15, 0))
            section.columnconfigure(0, weight=1)
            heading = ttk.Frame(section, style="Card.TFrame")
            heading.grid(row=0, column=0, sticky="ew", pady=(0, 7))
            ttk.Label(heading, text=title, style="QueueHeading.TLabel").pack(side="left")
            ttk.Label(heading, text=str(len(snapshot[key])), style="QueueCount.TLabel").pack(side="right")
            row = 1
            if not snapshot[key]:
                label = ttk.Label(section, text=empty_text, style="CardMuted.TLabel", wraplength=width - 24, font=(UI_FONT, 9))
                label.grid(row=row, column=0, sticky="ew", pady=(0, 3))
                self._execution_label_widgets.append(label)
                label.bind("<MouseWheel>", self._scroll_execution_panel)
            else:
                for position, item in enumerate(snapshot[key], 1):
                    card = ttk.Frame(section, style="QueueItem.TFrame", padding=(10, 8))
                    card.grid(row=row, column=0, sticky="ew", pady=(0, 6))
                    card.columnconfigure(0, weight=1)
                    action = item["action"] if key == "running" else f"{position:02d}  {item['action']}"
                    for line_index, (text, style) in enumerate(((action, "QueueAction.TLabel"), (item["title"], "QueueItem.TLabel"), (item["detail"], "QueueDetail.TLabel"))):
                        if not text:
                            continue
                        label = ttk.Label(card, text=text, style=style, wraplength=width - 24, justify="left")
                        label.grid(row=line_index, column=0, sticky="ew", pady=(0 if line_index == 0 else 4, 0))
                        self._execution_label_widgets.append(label)
                        label.bind("<MouseWheel>", self._scroll_execution_panel)
                    card.bind("<MouseWheel>", self._scroll_execution_panel)
                    row += 1
            if hint and snapshot[key]:
                label = ttk.Label(section, text=hint, style="CardMuted.TLabel", font=(UI_FONT, 9), wraplength=width - 24)
                label.grid(row=row + 1, column=0, sticky="ew", pady=(1, 0))
                self._execution_label_widgets.append(label)
                label.bind("<MouseWheel>", self._scroll_execution_panel)
            for target in (section, heading, *heading.winfo_children()):
                target.bind("<MouseWheel>", self._scroll_execution_panel)
        self.execution_content.bind("<MouseWheel>", self._scroll_execution_panel)
        self.execution_canvas.update_idletasks()
        self.execution_canvas.configure(scrollregion=self.execution_canvas.bbox("all"))
        self.execution_canvas.yview_moveto(old_y)
        return snapshot
