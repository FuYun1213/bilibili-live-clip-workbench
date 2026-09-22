#!/usr/bin/env python3
"""Tkinter frontend for manual and unattended livestream clipping workflows."""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
import csv
import copy
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import time
import webbrowser
from datetime import datetime
import unicodedata
from pathlib import Path
from tkinter import Canvas, END, BOTH, LEFT, RIGHT, X, Y, BooleanVar, DoubleVar, Label, Listbox, Menu, MULTIPLE, StringVar, Tk, Toplevel
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import biliup_publish as publish
import creator_profiles
import cover_emotes
import local_publish
import publish_diagnostics
import review_workspace
import review_speaker_roles
import virtuareal_glossary as glossary
import validate_selection_tags as selection_tags
import workflow_app_core as core
from windows_process import hidden_subprocess_kwargs
import workflow_auto as auto
from workflow_workbench_ui import WorkbenchUiMixin, configure_windows_dpi, command_arguments, execution_command_item

try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # The editor remains usable without thumbnail support.
    Image = None
    ImageDraw = None
    ImageTk = None


APP_TITLE = "直播切片工作台"
UI_FONT = "Microsoft YaHei UI"
REVIEW_APPROVAL_UNDO_GRACE_MS = 8000
MODE_LABELS = {
    "普通切片": "narrative",
    "歌切": "song",
    "混合切片（普通+歌切）": "mixed",
}
CODEX_INSTALL_URL = "https://learn.chatgpt.com/docs/codex/cli"
COLLABORATION_TYPE_LABELS = {
    "单人": "single",
    "多人连麦": "collaboration",
    "同步试听": "reaction",
    "同看反应": "reaction",
}
COLLABORATION_TYPE_NAMES = {
    "single": "单人",
    "collaboration": "多人连麦",
    "reaction": "同步试听",
}
FLOW_ORDER = ("flow1", "flow2", "flow3", "flow4", "flow5")
FLOW_LABELS = {
    "flow1": "流程1｜转写与弹幕",
    "flow2": "流程2｜Codex 选片",
    "flow3": "流程3｜切片/字幕/封面",
    "flow4": "流程4｜烧录与审计",
    "flow5": "流程5｜预览与投稿",
}
REDO_FLOW_OPTIONS = {
    "流程 0｜重新校验、合并录播素材": "flow0",
    "流程 1｜重新转写与互动分析": "flow1",
    "流程 2｜重新生成 Prompt 与选片": "flow2",
    "流程 3｜重新生成视频、ASS 与封面": "flow3",
    "流程 4｜重新烧录与审计": "flow4",
    "流程 5｜重新生成投稿预览": "flow5",
}
STEP_STATUS_LABELS = {
    "pending": "未开始",
    "running": "进行中",
    "completed": "已完成",
    "published": "已发布",
    "failed": "失败",
}
JOB_STATUS_LABELS = {
    "live": "正在直播",
    "live_finalizing": "录播封口中",
    "queued": "排队中",
    "running": "处理中",
    "needs_creator": "待识别主播",
    "needs_codex": "等待 Codex",
    "awaiting_selection_review": "选片生成失败",
    "awaiting_delivery_review": "等待审核",
    "ready_to_publish": "等待投稿",
    "published": "已发布待清理",
    "cleaned_published": "已上传并清理",
    "cleaned_discarded": "已废弃并清理",
    "cleanup_failed": "清理未完成",
    "no_candidates": "无合格片段",
    "failed": "失败",
    "late_segment": "晚到分段暂停",
    "paused": "已暂停",
}

AUTO_JOB_UI_HIDDEN_STATUSES = frozenset({"cleaned_published", "cleaned_discarded"})
REVIEW_QUEUE_STATUSES = frozenset({"pending", "revise"})
MAX_PARALLEL_COMMANDS = 2
COOKIE_STATUS_LABELS = {
    "missing": "尚未创建",
    "invalid": "格式错误",
    "template": "模板未填写完整",
    "ready": "已登录，可投稿",
}


def split_monitor_detail(value: str, width_units: int = 44, *, measure=None) -> list[str]:
    """Wrap all text to the available width; preserve whitespace and explicit newlines.

    The UI supplies Font.measure and a pixel width. The default measure is useful
    for callers without Tk; neither path limits the number of output lines.
    """
    if measure is None:
        def measure(text):
            return sum(2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1 for char in text)
    width = max(1, int(width_units))
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        start = 0
        while start < len(paragraph):
            low, high = 1, len(paragraph) - start
            while low < high:
                middle = (low + high + 1) // 2
                if measure(paragraph[start:start + middle]) <= width:
                    low = middle
                else:
                    high = middle - 1
            # Always advance, including when one glyph is wider than the column.
            lines.append(paragraph[start:start + low])
            start += low
    return lines


def split_participant_names(value: str) -> list[str]:
    """Return stable, de-duplicated people from the review participant field."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,，、;；|\n]+", str(value or "")):
        name = raw.strip()
        key = re.sub(r"\s+", "", name).casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def filtered_subtitle_rows(rows: list[dict], query: str) -> list[dict]:
    """Filter subtitles by text, speaker, or visible time without changing order."""
    needle = str(query or "").strip().casefold()
    if not needle:
        return list(rows)
    return [
        row for row in rows
        if needle in " ".join(
            str(row.get(key, "")) for key in ("name", "start", "end", "text")
        ).casefold()
    ]


def composed_review_description(item: dict, profiles: dict[str, dict]) -> str:
    """Show the same creator-template description that publishing will use."""
    creator = str(item.get("creator") or "").strip()
    profile = profiles.get(creator, {})
    fixed = str(profile.get("upload", {}).get("description") or "").strip()
    lead = str(item.get("description") or "").strip()
    return publish.compose_description(fixed, lead)


def subtitle_preview_spec(
    *,
    display_name: str,
    role_color: str,
    subtitle_fill_color: str = "",
    subtitle_outline_color: str = "",
    subtitle_outline_width: float | str = creator_profiles.DEFAULT_SUBTITLE_OUTLINE_WIDTH,
    dialogue_font: str,
    dialogue_font_size: int | str,
    radio_subtitle_size: int | str,
    radio: bool = False,
) -> dict[str, object]:
    """Validate unsaved profile fields and return a canvas-friendly preview spec."""
    name = str(display_name).strip() or "主播名"
    color = creator_profiles.validate_color(role_color)
    fill_color = creator_profiles.validate_color(subtitle_fill_color or color)
    outline_color = creator_profiles.validate_color(
        subtitle_outline_color or creator_profiles.DEFAULT_SUBTITLE_OUTLINE_COLOR
    )
    font = str(dialogue_font).strip() or creator_profiles.DEFAULT_FONT
    normal_size = creator_profiles.validate_size(dialogue_font_size, "普通字幕字号")
    radio_size = creator_profiles.validate_size(radio_subtitle_size, "电台字幕字号")
    source_size = radio_size if radio else normal_size
    try:
        outline_width = float(str(subtitle_outline_width).strip())
    except ValueError as exc:
        raise ValueError("字幕描边宽度必须是数字") from exc
    if not 3 <= outline_width <= 8:
        raise ValueError("字幕描边宽度必须在 3–8 之间")
    canvas_size = max(14, min(48, round(source_size * 0.48)))
    scale = canvas_size / source_size
    return {
        "display_name": name,
        "role_color": color,
        "subtitle_fill_color": fill_color,
        "subtitle_outline_color": outline_color,
        "font": font,
        "source_size": source_size,
        "outline_width": outline_width,
        "canvas_size": canvas_size,
        "canvas_outline_width": max(1, round(outline_width * scale)),
        "canvas_shadow_offset": max(1, round(2.5 * scale)),
        "radio": bool(radio),
    }


def draw_outlined_canvas_text(
    canvas, x: float, y: float, text_value: str, **kwargs
) -> None:
    """Draw readable preview text without passing Tk duplicate fill options."""
    options = dict(kwargs)
    foreground = options.pop("fill", "#FFFFFF")
    outline = options.pop("outline", "#111111")
    outline_width = max(1, int(options.pop("outline_width", 2)))
    shadow_color = options.pop("shadow_color", "#000000")
    shadow_offset = max(0, int(options.pop("shadow_offset", 0)))
    if shadow_offset:
        canvas.create_text(
            x + shadow_offset,
            y + shadow_offset,
            text=text_value,
            fill=shadow_color,
            **options,
        )
    offsets = [
        (dx, dy)
        for dy in range(-outline_width, outline_width + 1)
        for dx in range(-outline_width, outline_width + 1)
        if (dx or dy) and dx * dx + dy * dy <= outline_width * outline_width + 1
    ]
    for dx, dy in offsets:
        canvas.create_text(
            x + dx, y + dy, text=text_value, fill=outline, **options
        )
    canvas.create_text(
        x, y, text=text_value, fill=foreground, **options
    )

def auto_job_row_tag(status: str, *, retry_pending: bool = False) -> str:
    """Give queued work a distinct color from human-review waiting states."""
    if status == "live":
        return "live"
    if status == "live_finalizing":
        return "queued"
    if retry_pending or status == "queued":
        return "queued"
    if status == "running":
        return "running"
    if status == "paused":
        return "paused"
    if status in {"failed", "cleanup_failed"}:
        return "failed"
    if status in {
        "published", "cleaned_published", "cleaned_discarded", "no_candidates",
    }:
        return "complete"
    return "waiting"


def review_queue_status(status: str) -> bool:
    return str(status or "pending") in REVIEW_QUEUE_STATUSES

def deferred_queue_display(job: dict, *, manual_active: bool, monitor_running: bool):
    if job.get("status") != "queued" or job.get("current_stage") != "手动流程优先，自动任务已避让":
        return None
    if manual_active:
        return "等待手动任务完成", "手动操作结束后，自动队列继续执行"
    if monitor_running:
        return "等待队列继续", "手动操作已结束，监控将在下一轮继续处理"
    return "监控已停止，等待启动", "当前没有手动任务；启动监控或执行已排队任务即可继续"


def auto_job_review_completion(job: dict) -> dict[str, object]:
    """Describe a finished human-review batch without mutating queue state."""
    if str(job.get("status", "")) != "awaiting_delivery_review":
        return {"complete": False, "approved": 0, "unresolved": 0, "review_dir": ""}
    try:
        project = Path(str(job.get("project", "")))
        _state_path, project_state = core.load_project(project)
        return auto.review_decision_summary(project_state)
    except Exception:
        return {"complete": False, "approved": 0, "unresolved": 0, "review_dir": ""}


def auto_job_review_display(
    summary: dict[str, object],
) -> tuple[str, str, str] | None:
    """Make the Flow 4 review gate visibly different from an active burn."""
    approved = int(summary.get("approved", 0) or 0)
    unresolved = int(summary.get("unresolved", 0) or 0)
    if bool(summary.get("complete")):
        if approved > 0:
            return (
                "审核已完成，待烧录发布",
                "待烧录发布",
                f"{approved} 条通过；选中本任务后点击“烧录并上传”",
            )
        return (
            "审核完成：没有通过稿",
            "审核完成",
            "全部候选均不通过；无需烧录或投稿",
        )
    if unresolved > 0:
        return (
            f"等待人工审核（{unresolved} 条未决定）",
            "等待审核",
            f"流程4尚未启动；审核台还有 {unresolved} 条需要选择通过、重做或不通过",
        )
    return None


def command_flow_key(command: list[str]) -> str:
    """Group commands so the same workflow queues while different flows can overlap."""
    names = [Path(str(part)).name.casefold() for part in command]
    if "workflow_app_core.py" in names:
        index = names.index("workflow_app_core.py")
        action = str(command[index + 1]).casefold() if index + 1 < len(command) else "core"
        flow = {
            "step1": "flow1",
            "prompt": "flow2",
            "import-selection": "flow2",
            "step3": "flow3",
            "step4": "flow4",
            "preview": "flow5",
            "upload": "flow5",
            "expected-confirmation": "flow5",
        }.get(action, action)
        return "manual:publish" if action == "upload" else f"manual:{flow}"
    if "workflow_auto.py" in names:
        index = names.index("workflow_auto.py")
        tail = [str(part).casefold() for part in command[index + 1:]]
        action = next(
            (
                value for value in tail
                if value in {
                    "scan", "watch", "batch", "run-job", "retry-codex",
                    "continue-late-job", "quick-clip", "pause-job", "resume-job", "prioritize-job",
                    "redo-job", "cleanup-job", "cleanup-jobs", "replace-published-job", "run-jobs", "run-queue", "status", "doctor",
                }
            ),
            "",
        )
        if action == "redo-job":
            try:
                flow = tail[tail.index("--from-flow") + 1]
            except (ValueError, IndexError):
                flow = "redo"
            return f"manual:{flow}"
        if action == "retry-codex":
            return "manual:flow2"
        if action == "continue-late-job":
            return "manual:flow0"
        if action == "quick-clip":
            return "manual:flow1"
        if action in {"pause-job", "resume-job", "prioritize-job"}:
            return "manual:queue-control"
        if action == "run-queue":
            return "manual:queue-run"
        if action in {"run-job", "run-jobs"}:
            return "manual:publish" if "--allow-upload" in tail else "manual:auto-run"
        if action == "replace-published-job":
            return "manual:publish"
        if action == "cleanup-job":
            return "automatic-queue"
        return "automatic-queue"
    return f"command:{names[0] if names else 'unknown'}"


def auto_job_drag_selection(
    visible_rows: list[str] | tuple[str, ...],
    anchor: str,
    target: str,
    *,
    base_selection: set[str] | None = None,
    mode: str = "replace",
) -> tuple[str, ...]:
    """Return the visible rows selected by a continuous mouse drag."""
    rows = list(visible_rows)
    if anchor not in rows or target not in rows:
        return tuple(row for row in rows if row in (base_selection or set()))
    first, last = sorted((rows.index(anchor), rows.index(target)))
    dragged = set(rows[first:last + 1])
    base = set(base_selection or set())
    if mode == "add":
        selected = base | dragged
    elif mode == "toggle":
        selected = base ^ dragged
    else:
        selected = dragged
    return tuple(row for row in rows if row in selected)




def visible_auto_jobs(state: dict) -> list[dict]:
    """Keep cleanup audit in queue state while removing finished rows from the UI."""
    return [
        job for job in state.get("jobs", {}).values()
        if str(job.get("status", "")) not in AUTO_JOB_UI_HIDDEN_STATUSES
    ]

def manual_progress_snapshot(state: dict | None) -> tuple[int, str, list[str]]:
    if not state:
        return 0, "0%｜尚未保存项目", ["未开始"] * len(FLOW_ORDER)
    steps = state.get("steps", {})
    progress = 0
    current = "等待开始"
    statuses: list[str] = []
    for index, name in enumerate(FLOW_ORDER, 1):
        status = str(steps.get(name, {}).get("status", "pending"))
        statuses.append(status)
        milestone = index * 20
        if status in {"completed", "published"}:
            progress = max(progress, milestone)
            current = f"{FLOW_LABELS[name]}已完成"
        elif status == "running":
            progress = max(progress, milestone - 10)
            current = f"{FLOW_LABELS[name]}进行中"
        elif status == "failed":
            progress = max(progress, milestone - 10)
            current = f"{FLOW_LABELS[name]}失败"
    progress = max(0, min(100, progress))
    return progress, f"{progress}%｜{current}", statuses


def auto_jobs_with_progress(jobs: list[dict]) -> list[dict]:
    """Use current attempt diagnostics everywhere without rewriting queue records."""
    return [{**job, **auto.job_progress_snapshot(job)} for job in jobs]


def local_publish_wait_display(job: dict, active_commands: dict, pending_commands) -> tuple[str, str] | None:
    """Mirror local retry cards without changing the persistent failure state."""
    if job.get("status") != "failed":
        return None
    job_id = str(job.get("id") or "")
    if not job_id:
        return None
    for pending, records in ((False, active_commands.values()), (True, pending_commands)):
        for record in records:
            if job_id not in command_arguments(record.get("command") or (), "--job-id"):
                continue
            item = execution_command_item(record, {job_id: job}, pending=pending)
            stage = item["detail"]
            # execution_command_item owns the publish-command and child-output
            # checks; reuse its result so the card and table cannot disagree.
            if stage == str(job.get("current_stage") or "").strip():
                return None
            detail = (
                "再次投递请求已排队；等待前面的本地任务完成后开始。"
                if pending else
                "再次投递进程已启动；正在准备或等待自动队列状态锁，取得执行权后会更新进度。"
            )
            return stage, detail
    return None


class Workbench(WorkbenchUiMixin, Tk):
    def __init__(self) -> None:
        super().__init__()
        self._configure_fonts()
        self.title(APP_TITLE)
        self._app_icon = None
        self._configure_icon()
        self.geometry("1720x980")
        self.minsize(1280, 760)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.output_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.monitor_process: subprocess.Popen[str] | None = None
        self._monitor_starting = False
        self.active_commands: dict[str, dict] = {}
        self.pending_commands: deque[dict] = deque()
        self.command_counter = 0
        self.task_processes: dict[int, subprocess.Popen[str]] = {}
        self._auto_xml_path = ""
        self.profiles = core.load_profiles()
        self.speaker_profile_labels = {
            f"{key} — {value['display_name']}": key
            for key, value in self.profiles.items()
            if not value.get("archived")
        }
        self.profile_labels = {
            f"{key} — {value['display_name']}": key
            for key, value in self.profiles.items()
            if not value.get("archived") and not value.get("guest_only")
        }
        self.creator_combobox = None
        default_creator = next(
            (label for label, key in self.profile_labels.items() if key == "sumire"),
            next(iter(self.profile_labels)),
        )
        self.vars = {
            "project": StringVar(
                value=str(
                    core.WORKSPACE_ROOT
                    / "workflow-projects"
                    / datetime.now().strftime("project-%Y%m%d")
                )
            ),
            "source": StringVar(),
            "xml": StringVar(),
            "creator": StringVar(value=default_creator),
            "mode": StringVar(value="普通切片"),
            "model": StringVar(value="local:qwen3-asr-auto"),
            "device": StringVar(value="cuda"),
            "compute_type": StringVar(value="float16"),
            "status": StringVar(value="就绪"),

            "watch_root": StringVar(value=str(core.WORKSPACE_ROOT / "录播")),
            "auto_output": StringVar(
                value=str(core.WORKSPACE_ROOT / "workflow-projects" / "自动队列")
            ),
            "codex_command": StringVar(),
            "selection_model": StringVar(value=auto.codex_model_label(
                auto.DEFAULT_CODEX_MODEL, auto.DEFAULT_CODEX_REASONING_EFFORT
            )),
            "stable_seconds": StringVar(value="120"),
            "quiet_seconds": StringVar(value="900"),
            "scheduled_delivery_time": StringVar(
                value=auto.DEFAULT_SCHEDULED_DELIVERY_TIME
            ),
            "batch_start": StringVar(value=datetime.now().strftime("%Y-%m-%d 00:00")),
            "batch_end": StringVar(value=datetime.now().strftime("%Y-%m-%d %H:%M")),
            "xml_status": StringVar(value="选择视频后自动匹配同名弹幕 XML"),
            "auto_status": StringVar(value="监控未启动"),
            "auto_selection_status": StringVar(value="未选择任务｜按住左键拖动可连续多选"),
            "review_dir": StringVar(),
            "review_show_approved": BooleanVar(value=False),
            "review_summary": StringVar(value="尚未载入审核批次"),
            "review_decision_status": StringVar(value="审核状态：未选择切片"),
            "review_start": StringVar(),
            "review_end": StringVar(),
            "review_notes": StringVar(),
            "review_revision_reason": StringVar(value="选择人工重做任务后显示理由"),
            "review_suggestions": StringVar(),
            "review_title": StringVar(),
            "review_subtitle": StringVar(),
            "review_cover_primary": StringVar(),
            "review_cover_secondary": StringVar(),
            "review_description": StringVar(),
            "review_tags": StringVar(),
            "review_collection": StringVar(),
            "review_season_id": StringVar(),
            "review_section_id": StringVar(),
            "review_tag_evidence": StringVar(),
            "review_collaboration_type": StringVar(value="单人"),
            "review_participants": StringVar(),
            "review_speaker_evidence": StringVar(),
            "review_guest_dialogue_verified": BooleanVar(value=False),
            "review_collaboration_status": StringVar(value="单人素材"),
            "review_speaker": StringVar(value="待确认"),
            "review_subtitle_query": StringVar(),
            "review_subtitle_match_status": StringVar(value="全部字幕"),
            "review_tag_status": StringVar(value="选择切片后可编辑投稿 Tag"),
            "review_line_status": StringVar(value="选择一条字幕后可校准"),
            "review_fuzzy_wrong": StringVar(),
            "review_fuzzy_replacement": StringVar(),
            "review_playback": StringVar(value="00:00.00 / 00:00.00｜滚轮移动｜Ctrl+滚轮缩放"),
            "review_speed": StringVar(value="1.0x"),
        }

        self.auto_burn = BooleanVar(value=False)
        self.auto_upload = BooleanVar(value=False)
        self.scheduled_delivery_enabled = BooleanVar(value=True)
        self.auto_backfill = BooleanVar(value=False)
        self.redo_flow_choice = StringVar(value=next(iter(REDO_FLOW_OPTIONS)))
        self.current_review_dir: Path | None = None
        self.current_review_video: Path | None = None
        self.current_review_media: Path | None = None
        self.current_review_ass: Path | None = None
        self.current_review_original_ass: Path | None = None
        self.review_proxy_jobs: set[str] = set()
        self.review_proxy_semaphore = threading.Semaphore(1)
        self.auto_job_row_to_job: dict[str, str] = {}
        self._auto_job_display_rows: list[tuple[str, tuple, str]] = []
        self._auto_job_row_anchors: dict[str, tuple[str, int]] = {}
        self._auto_job_render_signature = None
        self._auto_job_reflow_after = None
        self._auto_job_drag: dict | None = None
        self._subtitle_autosave_after = None
        self._subtitle_loading = False
        self._subtitle_autosave_busy = False
        self._review_edit_line: int | None = None
        self._subtitle_preserve_editor_line: int | None = None
        self._preview_segment_index: int | None = None
        self._pending_stream_extension: tuple[str, str, float] | None = None
        self._review_requested_source_start: float | None = None
        self._review_starting_source: float | None = None
        self._review_starting_until = 0.0
        self._review_jump_source: float | None = None
        self._review_jump_until = 0.0
        self._preview_media_path = ""
        self._preview_playback_generation = 0
        self._preview_at_timeline_end = False

        self.review_waveform_semaphore = threading.Semaphore(1)
        self.review_dialogue_rows: list[dict] = []
        self.review_cover_image = None
        self.preview_player: review_workspace.EmbeddedVlcPlayer | None = None
        self.review_item_map: dict[str, dict] = {}
        self.review_global_mode = True
        self.review_waveform_data: dict | None = None
        self.review_waveform_cache: dict[tuple[str, int, int], dict] = {}
        self.review_waveform_token = 0
        self.timeline_state_by_video: dict[str, dict] = {}
        self.timeline_state: dict | None = None
        self.timeline_selected_segment: int | None = None
        self.timeline_selection_kind: str | None = None
        self.timeline_undo: list[dict] = []
        self.timeline_undo_by_video: dict[str, list[dict]] = {}
        self.timeline_redo: list[dict] = []
        self.timeline_redo_by_video: dict[str, list[dict]] = {}
        self.timeline_zoom = 8.0
        self.timeline_drag: dict | None = None
        self.timeline_rendering = False
        self.timeline_rendering_video_key = ""
        self._timeline_approval_request: dict[str, str] | None = None
        self._review_continuation_after_ids: dict[str, str] = {}
        self._last_review_approval_undo: dict[str, str] | None = None
        glossary.load_correction_dictionary(create=True)
        self._load_auto_config()
        self._build_ui()
        self._streaming_reveal_busy = False
        self.vars["source"].trace_add("write", self._source_changed)
        self.vars["review_tags"].trace_add("write", self.refresh_review_tag_status)
        self.vars["review_tag_evidence"].trace_add("write", self.refresh_review_tag_status)
        self.vars["review_collaboration_type"].trace_add(
            "write", lambda *_args: self.refresh_review_collaboration_status()
        )
        self.vars["review_participants"].trace_add(
            "write", lambda *_args: self._review_participants_changed()
        )
        self.vars["review_guest_dialogue_verified"].trace_add(
            "write", lambda *_args: self.refresh_review_collaboration_status()
        )
        self.vars["review_subtitle_query"].trace_add(
            "write", lambda *_args: self.filter_review_subtitles()
        )
        self.after(120, self._drain_queue)
        self.vars["review_start"].trace_add("write", self._subtitle_field_changed)
        self.vars["review_end"].trace_add("write", self._subtitle_field_changed)
        self.vars["review_speaker"].trace_add("write", self._subtitle_field_changed)
        self.review_text_editor.bind("<<Modified>>", self._subtitle_text_modified)
        self.after(500, self._periodic_progress_refresh)

        self.bind_all("<Left>", self.mark_review_start_shortcut, add="+")
        self.bind_all("<Right>", self.mark_review_end_shortcut, add="+")
        self.bind_all("<space>", self.timeline_space_shortcut, add="+")
        self.bind_all("<KeyPress-c>", self.timeline_cut_shortcut, add="+")
        self.bind_all("<KeyPress-C>", self.timeline_cut_shortcut, add="+")
        self.bind_all("<Control-z>", self.timeline_undo_shortcut, add="+")
        self.bind_all("<Control-y>", self.timeline_redo_shortcut, add="+")
        self.bind_all("<Control-Y>", self.timeline_redo_shortcut, add="+")
        self.bind_all("<Control-Shift-Z>", self.timeline_redo_shortcut, add="+")
        self.bind_all("<Control-s>", self.save_workbench, add="+")
        self.bind_all("<Control-S>", self.save_workbench, add="+")
        self.after(100, self._review_playback_tick)
        self.after(300, self._warn_low_disk_space)
        self.after(900, self._auto_start_safe_monitor)
        self.after(1300, lambda: self.load_global_review_pool(select_tab=False))

    def _warn_low_disk_space(self) -> None:
        try:
            output_root = Path(self.vars["auto_output"].get()).expanduser().resolve()
            free = auto.ensure_heavy_job_disk_space(output_root)
            self.vars["auto_status"].set(
                f"磁盘可用 {free / (1024 ** 3):.2f} GB｜监控准备启动"
            )
        except auto.AutomationError as exc:
            self.vars["auto_status"].set(str(exc))
            messagebox.showwarning(APP_TITLE, str(exc), parent=self)
        except OSError as exc:
            self.vars["auto_status"].set(f"磁盘状态读取失败：{exc}")

    def _configure_icon(self) -> None:
        """Use the supplied image for the window and taskbar icon."""
        assets = Path(__file__).resolve().parents[1] / "assets"
        ico = assets / "workflow-app-icon.ico"
        png = assets / "workflow-app-icon.png"
        try:
            if os.name == "nt" and ico.is_file():
                self.iconbitmap(default=str(ico))
        except Exception:
            pass
        if Image is None or ImageTk is None or not png.is_file():
            return
        try:
            icon = Image.open(png).convert("RGBA")
            icon.thumbnail((256, 256))
            self._app_icon = ImageTk.PhotoImage(icon)
            self.iconphoto(True, self._app_icon)
        except Exception:
            self._app_icon = None
    def _configure_fonts(self) -> None:
        """Use a readable, consistent UI font across all windows."""
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                value = tkfont.nametofont(name)
                value.configure(family=UI_FONT, size=11)
            except Exception:
                continue
        tkfont.nametofont("TkFixedFont").configure(family="Consolas", size=11)
        self.option_add("*Font", (UI_FONT, 11))

    @property
    def manual_activity_marker(self) -> Path:
        output = Path(self.vars["auto_output"].get().strip()).expanduser()
        return output / auto.MANUAL_ACTIVITY_FILENAME

    def _sync_manual_activity_marker(self) -> None:
        flows = sorted({
            str(record["flow_key"])
            for record in [*self.active_commands.values(), *self.pending_commands]
            if str(record["flow_key"]).startswith("manual:")
        })
        marker = self.manual_activity_marker
        if flows:
            marker.parent.mkdir(parents=True, exist_ok=True)
            core.atomic_json(
                marker,
                {
                    "version": 1,
                    "pid": os.getpid(),
                    "flows": flows,
                    "updated_at": core.now_iso(),
                },
            )
            return
        try:
            payload = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = {}
        if int(payload.get("pid", -1) or -1) == os.getpid():
            marker.unlink(missing_ok=True)

    def _clear_manual_activity_marker(self) -> None:
        try:
            marker = self.manual_activity_marker
            payload = json.loads(marker.read_text(encoding="utf-8-sig"))
            if int(payload.get("pid", -1) or -1) == os.getpid():
                marker.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    @property
    def auto_config_path(self) -> Path:
        return core.WORKSPACE_ROOT / "workflow-auto.json"

    def _load_auto_config(self) -> None:
        if not self.auto_config_path.is_file():
            return
        try:
            value = auto.load_config(self.auto_config_path)
            self.vars["watch_root"].set(str(value["_watch_root"]))
            self.vars["auto_output"].set(str(value["_output_root"]))
            self.vars["codex_command"].set(str(value.get("codex_command", "")))
            self.vars["selection_model"].set(auto.codex_model_label(
                str(value.get("codex_model", auto.DEFAULT_CODEX_MODEL)),
                str(value.get("codex_reasoning_effort", auto.DEFAULT_CODEX_REASONING_EFFORT)),
            ))
            self.vars["stable_seconds"].set(str(value.get("stable_seconds", 120)))
            self.vars["quiet_seconds"].set(str(value.get("session_quiet_seconds", 900)))
            self.vars["mode"].set(next(label for label, mode in MODE_LABELS.items() if mode == value.get("mode", "narrative")))
            self.auto_burn.set(bool(value.get("allow_burn", False)))
            self.auto_upload.set(bool(value.get("allow_upload", False)))
            self.scheduled_delivery_enabled.set(
                bool(value.get("scheduled_delivery_enabled", True))
            )
            self.vars["scheduled_delivery_time"].set(
                str(
                    value.get(
                        "scheduled_delivery_time",
                        auto.DEFAULT_SCHEDULED_DELIVERY_TIME,
                    )
                )
            )
            self.auto_backfill.set(bool(value.get("backfill_existing", False)))
        except Exception:
            pass

    def _build_ui(self) -> None:
        self.configure_workbench_style()
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=(UI_FONT, 18, "bold"))
        style.configure("Step.TLabelframe.Label", font=(UI_FONT, 11, "bold"))
        style.configure("Auto.TLabelframe.Label", font=(UI_FONT, 11, "bold"))
        style.configure("TNotebook.Tab", font=(UI_FONT, 11, "bold"), padding=(18, 8))
        style.configure("Muted.TLabel", foreground="#64748B")
        style.configure("Review.TNotebook.Tab", font=(UI_FONT, 10), padding=(10, 4))
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=BOTH, expand=True)
        header = ttk.Frame(outer)
        self.app_header = header
        header.pack(fill=X, pady=(0, 4))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side=LEFT)
        ttk.Label(header, text="录播  /  剪辑  /  发布", style="Muted.TLabel").pack(side=RIGHT)
        self.app_caption = ttk.Label(outer, text="从录播到发布，在一个工作台完成。", style="Muted.TLabel")
        self.app_caption.pack(anchor="w", pady=(3, 10))

        # Reserve the bottom log before the large notebook requests its height.
        # Packing both as expanding siblings allowed the review tab's requested
        # size to push the log completely below the visible window.
        self.console_panel = ttk.Frame(outer, padding=(0, 3))
        self.console_panel.pack(side="bottom", fill=X, pady=(8, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=BOTH, expand=True)
        self.dashboard_tab = ttk.Frame(self.notebook, padding=10)
        self.review_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.dashboard_tab, text="任务与流程")
        self.notebook.add(self.review_tab, text="切片审核台")
        self._build_dashboard_tab(self.dashboard_tab)
        self._build_review_tab(self.review_tab)
        self.notebook.select(self.dashboard_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._update_workspace_layout)

        status = ttk.Frame(self.console_panel)
        status.pack(fill=X, pady=(0, 5))
        ttk.Button(status, text="刷新", command=self.refresh_workbench).pack(side=RIGHT)
        self.console_toggle_button = ttk.Button(
            status, text="收起终端", command=self.toggle_console_log
        )
        self.console_toggle_button.pack(side=RIGHT, padx=(0, 6))
        ttk.Button(status, text="保存（Ctrl+S）", command=self.save_workbench).pack(
            side=RIGHT, padx=(0, 6)
        )
        ttk.Button(status, text="取消运行/排队任务", command=self.cancel_process).pack(
            side=RIGHT, padx=6
        )
        self.status_label = ttk.Label(status, textvariable=self.vars["status"])
        self.status_label.pack(side=LEFT, fill=X, expand=True)

        self.log = ScrolledText(
            self.console_panel, wrap="word", height=3, font=(UI_FONT, 10),
            background="#101B2E", foreground="#DBE7F5", insertbackground="#FFFFFF",
            relief="flat", borderwidth=0, padx=10, pady=8
        )
        self.console_toggle_button.configure(text="展开终端")
        self.log.insert(
            END,
            "工作台已启动。任务与流程集中管理录播、队列和手动项目；切片审核台集中审片。\n",
        )

    def _update_workspace_layout(self, _event=None) -> None:
        review = self.notebook.select() == str(self.review_tab)
        status_label = self.__dict__.get("status_label")
        if status_label is not None:
            status_label.configure(textvariable=self.vars["review_line_status" if review else "status"])
        if review:
            self.app_header.pack_forget()
            self.app_caption.pack_forget()
        elif not self.app_header.winfo_manager():
            self.app_header.pack(fill=X, pady=(0, 4), before=self.notebook)
            self.app_caption.pack(anchor="w", pady=(3, 10), before=self.notebook)

    def toggle_console_log(self) -> None:
        """Show the full log in a separate window, preserving the editor's space."""
        popup = self.__dict__.get("_console_popup")
        if popup is not None and popup.winfo_exists():
            popup.destroy()
            self._console_popup = None
            self._console_popup_text = None
            self.console_toggle_button.configure(text="展开终端")
            return
        popup = Toplevel(self)
        self._console_popup = popup
        popup.title("运行日志 · 直播切片工作台")
        popup.geometry("900x380")
        viewer = ScrolledText(popup, wrap="word", font=("Consolas", 11),
            background="#101B2E", foreground="#DBE7F5", padx=10, pady=8)
        viewer.pack(fill=BOTH, expand=True)
        viewer.insert("1.0", self.log.get("1.0", "end-1c"))
        viewer.see(END)
        self._console_popup_text = viewer
        self.console_toggle_button.configure(text="关闭终端")
        popup.protocol("WM_DELETE_WINDOW", self.toggle_console_log)

    def _build_dashboard_tab(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=310)
        parent.rowconfigure(2, weight=1)
        toolbar = ttk.Frame(parent)
        self.dashboard_toolbar = toolbar
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        monitor_actions = ttk.Frame(toolbar)
        monitor_actions.pack(fill=X)
        for label, command in (
            ("启动自动监控", self.start_monitor),
            ("停止监控", self.stop_monitor),
            ("执行已排队任务", self.start_pending_queue),
        ):
            ttk.Button(monitor_actions, text=label, command=command).pack(side=LEFT, padx=(0, 6))
        self.dashboard_panel = ""
        self.dashboard_panel_buttons = {}
        self.dashboard_panel_labels = {
            "manual": "手动项目", "settings": "监控与处理设置", "batch": "历史批处理",
        }
        for name, label in reversed(list(self.dashboard_panel_labels.items())):
            button = ttk.Button(
                monitor_actions, text=f"展开{label}",
                command=lambda key=name: self.show_dashboard_panel(
                    "" if self.dashboard_panel == key else key
                ),
            )
            button.pack(side=RIGHT, padx=(6, 0))
            self.dashboard_panel_buttons[name] = button
        tools = ttk.Frame(toolbar)
        tools.pack(fill=X, pady=(7, 0))
        for label, command in (
            ("人物与字幕模板", self.open_creator_profile_manager),
            ("片头片尾", self.open_media_packaging_settings),
            ("热词设置", self.open_hotword_editor),
            ("纠错词典", self.open_correction_dictionary),
        ):
            ttk.Button(tools, text=label, command=command).pack(side=LEFT, padx=(0, 6))
        for label, actions in (
            ("投稿账号", (
                ("扫码登录", self.open_qr_login_dialog),
                ("创建 Cookie 模板", self.open_cookie_template_dialog),
            )),
            ("环境检查", (
                ("检查自动队列环境", self.doctor_auto),
                ("检查手动项目环境", lambda: self.run_core(["doctor", "--project", self.project_dir()])),
            )),
            ("打开目录", (
                ("录播目录", lambda: self.open_path(self.vars["watch_root"].get().strip())),
                ("自动项目目录", lambda: self.open_path(self.vars["auto_output"].get().strip())),
                ("手动项目目录", lambda: self.open_path(self.project_dir())),
            )),
        ):
            button = ttk.Menubutton(tools, text=label)
            menu = Menu(button, tearoff=0)
            for action_label, command in actions:
                menu.add_command(label=action_label, command=command)
            button.configure(menu=menu)
            button.pack(side=LEFT, padx=(0, 6))

        # Bound the optional editors so the queue remains visible on small screens.
        self.dashboard_panel_host = ttk.Frame(parent)
        self.dashboard_panel_host.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.dashboard_canvas = Canvas(
            self.dashboard_panel_host, height=260, highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(
            self.dashboard_panel_host, orient="vertical", command=self.dashboard_canvas.yview,
        )
        self.dashboard_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.dashboard_canvas.pack(side=LEFT, fill=X, expand=True)
        panel_content = ttk.Frame(self.dashboard_canvas)
        window_id = self.dashboard_canvas.create_window(0, 0, anchor="nw", window=panel_content)
        self.dashboard_canvas.bind(
            "<Configure>", lambda event: self.dashboard_canvas.itemconfigure(window_id, width=event.width),
        )
        panel_content.bind(
            "<Configure>", lambda _event: self.dashboard_canvas.configure(
                scrollregion=self.dashboard_canvas.bbox("all")
            ),
        )
        self.dashboard_panels = {}
        for name, builder in (
            ("manual", self._build_manual_panel),
            ("settings", self._build_monitor_settings),
            ("batch", self._build_batch_panel),
        ):
            frame = ttk.Frame(panel_content)
            self.dashboard_panels[name] = frame
            builder(frame)
            frame.bind("<Configure>", self._resize_dashboard_panel, add="+")
        self.dashboard_panel_host.grid_remove()
        self.bind("<MouseWheel>", self._scroll_dashboard_panel, add="+")
        self._build_auto_queue(parent)
        self.build_execution_panel(parent).grid(row=2, column=1, sticky="nsew", padx=(12, 0))
        parent.bind("<Configure>", self._resize_dashboard_panel, add="+")

    def show_dashboard_panel(self, name: str) -> None:
        if name and name not in self.dashboard_panels:
            raise ValueError(f"未知任务面板：{name}")
        for frame in self.dashboard_panels.values():
            frame.pack_forget()
        self.dashboard_panel = name
        if name:
            self.dashboard_panels[name].pack(fill=X)
            self.dashboard_panel_host.grid()
            self.dashboard_canvas.yview_moveto(0)
            self._resize_dashboard_panel()
        else:
            self.dashboard_panel_host.grid_remove()
        for key, button in self.dashboard_panel_buttons.items():
            verb = "收起" if key == name else "展开"
            button.configure(text=f"{verb}{self.dashboard_panel_labels[key]}")

    def _resize_dashboard_panel(self, event=None) -> None:
        # Reserve the queue's controls and at least a few visible task rows.
        queue = self.auto_job_tree.master.master
        chrome = queue.winfo_reqheight() - self.auto_job_tree.master.winfo_reqheight()
        available = self.dashboard_tab.winfo_height() - self.dashboard_toolbar.winfo_reqheight() - chrome - 220
        height = max(64, min(280, available))
        if self.dashboard_panel:
            requested = self.dashboard_panels[self.dashboard_panel].winfo_reqheight()
            if requested > 1:
                height = min(height, requested)
        if int(self.dashboard_canvas.cget("height")) != height:
            self.dashboard_canvas.configure(height=height)

    def _scroll_dashboard_panel(self, event):
        widget = event.widget
        while widget is not None:
            if widget == self.dashboard_panel_host:
                if event.delta:
                    self.dashboard_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            widget = getattr(widget, "master", None)
        return None

    def _build_manual_panel(self, parent) -> None:
        config = ttk.LabelFrame(parent, text="项目设置", padding=10)
        config.pack(fill=X)
        self._path_row(config, 0, "项目文件夹", "project", self._choose_project, self.load_project)
        self._path_row(config, 1, "录播视频/音视频", "source", self._choose_source)
        self._path_row(config, 2, "弹幕 XML（自动匹配，可改）", "xml", self._choose_xml)
        ttk.Label(
            config,
            textvariable=self.vars["xml_status"],
            foreground="#555555",
        ).grid(row=3, column=1, columnspan=3, sticky="w", padx=6, pady=(0, 3))

        options = ttk.Frame(config)
        options.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 2))
        ttk.Label(options, text="主播").pack(side=LEFT)
        self.creator_combobox = ttk.Combobox(
            options, textvariable=self.vars["creator"], values=list(self.profile_labels),
            state="readonly", width=25,
        )
        self.creator_combobox.pack(side=LEFT, padx=(6, 12))
        ttk.Label(options, textvariable=self.vars["mode"]).pack(side=LEFT, padx=(0, 12))
        ttk.Label(options, text="模式和 ASR 参数在“监控与处理设置”中共用", foreground="#555555").pack(side=LEFT)
        ttk.Button(options, text="保存/更新项目", command=self.save_project).pack(side=RIGHT)
        config.columnconfigure(1, weight=1)

        steps = ttk.Frame(parent)
        steps.pack(fill=BOTH, expand=True, pady=(12, 0))
        steps.columnconfigure(0, weight=1)
        self._step_box(
            steps,
            0,
            "流程 1｜后台转写",
            "转写录播自身音轨；有 XML 时分析弹幕与 SC。自动队列会先由流程0准备连续母版。",
            [
                ("开始后台转写", lambda: self.run_core(["step1", "--project", self.project_dir()])),
                ("打开转写", lambda: self.open_path(Path(self.project_dir()) / "analysis")),
            ],
        )
        self._step_box(
            steps,
            1,
            "流程 2｜Prompt 与选片",
            self.flow2_prompt_description(),
            [
                (
                    "生成 Prompt",
                    lambda: self.run_core(
                        ["prompt", "--project", self.project_dir()],
                        after=self.open_selection_folder,
                    ),
                ),
                ("导入选片 JSON", self.choose_selection_json),
                ("复制 Prompt", self.copy_prompt),
            ],
        )
        self._step_box(
            steps,
            2,
            "流程 3｜切片/ASS/封面",
            "按时间戳导出，成片轴重转写 ASS，并生成同目录封面。",
            [
                (
                    "生成审核素材",
                    lambda: self.run_core(
                        ["step3", "--project", self.project_dir()], after=self.open_review_folder
                    ),
                ),
                ("打开审核台", self.open_review_folder),
            ],
        )
        self._step_box(
            steps,
            3,
            "流程 4｜烧录与审计",
            "可人工确认后烧录，也可明确授权跳过人工审核；两种模式都会执行最终媒体审计。",
            [("开始烧录", self.start_burn), ("打开交付目录", self.open_delivery_folder)],
        )
        self._step_box(
            steps,
            4,
            "流程 5｜预览与投稿",
            "先检查投稿预览，再确认公开上传。队列任务使用下方“烧录并上传”。",
            [
                ("生成投稿预览（手动项目）", lambda: self.run_core(["preview", "--project", self.project_dir()])),
                ("执行上传（手动项目）", self.start_upload),
            ],
        )

    def _build_processing_settings(self, parent) -> None:
        options = ttk.LabelFrame(parent, text="处理参数（自动队列与手动项目共用）", padding=10)
        options.pack(fill=X, pady=(0, 8))
        specs = (
            ("模式", "mode", list(MODE_LABELS), "readonly"),
            ("ASR 模型", "model", ["local:qwen3-asr-auto", "local:qwen3-asr-1.7b", "local:qwen3-asr-0.6b", "large-v3-turbo", "qwen-audio-3.0-asr-flash-filetrans"], "normal"),
            ("选片模型", "selection_model", list(dict.fromkeys([
                *auto.CODEX_MODEL_PRESETS, self.vars["selection_model"].get()
            ])), "readonly"),
            ("设备", "device", ["cuda", "cpu"], "readonly"),
            ("精度", "compute_type", ["float16", "int8", "int8_float16"], "normal"),
        )
        for index, (label, key, values, state) in enumerate(specs):
            row, pair = divmod(index, 2)
            column = pair * 2
            ttk.Label(options, text=label).grid(row=row, column=column, sticky="w", pady=3)
            combo = ttk.Combobox(options, textvariable=self.vars[key], values=values, state=state, width=28)
            combo.grid(row=row, column=column + 1, sticky="ew", padx=(8, 16), pady=3)
            options.columnconfigure(column + 1, weight=1)
            if key == "device":
                combo.bind("<<ComboboxSelected>>", self._device_changed)
            elif key == "selection_model":
                combo.bind("<<ComboboxSelected>>", self._selection_model_changed)
        ttk.Label(options, text="选片模型切换后立即保存，下一次选片调用生效。", foreground="#555555").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(5, 0)
        )

    def _selection_model_changed(self, _event=None) -> None:
        label = self.vars["selection_model"].get()
        pair = auto.CODEX_MODEL_PRESETS.get(label)
        if pair is None:
            return
        previous = (auto.DEFAULT_CODEX_MODEL, auto.DEFAULT_CODEX_REASONING_EFFORT)
        try:
            payload = (
                json.loads(self.auto_config_path.read_text(encoding="utf-8-sig"))
                if self.auto_config_path.is_file() else auto.default_config()
            )
            previous = (
                str(payload.get("codex_model", previous[0])),
                str(payload.get("codex_reasoning_effort", previous[1])),
            )
            payload["codex_model"], payload["codex_reasoning_effort"] = pair
            auto.save_config(self.auto_config_path, payload)
        except Exception as exc:
            self.vars["selection_model"].set(auto.codex_model_label(*previous))
            messagebox.showerror(APP_TITLE, f"选片模型未保存：{exc}")
            return
        for widget in self.__dict__.get("_flow2_description_labels", []):
            widget.configure(text=self.flow2_prompt_description())
        self.vars["status"].set(f"选片模型已保存：{label}；下一次调用生效")
        self.append_log(f"选片模型已切换为 {pair[0]} / {pair[1]}，正在运行的任务继续完成。\n")

    def _build_monitor_settings(self, parent) -> None:
        self._build_processing_settings(parent)
        monitor = ttk.LabelFrame(parent, text="录播目录自动队列", style="Auto.TLabelframe", padding=12)
        monitor.pack(fill=X)
        self._path_row(monitor, 0, "监控的录播文件夹", "watch_root", self._choose_watch_root)
        self._path_row(monitor, 1, "自动项目输出目录", "auto_output", self._choose_auto_output)
        ttk.Label(monitor, text="Codex CLI（可空）", width=18).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(monitor, textvariable=self.vars["codex_command"]).grid(
            row=2, column=1, sticky="ew", padx=6, pady=3
        )
        ttk.Button(monitor, text="选择", command=self._choose_codex, width=8).grid(
            row=2, column=2, pady=3
        )
        ttk.Button(monitor, text="安装说明", command=lambda: webbrowser.open(CODEX_INSTALL_URL), width=9).grid(
            row=2, column=3, padx=(6, 0), pady=3
        )
        monitor.columnconfigure(1, weight=1)

        timing = ttk.Frame(monitor)
        timing.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        ttk.Label(timing, text="文件稳定秒数").pack(side=LEFT)
        ttk.Entry(timing, textvariable=self.vars["stable_seconds"], width=8).pack(side=LEFT, padx=(6, 18))
        ttk.Label(timing, text="断线重连观察秒数").pack(side=LEFT)
        ttk.Entry(timing, textvariable=self.vars["quiet_seconds"], width=8).pack(side=LEFT, padx=(6, 18))
        ttk.Label(timing, text="同直播间出现新分段或仍在写入时会重新计时；倒计时结束后才进入流程0。", foreground="#555555").pack(side=LEFT)

        permissions = ttk.Frame(monitor)
        permissions.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 3))
        ttk.Checkbutton(
            permissions,
            text="允许未人工审核任务自动烧录",
            variable=self.auto_burn,
            command=lambda: self._auto_permission_changed("burn"),
        ).pack(side=LEFT)
        ttk.Checkbutton(
            permissions,
            text="允许未人工审核任务直接自动投稿",
            variable=self.auto_upload,
            command=lambda: self._auto_permission_changed("upload"),
        ).pack(side=LEFT, padx=(18, 0))
        ttk.Checkbutton(
            permissions,
            text="首次处理已有素材（默认只处理以后新增）",
            variable=self.auto_backfill,
        ).pack(side=LEFT, padx=(18, 0))

        delivery_schedule = ttk.Frame(monitor)
        delivery_schedule.grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=(6, 3)
        )
        ttk.Checkbutton(
            delivery_schedule,
            text="每天定时批量烧录并上传",
            variable=self.scheduled_delivery_enabled,
        ).pack(side=LEFT)
        ttk.Label(delivery_schedule, text="本机时间").pack(side=LEFT, padx=(18, 0))
        ttk.Entry(
            delivery_schedule,
            textvariable=self.vars["scheduled_delivery_time"],
            width=7,
        ).pack(side=LEFT, padx=(6, 12))
        ttk.Label(
            delivery_schedule,
            text="默认 17:00；只控制自动监控，且仍需上方本次烧录/投稿授权",
            foreground="#555555",
        ).pack(side=LEFT)

    def _build_batch_panel(self, parent) -> None:
        batch = ttk.LabelFrame(parent, text="按录播时间处理历史素材", padding=12)
        batch.pack(fill=X)
        ttk.Label(batch, text="批量开始（本机，含）").pack(side=LEFT)
        ttk.Entry(batch, textvariable=self.vars["batch_start"], width=19).pack(
            side=LEFT, padx=(6, 14)
        )
        ttk.Label(batch, text="批量结束（本机，含）").pack(side=LEFT)
        ttk.Entry(batch, textvariable=self.vars["batch_end"], width=19).pack(
            side=LEFT, padx=(6, 14)
        )
        ttk.Button(
            batch,
            text="批量处理这个时间段",
            command=self.start_batch,
        ).pack(side=LEFT)

        ttk.Label(
            parent, text="输入本机时间；文件名按 UTC+8 换算。只处理已稳定的完整场次，已有任务自动复用。",
            foreground="#555555", wraplength=1000,
        ).pack(anchor="w", pady=(6, 0))

    def _build_auto_queue(self, parent) -> None:
        explanation = ttk.LabelFrame(parent, text="全部任务", padding=10)
        explanation.grid(row=2, column=0, sticky="nsew")
        explanation.columnconfigure(0, weight=1)
        explanation.rowconfigure(1, weight=1)
        queue_header = ttk.Frame(explanation)
        queue_header.grid(row=0, column=0, sticky="ew")
        queue_status = ttk.Label(
            queue_header, textvariable=self.vars["auto_status"], foreground="#555555",
            wraplength=1100,
        )
        queue_status.pack(fill=X)
        queue_header.bind("<Configure>", lambda event: queue_status.configure(wraplength=max(200, event.width - 8)))
        queue_header.bind("<Configure>", self._resize_dashboard_panel, add="+")

        table = ttk.Frame(explanation)
        table.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        columns = ("started", "creator", "title", "stage", "progress", "status", "detail")
        self.auto_job_tree = ttk.Treeview(
            table,
            columns=columns,
            show="headings",
            height=8,
            selectmode="extended",
        )
        headings = {
            "started": "直播时间（本机）",
            "creator": "主播",
            "title": "场次/标题",
            "stage": "当前工作",
            "progress": "进度",
            "status": "状态",
            "detail": "最近说明",
        }
        widths = {
            "started": 132,
            "creator": 95,
            "title": 165,
            "stage": 170,
            "progress": 60,
            "status": 110,
            "detail": 260,
        }
        for name in columns:
            self.auto_job_tree.heading(name, text=headings[name])
            self.auto_job_tree.column(
                name,
                width=widths[name],
                minwidth=60,
                stretch=name in {"title", "detail"},
                anchor="w" if name != "progress" else "center",
            )
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.auto_job_tree.yview)
        horizontal = ttk.Scrollbar(
            explanation, orient="horizontal", command=self.auto_job_tree.xview
        )
        self.auto_job_tree.configure(
            yscrollcommand=scrollbar.set,
            xscrollcommand=horizontal.set,
        )
        scrollbar.pack(side=RIGHT, fill=Y)
        self.auto_job_tree.pack(side=LEFT, fill=BOTH, expand=True)
        horizontal.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        ttk.Label(
            explanation,
            textvariable=self.vars["auto_selection_status"],
            foreground="#275D8C",
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))

        selected_actions = ttk.Frame(explanation, padding=(0, 2))
        selected_actions.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        action_row = ttk.Frame(selected_actions)
        action_row.pack(fill=X)
        for index, (label, command) in enumerate((
            ("打开审核台", self.open_selected_auto_review),
            ("暂停 / 继续", self.toggle_pause_selected_auto_job),
            ("移到队首", self.prioritize_selected_auto_job),
            ("烧录并上传", self.publish_selected_auto_job),
            ("失败投稿再次投递", self.retry_selected_failed_publish),
            ("打开交付文件夹", self.open_selected_publish_preview),
        )):
            ttk.Button(action_row, text=label, command=command,
                       style="Accent.TButton" if index == 3 else "TButton").grid(
                row=index // 4, column=index % 4, sticky="ew", padx=(0, 5), pady=(0, 5))
        more_button = ttk.Menubutton(action_row, text="更多操作")
        more_menu = Menu(more_button, tearoff=0)
        more_menu.add_command(label="分析晚到分段", command=self.continue_selected_late_segments)
        more_menu.add_command(label="重新调用 Codex", command=self.retry_selected_auto_codex)
        more_menu.add_separator()
        more_menu.add_command(label="废弃并清理", command=self.discard_selected_auto_job)
        more_button.configure(menu=more_menu)
        more_button.grid(row=1, column=2, sticky="ew", padx=(0, 5), pady=(0, 5))
        redo_row = ttk.Frame(selected_actions)
        redo_row.pack(fill=X, pady=(6, 0))
        ttk.Label(redo_row, text="重做起点").pack(side=LEFT)
        ttk.Combobox(
            redo_row, textvariable=self.redo_flow_choice, values=list(REDO_FLOW_OPTIONS),
            state="readonly", width=30,
        ).pack(side=LEFT, padx=(6, 4))
        ttk.Button(redo_row, text="从这里重做", command=self.redo_selected_auto_flow).pack(side=LEFT)
        ttk.Label(
            redo_row, text="Ctrl 多选 · 双击审核",
            foreground="#555555",
        ).pack(side=LEFT, padx=(12, 0))
        self.auto_job_tree.bind("<Configure>", self._schedule_auto_job_reflow, add="+")
        self.auto_job_tree.bind("<<ThemeChanged>>", self._schedule_auto_job_reflow, add="+")
        self.auto_job_tree.bind("<ButtonPress-1>", self.begin_auto_job_drag)
        self.auto_job_tree.bind("<B1-Motion>", self.drag_select_auto_jobs)
        self.auto_job_tree.bind("<ButtonRelease-1>", self.finish_auto_job_drag)
        self.auto_job_tree.bind("<<TreeviewSelect>>", self.update_auto_job_selection_status)
        self.auto_job_tree.bind("<Double-1>", self.open_selected_auto_review)
        self.auto_job_tree.bind("<Button-3>", self.show_auto_job_context_menu)
        self.auto_job_tree.tag_configure(
            "live", background="#E3FFF1", foreground="#006B3C"
        )
        self.auto_job_tree.tag_configure("running", background="#E8F2FF")
        self.auto_job_tree.tag_configure(
            "queued", background="#EEE7FF", foreground="#4D2B7A"
        )
        self.auto_job_tree.tag_configure("waiting", background="#FFF7DD")
        self.auto_job_tree.tag_configure(
            "paused", background="#ECEFF1", foreground="#546E7A"
        )
        self.auto_job_tree.tag_configure("failed", background="#FFE8E8")
        self.auto_job_tree.tag_configure("complete", background="#E9F7EC")

    @staticmethod
    def _bind_multiline_variable(widget: ScrolledText, variable: StringVar) -> None:
        """Keep a Tk Text widget and StringVar in sync without disturbing typing."""
        syncing = {"active": False}

        def variable_changed(*_args) -> None:
            if syncing["active"] or not widget.winfo_exists():
                return
            target = variable.get()
            current = widget.get("1.0", "end-1c")
            if current == target:
                return
            syncing["active"] = True
            try:
                widget.delete("1.0", END)
                widget.insert("1.0", target)
                widget.edit_modified(False)
            finally:
                syncing["active"] = False

        def widget_changed(_event=None) -> None:
            if syncing["active"] or not widget.edit_modified():
                return
            syncing["active"] = True
            try:
                value = widget.get("1.0", "end-1c")
                if variable.get() != value:
                    variable.set(value)
                widget.edit_modified(False)
            finally:
                syncing["active"] = False

        variable.trace_add("write", variable_changed)
        widget.bind("<<Modified>>", widget_changed, add="+")
        variable_changed()

    def _build_review_tab(self, parent) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=X)
        ttk.Button(toolbar, text="刷新审核列表", command=self.load_global_review_pool).pack(side=LEFT)
        ttk.Checkbutton(toolbar, text="显示通过历史", variable=self.vars["review_show_approved"],
            command=self.reload_review_directory).pack(side=LEFT, padx=8)
        location = ttk.Menubutton(toolbar, text="审核文件夹")
        locations = Menu(location, tearoff=0)
        locations.add_command(label="选择并载入批次", command=self.choose_review_directory)
        locations.add_command(label="打开当前文件夹", command=self.open_loaded_review_folder)
        location.configure(menu=locations)
        location.pack(side=LEFT)
        ttk.Label(toolbar, textvariable=self.vars["review_summary"],
            foreground="#555555", width=32).pack(side=LEFT, fill=X, expand=True, padx=(12, 0))

        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill=BOTH, expand=True)
        left = ttk.LabelFrame(
            paned, text="所有主播、所有直播场次的审核切片", padding=8
        )
        right = ttk.Frame(paned, padding=(6, 0, 0, 0))
        self.review_paned = paned
        paned.add(left, weight=1)
        paned.add(right, weight=1)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.review_editor_notebook = ttk.Notebook(right, style="Review.TNotebook")
        self.review_editor_notebook.grid(row=0, column=0, sticky="nsew")
        self.review_subtitle_tab = ttk.Frame(self.review_editor_notebook, padding=6)
        self.review_editor_notebook.add(self.review_subtitle_tab, text="字幕编辑")
        self.review_metadata_tab, metadata_body = self.build_review_scroll_page(
            self.review_editor_notebook, "投稿信息")
        self.review_feedback_tab, feedback_body = self.build_review_scroll_page(
            self.review_editor_notebook, "审核意见")
        self.review_editor_notebook.select(self.review_subtitle_tab)

        review_top = ttk.PanedWindow(left, orient="vertical")
        self.review_media_paned = review_top
        review_top.bind("<Configure>", self._resize_review_media)
        review_top.grid(row=0, column=0, sticky="nsew")
        clip_table = ttk.Frame(review_top)
        clip_columns = ("creator", "session", "clip", "status", "title")
        self.review_clip_tree = ttk.Treeview(
            clip_table, columns=clip_columns, show="headings", height=4, selectmode="browse"
        )
        for name, label, width in (
            ("creator", "主播", 90),
            ("session", "直播场次", 215),
            ("clip", "片段", 58),
            ("status", "状态", 82),
            ("title", "标题", 330),
        ):
            self.review_clip_tree.heading(name, text=label)
            self.review_clip_tree.column(name, width=width, anchor="w")
        clip_scroll = ttk.Scrollbar(
            clip_table, orient="vertical", command=self.review_clip_tree.yview
        )
        clip_horizontal = ttk.Scrollbar(
            clip_table, orient="horizontal", command=self.review_clip_tree.xview
        )
        self.review_clip_tree.configure(
            yscrollcommand=clip_scroll.set, xscrollcommand=clip_horizontal.set
        )
        self.review_clip_tree.grid(row=0, column=0, sticky="nsew")
        clip_scroll.grid(row=0, column=1, sticky="ns")
        clip_horizontal.grid(row=1, column=0, sticky="ew")
        clip_table.rowconfigure(0, weight=1)
        clip_table.columnconfigure(0, weight=1)
        self.review_clip_tree.bind("<<TreeviewSelect>>", self.review_clip_selected)
        self.review_clip_tree.bind(
            "<ButtonRelease-1>", self.review_clip_clicked, add="+"
        )
        self.review_clip_tree.tag_configure("pending", background="#FFF7DD")
        self.review_clip_tree.tag_configure("approved", background="#E9F7EC")
        self.review_clip_tree.tag_configure("revise", background="#FFE8E8")
        self.review_clip_tree.tag_configure("skipped", background="#EEEEEE")

        self.review_player_panel = ttk.Frame(review_top, width=420, height=170)
        review_top.add(clip_table, weight=1)
        review_top.add(self.review_player_panel, weight=2)
        self.review_player_panel.grid_propagate(False)
        self.review_player_panel.columnconfigure(0, weight=1)
        self.review_player_panel.rowconfigure(0, weight=1)
        self.review_player_host = ttk.Frame(self.review_player_panel)
        self.review_player_host.grid(row=0, column=0, sticky="nsew")
        self.review_cover = ttk.Label(
            self.review_player_host,
            text="选择切片后显示封面；点击“播放”可直接在这里看视频",
            anchor="center",
        )
        self.review_cover.place(x=0, y=0, relwidth=1, relheight=1)
        self.review_video_subtitle = Label(
            self.review_player_panel,
            text=" ",
            anchor="center",
            justify="center",
            background="#111111",
            foreground="#FFFFFF",
            font=(UI_FONT, 16, "bold"),
            wraplength=580,
            padx=10,
            pady=3,
            height=2,
        )
        self.review_video_subtitle.grid(row=1, column=0, sticky="ew")

        clip_actions = ttk.Frame(left)
        clip_actions.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.review_waveform = Canvas(
            clip_actions,
            # The video track ends at y=79; subtitle blocks and their handles
            # occupy y=90..128. Reserve both tracks in the actual viewport.
            height=140,
            background="#111827",
            highlightthickness=1,
            highlightbackground="#44546A",
            cursor="crosshair",
            xscrollincrement=40,
        )
        self.review_waveform.pack(fill=X)
        self.timeline_scroll = ttk.Scrollbar(
            clip_actions, orient="horizontal", command=self.review_waveform.xview
        )
        self.timeline_scroll.pack(fill=X, pady=(0, 3))
        self.review_waveform.configure(xscrollcommand=self.timeline_scroll.set)
        self.review_waveform.bind("<Configure>", lambda _event: self.draw_review_waveform())
        self.review_waveform.bind("<ButtonPress-1>", self.timeline_mouse_down)
        self.review_waveform.bind("<B1-Motion>", self.timeline_mouse_drag)
        self.review_waveform.bind("<ButtonRelease-1>", self.timeline_mouse_up)
        self.review_waveform.bind("<Button-3>", self.timeline_context_menu)
        self.review_waveform.bind("<MouseWheel>", self.timeline_mouse_wheel)
        self.review_waveform.bind("<Control-MouseWheel>", self.timeline_mouse_wheel)
        self.review_waveform.bind("<Delete>", self.timeline_delete_shortcut)
        ttk.Label(
            clip_actions,
            textvariable=self.vars["review_playback"],
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 4))


        play_row = ttk.Frame(clip_actions)
        play_row.pack(fill=X)
        for text, command in (("播放/重播", self.play_current_embedded),
                              ("暂停/继续", self.pause_current_embedded),
                              ("停止", self.stop_embedded_preview)):
            ttk.Button(play_row, text=text, command=command).pack(side=LEFT, padx=(0, 4))
        self.review_speed_box = ttk.Combobox(play_row, textvariable=self.vars["review_speed"],
            values=("0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"), state="readonly", width=5)
        self.review_speed_box.pack(side=LEFT)
        self.review_speed_box.bind("<<ComboboxSelected>>", self.set_review_playback_rate)
        playback_more = ttk.Menubutton(play_row, text="播放选项")
        playback_menu = Menu(playback_more, tearoff=0)
        for text, command in (("后退 5 秒", lambda: self.seek_current_embedded(-5)),
                              ("前进 5 秒", lambda: self.seek_current_embedded(5)),
                              ("标记开始 ←", lambda: self.mark_review_boundary("start")),
                              ("标记结束 →", lambda: self.mark_review_boundary("end")),
                              ("外部播放器", self.open_current_video)):
            playback_menu.add_command(label=text, command=command)
        playback_more.configure(menu=playback_menu)
        playback_more.pack(side=LEFT, padx=(4, 0))
        play_row_2 = ttk.Frame(clip_actions)
        play_row_2.pack(fill=X, pady=(4, 0))
        self.timeline_apply_button = ttk.Button(play_row_2, text="应用剪辑稿", command=self.apply_timeline_edit, state="disabled")
        self.timeline_apply_button.pack(side=LEFT)
        timeline_more = ttk.Menubutton(play_row_2, text="剪辑操作")
        timeline_menu = Menu(timeline_more, tearoff=0)
        for text, command in (("往前增加 5 秒", lambda: self.extend_timeline_context("before")),
                              ("往后增加 5 秒", lambda: self.extend_timeline_context("after")),
                              ("切开 C", self.cut_timeline_at_playhead),
                              ("删除片段 Delete", self.delete_selected_timeline_segment),
                              ("放弃未应用剪辑", self.discard_timeline_edit)):
            timeline_menu.add_command(label=text, command=command)
        timeline_more.configure(menu=timeline_menu)
        timeline_more.pack(side=LEFT, padx=4)
        ttk.Button(play_row_2, text="整场录播 / 新建切片", command=self.open_current_session_clipper).pack(side=LEFT)
        review_meta = ttk.LabelFrame(
            metadata_body, text="标题、简介、Tag 与合集", padding=8
        )
        review_meta.pack(fill=X, pady=(0, 8))
        title_row = ttk.Frame(review_meta)
        title_row.pack(fill=X)
        ttk.Label(title_row, text="标题", width=7).pack(side=LEFT)
        ttk.Entry(title_row, textvariable=self.vars["review_title"]).pack(
            side=LEFT, fill=X, expand=True
        )
        primary_row = ttk.Frame(review_meta)
        primary_row.pack(fill=X, pady=(5, 0))
        ttk.Label(primary_row, text="封面上区", width=7).pack(side=LEFT)
        self.review_cover_primary_editor = ScrolledText(
            primary_row, wrap="word", height=2, font=(UI_FONT, 12)
        )
        self.review_cover_primary_editor.pack(side=LEFT, fill=X, expand=True)
        self._bind_multiline_variable(
            self.review_cover_primary_editor, self.vars["review_cover_primary"]
        )
        # Compatibility alias for callers that still focus the former combined editor.
        self.review_subtitle_editor = self.review_cover_primary_editor
        secondary_row = ttk.Frame(review_meta)
        secondary_row.pack(fill=X, pady=(5, 0))
        ttk.Label(secondary_row, text="封面下区", width=7).pack(side=LEFT)
        self.review_cover_secondary_editor = ScrolledText(
            secondary_row, wrap="word", height=2, font=(UI_FONT, 12)
        )
        self.review_cover_secondary_editor.pack(side=LEFT, fill=X, expand=True)
        self._bind_multiline_variable(
            self.review_cover_secondary_editor, self.vars["review_cover_secondary"]
        )
        ttk.Label(
            review_meta,
            text=(
                "副标题共 2–3 句，每句 3–22 字；第一句放上区，下区可用｜分隔第二、三句。"
                "文字始终位于居中 4:3 安全区内。"
            ),
            foreground="#555555",
            wraplength=680,
            justify="left",
        ).pack(fill=X, padx=(56, 0), pady=(2, 0))
        description_row = ttk.Frame(review_meta)
        description_row.pack(fill=X, pady=(5, 0))
        ttk.Label(description_row, text="投稿简介", width=7).pack(side=LEFT)
        self.review_description_editor = ScrolledText(
            description_row, wrap="word", height=4, font=(UI_FONT, 11)
        )
        self.review_description_editor.pack(side=LEFT, fill=X, expand=True)
        self._bind_multiline_variable(
            self.review_description_editor, self.vars["review_description"]
        )

        collaboration_row = ttk.Frame(review_meta)
        collaboration_row.pack(fill=X, pady=(5, 0))
        ttk.Label(collaboration_row, text="多人素材", width=7).pack(side=LEFT)
        ttk.Combobox(
            collaboration_row,
            textvariable=self.vars["review_collaboration_type"],
            values=list(COLLABORATION_TYPE_LABELS),
            state="readonly",
            width=10,
        ).pack(side=LEFT)
        ttk.Label(collaboration_row, text="参与者").pack(side=LEFT, padx=(8, 3))
        ttk.Entry(
            collaboration_row, textvariable=self.vars["review_participants"]
        ).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(
            collaboration_row,
            text="选择参与嘉宾（可多选）",
            command=self.open_participant_selector,
        ).pack(side=LEFT, padx=(6, 0))
        ttk.Checkbutton(
            collaboration_row,
            text="嘉宾台词已核实",
            variable=self.vars["review_guest_dialogue_verified"],
        ).pack(side=LEFT, padx=(8, 3))
        ttk.Button(
            collaboration_row,
            text="封面制作工具",
            command=self.open_cover_maker,
        ).pack(side=LEFT, padx=(8, 3))
        ttk.Button(
            collaboration_row,
            text="重生成当前封面",
            command=self.rerender_current_review_cover,
        ).pack(side=LEFT)
        evidence_row = ttk.Frame(review_meta)
        evidence_row.pack(fill=X, pady=(5, 0))
        ttk.Label(evidence_row, text="说话依据", width=7).pack(side=LEFT)
        ttk.Entry(
            evidence_row, textvariable=self.vars["review_speaker_evidence"]
        ).pack(side=LEFT, fill=X, expand=True)
        ttk.Label(
            evidence_row,
            textvariable=self.vars["review_collaboration_status"],
            foreground="#555555",
        ).pack(side=LEFT, padx=(8, 0))

        tags_row = ttk.Frame(review_meta)
        tags_row.pack(fill=X, pady=(5, 0))
        ttk.Label(tags_row, text="投稿Tag", width=7).pack(side=LEFT)
        ttk.Entry(tags_row, textvariable=self.vars["review_tags"]).pack(
            side=LEFT, fill=X, expand=True
        )
        collection_row = ttk.Frame(review_meta)
        collection_row.pack(fill=X, pady=(5, 0))
        ttk.Label(collection_row, text="投稿合集", width=7).pack(side=LEFT)
        ttk.Entry(
            collection_row, textvariable=self.vars["review_collection"]
        ).pack(side=LEFT, fill=X, expand=True)
        ttk.Label(collection_row, text="season").pack(side=LEFT, padx=(6, 3))
        ttk.Entry(
            collection_row, textvariable=self.vars["review_season_id"], width=9
        ).pack(side=LEFT)
        ttk.Label(collection_row, text="section").pack(side=LEFT, padx=(6, 3))
        ttk.Entry(
            collection_row, textvariable=self.vars["review_section_id"], width=9
        ).pack(side=LEFT)
        ttk.Button(
            collection_row,
            text="保存投稿信息",
            command=self.save_current_review_copy,
        ).pack(side=LEFT, padx=(6, 0))
        tag_status_row = ttk.Frame(review_meta)
        tag_status_row.pack(fill=X, pady=(3, 0))
        ttk.Label(tag_status_row, text="", width=7).pack(side=LEFT)
        self.review_tag_status_label = ttk.Label(
            tag_status_row, textvariable=self.vars["review_tag_status"]
        )
        self.review_tag_status_label.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(feedback_body, text="人工重做理由", style="QueueHeading.TLabel").pack(anchor="w")
        self.review_reason_display = ScrolledText(feedback_body, wrap="word", height=6, width=35, font=(UI_FONT, 11))
        self.review_reason_display.pack(fill=X, pady=(5, 10))
        self._bind_multiline_variable(self.review_reason_display, self.vars["review_revision_reason"])
        self.review_reason_display.configure(state="disabled")
        ttk.Label(feedback_body, text="审核备注 / 补充重做理由").pack(anchor="w")
        self.review_reason_editor = ScrolledText(feedback_body, wrap="word", height=3, width=35, font=(UI_FONT, 11))
        self.review_reason_editor.pack(fill=X, pady=(5, 6))
        self._bind_multiline_variable(self.review_reason_editor, self.vars["review_notes"])
        ttk.Button(feedback_body, text="保存理由与建议", command=self.save_current_review_feedback).pack(anchor="w", pady=(0, 10))
        suggestions_box = ttk.LabelFrame(feedback_body, text="人工审核建议", padding=6)
        suggestions_box.pack(fill=X)
        self.review_suggestions_editor = ScrolledText(
            suggestions_box, wrap="word", height=3, font=(UI_FONT, 11)
        )
        self.review_suggestions_editor.pack(fill=X)
        self._bind_multiline_variable(
            self.review_suggestions_editor, self.vars["review_suggestions"]
        )
        suggestions_actions = ttk.Frame(suggestions_box)
        suggestions_actions.pack(fill=X, pady=(3, 0))
        ttk.Label(suggestions_actions, text="建议单独保存，不改变审核决定。",
                  foreground="#555555").pack(side=LEFT)
        ttk.Button(suggestions_actions, text="保存建议",
                   command=self.save_current_review_suggestions).pack(side=RIGHT)
        decision_panel = ttk.Frame(right)
        decision_panel.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        status_row = ttk.Frame(decision_panel)
        status_row.pack(fill=X)
        decision_row = ttk.Frame(decision_panel)
        decision_row.pack(fill=X, pady=(3, 0))
        self.review_decision_status_label = ttk.Label(
            status_row,
            textvariable=self.vars["review_decision_status"],
            foreground="#555555",
            font=(UI_FONT, 11, "bold"),
        )
        self.review_decision_status_label.pack(side=LEFT)
        ttk.Button(status_row, text="查看重做理由", command=lambda: self.review_editor_notebook.select(self.review_feedback_tab)).pack(side=RIGHT)
        self.review_approve_button = ttk.Button(
            decision_row,
            text="写入为通过", width=-5,
            takefocus=False,
            command=self.approve_or_replace_current_review,
        )
        self.review_approve_button.pack(side=LEFT)
        self.review_undo_approval_button = ttk.Button(
            decision_row,
            text="回退通过", width=-7,
            takefocus=False,
            command=self.undo_last_review_approval,
        )
        self.review_undo_approval_button.pack(side=LEFT, padx=(6, 0))
        self.review_skip_button = ttk.Button(
            decision_row,
            text="不通过", width=-5,
            takefocus=False,
            command=lambda: self.set_current_review_decision("skipped"),
        )
        self.review_skip_button.pack(side=LEFT, padx=6)
        self.review_redo_button = ttk.Button(decision_row, text="人工重做", width=-7, takefocus=False,
            command=lambda: self.set_current_review_decision("revise"))
        self.review_redo_button.pack(side=LEFT, padx=(3, 0))
        for button in (
            self.review_redo_button,
            self.review_approve_button,
            self.review_undo_approval_button,
            self.review_skip_button,
        ):
            button.bind("<space>", self.timeline_space_shortcut)
        subtitle_page = self.review_subtitle_tab
        subtitle_page.columnconfigure(0, weight=1)
        subtitle_page.rowconfigure(1, weight=1, minsize=150)
        subtitle_search = ttk.Frame(subtitle_page)
        self.review_subtitle_search = subtitle_search
        subtitle_search.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(subtitle_search, text="检索").pack(side=LEFT)
        ttk.Button(subtitle_search, text="全部字幕", command=lambda: self.vars["review_subtitle_query"].set("")).pack(side=RIGHT)
        ttk.Button(subtitle_search, text="下一个", command=self.select_next_subtitle_match).pack(side=RIGHT, padx=4)
        subtitle_query_entry = ttk.Entry(subtitle_search, textvariable=self.vars["review_subtitle_query"])
        subtitle_query_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        subtitle_query_entry.bind("<Return>", lambda _event: self.select_next_subtitle_match())
        subtitle_frame = ttk.Frame(subtitle_page)
        subtitle_frame.grid(row=1, column=0, sticky="nsew")
        subtitle_frame.columnconfigure(0, weight=1)
        subtitle_frame.rowconfigure(0, weight=1)
        columns = ("speaker", "start", "end", "text")
        self.review_subtitle_tree = ttk.Treeview(subtitle_frame, columns=columns, show="headings", height=5, selectmode="browse")
        for name, label, width in (("speaker", "说话人", 76), ("start", "开始", 78), ("end", "结束", 78), ("text", "字幕", 420)):
            self.review_subtitle_tree.heading(name, text=label)
            self.review_subtitle_tree.column(name, width=width, minwidth=60, stretch=name == "text", anchor="w")
        self.review_subtitle_scroll = ttk.Scrollbar(subtitle_frame, orient="vertical", command=self.review_subtitle_tree.yview)
        self.review_subtitle_horizontal = ttk.Scrollbar(subtitle_frame, orient="horizontal", command=self.review_subtitle_tree.xview)
        self.review_subtitle_tree.configure(yscrollcommand=self.review_subtitle_scroll.set, xscrollcommand=self.review_subtitle_horizontal.set)
        self.review_subtitle_tree.grid(row=0, column=0, sticky="nsew")
        self.review_subtitle_scroll.grid(row=0, column=1, sticky="ns")
        self.review_subtitle_horizontal.grid(row=1, column=0, sticky="ew")
        self.review_subtitle_tree.bind("<<TreeviewSelect>>", self.review_subtitle_selected)
        self.review_subtitle_tree.bind("<ButtonRelease-1>", self.play_selected_subtitle)
        self.review_subtitle_tree.bind("<Delete>", self.timeline_delete_shortcut)
        editor = ttk.Frame(subtitle_page)
        self.review_subtitle_editor_panel = editor
        editor.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        row = ttk.Frame(editor)
        row.pack(fill=X)
        ttk.Label(row, text="角色").pack(side=LEFT)
        self.review_speaker_box = ttk.Combobox(row, textvariable=self.vars["review_speaker"],
            values=["待确认", *list(self.speaker_profile_labels)], state="normal", width=8)
        self.review_speaker_box.pack(side=LEFT, padx=(4, 5))
        time_row = row
        for label, name in (("开始", "review_start"), ("结束", "review_end")):
            ttk.Label(time_row, text=label).pack(side=LEFT)
            ttk.Entry(time_row, textvariable=self.vars[name], width=8).pack(side=LEFT, padx=(4, 6))
        self.review_text_editor = ScrolledText(editor, wrap="word", height=2, width=30, font=(UI_FONT, 12))
        self.review_text_editor.pack(fill=X, pady=4)
        save_row = ttk.Frame(editor)
        save_row.pack(fill=X)
        for text, command in (("新增", self.add_current_subtitle), ("删除", self.delete_current_subtitle), ("撤销", self.undo_timeline_action), ("重做", self.redo_timeline_action)):
            ttk.Button(save_row, text=text, command=command, width=4).pack(side=LEFT, padx=(0, 3))
        more = ttk.Menubutton(save_row, text="字幕工具")
        menu = Menu(more, tearoff=0)
        menu.add_command(label="说话人对应角色…", command=self.open_speaker_role_mapping)
        for label, field, delta in (("开始 -0.1 秒", "start", -0.1), ("开始 +0.1 秒", "start", 0.1), ("结束 -0.1 秒", "end", -0.1), ("结束 +0.1 秒", "end", 0.1)):
            menu.add_command(label=label, command=lambda key=field, value=delta: self.nudge_review_time(key, value))
        menu.add_separator()
        menu.add_command(label="纠错词组…", command=self.open_review_fuzzy_editor)
        menu.add_command(label="打开完整词库", command=self.open_correction_dictionary)
        menu.add_command(label="整份 ASS 强制单行", command=self.force_current_ass_single_line)
        menu.add_command(label="检查并修复重叠字幕", command=self.repair_current_subtitle_overlaps)
        more.configure(menu=menu)
        more.pack(side=LEFT)
        self.review_line_status_label = ttk.Label(editor, textvariable=self.vars["review_line_status"], foreground="#555555", wraplength=450)
        # The full status remains in its variable and log; reserve space for subtitles.
        # Show it in the expanded subtitle tools when detailed feedback is needed.
        # Keep long feedback from changing the height available to subtitle rows.
        self.review_line_status_label.configure(anchor="w")
        paned.bind("<Configure>", self._resize_review_layout, add="+")
        self._resize_review_layout()

    def _resize_review_media(self, event=None) -> None:
        pane = self.__dict__.get("review_media_paned")
        if pane is not None and pane.winfo_height() > 100:
            height = pane.winfo_height()
            if self.__dict__.get("_review_media_height") != height:
                pane.sashpos(0, max(65, int(height * 0.38)))
                self._review_media_height = height

    def _resize_review_layout(self, event=None) -> None:
        paned = self.__dict__.get("review_paned")
        if paned is None or paned.winfo_width() < 100:
            return
        width = paned.winfo_width()
        if self.__dict__.get("_review_layout_width") != width:
            paned.sashpos(0, width // 2)
            self._review_layout_width = width
        # Status is detailed in the log as well; use a single line in the editor.
        self.review_line_status_label.configure(wraplength=0)

    def open_review_fuzzy_editor(self) -> None:
        window = Toplevel(self)
        window.title("字幕纠错词组")
        box = ttk.Frame(window, padding=12)
        box.pack(fill=BOTH, expand=True)
        for row, (label, key) in enumerate((("原词", "review_fuzzy_wrong"), ("替换为", "review_fuzzy_replacement"))):
            ttk.Label(box, text=label).grid(row=row, column=0, padx=5, pady=5)
            ttk.Entry(box, textvariable=self.vars[key], width=28).grid(row=row, column=1, padx=5, pady=5)
        ttk.Button(box, text="加入词库并替换当前行", command=self.add_review_fuzzy_correction).grid(row=2, column=0, columnspan=2, pady=8)

    def refresh_review_revision_reason(self, item: dict) -> None:
        variable = self.__dict__.get("vars", {}).get("review_revision_reason")
        if variable is None:
            return
        display = self.__dict__.get("review_reason_display")
        if display is not None:
            display.configure(state="normal")
        variable.set(review_workspace.review_revision_reason(item) or "当前切片未标记为人工重做")
        if display is not None:
            display.configure(state="disabled")

    def save_current_review_feedback(self) -> bool:
        if not self.current_review_dir or not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return False
        try:
            review_workspace.set_review_notes(self.current_review_dir, self.current_review_video.name, self.vars["review_notes"].get())
            if not self.save_current_review_suggestions(silent=True):
                self.vars["review_line_status"].set("审核备注已保存，但审核建议保存失败，请重试")
                return False
            row = self._current_review_decision_row()
            self.refresh_review_revision_reason(row)
            self.vars["review_line_status"].set("审核理由与建议已保存")
            return True
        except Exception as exc:
            self.vars["review_line_status"].set(f"审核理由保存失败：{exc}")
            messagebox.showerror(APP_TITLE, str(exc))
            return False

    def _path_row(self, parent, row: int, label: str, key: str, browse, extra=None) -> None:
        ttk.Label(parent, text=label, width=18).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(
            row=row, column=1, sticky="ew", padx=6, pady=3
        )
        ttk.Button(parent, text="选择", command=browse, width=8).grid(row=row, column=2, pady=3)
        if extra:
            ttk.Button(parent, text="载入", command=extra, width=8).grid(
                row=row, column=3, padx=(6, 0), pady=3
            )

    def _step_box(self, parent, row: int, title: str, description: str, actions) -> None:
        box = ttk.LabelFrame(parent, text=title, style="Step.TLabelframe", padding=8)
        box.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        controls = ttk.Frame(box)
        controls.pack(side=RIGHT)
        for label, command in actions:
            ttk.Button(controls, text=label, command=command).pack(side=LEFT, padx=(6, 0))
        description_label = ttk.Label(box, text=description, wraplength=490, justify="left")
        description_label.pack(side=LEFT, fill=X, expand=True)
        if title.startswith("流程 2"):
            self.__dict__.setdefault("_flow2_description_labels", []).append(description_label)

    def open_correction_dictionary(self) -> None:
        window = Toplevel(self)
        window.title("字幕纠错词典")
        window.geometry("980x680")
        window.minsize(760, 520)
        window.transient(self)

        query = StringVar()
        wrong = StringVar()
        replacement = StringVar()
        outer = ttk.Frame(window, padding=14)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer,
            text=(
                "这里按完整词组精确纠错，不做单字全局替换。只有主动点击“新增并启用”"
                "或字幕下方的“加入词库并替换当前行”才会写入词库；普通字幕修改不会被记录。"
            ),
            wraplength=920,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))
        search = ttk.Frame(outer)
        search.pack(fill=X)
        ttk.Label(search, text="查询").pack(side=LEFT)
        search_entry = ttk.Entry(search, textvariable=query)
        search_entry.pack(side=LEFT, fill=X, expand=True, padx=(6, 0))

        columns = ("wrong", "replacement", "status", "count", "source", "updated")
        table = ttk.Frame(outer)
        table.pack(fill=BOTH, expand=True, pady=(8, 8))
        tree = ttk.Treeview(table, columns=columns, show="headings", height=20)
        for name, label, width in (
            ("wrong", "识别错词", 180),
            ("replacement", "正确词", 180),
            ("status", "状态", 90),
            ("count", "出现次数", 75),
            ("source", "来源", 130),
            ("updated", "最近记录", 170),
        ):
            tree.heading(name, text=label)
            tree.column(name, width=width, anchor="w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)
        row_map: dict[str, dict] = {}

        def refresh(*_args) -> None:
            tree.delete(*tree.get_children())
            row_map.clear()
            status_labels = {
                "active": "已启用",
                "candidate": "待确认",
                "disabled": "已停用",
                "builtin": "共享内置",
            }
            for index, row in enumerate(glossary.correction_dictionary_rows(query.get()), 1):
                iid = f"correction-{index}"
                row_map[iid] = row
                tree.insert(
                    "", "end", iid=iid,
                    values=(
                        row.get("wrong", ""),
                        row.get("replacement", ""),
                        status_labels.get(str(row.get("status", "")), row.get("status", "")),
                        row.get("count", 0),
                        row.get("source", ""),
                        row.get("updated_at", ""),
                    ),
                )

        def selected_row() -> dict | None:
            selected = tree.selection()
            return row_map.get(selected[0]) if selected else None

        def select_row(_event=None) -> None:
            row = selected_row()
            if row:
                wrong.set(str(row.get("wrong", "")))
                replacement.set(str(row.get("replacement", "")))

        def add_active() -> None:
            try:
                wrong_value = wrong.get().strip()
                if (
                    self.current_review_ass
                    and self.current_review_ass.is_file()
                    and wrong_value
                    and wrong_value in self.current_review_ass.read_text(encoding="utf-8-sig")
                    and not self._ensure_current_approved_revision("字幕")
                ):
                    return
                glossary.upsert_correction(wrong.get(), replacement.get(), status="active")
                changed = self._apply_active_corrections_to_current_ass()
                refresh()
                if changed:
                    self.vars["review_line_status"].set(
                        f"已把启用纠错应用到当前切片全部字幕：更新 {changed} 行"
                    )
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        def set_status(status: str) -> None:
            row = selected_row()
            if not row:
                messagebox.showwarning(APP_TITLE, "请先选择一条可编辑词条。", parent=window)
                return
            correction_id = str(row.get("id", ""))
            if not correction_id:
                messagebox.showinfo(APP_TITLE, "共享内置词条不能在这里停用。", parent=window)
                return
            try:
                wrong_value = str(row.get("wrong", "")).strip()
                if (
                    status == "active"
                    and self.current_review_ass
                    and self.current_review_ass.is_file()
                    and wrong_value
                    and wrong_value in self.current_review_ass.read_text(encoding="utf-8-sig")
                    and not self._ensure_current_approved_revision("字幕")
                ):
                    return
                glossary.set_correction_status(correction_id, status)
                changed = (
                    self._apply_active_corrections_to_current_ass()
                    if status == "active" else 0
                )
                refresh()
                if changed:
                    self.vars["review_line_status"].set(
                        f"已把启用纠错应用到当前切片全部字幕：更新 {changed} 行"
                    )
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        tree.bind("<<TreeviewSelect>>", select_row)
        query.trace_add("write", refresh)
        edit = ttk.LabelFrame(outer, text="新增或修改词条", padding=8)
        edit.pack(fill=X)
        ttk.Label(edit, text="错词").pack(side=LEFT)
        ttk.Entry(edit, textvariable=wrong, width=22).pack(side=LEFT, padx=(5, 12))
        ttk.Label(edit, text="正确词").pack(side=LEFT)
        ttk.Entry(edit, textvariable=replacement, width=22).pack(side=LEFT, padx=(5, 12))
        ttk.Button(edit, text="新增并启用", command=add_active).pack(side=LEFT)
        ttk.Button(edit, text="启用选中", command=lambda: set_status("active")).pack(
            side=LEFT, padx=(6, 0)
        )
        ttk.Button(edit, text="停用选中", command=lambda: set_status("disabled")).pack(
            side=LEFT, padx=(6, 0)
        )
        ttk.Button(
            edit,
            text="打开词典文件",
            command=lambda: self.open_path(glossary.DEFAULT_CORRECTION_DICTIONARY),
        ).pack(side=RIGHT)
        refresh()
        search_entry.focus_set()
    def refresh_creator_profiles(self, selected_key: str = "") -> None:
        current_key = self.profile_labels.get(self.vars["creator"].get(), "")
        self.profiles = core.load_profiles()
        self.speaker_profile_labels = {
            f"{key} — {value['display_name']}": key
            for key, value in self.profiles.items()
            if not value.get("archived")
        }
        self.profile_labels = {
            f"{key} — {value['display_name']}": key
            for key, value in self.profiles.items()
            if not value.get("archived") and not value.get("guest_only")
        }
        if self.creator_combobox is not None:
            self.creator_combobox.configure(values=list(self.profile_labels))
        if hasattr(self, "review_speaker_box"):
            self.refresh_review_speaker_choices()
        target = selected_key or current_key
        label = next(
            (name for name, key in self.profile_labels.items() if key == target),
            next(iter(self.profile_labels), ""),
        )
        if label:
            self.vars["creator"].set(label)

    def open_cookie_template_dialog(self) -> None:
        window = Toplevel(self)
        window.title("Cookie 模板与导入")
        window.geometry("720x360")
        window.minsize(620, 320)
        window.transient(self)
        outer = ttk.Frame(window, padding=14)
        outer.pack(fill=BOTH, expand=True)
        cookie_path = publish.default_cookie_path()
        status_value = StringVar()

        ttk.Label(
            outer,
            text="投稿凭证只保存在本机私有目录，不会写入项目、日志或分享包。",
            wraplength=670,
            justify="left",
        ).pack(anchor="w")
        path_box = ttk.LabelFrame(outer, text="凭证位置", padding=10)
        path_box.pack(fill=X, pady=(12, 8))
        ttk.Label(path_box, text=str(cookie_path), wraplength=650).pack(anchor="w")
        ttk.Label(path_box, textvariable=status_value, foreground="#555555").pack(
            anchor="w", pady=(6, 0)
        )
        ttk.Label(
            outer,
            text=(
                "推荐使用“扫码登录”。空模板仅用于已有 Cookie 的人工迁移；"
                "填写完成后至少要包含非空的 SESSDATA 和 bili_jct。"
            ),
            wraplength=670,
            justify="left",
        ).pack(anchor="w", pady=(2, 12))

        def refresh() -> None:
            code = publish.cookie_json_status(cookie_path)
            status_value.set(f"当前状态：{COOKIE_STATUS_LABELS.get(code, code)}")

        def create_template() -> None:
            try:
                current = publish.cookie_json_status(cookie_path)
                if current == "ready":
                    messagebox.showinfo(
                        APP_TITLE,
                        "当前凭证已经可用，为防止退出登录，不会用空模板覆盖。",
                        parent=window,
                    )
                    return
                force = cookie_path.exists()
                if force and not messagebox.askyesno(
                    APP_TITLE,
                    "现有文件不是可用登录凭证。要替换为空模板吗？",
                    parent=window,
                ):
                    return
                publish.create_cookie_template(cookie_path, force=force)
                refresh()
                messagebox.showinfo(APP_TITLE, "空模板已创建。", parent=window)
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        def import_existing() -> None:
            source = filedialog.askopenfilename(
                title="选择已有 cookies.json",
                filetypes=[("JSON", "*.json"), ("全部文件", "*.*")],
                parent=window,
            )
            if not source:
                return
            try:
                publish.import_cookie_file(Path(source), cookie_path)
                refresh()
                messagebox.showinfo(APP_TITLE, "Cookie 已导入并通过校验。", parent=window)
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        def open_template() -> None:
            try:
                if not cookie_path.exists():
                    publish.create_cookie_template(cookie_path)
                self.open_path(cookie_path)
                refresh()
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        def open_private_directory() -> None:
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            self.open_path(cookie_path.parent)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X, pady=(8, 0))
        ttk.Button(buttons, text="创建空模板", command=create_template).pack(side=LEFT)
        ttk.Button(buttons, text="导入已有 Cookie", command=import_existing).pack(
            side=LEFT, padx=8
        )
        ttk.Button(buttons, text="打开模板文件", command=open_template).pack(side=LEFT)
        ttk.Button(
            buttons,
            text="打开私有目录",
            command=open_private_directory,
        ).pack(side=LEFT, padx=8)
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side=RIGHT)
        refresh()

    def open_qr_login_dialog(self) -> None:
        window = Toplevel(self)
        window.title("哔哩哔哩扫码登录")
        window.geometry("620x720")
        window.minsize(540, 620)
        window.transient(self)
        outer = ttk.Frame(window, padding=14)
        outer.pack(fill=BOTH, expand=True)
        status_value = StringVar(value="正在生成二维码…")
        qr_label = ttk.Label(outer, text="正在连接哔哩哔哩…", anchor="center")
        qr_label.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer,
            textvariable=status_value,
            font=(UI_FONT, 13, "bold"),
        ).pack(pady=(10, 4))
        ttk.Label(
            outer,
            text="请使用哔哩哔哩手机客户端扫码并确认。登录凭证只保存到本机私有目录。",
            wraplength=570,
            justify="center",
        ).pack()
        messages: queue.Queue[tuple[str, object]] = queue.Queue()
        active_cancel: dict[str, threading.Event | None] = {"event": None}
        qr_photo: dict[str, object | None] = {"image": None}
        current_url: dict[str, str] = {"value": ""}

        def worker(cancel_event: threading.Event) -> None:
            try:
                session = publish.start_bili_qr_login()
                messages.put(("qr", str(session["url"])))
                deadline = time.monotonic() + 180
                while not cancel_event.wait(2.0):
                    if time.monotonic() >= deadline:
                        messages.put(("error", "二维码已超时，请重新生成。"))
                        return
                    data = publish.poll_bili_qr_login(session)
                    code = int(data.get("code", -1))
                    status = publish.BILI_QR_STATUS.get(
                        code, str(data.get("message") or code)
                    )
                    messages.put(("status", status))
                    if code == 0:
                        destination = publish.save_qr_login_credentials(
                            publish.default_cookie_path(),
                            publish.cookie_jar_values(session["cookie_jar"]),
                            refresh_token=str(data.get("refresh_token", "")),
                        )
                        messages.put(("done", str(destination)))
                        return
                    if code == 86038:
                        messages.put(("error", "二维码已过期，请重新生成。"))
                        return
            except Exception as exc:
                messages.put(("error", str(exc)))

        def set_running(running: bool) -> None:
            start_button.configure(state="disabled" if running else "normal")
            cancel_button.configure(state="normal" if running else "disabled")

        def start_login() -> None:
            previous = active_cancel.get("event")
            if previous is not None:
                previous.set()
            cancel_event = threading.Event()
            active_cancel["event"] = cancel_event
            status_value.set("正在生成二维码…")
            qr_label.configure(image="", text="正在连接哔哩哔哩…")
            qr_photo["image"] = None
            set_running(True)
            threading.Thread(target=worker, args=(cancel_event,), daemon=True).start()

        def cancel_login() -> None:
            cancel_event = active_cancel.get("event")
            if cancel_event is not None:
                cancel_event.set()
            active_cancel["event"] = None
            status_value.set("已取消本次扫码登录")
            set_running(False)

        def open_login_url() -> None:
            value = current_url["value"]
            if value:
                webbrowser.open(value)

        def pump() -> None:
            if not window.winfo_exists():
                return
            try:
                while True:
                    kind, value = messages.get_nowait()
                    if kind == "qr":
                        current_url["value"] = str(value)
                        try:
                            import qrcode

                            image = qrcode.make(str(value)).convert("RGB")
                            image.thumbnail((390, 390))
                            photo = ImageTk.PhotoImage(image)
                            qr_photo["image"] = photo
                            qr_label.configure(image=photo, text="")
                            status_value.set("等待扫码")
                        except Exception as exc:
                            qr_label.configure(
                                image="",
                                text=(
                                    f"二维码组件不可用：{exc}\n"
                                    "可点击下方按钮在浏览器打开登录链接。"
                                ),
                            )
                    elif kind == "status":
                        status_value.set(str(value))
                    elif kind == "done":
                        active_cancel["event"] = None
                        status_value.set("登录成功，凭证已安全保存")
                        set_running(False)
                        messagebox.showinfo(
                            APP_TITLE,
                            f"扫码登录成功。\n凭证位置：{value}",
                            parent=window,
                        )
                    elif kind == "error":
                        active_cancel["event"] = None
                        status_value.set(str(value))
                        set_running(False)
            except queue.Empty:
                pass
            window.after(120, pump)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X, pady=(12, 0))
        start_button = ttk.Button(buttons, text="重新生成二维码", command=start_login)
        start_button.pack(side=LEFT)
        cancel_button = ttk.Button(buttons, text="取消登录", command=cancel_login)
        cancel_button.pack(side=LEFT, padx=8)
        ttk.Button(
            buttons,
            text="浏览器打开登录链接",
            command=open_login_url,
        ).pack(side=LEFT)
        close_button = ttk.Button(buttons, text="关闭", command=window.destroy)
        close_button.pack(side=RIGHT)

        def close_window() -> None:
            cancel_event = active_cancel.get("event")
            if cancel_event is not None:
                cancel_event.set()
            window.destroy()

        close_button.configure(command=close_window)
        window.protocol("WM_DELETE_WINDOW", close_window)
        pump()
        start_login()

    def open_creator_profile_manager(self) -> None:
        self.refresh_creator_profiles()
        window = Toplevel(self)
        window.title("人物与字幕模板")
        window.geometry("1120x900")
        window.minsize(920, 720)
        window.transient(self)

        selected = StringVar(value=self.vars["creator"].get())
        fields = {
            "key": StringVar(),
            "display_name": StringVar(),
            "room_id": StringVar(),
            "role_color": StringVar(value="#7FA8D6"),
            "subtitle_fill_color": StringVar(value="#FFFFFF"),
            "subtitle_outline_color": StringVar(
                value=creator_profiles.DEFAULT_SUBTITLE_OUTLINE_COLOR
            ),
            "subtitle_outline_width": StringVar(
                value=str(creator_profiles.DEFAULT_SUBTITLE_OUTLINE_WIDTH)
            ),
            "dialogue_font": StringVar(value=creator_profiles.DEFAULT_FONT),
            "dialogue_font_size": StringVar(value=str(creator_profiles.DEFAULT_DIALOGUE_SIZE)),
            "radio_subtitle_size": StringVar(value=str(creator_profiles.DEFAULT_RADIO_SIZE)),
            "cover_character_image": StringVar(),
            "upload_enabled": BooleanVar(value=False),
            "upload_description": StringVar(),
            "upload_tags": StringVar(),
            "narrative_collection_title": StringVar(),
            "narrative_season_id": StringVar(),
            "narrative_section_id": StringVar(),
            "song_collection_title": StringVar(),
            "song_season_id": StringVar(),
            "song_section_id": StringVar(),
            "status": StringVar(value="选择人物或点击“新增人物”"),
        }

        outer = ttk.Frame(window, padding=14)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer,
            text=(
                "这里同时管理人物身份和字幕模板。普通切片与电台版式都会使用所选字体、"
                "字号、独立正文色和深色描边；封面主题色不再直接充当字幕描边。"
                "自动发现的新房间会先建为禁止投稿的草稿。"
            ),
            wraplength=770,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        chooser = ttk.Frame(outer)
        chooser.pack(fill=X)
        profile_box = ttk.Combobox(
            chooser,
            textvariable=selected,
            values=list(self.profile_labels),
            state="readonly",
            width=42,
        )
        profile_box.pack(side=LEFT, fill=X, expand=True)
        ttk.Button(chooser, text="新增人物", command=lambda: load_profile("")).pack(
            side=LEFT, padx=(8, 0)
        )
        ttk.Button(chooser, text="移除当前人物", command=lambda: remove_profile()).pack(
            side=LEFT, padx=(8, 0)
        )

        form = ttk.LabelFrame(outer, text="人物与字幕参数", padding=12)
        form.pack(fill=BOTH, expand=True, pady=(12, 8))
        form.columnconfigure(1, weight=1)

        def row(index: int, label: str, key: str, widget=None):
            ttk.Label(form, text=label, width=20).grid(
                row=index, column=0, sticky="w", pady=6
            )
            control = widget or ttk.Entry(form, textvariable=fields[key])
            control.grid(row=index, column=1, sticky="ew", padx=(8, 0), pady=6)
            return control

        row(0, "人物代号（英文）", "key")
        row(1, "显示名 / 标题署名", "display_name")
        row(2, "直播间房间号", "room_id")

        color_line = ttk.Frame(form)
        def color_control(label: str, key: str, title: str) -> None:
            ttk.Label(color_line, text=label).pack(side=LEFT, padx=(0, 3))
            ttk.Entry(color_line, textvariable=fields[key], width=10).pack(side=LEFT)

            def choose_color() -> None:
                picked = colorchooser.askcolor(
                    color=fields[key].get(), title=title, parent=window
                )[1]
                if picked:
                    fields[key].set(picked.upper())

            ttk.Button(color_line, text="选择", command=choose_color).pack(
                side=LEFT, padx=(4, 12)
            )

        color_control("封面", "role_color", "选择封面主题色")
        color_control("字幕正文", "subtitle_fill_color", "选择字幕正文色")
        color_control("字幕描边", "subtitle_outline_color", "选择字幕描边色")
        ttk.Label(color_line, text="描边宽").pack(side=LEFT, padx=(0, 3))
        ttk.Spinbox(
            color_line,
            from_=3,
            to=8,
            increment=0.5,
            textvariable=fields["subtitle_outline_width"],
            width=5,
        ).pack(side=LEFT)
        row(3, "封面 / 字幕颜色", "role_color", color_line)

        fonts = sorted(set(tkfont.families(self)), key=str.casefold)
        font_box = ttk.Combobox(
            form,
            textvariable=fields["dialogue_font"],
            values=fonts,
            state="readonly" if fonts else "normal",
        )
        row(4, "字幕字体", "dialogue_font", font_box)
        row(
            5,
            "普通字幕字号",
            "dialogue_font_size",
            ttk.Spinbox(
                form,
                from_=24,
                to=160,
                textvariable=fields["dialogue_font_size"],
                width=10,
            ),
        )
        row(
            6,
            "电台字幕字号",
            "radio_subtitle_size",
            ttk.Spinbox(
                form,
                from_=24,
                to=160,
                textvariable=fields["radio_subtitle_size"],
                width=10,
            ),
        )
        upload_toggle = ttk.Checkbutton(
            form,
            text="允许这个人物公开投稿",
            variable=fields["upload_enabled"],
        )
        row(7, "投稿开关", "upload_enabled", upload_toggle)
        row(8, "投稿简介", "upload_description")
        row(9, "固定 Tag（逗号分隔）", "upload_tags")

        def collection_line(
            title_key: str, season_key: str, section_key: str
        ) -> ttk.Frame:
            line = ttk.Frame(form)
            ttk.Entry(line, textvariable=fields[title_key]).pack(
                side=LEFT, fill=X, expand=True
            )
            ttk.Label(line, text="season").pack(side=LEFT, padx=(8, 3))
            ttk.Entry(line, textvariable=fields[season_key], width=10).pack(side=LEFT)
            ttk.Label(line, text="section").pack(side=LEFT, padx=(8, 3))
            ttk.Entry(line, textvariable=fields[section_key], width=10).pack(side=LEFT)
            return line

        row(
            10,
            "普通切片合集",
            "narrative_collection_title",
            collection_line(
                "narrative_collection_title",
                "narrative_season_id",
                "narrative_section_id",
            ),
        )
        row(
            11,
            "歌切合集",
            "song_collection_title",
            collection_line(
                "song_collection_title", "song_season_id", "song_section_id"
            ),
        )
        character_line = ttk.Frame(form)
        ttk.Entry(
            character_line, textvariable=fields["cover_character_image"]
        ).pack(side=LEFT, fill=X, expand=True)

        def choose_character_image() -> None:
            value = filedialog.askopenfilename(
                title="选择多人封面透明人物图",
                filetypes=[
                    ("透明人物图", "*.png *.webp"),
                    ("全部文件", "*.*"),
                ],
                parent=window,
            )
            if value:
                fields["cover_character_image"].set(value)

        ttk.Button(
            character_line, text="选择 PNG / WebP", command=choose_character_image
        ).pack(side=LEFT, padx=(8, 0))
        row(12, "多人封面透明人物图", "cover_character_image", character_line)
        ttk.Label(
            form,
            text=(
                "合集名称必填；season / section 可同时留空，投稿时会按名称查找或创建。"
                "竖屏、方屏、旋转后为竖屏，或文件名包含“电台 / radio”时，"
                "流程3自动使用 1920×1080 电台模板。"
            ),
            foreground="#555555",
            wraplength=780,
            justify="left",
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(12, 4))
        ttk.Label(
            form, textvariable=fields["status"], foreground="#555555"
        ).grid(row=14, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def load_profile(key: str | None = None) -> None:
            creator_key = key
            if creator_key is None:
                creator_key = self.profile_labels.get(selected.get(), "")
            if not creator_key:
                fields["key"].set("")
                fields["display_name"].set("")
                fields["room_id"].set("")
                fields["role_color"].set("#7FA8D6")
                fields["subtitle_fill_color"].set("#FFFFFF")
                fields["subtitle_outline_color"].set(
                    creator_profiles.DEFAULT_SUBTITLE_OUTLINE_COLOR
                )
                fields["subtitle_outline_width"].set(
                    str(creator_profiles.DEFAULT_SUBTITLE_OUTLINE_WIDTH)
                )
                fields["dialogue_font"].set(creator_profiles.DEFAULT_FONT)
                fields["dialogue_font_size"].set(
                    str(creator_profiles.DEFAULT_DIALOGUE_SIZE)
                )
                fields["radio_subtitle_size"].set(
                    str(creator_profiles.DEFAULT_RADIO_SIZE)
                )
                fields["cover_character_image"].set("")
                fields["upload_enabled"].set(False)
                fields["upload_description"].set("")
                fields["upload_tags"].set("")
                for name in (
                    "narrative_collection_title", "narrative_season_id",
                    "narrative_section_id", "song_collection_title",
                    "song_season_id", "song_section_id",
                ):
                    fields[name].set("")
                fields["status"].set("新增人物：补全投稿设置并勾选后即可公开投稿")
                return
            profile = self.profiles[creator_key]
            fields["key"].set(creator_key)
            fields["display_name"].set(str(profile.get("display_name", "")))
            fields["room_id"].set(str(profile.get("room_id", "")))
            fields["role_color"].set(str(profile.get("role_color", "#7FA8D6")))
            fill_color, outline_color = creator_profiles.subtitle_colors(profile)
            fields["subtitle_fill_color"].set(fill_color)
            fields["subtitle_outline_color"].set(outline_color)
            fields["subtitle_outline_width"].set(
                str(
                    profile.get(
                        "subtitle_outline_width",
                        creator_profiles.DEFAULT_SUBTITLE_OUTLINE_WIDTH,
                    )
                )
            )
            fields["dialogue_font"].set(
                str(profile.get("dialogue_font", creator_profiles.DEFAULT_FONT))
            )
            fields["dialogue_font_size"].set(
                str(profile.get("dialogue_font_size", creator_profiles.DEFAULT_DIALOGUE_SIZE))
            )
            fields["radio_subtitle_size"].set(
                str(profile.get("radio_subtitle_size", creator_profiles.DEFAULT_RADIO_SIZE))
            )
            fields["cover_character_image"].set(
                str(profile.get("cover_character_image", ""))
            )
            upload = profile.get("upload", {})
            fields["upload_enabled"].set(upload.get("enabled") is not False)
            fields["upload_description"].set(str(upload.get("description", "")))
            fields["upload_tags"].set(",".join(str(tag) for tag in upload.get("tags", [])))
            collections = upload.get("collections", {})
            for kind in ("narrative", "song"):
                collection = collections.get(kind, {}) or {}
                fields[f"{kind}_collection_title"].set(
                    str(collection.get("title", ""))
                )
                fields[f"{kind}_season_id"].set(
                    str(collection.get("season_id") or "")
                )
                fields[f"{kind}_section_id"].set(
                    str(collection.get("section_id") or "")
                )
            upload_enabled = upload.get("enabled") is not False
            origin = "自动草稿" if profile.get("auto_created") else "人物配置"
            publish = "允许投稿" if upload_enabled else "投稿关闭"
            fields["status"].set(f"{origin}｜{publish}")

        def preview_profile() -> None:
            preview = Toplevel(window)
            preview.title("人物与字幕模板预览")
            preview.geometry("860x590")
            preview.minsize(760, 540)
            preview.transient(window)
            preview_mode = StringVar(value="普通切片")
            preview_status = StringVar()
            preview_character_image = {"value": None}

            controls = ttk.Frame(preview, padding=(12, 10, 12, 6))
            controls.pack(fill=X)
            ttk.Label(controls, text="版式").pack(side=LEFT)
            ttk.Radiobutton(
                controls, text="普通切片", variable=preview_mode,
                value="普通切片",
            ).pack(side=LEFT, padx=(8, 4))
            ttk.Radiobutton(
                controls, text="电台回", variable=preview_mode,
                value="电台回",
            ).pack(side=LEFT)
            ttk.Label(
                controls,
                text="预览使用当前输入值，不需要先保存。",
                foreground="#555555",
            ).pack(side=RIGHT)

            canvas = Canvas(
                preview, width=800, height=450, background="#121722",
                highlightthickness=1, highlightbackground="#5B6472",
            )
            canvas.pack(fill=BOTH, expand=True, padx=12, pady=(0, 6))
            ttk.Label(
                preview, textvariable=preview_status, foreground="#555555"
            ).pack(anchor="w", padx=12, pady=(0, 10))

            def outlined_text(x: float, y: float, text_value: str, **kwargs) -> None:
                draw_outlined_canvas_text(canvas, x, y, text_value, **kwargs)

            def redraw(*_args) -> None:
                if not preview.winfo_exists():
                    return
                try:
                    radio = preview_mode.get() == "电台回"
                    spec = subtitle_preview_spec(
                        display_name=fields["display_name"].get(),
                        role_color=fields["role_color"].get(),
                        subtitle_fill_color=fields["subtitle_fill_color"].get(),
                        subtitle_outline_color=fields["subtitle_outline_color"].get(),
                        subtitle_outline_width=fields["subtitle_outline_width"].get(),
                        dialogue_font=fields["dialogue_font"].get(),
                        dialogue_font_size=fields["dialogue_font_size"].get(),
                        radio_subtitle_size=fields["radio_subtitle_size"].get(),
                        radio=radio,
                    )
                    image_note = "｜多人立绘已配置" if fields[
                        "cover_character_image"
                    ].get().strip() else "｜未配置多人立绘"
                    preview_status.set(
                        f"{spec['display_name']}｜{spec['font']}｜"
                        f"原始字号 {spec['source_size']}｜封面 {spec['role_color']}｜"
                        f"正文 {spec['subtitle_fill_color']}｜描边 {spec['subtitle_outline_color']} "
                        f"× {spec['outline_width']:g}"
                        f"{image_note}"
                    )
                except Exception as exc:
                    preview_status.set(f"无法预览：{exc}")
                    return

                width = max(720, canvas.winfo_width())
                height = max(405, canvas.winfo_height())
                color = str(spec["role_color"])
                subtitle_fill = str(spec["subtitle_fill_color"])
                subtitle_outline = str(spec["subtitle_outline_color"])
                font = str(spec["font"])
                size = int(spec["canvas_size"])
                preview_outline_width = int(spec["canvas_outline_width"])
                preview_shadow_offset = int(spec["canvas_shadow_offset"])
                name = str(spec["display_name"])
                canvas.delete("all")
                canvas.configure(background="#121722")
                canvas.create_rectangle(0, 0, width, height, fill="#121722", outline="")
                canvas.create_rectangle(0, 0, width, 64, fill="#1C2432", outline="")
                canvas.create_oval(22, 14, 70, 62, fill=color, outline="#FFFFFF", width=2)
                canvas.create_text(
                    82, 26, text=name, anchor="w", fill="#FFFFFF",
                    font=(font, max(14, size - 3), "bold"),
                )
                room = fields["room_id"].get().strip() or "未填写房间号"
                canvas.create_text(
                    82, 48, text=f"直播间 {room}", anchor="w", fill="#AEB8C6",
                    font=(UI_FONT, 10),
                )
                canvas.create_rectangle(
                    width - 286, 12, width - 244, 54,
                    fill=subtitle_fill, outline=subtitle_outline, width=4,
                )
                canvas.create_text(
                    width - 232, 24, text="实际正文色", anchor="w",
                    fill="#FFFFFF", font=(UI_FONT, 11, "bold"),
                )
                canvas.create_text(
                    width - 232, 45,
                    text=f"描边 {subtitle_outline} × {spec['outline_width']:g}",
                    anchor="w", fill="#AEB8C6", font=(UI_FONT, 9),
                )

                if radio:
                    canvas.create_rectangle(
                        26, 82, width * 0.38, height - 30,
                        fill="#252F42", outline=color, width=3,
                    )
                    canvas.create_oval(
                        width * 0.13, 130, width * 0.29, 250,
                        fill=color, outline="#FFFFFF", width=3,
                    )
                    canvas.create_text(
                        width * 0.19, 280, text=name, fill="#FFFFFF",
                        font=(font, max(16, size), "bold"),
                    )
                    canvas.create_rectangle(
                        width * 0.42, 92, width - 30, height - 42,
                        fill="#192130", outline="#3A465A", width=2,
                    )
                    for index, bar_width in enumerate((0.24, 0.38, 0.18, 0.31, 0.27)):
                        y = 130 + index * 34
                        canvas.create_rectangle(
                            width * 0.48, y, width * (0.48 + bar_width), y + 12,
                            fill=color if index == 2 else "#526078", outline="",
                        )
                else:
                    canvas.create_rectangle(
                        26, 82, width - 26, height - 28,
                        fill="#253247", outline="#3A465A", width=2,
                    )
                    canvas.create_oval(
                        width * 0.38, 120, width * 0.62, 300,
                        fill="#394A63", outline=color, width=4,
                    )
                    canvas.create_text(
                        width / 2, 210, text="视频画面预览区", fill="#B8C2D1",
                        font=(UI_FONT, 18, "bold"),
                    )
                raw_character = fields["cover_character_image"].get().strip()
                preview_character_image["value"] = None
                if raw_character and Image is not None and ImageTk is not None:
                    try:
                        character_path = Path(raw_character)
                        if not character_path.is_absolute():
                            character_path = (
                                creator_profiles.PROFILE_PATH.parent / character_path
                            )
                        with Image.open(character_path) as source:
                            character = source.convert("RGBA")
                            character.thumbnail((260, 300))
                            preview_character_image["value"] = ImageTk.PhotoImage(
                                character
                            )
                        canvas.create_image(
                            width - 30,
                            height - 38,
                            image=preview_character_image["value"],
                            anchor="se",
                        )
                    except Exception as exc:
                        preview_status.set(f"人物图无法预览：{exc}")
                canvas.create_rectangle(
                    24, height - 34, width - 24, height - 26,
                    fill=color, outline="",
                )
                canvas.create_rectangle(
                    26, height - 132, width / 2, height - 38,
                    fill="#253247", outline="",
                )
                canvas.create_rectangle(
                    width / 2, height - 132, width - 26, height - 38,
                    fill="#F4EEDF", outline="",
                )
                canvas.create_text(
                    38, height - 121, text="暗背景", anchor="w",
                    fill="#AEB8C6", font=(UI_FONT, 9),
                )
                canvas.create_text(
                    width / 2 + 12, height - 121, text="亮背景", anchor="w",
                    fill="#47505C", font=(UI_FONT, 9),
                )
                outlined_text(
                    width / 2,
                    height - 78,
                    f"{name}：这是字幕样式预览",
                    fill=subtitle_fill,
                    outline=subtitle_outline,
                    outline_width=preview_outline_width,
                    shadow_color="#000000",
                    shadow_offset=preview_shadow_offset,
                    width=width - 90,
                    justify="center",
                    font=(font, size, "bold"),
                )

            trace_tokens: list[tuple[StringVar, str]] = []
            for key in (
                "display_name", "room_id", "role_color",
                "subtitle_fill_color", "subtitle_outline_color",
                "subtitle_outline_width", "dialogue_font",
                "dialogue_font_size", "radio_subtitle_size",
                "cover_character_image",
            ):
                variable = fields[key]
                trace_tokens.append((variable, variable.trace_add("write", redraw)))
            trace_tokens.append(
                (preview_mode, preview_mode.trace_add("write", redraw))
            )

            def close_preview() -> None:
                for variable, token in trace_tokens:
                    try:
                        variable.trace_remove("write", token)
                    except Exception:
                        pass
                preview.destroy()

            preview.protocol("WM_DELETE_WINDOW", close_preview)
            preview.bind("<Configure>", lambda _event: preview.after_idle(redraw))
            redraw()

        def save_profile() -> None:
            try:
                key = creator_profiles.upsert_profile(
                    key=fields["key"].get(),
                    display_name=fields["display_name"].get(),
                    room_id=fields["room_id"].get(),
                    role_color=fields["role_color"].get(),
                    subtitle_fill_color=fields["subtitle_fill_color"].get(),
                    subtitle_outline_color=fields["subtitle_outline_color"].get(),
                    subtitle_outline_width=fields["subtitle_outline_width"].get(),
                    dialogue_font=fields["dialogue_font"].get(),
                    dialogue_font_size=fields["dialogue_font_size"].get(),
                    radio_subtitle_size=fields["radio_subtitle_size"].get(),
                    cover_character_image=fields["cover_character_image"].get(),
                    upload_enabled=bool(fields["upload_enabled"].get()),
                    upload_description=fields["upload_description"].get(),
                    upload_tags=fields["upload_tags"].get(),
                    narrative_collection_title=fields[
                        "narrative_collection_title"
                    ].get(),
                    narrative_season_id=fields["narrative_season_id"].get(),
                    narrative_section_id=fields["narrative_section_id"].get(),
                    song_collection_title=fields["song_collection_title"].get(),
                    song_season_id=fields["song_season_id"].get(),
                    song_section_id=fields["song_section_id"].get(),
                )
                self.refresh_creator_profiles(key)
                profile_box.configure(values=list(self.profile_labels))
                selected.set(self.vars["creator"].get())
                load_profile(key)
                self.append_log(f"已保存人物与字幕模板：{key}\n")
                messagebox.showinfo(
                    APP_TITLE,
                    "人物、字幕模板、多人封面立绘与投稿配置已保存；设置立即生效。",
                    parent=window,
                )
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        def remove_profile() -> None:
            key = fields["key"].get().strip()
            profile = self.profiles.get(key)
            if not key or not profile or profile.get("archived"):
                messagebox.showwarning(APP_TITLE, "请先选择一个可用人物。", parent=window)
                return

            references: list[str] = []
            manual_state = self.read_state()
            if manual_state and manual_state.get("config", {}).get("creator") == key:
                _, _, statuses = manual_progress_snapshot(manual_state)
                if any(status not in {"completed", "published"} for status in statuses):
                    references.append("当前手动项目")
            try:
                auto_config = auto.load_config(self.auto_config_path)
                auto_state = auto.load_state(auto_config)
                terminal_statuses = {
                    "published", "cleaned_published", "cleaned_discarded", "no_candidates"
                }
                for job in auto_state.get("jobs", {}).values():
                    if (
                        str(job.get("creator") or "") == key
                        and str(job.get("status") or "") not in terminal_statuses
                    ):
                        references.append(
                            str(job.get("title") or job.get("id") or "未命名自动任务")
                        )
            except Exception:
                pass
            if references:
                preview = "\n".join(f"• {value}" for value in references[:6])
                messagebox.showerror(
                    APP_TITLE,
                    "该人物仍被未完成任务使用，请先完成、废弃或改派这些任务：\n" + preview,
                    parent=window,
                )
                return

            name = str(profile.get("display_name") or key)
            if not messagebox.askyesno(
                APP_TITLE,
                f"确认从字幕识别人物列表移除“{name}”吗？\n\n"
                "人物配置会保留为可恢复归档；不会删除录播、工程、字幕或成片。\n"
                "同一房间不会再次被自动添加；以后用相同人物代号保存即可恢复。",
                icon="warning",
                parent=window,
            ):
                return
            try:
                creator_profiles.archive_profile(key)
                self.refresh_creator_profiles()
                profile_box.configure(values=list(self.profile_labels))
                selected.set(self.vars["creator"].get())
                load_profile()
                self.append_log(f"已从人物列表移除并归档：{key} — {name}\n")
                messagebox.showinfo(
                    APP_TITLE,
                    f"已移除“{name}”。人物配置已归档，素材和历史项目未删除。",
                    parent=window,
                )
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        profile_box.bind("<<ComboboxSelected>>", lambda _event: load_profile())
        buttons = ttk.Frame(outer)
        buttons.pack(fill=X)
        ttk.Button(buttons, text="预览当前人物与字幕", command=preview_profile).pack(
            side=LEFT
        )
        ttk.Button(buttons, text="保存人物、字幕与投稿设置", command=save_profile).pack(
            side=LEFT, padx=(6, 0)
        )
        ttk.Button(
            buttons,
            text="打开完整人物配置 JSON",
            command=lambda: self.open_path(core.PROFILES_PATH),
        ).pack(side=LEFT, padx=6)
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side=RIGHT)
        load_profile()

    def open_media_packaging_settings(self):
        """Configure shared Flow 4 bookends without changing any review timeline."""
        import media_packaging
        path = core.WORKSPACE_ROOT / "media-packaging.json"
        try:
            settings = media_packaging.load_media_packaging(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"片头片尾设置读取失败：{exc}")
            return None
        existing = self.__dict__.get("_media_packaging_window")
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return existing
        window = Toplevel(self)
        window.title("片头片尾 · 烧录设置")
        window.geometry("820x420")
        window.minsize(740, 360)
        window.transient(self)
        self._media_packaging_window = window
        fields = {
            "enabled": BooleanVar(master=window, value=settings["enabled"]),
            "intro_path": StringVar(master=window, value=settings["intro_path"]),
            "outro_path": StringVar(master=window, value=settings["outro_path"]),
            "status": StringVar(master=window, value="应用于后续烧录；已有成片需从流程4重新烧录。"),
        }
        self._media_packaging_fields = fields
        outer = ttk.Frame(window, padding=16)
        outer.pack(fill=BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        ttk.Checkbutton(outer, text="烧录时自动加入片头片尾", variable=fields["enabled"]).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 14))
        def choose(key):
            chosen = filedialog.askopenfilename(parent=window, title="选择片头" if key == "intro_path" else "选择片尾",
                filetypes=[("视频素材", "*.mp4 *.mov *.mkv *.webm *.avi"), ("所有文件", "*.*")])
            if chosen:
                fields[key].set(chosen)
        for index, (key, label) in enumerate((("intro_path", "片头视频"), ("outro_path", "片尾视频")), 1):
            ttk.Label(outer, text=label).grid(row=index, column=0, sticky="w", padx=(0, 8), pady=6)
            ttk.Entry(outer, textvariable=fields[key]).grid(row=index, column=1, sticky="ew", pady=6)
            ttk.Button(outer, text="选择", command=lambda name=key: choose(name)).grid(row=index, column=2, padx=(6, 0))
            ttk.Button(outer, text="清空", command=lambda name=key: fields[name].set("")).grid(row=index, column=3, padx=(6, 0))
        note = ttk.Label(outer, text="可只设置片头或片尾。用于自动队列和未单独配置的项目；字幕会按片头时长同步后移。",
                        wraplength=720, justify="left", foreground="#555555")
        note.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(12, 8))
        window.bind("<Configure>", lambda _event: note.configure(wraplength=max(300, outer.winfo_width() - 32)), add="+")
        ttk.Label(outer, textvariable=fields["status"], wraplength=700, justify="left").grid(
            row=4, column=0, columnspan=4, sticky="ew", pady=8)
        def save(_event=None):
            try:
                media_packaging.save_media_packaging(path, {key: fields[key].get() for key in ("enabled", "intro_path", "outro_path")})
            except Exception as exc:
                fields["status"].set(f"保存失败：{exc}")
                messagebox.showerror(APP_TITLE, f"片头片尾设置保存失败：{exc}", parent=window)
            else:
                fields["status"].set("已保存，将在下一次烧录时应用。")
                self.append_log("片头片尾设置已保存；后续烧录使用新配置。\n")
            return "break"
        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=4, sticky="e", pady=(10, 0))
        self._media_packaging_save_button = ttk.Button(actions, text="保存烧录设置", style="Accent.TButton", command=save)
        self._media_packaging_save_button.pack(side=LEFT, padx=(0, 8))
        ttk.Button(actions, text="关闭", command=window.destroy).pack(side=LEFT)
        window.bind("<Control-s>", save)
        window.bind("<Control-S>", save)
        window.update_idletasks()
        # A fixed 360px minimum clips the save row with 150% system fonts.
        window.minsize(740, max(360, outer.winfo_reqheight()))
        return window

    def open_hotword_editor(self) -> None:
        creator_key = self.profile_labels[self.vars["creator"].get()]
        general_path = core.SKILL_ROOT / "references" / "general-hotwords.json"
        profiles_path = core.PROFILES_PATH
        try:
            general_data = json.loads(general_path.read_text(encoding="utf-8-sig"))
            profiles_data = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
            profile = profiles_data["profiles"][creator_key]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"热词配置读取失败：{exc}")
            return

        window = Toplevel(self)
        window.title(f"热词设置｜{profile.get('display_name', creator_key)}")
        window.geometry("860x720")
        window.minsize(680, 560)
        window.transient(self)
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer,
            text="每行一个词；热词用于字幕校验和专名核对，不直接注入识别模型，避免词表被复读。已核实的纠错词典仍在转写后应用。",
            wraplength=800,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        def editor(label: str, values: list[str], height: int) -> ScrolledText:
            frame = ttk.LabelFrame(outer, text=f"{label}（{len(values)} 条）", padding=7)
            frame.pack(fill=BOTH, expand=True, pady=(0, 8))
            box = ScrolledText(frame, wrap="word", height=height, font=(UI_FONT, 13))
            box.pack(fill=BOTH, expand=True)
            box.insert("1.0", "\n".join(str(value) for value in values))
            return box

        general_box = editor("通用热词", list(general_data.get("terms", [])), 9)
        creator_box = editor("主播专属热词", list(profile.get("hotwords", [])), 8)
        stream_box = editor("本场/常见口癖热词", list(profile.get("stream_hotwords", [])), 5)

        def parse_terms(box: ScrolledText, label: str, *, required: bool = False) -> list[str]:
            values: list[str] = []
            seen: set[str] = set()
            for raw in box.get("1.0", END).splitlines():
                term = raw.strip()
                if not term:
                    continue
                folded = term.casefold()
                if folded in seen:
                    raise ValueError(f"{label}存在重复词：{term}")
                seen.add(folded)
                values.append(term)
            if required and not values:
                raise ValueError(f"{label}不能为空")
            return values

        def atomic_json(path: Path, value: dict) -> None:
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)

        def save() -> None:
            try:
                general_terms = parse_terms(general_box, "通用热词", required=True)
                creator_terms = parse_terms(creator_box, "主播专属热词")
                stream_terms = parse_terms(stream_box, "本场热词")
                general_data["terms"] = general_terms
                general_data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                profile["hotwords"] = creator_terms
                profile["stream_hotwords"] = stream_terms
                atomic_json(general_path, general_data)
                atomic_json(profiles_path, profiles_data)
                self.profiles = core.load_profiles()
                messagebox.showinfo(
                    APP_TITLE,
                    f"已保存：通用 {len(general_terms)} 条，主播专属 {len(creator_terms)} 条，本场 {len(stream_terms)} 条。\n下一次转写自动生效。",
                    parent=window,
                )
                window.destroy()
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X)
        ttk.Button(buttons, text="保存并用于下一次转写", command=save).pack(side=LEFT)
        ttk.Button(
            buttons, text="打开通用热词 JSON", command=lambda: self.open_path(general_path)
        ).pack(side=LEFT, padx=6)
        ttk.Button(
            buttons, text="打开主播配置 JSON", command=lambda: self.open_path(profiles_path)
        ).pack(side=LEFT)
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=RIGHT)

    def _device_changed(self, _event=None) -> None:
        self.vars["compute_type"].set("float16" if self.vars["device"].get() == "cuda" else "int8")

    def _choose_project(self) -> None:
        value = filedialog.askdirectory(title="选择或创建项目文件夹")
        if value:
            self.vars["project"].set(value)

    def _choose_source(self) -> None:
        value = filedialog.askopenfilename(
            title="选择录播视频",
            filetypes=[("媒体文件", "*.flv *.mp4 *.mkv *.mov *.ts *.m4a *.wav"), ("全部文件", "*.*")],
        )
        if value:
            self.vars["source"].set(value)
            self._sync_xml_for_source(Path(value), force=True, announce=True)
            current = Path(self.project_dir())
            if not (current / core.PROJECT_FILENAME).is_file():
                safe = "".join(char if char not in '<>:"/\\|?*' else "_" for char in Path(value).stem)
                self.vars["project"].set(
                    str(core.WORKSPACE_ROOT / "workflow-projects" / f"{datetime.now():%Y%m%d}-{safe[:48]}")
                )

    def _choose_xml(self) -> None:
        value = filedialog.askopenfilename(
            title="选择弹幕 XML", filetypes=[("XML", "*.xml"), ("全部文件", "*.*")]
        )
        if value:
            self.vars["xml"].set(value)
            self._auto_xml_path = ""
            self.vars["xml_status"].set(f"已手动选择：{Path(value).name}")

    def _source_changed(self, *_args) -> None:
        value = self.vars["source"].get().strip()
        if value:
            self._sync_xml_for_source(Path(value), force=False, announce=False)

    def _sync_xml_for_source(
        self, source: Path, *, force: bool, announce: bool
    ) -> Path | None:
        if not source.is_file():
            return None
        candidate = auto.companion_xml(source)
        current = self.vars["xml"].get().strip()
        if candidate is not None:
            candidate_text = str(candidate)
            if force or not current or current == self._auto_xml_path:
                self.vars["xml"].set(candidate_text)
                self._auto_xml_path = candidate_text
                self.vars["xml_status"].set(f"已自动匹配：{candidate.name}")
                if announce:
                    self.append_log(f"已自动绑定弹幕：{candidate}\n")
            elif Path(current).resolve() == candidate:
                self._auto_xml_path = candidate_text
                self.vars["xml_status"].set(f"已绑定同名弹幕：{candidate.name}")
            else:
                self.vars["xml_status"].set("保留当前手动 XML；可点“选择”更换")
            return candidate
        if current and current == self._auto_xml_path:
            self.vars["xml"].set("")
        self._auto_xml_path = ""
        self.vars["xml_status"].set("未找到可用的非空同名 XML，可手动选择")
        if announce:
            self.append_log(f"未找到同名弹幕 XML：{source.name}\n")
        return None

    def _choose_watch_root(self) -> None:
        value = filedialog.askdirectory(title="选择录播监控文件夹")
        if value:
            self.vars["watch_root"].set(value)

    def _choose_auto_output(self) -> None:
        value = filedialog.askdirectory(title="选择自动项目输出文件夹")
        if value:
            self.vars["auto_output"].set(value)

    def _choose_codex(self) -> None:
        value = filedialog.askopenfilename(
            title="选择独立 Codex CLI",
            filetypes=[("Codex CLI", "codex.exe codex.cmd"), ("全部文件", "*.*")],
        )
        if value:
            self.vars["codex_command"].set(value)

    def project_dir(self) -> str:
        return self.vars["project"].get().strip()

    def _save_active_context(self) -> bool:
        """Save the active tab without requiring the user to hunt for tab-specific buttons."""
        current_tab = self.notebook.select()
        if current_tab == str(self.review_tab):
            if not self.current_review_video:
                self.vars["status"].set("审核台当前没有选中的切片，无需保存")
                return True
            if not self.save_current_review_copy(silent=True, draft=True):
                return False
            if self.current_review_ass:
                selected = self.review_subtitle_tree.selection()
                if selected and not self.save_current_subtitle(silent=True):
                    return False
            if "review_notes" in self.vars:
                try:
                    review_workspace.set_review_notes(self.current_review_dir, self.current_review_video.name, self.vars["review_notes"].get())
                except Exception as exc:
                    self.vars["review_line_status"].set(f"审核理由保存失败：{exc}")
                    self.vars["status"].set(f"审核理由保存失败：{exc}")
                    return False
            if "review_suggestions" in self.vars and not self.save_current_review_suggestions(silent=True):
                self.vars["status"].set("审核建议保存失败，请重试保存")
                return False
            self.vars["status"].set("当前审核切片已保存")
            self.append_log("当前审核切片、标题和字幕已保存。\n")
            return True
        if current_tab == str(self.dashboard_tab):
            if self.dashboard_panel == "manual":
                return self.save_project()
            try:
                path = self.save_auto_config()
                self.vars["status"].set(f"自动流程设置已保存：{path}")
                self.append_log(f"自动流程设置已保存：{path}\n")
                return True
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc))
                return False
        return False

    def save_workbench(self, event=None):
        saved = self._save_active_context()
        if saved:
            self.vars["status"].set("保存完成")
        if event is not None:
            return "break"
        return saved
    def save_project(self, quiet: bool = False) -> bool:
        try:
            project = Path(self.project_dir())
            source = Path(self.vars["source"].get().strip())
            xml_text = self.vars["xml"].get().strip()
            path = core.init_project(
                project=project,
                source=source,
                xml=Path(xml_text) if xml_text else None,
                creator=self.profile_labels[self.vars["creator"].get()],
                mode=MODE_LABELS[self.vars["mode"].get()],
                model=self.vars["model"].get().strip(),
                device=self.vars["device"].get(),
                compute_type=self.vars["compute_type"].get().strip(),
            )
            self.vars["status"].set(f"项目已保存：{path.parent}")
            if not quiet:
                self.append_log(f"项目已保存：{path}\n")
            return True
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return False

    def save_auto_config(self) -> Path:
        stable = int(self.vars["stable_seconds"].get().strip())
        quiet = int(self.vars["quiet_seconds"].get().strip())
        if stable < 0 or quiet < stable:
            raise ValueError("断线重连观察秒数必须大于或等于文件稳定秒数")
        payload = auto.default_config()
        if self.auto_config_path.is_file():
            saved = auto.load_config(self.auto_config_path)
            for key in ("codex_model", "codex_reasoning_effort"):
                value = str(saved.get(key, "")).strip()
                if value:
                    payload[key] = value
        model_var = self.vars.get("selection_model")
        if model_var is not None:
            pair = auto.CODEX_MODEL_PRESETS.get(model_var.get())
            if pair is not None:
                payload["codex_model"], payload["codex_reasoning_effort"] = pair
        payload.update(
            {
                "watch_root": self.vars["watch_root"].get().strip(),
                "output_root": self.vars["auto_output"].get().strip(),
                "state_file": str(Path(self.vars["auto_output"].get().strip()) / "workflow-auto-state.json"),
                "stable_seconds": stable,
                "session_quiet_seconds": quiet,
                "backfill_existing": bool(self.auto_backfill.get()),
                "allow_burn": bool(self.auto_burn.get()),
                "allow_upload": bool(self.auto_upload.get()),
                "scheduled_delivery_enabled": bool(
                    self.scheduled_delivery_enabled.get()
                ),
                "scheduled_delivery_time": auto.normalize_daily_time(
                    self.vars["scheduled_delivery_time"].get()
                ),
                "mode": MODE_LABELS[self.vars["mode"].get()],
                "codex_command": self.vars["codex_command"].get().strip(),
                "asr": {
                    "model": self.vars["model"].get().strip(),
                    "device": self.vars["device"].get(),
                    "compute_type": self.vars["compute_type"].get().strip(),
                },
            }
        )
        return auto.save_config(self.auto_config_path, payload)

    def _auto_permission_changed(self, permission: str) -> None:
        if permission == "upload" and self.auto_upload.get():
            confirmed = messagebox.askokcancel(
                APP_TITLE,
                "允许未人工审核任务自动投稿后，监控会先完成自动烧录和技术审计，再公开投稿到 B 站。确认启用吗？",
                icon="warning",
            )
            if not confirmed:
                self.auto_upload.set(False)
            else:
                self.auto_burn.set(True)
        elif permission == "burn" and self.auto_burn.get():
            if not messagebox.askyesno(
                APP_TITLE,
                "确认允许自动监控在机器审计通过后无需逐条人工审核直接烧录吗？",
            ):
                self.auto_burn.set(False)
        if not self.auto_burn.get() and self.auto_upload.get():
            self.auto_upload.set(False)
        try:
            self.save_auto_config()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if self.monitor_process is not None:
            self.vars["auto_status"].set("授权设置已保存｜当前安全阶段结束后自动生效")
            self.append_log("\n自动监控授权已更新；后台将在下一安全轮次重新读取，无需重启。\n")
        else:
            self.vars["auto_status"].set("授权设置已保存｜启动监控后生效")

    def load_project(self) -> None:
        initial = self.project_dir() or str(core.WORKSPACE_ROOT)
        value = filedialog.askopenfilename(
            title="选择 workflow-project.json",
            initialdir=initial if Path(initial).is_dir() else str(core.WORKSPACE_ROOT),
            filetypes=[("工作台项目", "workflow-project.json"), ("JSON", "*.json")],
        )
        if not value:
            return
        try:
            path, state = core.load_project(Path(value))
            config = state["config"]
            self.vars["project"].set(str(path.parent))
            self.vars["source"].set(config["source"])
            self.vars["xml"].set(config.get("xml", ""))
            self.vars["creator"].set(
                next(key for key, profile in self.profile_labels.items() if profile == config["creator"])
            )
            self.vars["mode"].set(next(key for key, mode in MODE_LABELS.items() if mode == config["mode"]))
            self.vars["model"].set(config["asr"]["model"])
            self.vars["device"].set(config["asr"]["device"])
            self.vars["compute_type"].set(config["asr"]["compute_type"])
            self.refresh_status()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def core_command(self, args: list[str]) -> list[str]:
        return [sys.executable, "-X", "utf8", str(core.SCRIPTS / "workflow_app_core.py"), *map(str, args)]

    def auto_command(
        self,
        command: str,
        allow_flags: bool = False,
        extra: list[str] | None = None,
    ) -> list[str]:
        result = [
            sys.executable,
            "-X",
            "utf8",
            str(core.SCRIPTS / "workflow_auto.py"),
            "--config",
            str(self.auto_config_path),
            command,
        ]
        if allow_flags and self.auto_burn.get():
            result.append("--allow-burn")
        if allow_flags and self.auto_upload.get():
            result.append("--allow-upload")
        if extra:
            result.extend(extra)
        return result

    def run_core(self, args: list[str], after=None, save_first: bool = True) -> None:
        if save_first and not self.save_project(quiet=True):
            return
        self.run_command(self.core_command(args), after=after)

    def run_command(self, command: list[str], after=None, *, flow_key: str = "",
                    label: str = "", on_failure=None) -> bool:
        key = flow_key or command_flow_key(command)
        if key == "manual:publish":
            publishing = [record["command"] for record in
                          [*self.active_commands.values(), *self.pending_commands]
                          if record["flow_key"] == key]
            busy_ids = {target for previous in publishing for target in command_arguments(previous, "--job-id")}
            requested_ids = command_arguments(command, "--job-id")
            duplicate_ids = set(requested_ids) & busy_ids
            if duplicate_ids and "run-jobs" in command and set(requested_ids) - duplicate_ids:
                filtered = []
                index = 0
                while index < len(command):
                    part = command[index]
                    if part == "--job-id" and index + 1 < len(command):
                        if command[index + 1] not in duplicate_ids:
                            filtered.extend(command[index:index + 2])
                        index += 2
                        continue
                    if part.startswith("--job-id=") and part.split("=", 1)[1] in duplicate_ids:
                        index += 1
                        continue
                    filtered.append(part)
                    index += 1
                command = filtered
                self.append_log(f"\n批量发布跳过 {len(duplicate_ids)} 个已在队列中的任务，其余继续排队。\n")
            elif duplicate_ids or any(
                list(command) == previous or
                set(command_arguments(command, "--project")) & set(command_arguments(previous, "--project"))
                for previous in publishing
            ):
                self.vars["status"].set("此发布任务已在执行或排队，无需重复提交")
                self.append_log("\n已跳过重复发布请求；原任务继续执行。\n")
                return False
        self.command_counter += 1
        record = {
            "id": self.command_counter,
            "flow_key": key,
            "command": list(command),
            "after": after,
            "label": label,
            "on_failure": on_failure,
        }
        if key in self.active_commands or len(self.active_commands) >= MAX_PARALLEL_COMMANDS:
            self.pending_commands.append(record)
            self._sync_manual_activity_marker()
            self.vars["status"].set(
                f"{key} 已排队｜运行 {len(self.active_commands)}/{MAX_PARALLEL_COMMANDS}｜"
                f"等待 {len(self.pending_commands)}"
            )
            self.append_log(
                f"\n[{key}] 已排队（同流程串行，最多双开）："
                + subprocess.list2cmdline(command)
                + "\n"
            )
            self.refresh_execution_panel()
            return True
        self._launch_command(record)
        self.refresh_execution_panel()
        return True

    def _launch_command(self, record: dict) -> None:
        key = str(record["flow_key"])
        task_id = int(record["id"])
        command = list(record["command"])
        self.active_commands[key] = record
        self._sync_manual_activity_marker()
        self.append_log(f"\n[{key}] > " + subprocess.list2cmdline(command) + "\n")
        self.vars["status"].set(
            f"并行任务 {len(self.active_commands)}/{MAX_PARALLEL_COMMANDS} 运行中"
        )

        def worker() -> None:
            automatic = not key.startswith("manual:")
            flags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                | (
                    int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
                    if automatic else 0
                )
                if os.name == "nt" else 0
            )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(core.WORKSPACE_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                self.output_queue.put(
                    ("task_started", {"id": task_id, "process": process})
                )
                assert process.stdout is not None
                for line in process.stdout:
                    self.output_queue.put(
                        ("task_line", {"id": task_id, "flow_key": key, "line": line})
                    )
                self.output_queue.put(
                    ("task_done", {"id": task_id, "code": process.wait()})
                )
            except Exception as exc:
                self.output_queue.put(
                    ("task_error", {"id": task_id, "error": str(exc)})
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_command(
        self, task_id: int, *, code: int | None = None, error: str = ""
    ) -> None:
        match = next(
            (
                (key, record)
                for key, record in self.active_commands.items()
                if int(record["id"]) == int(task_id)
            ),
            None,
        )
        self.task_processes.pop(int(task_id), None)
        self.process = next(iter(self.task_processes.values()), None)
        if match is None:
            self._launch_ready_commands()
            return
        key, record = match
        self.active_commands.pop(key, None)
        try:
            self._sync_manual_activity_marker()
            if error:
                self.append_log(f"[{key}] 启动失败：{error}\n")
            else:
                if code == 0:
                    result = "完成"
                else:
                    diagnosis = publish_diagnostics.publish_error_summary(str(record.get("output_tail", "")))
                    detail = diagnosis["detail"] or "程序未成功完成，请查看此任务上方的具体错误"
                    result = f"失败：{detail}（退出码 {code}）"
                self.append_log(f"[{key}] {result}\n")
            callback = record.get("after") if code == 0 and not error else record.get("on_failure")
            if callback:
                callback()
        except Exception:
            self.append_log("任务收尾发生错误，后续任务继续：\n" + traceback.format_exc())
        finally:
            try:
                self.refresh_status(silent=True)
            except Exception:
                self.append_log("状态刷新失败：\n" + traceback.format_exc())
            finally:
                self._launch_ready_commands()
                self._sync_manual_activity_marker()
                self.refresh_execution_panel()

    def _launch_ready_commands(self) -> None:
        while self.pending_commands and len(self.active_commands) < MAX_PARALLEL_COMMANDS:
            selected = next(
                (
                    index
                    for index, record in enumerate(self.pending_commands)
                    if str(record["flow_key"]) not in self.active_commands
                ),
                None,
            )
            if selected is None:
                break
            record = self.pending_commands[selected]
            del self.pending_commands[selected]
            self._launch_command(record)
        if not self.active_commands and not self.pending_commands:
            self.vars["status"].set("任务队列空闲")
        elif self.pending_commands:
            self.vars["status"].set(
                f"运行 {len(self.active_commands)}/{MAX_PARALLEL_COMMANDS}｜"
                f"排队 {len(self.pending_commands)}"
            )

    def _auto_start_safe_monitor(self) -> None:
        if self.monitor_process is not None:
            return
        watch_root = Path(self.vars["watch_root"].get().strip())
        if not watch_root.is_dir():
            self.vars["auto_status"].set("未自动启动：请先选择有效的录播文件夹")
            return
        if self.auto_backfill.get() or self.auto_burn.get() or self.auto_upload.get():
            self.vars["auto_status"].set("自动启动已暂停：高影响选项需要手动点击启动并确认")
            return
        self.start_monitor(automatic=True)

    def start_monitor(self, automatic: bool = False) -> None:
        if self.monitor_process is not None or self._monitor_starting:
            if not automatic:
                messagebox.showwarning(APP_TITLE, "自动监控已经在运行或启动中。")
            return
        try:
            self.save_auto_config()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if not automatic and self.auto_backfill.get() and not messagebox.askyesno(
            APP_TITLE, "已勾选处理已有素材，可能产生大量任务。确认继续吗？"
        ):
            return
        if not automatic and self.auto_burn.get() and not messagebox.askyesno(
            APP_TITLE,
            "已允许无需逐条人工审核自动烧录；程序将采用现有字幕/机器歌词，并继续执行素材完整性与最终媒体审计。确认本次运行允许吗？",
        ):
            return
        if not automatic and self.auto_upload.get() and not messagebox.askokcancel(
            APP_TITLE,
            "本次监控允许无需人工审核直接公开投稿到 B 站；pending 切片会采用现有字幕、标题与封面。\n\n"
            "选择“确认”继续，选择“取消”保持仅审核模式。",
            icon="warning",
        ):
            return
        command = self.auto_command("watch", allow_flags=False)
        self.append_log("\n> " + subprocess.list2cmdline(command) + "\n")
        self.vars["auto_status"].set(
            "工作台启动，正在自动开启安全监控…" if automatic else "自动监控启动中…"
        )

        self._monitor_starting = True

        def worker() -> None:
            flags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                | int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
                if os.name == "nt" else 0
            )
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(core.WORKSPACE_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                self.monitor_process = process
                log_path = core.WORKSPACE_ROOT / "logs" / f"workflow-monitor-{core.run_stamp()}.log"

                def record_log(line: str) -> None:
                    try:
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        with log_path.open("a", encoding="utf-8") as handle:
                            handle.write(line)
                    except OSError:
                        pass

                record_log(f"监控进程 PID={process.pid}\n")
                assert process.stdout is not None
                for line in process.stdout:
                    record_log(line)
                    self.output_queue.put(("monitor_line", line))
                code = process.wait()
                record_log(f"\n监控进程退出，代码={code}\n")
                self.output_queue.put(
                    ("monitor_done", {"process": process, "code": code})
                )
            except Exception as exc:
                self.output_queue.put(
                    ("monitor_error", {"process": process, "error": str(exc)})
                )

        threading.Thread(target=worker, daemon=True).start()

    def start_pending_queue(
        self, automatic: bool = False, *, safe_only: bool = False
    ) -> None:
        try:
            self.save_auto_config()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if (
            not safe_only
            and not automatic
            and self.auto_burn.get()
            and not messagebox.askyesno(
                APP_TITLE,
                "现有排队任务将允许无需人工审核继续烧录。确认本次允许吗？",
            )
        ):
            return
        if (
            not safe_only
            and not automatic
            and self.auto_upload.get()
            and not messagebox.askokcancel(
                APP_TITLE,
                "现有排队任务将允许烧录后直接公开投稿到 B 站。确认本次允许吗？",
                icon="warning",
            )
        ):
            return

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool()
            return False

        self._run_selected_auto_command(
            self.auto_command("run-queue", allow_flags=not safe_only),
            action_name=("安全执行已排队任务" if safe_only else "执行已排队任务"),
            after=completed,
        )

    def start_batch(self) -> None:
        try:
            self.save_auto_config()
            start, end = auto.parse_batch_range(
                self.vars["batch_start"].get(),
                self.vars["batch_end"].get(),
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "将批量纳入开始时间或持续区间与下列范围相交的已稳定完整场次：\n"
            f"{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}\n\n"
            "同场断点会先合并，已经存在的任务会复用而不会复制。确认继续吗？",
        ):
            return
        if self.auto_burn.get() and not messagebox.askyesno(
            APP_TITLE,
            "批量任务将允许无需人工审核直接烧录，并继续执行素材与媒体技术审计。确认本次允许吗？",
        ):
            return
        if self.auto_upload.get() and not messagebox.askokcancel(
            APP_TITLE,
            "本次批量任务允许无需人工审核直接公开投稿到 B 站。\n\n"
            "选择“确认”继续，选择“取消”不启动。",
            icon="warning",
        ):
            return
        command = self.auto_command(
            "batch",
            allow_flags=True,
            extra=[
                "--start",
                self.vars["batch_start"].get().strip(),
                "--end",
                self.vars["batch_end"].get().strip(),
            ],
        )
        self.vars["auto_status"].set("时间段批量任务运行中…")
        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            return False

        self._run_selected_auto_command(
            command, action_name="时间段批量任务", after=completed
        )

    def stop_monitor(self, ask: bool = True) -> None:
        process = self.monitor_process
        if process is None:
            return
        if ask and not messagebox.askyesno(APP_TITLE, "确定停止自动监控及当前自动任务树吗？"):
            return
        if self._kill_tree(process):
            if self.monitor_process is process:
                self.monitor_process = None
            self._monitor_starting = False
            try:
                recovered = self._recover_interrupted_auto_jobs()
            except Exception as exc:
                recovered = 0
                self.append_log(f"\n停止后的任务状态恢复失败：{exc}\n")
            detail = (
                f"；{recovered} 个中断任务已保留成果并恢复为可续跑"
                if recovered else ""
            )
            self.vars["auto_status"].set(f"监控已停止{detail}")
            self.append_log(f"\n自动监控已由用户停止{detail}。\n")
            self.refresh_auto_status(silent=True)
        else:
            messagebox.showerror(
                APP_TITLE,
                "自动监控进程树在 12 秒内没有退出，请再次停止或关闭工作台。",
            )

    def doctor_auto(self) -> None:
        try:
            self.save_auto_config()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._run_selected_auto_command(
            self.auto_command("doctor"), action_name="队列环境检查"
        )

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, value = self.output_queue.get_nowait()
                try:
                    if kind in {"line", "monitor_line"}:
                        self.append_log(str(value))
                        if kind == "monitor_line":
                            self.vars["auto_status"].set("监控运行中｜任务进度每 1.5 秒刷新")
                    elif kind == "task_started":
                        task_id = int(value["id"])
                        self.task_processes[task_id] = value["process"]
                        self.process = next(iter(self.task_processes.values()), None)
                    elif kind == "task_line":
                        record = next((item for item in self.active_commands.values()
                                       if int(item["id"]) == int(value["id"])), None)
                        if record is not None:
                            record["output_tail"] = (str(record.get("output_tail", "")) + str(value["line"]))[-131072:]
                        self.append_log(f"[{value['flow_key']}] {value['line']}")
                    elif kind == "task_done":
                        self._finish_command(
                            int(value["id"]), code=int(value["code"])
                        )
                    elif kind == "task_error":
                        self._finish_command(
                            int(value["id"]), error=str(value["error"])
                        )
                    elif kind == "monitor_done":
                        payload = value if isinstance(value, dict) else {"process": self.monitor_process, "code": value}
                        completed_process = payload.get("process")
                        code = int(payload.get("code", 1))
                        if completed_process is self.monitor_process:
                            self.monitor_process = None
                            self._monitor_starting = False
                            self.vars["auto_status"].set(
                                "监控已停止" if code in {0, 130} else
                                f"监控异常退出（{code}）；待处理任务已保留，请重新启动监控"
                            )
                            self.refresh_auto_status(silent=True)
                    elif kind == "waveform":
                        self.finish_review_waveform(value)
                    elif kind == "timeline_render":
                        self.finish_timeline_render(value)
                    elif kind == "review_stream":
                        self.finish_streaming_review(value)
                    elif kind == "monitor_error":
                        payload = value if isinstance(value, dict) else {"process": self.monitor_process, "error": value}
                        failed_process = payload.get("process")
                        if failed_process is None or failed_process is self.monitor_process:
                            self.monitor_process = None
                            self._monitor_starting = False
                            self.vars["auto_status"].set("监控启动失败")
                            messagebox.showerror(APP_TITLE, str(payload.get("error", "未知错误")))
                except Exception:
                    self.report_callback_exception(*sys.exc_info())
        except queue.Empty:
            pass
        finally:
            self.after(120, self._drain_queue)

    def report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            path = core.WORKSPACE_ROOT / "logs" / "workbench-callback-errors.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{detail}")
        except OSError:
            pass
        self.append_log(f"\n界面任务处理失败：{exc_value}\n{detail}")

    def append_log(self, value: str) -> None:
        viewer = self.__dict__.get("_console_popup_text")
        if viewer is not None and viewer.winfo_exists():
            viewer.insert(END, value)
            viewer.see(END)
        if not hasattr(self, "log"):
            return
        self.log.insert(END, value)
        self.log.see(END)

    def _kill_tree(self, process: subprocess.Popen[str], wait_seconds: float = 12.0) -> bool:
        if process.poll() is not None:
            return True
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=max(15.0, float(wait_seconds) + 3.0),
                    **hidden_subprocess_kwargs(),
                )
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            process.terminate()
        try:
            process.wait(timeout=max(0.1, float(wait_seconds)))
            return True
        except subprocess.TimeoutExpired:
            return process.poll() is not None

    def _recover_interrupted_auto_jobs(self) -> int:
        """Release a dead monitor lock and make interrupted jobs resumable."""
        config = auto.load_config(self.auto_config_path)
        with auto.queue_lock(
            config["_state_file"],
            conflict_message="正在恢复被中断的自动任务",
            wait_seconds=5.0,
        ):
            before = auto.load_state(config)
            running_ids = {
                str(job_id)
                for job_id, job in before.get("jobs", {}).items()
                if str(job.get("status", "")) == "running"
            }
            state = auto.load_state(config, recover_running=True)
            auto.save_state(config, state)
        return len(running_ids)

    def cancel_process(self) -> None:
        if not self.task_processes and not self.pending_commands:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"确定终止 {len(self.task_processes)} 个运行任务并清空 {len(self.pending_commands)} 个排队任务吗？",
        ):
            return
        self.pending_commands.clear()
        self._sync_manual_activity_marker()
        self.refresh_execution_panel()
        for process in list(self.task_processes.values()):
            self._kill_tree(process)
        self.append_log("\n所有运行中的手动任务已取消；等待队列已清空。\n")

    def choose_selection_json(self) -> None:
        value = filedialog.askopenfilename(
            title="选择模型返回的选片 JSON", filetypes=[("JSON", "*.json"), ("全部文件", "*.*")]
        )
        if value:
            self.run_core(["import-selection", "--project", self.project_dir(), "--json", value])

    def copy_prompt(self) -> None:
        project_dir = self.project_dir()
        path = Path(project_dir) / "selection" / "selection-prompt.md"

        def copy_generated_prompt() -> None:
            try:
                prompt = path.read_text(encoding="utf-8")
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"无法读取新生成的 Prompt：{exc}")
                return
            self.clipboard_clear()
            self.clipboard_append(prompt)
            self.vars["status"].set("最新 Prompt 已生成并复制到剪贴板")

        self.run_core(
            ["prompt", "--project", project_dir], after=copy_generated_prompt
        )

    def flow2_prompt_description(self) -> str:
        try:
            config = (
                auto.load_config(self.auto_config_path)
                if self.auto_config_path.is_file() else auto.default_config()
            )
            model = str(config.get("codex_model", auto.DEFAULT_CODEX_MODEL))
            effort = str(config.get("codex_reasoning_effort", auto.DEFAULT_CODEX_REASONING_EFFORT))
            model_label = auto.codex_model_label(model, effort)
        except Exception:
            model_label = "配置读取失败，请检查自动任务设置"
        return (
            f"自动选片：{model_label}。\n"
            "副标题：具体事件＋观众吐槽，允许 2–3 句。\n"
            "生成或复制都会更新 Prompt；支持手动导入选片。"
        )

    def start_burn(self) -> None:
        choice = messagebox.askyesnocancel(
            APP_TITLE,
            "是否已经完成人工审核？\n\n"
            "是：按人工审核结果烧录。\n"
            "否：跳过人工审核，直接采用现有片段、字幕/机器歌词和封面烧录。\n"
            "取消：暂不烧录。\n\n"
            "无论选择哪种方式，缺失素材、未应用剪辑和最终媒体审计仍会拦截。",
        )
        if choice is None:
            return
        review_dir = self.__dict__.get("current_review_dir")
        if review_dir and self.__dict__.get("current_review_video"):
            review_project = review_workspace._workflow_project_root(review_dir)
            if review_project and review_project.resolve() == Path(self.project_dir()).resolve():
                if not self.save_current_review_copy(silent=True):
                    return
        review_flag = "--confirm-reviewed" if choice else "--skip-human-review"
        self.run_core(
            ["step4", "--project", self.project_dir(), review_flag],
            after=self.open_delivery_folder,
        )

    def start_upload(self) -> None:
        if not self.save_project(quiet=True):
            return
        try:
            result = subprocess.run(
                self.core_command(["expected-confirmation", "--project", self.project_dir()]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                **hidden_subprocess_kwargs(),
            )
            expected = result.stdout.strip().splitlines()[-1]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法准备投稿授权：{exc}")
            return
        if messagebox.askokcancel(
            APP_TITLE,
            "将把当前交付目录中的稿件公开投稿到 B 站。\n"
            "请确认已检查视频、封面、标题、简介、分区和 Tag。",
            icon="warning",
        ):
            self.run_core(["upload", "--project", self.project_dir(), "--confirm", expected])

    def read_state(self):
        try:
            return core.load_project(Path(self.project_dir()))[1]
        except Exception:
            return None

    def refresh_workbench(self) -> None:
        self.refresh_status()
        self.refresh_auto_status()

    def _periodic_progress_refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
            self.refresh_status(silent=True)
            self.refresh_auto_status(silent=True)
        finally:
            try:
                if self.winfo_exists():
                    self.after(1500, self._periodic_progress_refresh)
            except Exception:
                pass

    def refresh_status(self, silent: bool = False) -> None:
        state = self.read_state()
        _progress, _summary, statuses = manual_progress_snapshot(state)
        if state is None:
            if self.active_commands or self.pending_commands:
                self.vars["status"].set(
                    f"后台运行 {len(self.active_commands)}/{MAX_PARALLEL_COMMANDS}｜排队 {len(self.pending_commands)}"
                )
            else:
                self.vars["status"].set("就绪")
            return
        parts = [
            f"流程{index}:{STEP_STATUS_LABELS.get(status, status)}"
            for index, status in enumerate(statuses, 1)
        ]
        queue_status = ""
        if self.active_commands or self.pending_commands:
            queue_status = (
                f"  ｜后台运行 {len(self.active_commands)}/{MAX_PARALLEL_COMMANDS}"
                f"，排队 {len(self.pending_commands)}"
            )
        self.vars["status"].set("  ".join(parts) + queue_status)

    def refresh_auto_status(self, silent: bool = False) -> None:
        try:
            self.refresh_creator_profiles()
            config = auto.load_config(self.auto_config_path)
            state = auto.load_state(config)
            lines = auto.status_lines(state) if not silent else []
            jobs = auto_jobs_with_progress(visible_auto_jobs(state))
            self.refresh_execution_panel(jobs)
            manual_priority = auto.manual_work_active(config)
            monitor_running = bool(
                self.monitor_process is not None and self.monitor_process.poll() is None
            )
            jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
            review_completions = {
                str(job.get("id", "")): auto_job_review_completion(job)
                for job in jobs
                if str(job.get("status", "")) == "awaiting_delivery_review"
            }
            pending_retry_ids = {
                str(job.get("id"))
                for job in jobs
                if job.get("status") == "failed"
                and auto.retry_request_pending(config, str(job.get("id")))
            }
            priority = {
                "live": 0,
                "running": 1,
                "queued": 2,
                "needs_codex": 2,
                "awaiting_selection_review": 2,
                "awaiting_delivery_review": 2,
                "ready_to_publish": 2,
                "needs_creator": 2,
                "paused": 2,
                "failed": 3,
                "cleanup_failed": 3,
                "late_segment": 3,
                "published": 4,
                "cleaned_published": 4,
                "cleaned_discarded": 4,
                "no_candidates": 4,
            }
            jobs.sort(key=lambda item: priority.get(str(item.get("status")), 3))
            queued_positions = [
                index for index, item in enumerate(jobs)
                if str(item.get("status", "")) == "queued"
            ]
            ordered_queued = iter(sorted(
                (jobs[index] for index in queued_positions), key=auto.queue_order_key
            ))
            for index in queued_positions:
                jobs[index] = next(ordered_queued)

            if hasattr(self, "auto_job_tree"):
                display_rows = []
                for job in jobs:
                    snapshot = auto.job_progress_snapshot(job)
                    status = str(job.get("status", "queued"))
                    creator_key = str(job.get("creator") or "")
                    creator = self.profiles.get(creator_key, {}).get("display_name", creator_key or "待识别")
                    started = auto.format_recording_time_local(
                        job.get("started_at"),
                        float(
                            job.get(
                                "recording_utc_offset_hours",
                                auto.DEFAULT_RECORDING_UTC_OFFSET_HOURS,
                            )
                        ),
                    )
                    segments = job.get("segments") or []
                    fallback = Path(segments[0]["path"]).name if segments else job.get("id", "未命名")
                    title = str(job.get("title") or fallback)
                    retry_pending = str(job["id"]) in pending_retry_ids
                    tag = auto_job_row_tag(status, retry_pending=retry_pending)
                    job_id = str(job["id"])
                    review_completion = review_completions.get(job_id, {})
                    display_stage = snapshot["current_stage"]
                    deferred_display = deferred_queue_display(
                        job, manual_active=manual_priority, monitor_running=monitor_running
                    )
                    display_status = (
                        "已提交重试" if retry_pending else JOB_STATUS_LABELS.get(status, status)
                    )
                    display_detail = (
                        "等待队列下一轮接收；将复用已有成果"
                        if retry_pending
                        else str(job.get("detail", ""))
                    )
                    if deferred_display is not None:
                        display_stage, display_detail = deferred_display
                    publish_wait = local_publish_wait_display(
                        job, self.__dict__.get("active_commands", {}),
                        self.__dict__.get("pending_commands", ()),
                    )
                    if publish_wait is not None:
                        display_stage, display_detail = publish_wait
                    review_display = auto_job_review_display(review_completion)
                    if review_display is not None:
                        display_stage, display_status, display_detail = review_display
                    display_rows.append((
                        job_id,
                        (started, creator, title, display_stage,
                         f"{snapshot['progress_percent']}%", display_status, display_detail),
                        tag,
                    ))
                self._auto_job_display_rows = display_rows
                self._render_auto_job_rows()

            counts: dict[str, int] = {}
            for job in jobs:
                status = str(job.get("status", "queued"))
                counts[status] = counts.get(status, 0) + 1
            live = counts.get("live", 0)
            running = counts.get("running", 0)
            resolved_review_count = sum(
                bool(summary.get("complete"))
                for summary in review_completions.values()
            )
            waiting = len(pending_retry_ids) + sum(
                counts.get(name, 0)
                for name in (
                    "queued", "needs_creator", "needs_codex", "awaiting_selection_review",
                    "awaiting_delivery_review", "ready_to_publish", "paused",
                )
            ) - resolved_review_count
            failed = (
                max(0, counts.get("failed", 0) - len(pending_retry_ids))
                + counts.get("cleanup_failed", 0)
                + counts.get("late_segment", 0)
            )
            complete = (
                counts.get("published", 0)
                + counts.get("cleaned_published", 0)
                + counts.get("cleaned_discarded", 0)
                + counts.get("no_candidates", 0)
                + resolved_review_count
            )
            invalid = len(state.get("invalid_media", {}))
            reconnect_waits = len(state.get("reconnect_waits", {}))
            monitor = "监控运行中" if monitor_running else "监控未启动"
            if manual_priority:
                monitor += "｜手动流程优先，自动重任务暂停"
            recorder_snapshot = state.get("recorder_status", {})
            if isinstance(recorder_snapshot, dict) and recorder_snapshot.get("available"):
                monitor += "｜录播姬状态直读"
            self.vars["auto_status"].set(
                f"{monitor}｜共 {len(jobs)} 项｜直播中 {live}｜处理中 {running}｜断线观察 {reconnect_waits}｜"
                f"等待 {waiting}｜异常/暂停 {failed}｜坏分段 {invalid}｜完成 {complete}"
            )
            if not silent:
                self.append_log("\n" + "\n".join(lines) + "\n")
        except Exception as exc:
            if not silent:
                messagebox.showerror(APP_TITLE, str(exc))

    def _schedule_auto_job_reflow(self, _event=None) -> None:
        if self._auto_job_reflow_after is not None:
            self.after_cancel(self._auto_job_reflow_after)
        self._auto_job_reflow_after = self.after(75, self._reflow_auto_job_rows)

    def _reflow_auto_job_rows(self) -> None:
        self._auto_job_reflow_after = None
        self._render_auto_job_rows()

    def _render_auto_job_rows(self) -> None:
        """Reflow logical task rows without truncation or disrupting reading/selection."""
        if self._auto_job_drag:
            return
        tree = self.auto_job_tree
        style = ttk.Style(tree)
        font = tkfont.Font(root=self, font=style.lookup(tree.cget("style") or "Treeview", "font") or "TkDefaultFont")
        width = max(1, int(tree.column("detail", "width")) - 12)
        signature = (width, tuple(sorted(font.actual().items())), tuple(self._auto_job_display_rows))
        if signature == self._auto_job_render_signature:
            return

        old_rows = tuple(tree.get_children())
        old_anchors = self._auto_job_row_anchors
        selected_jobs = set(self.selected_auto_job_ids())
        old_focus = str(tree.focus())
        focus_anchor = old_anchors.get(old_focus)
        yview = tree.yview()
        top_index = min(len(old_rows) - 1, int(round(yview[0] * len(old_rows)))) if old_rows else 0
        top_anchor = old_anchors.get(old_rows[top_index]) if old_rows else None
        xview = tree.xview()

        # Treeview has a uniform physical row height. Use as many linked display
        # rows as each task needs, while actions and selections remain task-based.
        rows = []
        anchors = {}
        row_to_job = {}
        used_ids = {job_id for job_id, _values, _tag in self._auto_job_display_rows}
        for job_id, values, tag in self._auto_job_display_rows:
            offset = 0
            for index, line in enumerate(split_monitor_detail(values[-1], width, measure=font.measure)):
                row_id = job_id if index == 0 else f"{job_id}::detail:{index}"
                if index:
                    while row_id in used_ids:
                        row_id += ":"
                used_ids.add(row_id)
                rows.append((row_id, (*values[:-1], line) if index == 0 else ("", "", "", "", "", "", line), tag))
                anchors[row_id] = (job_id, offset)
                row_to_job[row_id] = job_id
                offset += len(line)

        if old_rows:
            tree.delete(*old_rows)
        self.auto_job_row_to_job = row_to_job
        self._auto_job_row_anchors = anchors
        for row_id, values, tag in rows:
            tree.insert("", "end", iid=row_id, values=values, tags=(tag,))
        selection = [row_id for row_id, _values, _tag in rows if row_to_job[row_id] in selected_jobs]
        tree.selection_set(selection)

        def anchor_row(anchor):
            if anchor is None:
                return None
            job_id, offset = anchor
            candidates = [(row_id, position) for row_id, (job, position) in anchors.items() if job == job_id]
            if not candidates:
                return None
            return next((row_id for row_id, position in reversed(candidates) if position <= offset), candidates[0][0])

        focus_row = anchor_row(focus_anchor)
        if focus_row:
            tree.focus(focus_row)
        elif old_focus and tree.exists(old_focus):
            tree.focus(old_focus)
        top_row = anchor_row(top_anchor)
        if rows:
            if top_row:
                index = next(index for index, row in enumerate(rows) if row[0] == top_row)
                tree.yview_moveto(index / len(rows))
            else:
                tree.yview_moveto(yview[0] if yview else 0)
        if xview:
            tree.xview_moveto(xview[0])
        self._auto_job_render_signature = signature
        self.update_auto_job_selection_status()

    def show_auto_job_context_menu(self, event) -> None:
        row_id = str(self.auto_job_tree.identify_row(event.y) or "")
        if not row_id:
            return
        self.auto_job_tree.selection_set(row_id)
        self.auto_job_tree.focus(row_id)
        job = self.selected_auto_job()
        if not job:
            return
        status = str(job.get("status") or "")
        final = status in auto.QUEUE_CONTROL_FINAL_STATUSES
        paused_live = (
            status == "paused"
            and job.get("paused_from_status") == auto.LIVE_JOB_STATUS
        )
        menu = Menu(self, tearoff=0)
        menu.add_command(
            label="暂停任务",
            command=self.pause_selected_auto_job,
            state="disabled" if final or status == "paused" else "normal",
        )
        menu.add_command(
            label="继续任务",
            command=self.resume_selected_auto_job,
            state="normal" if status == "paused" else "disabled",
        )
        menu.add_command(
            label="排队最先",
            command=self.prioritize_selected_auto_job,
            state="disabled" if final else "normal",
        )
        menu.add_separator()
        menu.add_command(
            label="立刻切片（回溯最近 5 分钟）",
            command=self.quick_clip_selected_live_job,
            state=(
                "normal"
                if (status == auto.LIVE_JOB_STATUS or paused_live)
                and bool(job.get("segments"))
                else "disabled"
            ),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_auto_job_drag_selection(self, target: str) -> None:
        drag = self._auto_job_drag
        if not drag or not target:
            return
        display_rows = tuple(str(row) for row in self.auto_job_tree.get_children())
        job_for = lambda row: self.auto_job_row_to_job.get(row, row)
        jobs = tuple(dict.fromkeys(job_for(row) for row in display_rows))
        selected_jobs = set(auto_job_drag_selection(
            jobs,
            job_for(str(drag["anchor"])),
            job_for(target),
            base_selection={job_for(row) for row in drag["base"]},
            mode=str(drag["mode"]),
        ))
        selected = tuple(row for row in display_rows if job_for(row) in selected_jobs)
        if selected:
            self.auto_job_tree.selection_set(*selected)
        else:
            current = self.auto_job_tree.selection()
            if current:
                self.auto_job_tree.selection_remove(*current)
        self.auto_job_tree.focus(target)
        drag["target"] = target
        drag["moved"] = bool(
            drag.get("moved")
            or target != drag["anchor"]
            or abs(int(drag.get("last_y", drag["start_y"])) - int(drag["start_y"])) >= 4
        )
        self.update_auto_job_selection_status()

    def begin_auto_job_drag(self, event):
        """Start a plain click-and-drag range selection in the task table."""
        region = str(self.auto_job_tree.identify_region(event.x, event.y) or "")
        if region not in {"cell", "tree"}:
            self._auto_job_drag = None
            return None
        row_id = str(self.auto_job_tree.identify_row(event.y) or "")
        if not row_id:
            self._auto_job_drag = None
            return None
        state = int(getattr(event, "state", 0) or 0)
        control = bool(state & 0x0004)
        shift = bool(state & 0x0001)
        current = set(str(row) for row in self.auto_job_tree.selection())
        previous_focus = str(self.auto_job_tree.focus() or "")
        anchor = previous_focus if shift and previous_focus else row_id
        mode = (
            "add" if shift and control
            else "replace" if shift
            else "toggle" if control
            else "replace"
        )
        base = current if mode in {"add", "toggle"} else set()
        self._auto_job_drag = {
            "anchor": anchor,
            "target": row_id,
            "base": base,
            "mode": mode,
            "start_x": int(event.x),
            "start_y": int(event.y),
            "last_y": int(event.y),
            "moved": False,
            "modified": control or shift,
        }
        self._set_auto_job_drag_selection(row_id)
        return "break"

    def drag_select_auto_jobs(self, event):
        """Extend a task selection while the left mouse button is held."""
        if not self._auto_job_drag:
            self._schedule_auto_job_reflow()
            return None
        height = max(1, int(self.auto_job_tree.winfo_height()))
        if event.y < 0:
            self.auto_job_tree.yview_scroll(-1, "units")
            lookup_y = 1
        elif event.y >= height:
            self.auto_job_tree.yview_scroll(1, "units")
            lookup_y = height - 1
        else:
            lookup_y = event.y
        row_id = str(self.auto_job_tree.identify_row(lookup_y) or "")
        if not row_id and event.y < 0:
            row_id = next(
                (
                    str(found)
                    for y in range(1, height, 4)
                    if (found := self.auto_job_tree.identify_row(y))
                ),
                "",
            )
        elif not row_id and event.y >= height:
            row_id = next(
                (
                    str(found)
                    for y in range(height - 1, 0, -4)
                    if (found := self.auto_job_tree.identify_row(y))
                ),
                "",
            )
        self._auto_job_drag["last_y"] = int(event.y)
        if row_id:
            self._set_auto_job_drag_selection(row_id)
        return "break"

    def finish_auto_job_drag(self, event):
        """Finish drag selection and preserve the existing click-to-retry shortcut."""
        drag = self._auto_job_drag
        if not drag:
            self._schedule_auto_job_reflow()
            return None
        drag["last_y"] = int(event.y)
        row_id = str(self.auto_job_tree.identify_row(event.y) or "")
        if row_id:
            self._set_auto_job_drag_selection(row_id)
        moved = bool(drag.get("moved")) or abs(int(event.x) - int(drag["start_x"])) >= 4
        modified = bool(drag.get("modified"))
        self._auto_job_drag = None
        self.update_auto_job_selection_status()
        if not moved and not modified:
            self.retry_failed_auto_job_on_click(event)
        self._schedule_auto_job_reflow()
        return "break"

    def selected_auto_job_ids(self) -> list[str]:
        if not hasattr(self, "auto_job_tree"):
            return []
        selected_rows = set(str(row) for row in self.auto_job_tree.selection())
        result: list[str] = []
        seen: set[str] = set()
        for row_id in self.auto_job_tree.get_children():
            row_id = str(row_id)
            if row_id not in selected_rows:
                continue
            job_id = str(self.auto_job_row_to_job.get(row_id, row_id))
            if job_id not in seen:
                seen.add(job_id)
                result.append(job_id)
        return result

    def update_auto_job_selection_status(self, _event=None) -> None:
        variable = self.vars.get("auto_selection_status") if hasattr(self, "vars") else None
        if variable is None:
            return
        count = len(self.selected_auto_job_ids())
        variable.set(
            f"已选择 {count} 个任务｜发布、重发和清理将处理全部所选任务"
            if count
            else "未选择任务｜按住左键拖动可连续多选"
        )

    def pause_selected_auto_job(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        if job.get("status") == "paused":
            messagebox.showwarning(APP_TITLE, "这个任务已经暂停。")
            return
        self.toggle_pause_selected_auto_job()

    def resume_selected_auto_job(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        if job.get("status") != "paused":
            messagebox.showwarning(APP_TITLE, "只有已暂停的任务可以继续。")
            return
        self.toggle_pause_selected_auto_job()

    def quick_clip_selected_live_job(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        status = str(job.get("status") or "")
        paused_live = (
            status == "paused"
            and job.get("paused_from_status") == auto.LIVE_JOB_STATUS
        )
        if status != auto.LIVE_JOB_STATUS and not paused_live:
            messagebox.showwarning(APP_TITLE, "请选择状态为“正在直播”的任务。")
            return
        if not job.get("segments"):
            messagebox.showwarning(
                APP_TITLE,
                "录播姬已经检测到开播，但首个录播媒体分段尚未生成；"
                "等列表显示“可快速切片”后再试。",
            )
            return
        if not str(job.get("creator") or "").strip():
            messagebox.showwarning(
                APP_TITLE,
                "尚未识别主播；请先在人物与字幕模板中填写该直播间的 room_id。",
            )
            return
        if not messagebox.askokcancel(
            APP_TITLE,
            f"立刻切片：\n{self._job_display_name(job, self.profiles)}\n\n"
            "程序会冻结当前录播的最近 5 分钟，调用 Whisper 转写，"
            "但不会调用 Codex 选片；随后生成一条可人工剪辑、改字幕和改封面的审核素材。\n\n"
            "整场直播录制和原自动任务不会中断。确认开始吗？",
        ):
            return
        job_id = str(job["id"])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool(select_tab=True)
            return False

        self._run_selected_auto_command(
            self.auto_command("quick-clip", extra=["--job-id", job_id]),
            action_name="直播快速切片",
            after=completed,
        )
    def retry_failed_auto_job_on_click(self, event) -> None:
        if not hasattr(self, "auto_job_tree"):
            return
        row_id = str(self.auto_job_tree.identify_row(event.y) or "")
        if not row_id:
            return
        job_id = self.auto_job_row_to_job.get(row_id, row_id)
        try:
            config = auto.load_config(self.auto_config_path)
            state = auto.load_state(config)
            job = state.get("jobs", {}).get(job_id)
            if not isinstance(job, dict) or job.get("status") != "failed":
                return
            publish_action = auto.failed_job_publish_action(job)
            if publish_action:
                self._publish_auto_job(job)
                return
            if auto.retry_request_pending(config, job_id):
                self.vars["auto_status"].set("这个失败任务已经提交重试，等待队列接收")
                return
            if not messagebox.askyesno(
                APP_TITLE,
                "把这个失败任务重新加入队列吗？\n\n"
                f"主播：{self.profiles.get(str(job.get('creator') or ''), {}).get('display_name', job.get('creator') or '待识别')}\n"
                f"任务：{job.get('title') or job_id}\n"
                f"失败位置：{job.get('current_stage') or '未知'}\n\n"
                "将复用已有转写、选片和工程成果；Flow 2/Codex 错误仍需使用原来的专用入口。",
                parent=self,
            ):
                return
            auto.request_failed_job_retry(config, job_id)
            self.vars["auto_status"].set("已提交重试请求，任务将在队列下一轮自动接收")
            self.append_log(f"\n已点击重新排队：{job_id}\n")
            self.refresh_auto_status(silent=True)
            if self.monitor_process is None:
                self.after(
                    100,
                    lambda: self.start_pending_queue(automatic=True, safe_only=True),
                )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)

    def selected_auto_jobs(self) -> list[dict]:
        job_ids = self.selected_auto_job_ids()
        if not job_ids:
            return []
        config = auto.load_config(self.auto_config_path)
        jobs = auto.load_state(config).get("jobs", {})
        return [jobs[job_id] for job_id in job_ids if isinstance(jobs.get(job_id), dict)]

    def selected_auto_job(self) -> dict | None:
        jobs = self.selected_auto_jobs()
        return jobs[0] if jobs else None

    def auto_job_by_id(self, job_id: str) -> dict | None:
        config = auto.load_config(self.auto_config_path)
        return auto.load_state(config).get("jobs", {}).get(job_id)

    def _selected_auto_job_or_warn(self) -> dict | None:
        job = self.selected_auto_job()
        if not job:
            messagebox.showwarning(APP_TITLE, "请先在自动任务表格中选择一场直播。")
            return None
        return job

    def _selected_auto_jobs_or_warn(self) -> list[dict]:
        jobs = self.selected_auto_jobs()
        if not jobs:
            messagebox.showwarning(APP_TITLE, "请先在自动任务表格中选择一个或多个任务。")
        return jobs

    def toggle_pause_selected_auto_job(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        status = str(job.get("status", ""))
        if status in auto.QUEUE_CONTROL_FINAL_STATUSES:
            messagebox.showwarning(
                APP_TITLE, "已完成、已发布、已清理或晚到暂停任务不需要暂停。"
            )
            return
        resuming = status == "paused"
        action = "resume-job" if resuming else "pause-job"
        verb = "继续" if resuming else "暂停"
        running_note = (
            "\n\n任务正在执行重步骤，将先安全完成当前 FFmpeg、Whisper 或 Codex 阶段再暂停。"
            if status == "running" else ""
        )
        if not messagebox.askokcancel(
            APP_TITLE,
            f"确认{verb}任务：\n{self._job_display_name(job, self.profiles)}？"
            f"{running_note}",
        ):
            return
        job_id = str(job["id"])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            return False

        self._run_selected_auto_command(
            self.auto_command(action, extra=["--job-id", job_id]),
            action_name=f"{verb}任务",
            after=completed,
            resume_monitor=resuming,
        )

    def prioritize_selected_auto_job(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        if str(job.get("status", "")) in auto.QUEUE_CONTROL_FINAL_STATUSES:
            messagebox.showwarning(
                APP_TITLE, "已完成、已发布、已清理或晚到暂停任务不能调整队列顺序。"
            )
            return
        if not messagebox.askokcancel(
            APP_TITLE,
            f"将任务移到队首：\n{self._job_display_name(job, self.profiles)}？\n\n"
            "当前正在运行的重步骤不会被强行终止；到达安全边界后会让位。",
        ):
            return
        job_id = str(job["id"])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            return False

        self._run_selected_auto_command(
            self.auto_command("prioritize-job", extra=["--job-id", job_id]),
            action_name="调整队列顺序",
            after=completed,
        )

    def continue_selected_late_segments(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        if job.get("status") != "late_segment":
            messagebox.showwarning(
                APP_TITLE, "请选择状态为“晚到分段暂停”的任务。"
            )
            return

        accounted = {
            auto.segment_path_identity(item)
            for item in auto.accounted_segment_manifest(job)
            if isinstance(item, dict)
        }
        new_segments = [
            item
            for item in job.get("late_segments", [])
            if isinstance(item, dict)
            and auto.segment_path_identity(item)
            and auto.segment_path_identity(item) not in accounted
        ]
        if not new_segments:
            messagebox.showwarning(
                APP_TITLE,
                "没有检测到新的独立分段；如果只是原文件继续增长，请从 Flow 0 重做。",
            )
            return
        duration_minutes = sum(
            max(0.0, float(item.get("duration", 0) or 0)) for item in new_segments
        ) / 60.0
        if not messagebox.askokcancel(
            APP_TITLE,
            f"将 {len(new_segments)} 个新增分段（约 {duration_minutes:.1f} 分钟）"
            "拆成一个独立的后半段续任务。\n\n"
            "原来已完成的前半场不会重做，也不会重复投稿。确认开始吗？",
        ):
            return

        self.stop_embedded_preview(show_cover=True)
        job_id = str(job["id"])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool()
            return False

        self._run_selected_auto_command(
            self.auto_command(
                "continue-late-job", extra=["--job-id", job_id]
            ),
            action_name="创建后半段续分析任务",
            after=completed,
            resume_monitor=True,
        )

    def redo_selected_auto_flow(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        choice = self.redo_flow_choice.get()
        from_flow = REDO_FLOW_OPTIONS.get(choice)
        if from_flow is None:
            messagebox.showwarning(APP_TITLE, "请选择有效的流程重做起点。")
            return
        status = str(job.get("status", ""))
        if status == "running":
            messagebox.showwarning(APP_TITLE, "这个任务正在运行，请先停止后再重做。")
            return
        if status in {"cleaned_published", "cleaned_discarded", "cleanup_failed"}:
            messagebox.showwarning(
                APP_TITLE, "该任务的素材或项目已经清理，无法安全重做；请从原录播新建任务。"
            )
            return

        notes = {
            "flow0": (
                "重新校验录播分段、重建同场连续母版，并重做转写、选片和全部切片素材；"
                "后续会重新调用 Codex，并产生新的模型用量。"
            ),
            "flow1": (
                "重新生成整场转写与互动分析、Prompt、选片及所有切片素材；"
                "会重新调用 Codex，并产生新的模型用量。"            ),
            "flow2": (
                "保留 Flow 1 转写，重新生成 Prompt、调用 Codex 选片并重做之后素材；"
                "会产生新的模型用量。"
            ),
            "flow3": (
                "保留转写和当前选片，重新生成视频、ASS、封面及之后素材。"
            ),
            "flow4": (
                "保留当前审核视频、ASS 和人工决定，重新烧录、审计并生成投稿预览；"
                "不会自动公开上传。"
            ),
            "flow5": (
                "保留审核与烧录交付，只重新生成投稿预览；不会自动公开上传。"
            ),
        }
        published_note = (
            "\n\n这是已发布任务：旧 B 站稿件不会被删除或覆盖；旧 BV 号与投稿回执会保留。"
            "修订版生成后不会自动重新投稿，必须再次点击投稿确认。"
            if status == "published" else ""
        )
        if not messagebox.askokcancel(
            APP_TITLE,
            f"任务：{self._job_display_name(job, self.profiles)}\n\n"
            f"重做起点：{choice}\n{notes[from_flow]}{published_note}\n\n"
            "所选流程及之后的旧成果会移动到项目内 redo-archives 文件夹，"
            "不会直接删除。确认开始吗？",
            icon="warning",
        ):
            return

        self.stop_embedded_preview(show_cover=True)
        job_id = str(job["id"])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool()
            return False

        self._run_selected_auto_command(
            self.auto_command(
                "redo-job",
                extra=["--job-id", job_id, "--from-flow", from_flow],
            ),
            action_name=f"从{from_flow}重做",
            after=completed,
        )

    def _restart_safe_monitor(self) -> None:
        self.vars["auto_status"].set("选中任务操作完成，正在恢复安全监控…")
        self.after(400, lambda: self.start_monitor(automatic=True))

    def _run_selected_auto_command(
        self,
        command: list[str],
        *,
        action_name: str,
        after=None,
        resume_monitor: bool = False,
    ) -> None:
        try:
            self.save_auto_config()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        was_monitoring = self.monitor_process is not None
        flow_key = command_flow_key(command)
        queue_behind_monitor = bool(
            was_monitoring
            and flow_key in {
                "manual:auto-run",
                "manual:publish",
            }
        )
        coexists_with_monitor = flow_key.startswith("manual:")
        monitor_will_stop = bool(was_monitoring and not coexists_with_monitor)
        should_resume = bool(monitor_will_stop or (resume_monitor and not was_monitoring))

        def completed() -> None:
            handled_resume = False
            try:
                self.refresh_auto_status(silent=True)
                handled_resume = bool(after(should_resume)) if after else False
            finally:
                if should_resume and not handled_resume:
                    self._restart_safe_monitor()

        def failed() -> None:
            try:
                self.refresh_auto_status(silent=True)
            finally:
                if should_resume:
                    self._restart_safe_monitor()

        def launch_when_stopped(attempt: int = 0) -> None:
            if monitor_will_stop and self.monitor_process is not None:
                if attempt >= 100:
                    messagebox.showerror(APP_TITLE, "自动监控未能及时停止，请手动停止后重试。")
                    return
                self.after(150, lambda: launch_when_stopped(attempt + 1))
                return
            if queue_behind_monitor:
                self.vars["auto_status"].set(
                    f"{action_name}已排队｜当前自动重步骤到达安全边界后执行"
                )
            else:
                self.vars["auto_status"].set(f"{action_name}运行中…")
            self.run_command(command, after=completed, label=action_name, on_failure=failed)

        if monitor_will_stop:
            monitor = self.monitor_process
            stopped = bool(monitor is not None and self._kill_tree(monitor))
            if stopped and self.monitor_process is monitor:
                self.monitor_process = None
                self._monitor_starting = False
            self.append_log(f"\n为执行{action_name}，正在暂停自动监控…\n")
            if not stopped:
                messagebox.showerror(
                    APP_TITLE,
                    "自动监控进程树在 12 秒内没有退出；本次操作没有启动，请再次停止监控后重试。",
                )
                return
            try:
                recovered = self._recover_interrupted_auto_jobs()
                if recovered:
                    self.append_log(
                        f"已保留 {recovered} 个被中断自动任务的成果，之后可续跑。\n"
                    )
            except Exception as exc:
                messagebox.showerror(
                    APP_TITLE, f"自动任务已停止，但恢复队列状态失败：{exc}"
                )
                return
            launch_when_stopped()
        else:
            if was_monitoring and coexists_with_monitor:
                if queue_behind_monitor:
                    self.append_log(
                        f"\n{action_name}已进入优先队列；"
                        "当前自动重步骤完成后让位，不会强制终止。\n"
                    )
                else:
                    self.vars["auto_status"].set(
                        f"{action_name}优先运行｜自动监控将在安全边界避让"
                    )
            launch_when_stopped()

    @staticmethod
    def _job_display_name(job: dict, profiles: dict) -> str:
        creator_key = str(job.get("creator", ""))
        creator = str(profiles.get(creator_key, {}).get("display_name", creator_key or "未知主播"))
        started = str(job.get("started_at", "")).replace("T", " ")[:16] or "未知时间"
        title = str(job.get("title", "")).strip() or "未命名直播"
        return f"{creator}｜{started}｜{title}"

    def _review_batch_for_job(self, job: dict) -> tuple[Path, list[str], list[str], int]:
        project = Path(str(job.get("project", ""))).expanduser().resolve()
        _, project_state = core.load_project(project)
        review_value = str(project_state.get("active_review_dir") or "").strip()
        if not review_value:
            raise ValueError("任务没有可用的审核目录")
        review_dir = Path(review_value).expanduser().resolve()
        selected, decisions = review_workspace.selected_video_names(
            review_dir, require_human_decisions=False
        )
        copy_rows = {
            Path(str(row.get("video", ""))).name: row
            for row in core.copy_rows(review_dir / "titles-and-covers.csv")
        }
        titles = [
            str(copy_rows.get(name, {}).get("title", "")).strip() or Path(name).stem
            for name in selected
        ]
        unreviewed = sum(
            decisions.get("clips", {}).get(name, {}).get("status") == "pending"
            for name in selected
        )
        return review_dir, selected, titles, unreviewed

    def replace_selected_published_job(self, job: dict | None = None) -> None:
        if job is None:
            job = self._selected_auto_job_or_warn()
        if not job:
            return
        status = str(job.get("status") or "")
        if (
            (status not in {"awaiting_delivery_review", "ready_to_publish"}
             and not auto.resumable_replacement_failure(job))
            or not (
                job.get("manual_republish_required")
                or job.get("auto_replace_revision")
            )
        ):
            messagebox.showwarning(
                APP_TITLE,
                "只有从“已发布”状态重做、并处于“等待审核”或“等待投稿”的任务"
                "才能重新烧录并替换原稿素材。",
            )
            return
        try:
            _review_dir, selected, titles, _unreviewed = self._review_batch_for_job(job)
            history = [
                row for row in (job.get("publication_history") or [])
                if isinstance(row, dict)
            ]
            bvids = list(history[-1].get("bvids") or []) if history else []
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法准备换源：{exc}")
            return
        preview = "\n".join(
            f"{index}. {title}" for index, title in enumerate(titles[:8], 1)
        )
        if len(titles) > 8:
            preview += f"\n……另有 {len(titles) - 8} 条"
        target_text = "、".join(str(value) for value in bvids[:6])
        if len(bvids) > 6:
            target_text += f" 等 {len(bvids)} 个稿件"
        preparation = (
            "程序会先按审核台当前选择重新烧录并生成投稿预览，随后自动换源。\n"
            if status == "awaiting_delivery_review"
            else ""
        )
        if not messagebox.askokcancel(
            APP_TITLE,
            f"将把以下 {len(selected)} 条修订视频换入原稿：\n{preview}\n\n"
            f"目标 BV：{target_text or '将按旧投稿回执逐条核对'}\n\n"
            f"{preparation}"
            "原 BV 号、播放数据与评论会保留，但稿件会重新进入 B 站审核。\n"
            "系统会先把新版作为临时分 P 上传，确认成功后才移除旧素材；"
            "任一步失败都会保留旧素材，并可再次重试。\n\n"
            "目前只自动处理原本为单 P、且新旧 clip_id 能精确对应的稿件。",
            icon="warning",
        ):
            return
        job_id = str(job["id"])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool()
            updated = self.auto_job_by_id(job_id)
            if updated and updated.get("status") == "published":
                self.vars["auto_status"].set("换源完成，原 BV 已保留；队列继续执行")
                self.append_log("\n换源已完成并回读确认，等待 B 站重新审核。\n")
            return False

        self._run_selected_auto_command(
            self.auto_command(
                "replace-published-job",
                extra=["--job-id", job_id],
            ),
            action_name=(
                "重新烧录并替换原稿素材"
                if status == "awaiting_delivery_review"
                else "替换已投稿原稿素材"
            ),
            after=completed,
        )

    def retry_selected_failed_publish(self) -> None:
        jobs = self._selected_auto_jobs_or_warn()
        if not jobs:
            return
        if any(not auto.failed_job_publish_action(job) for job in jobs):
            messagebox.showwarning(APP_TITLE, "请选择投稿失败且已有成片的任务。其他阶段失败请使用对应重试入口。")
            return
        if len(jobs) == 1:
            self._publish_auto_job(jobs[0])
        else:
            self._publish_auto_jobs(jobs)

    def publish_selected_auto_job(self) -> None:
        jobs = self._selected_auto_jobs_or_warn()
        if not jobs:
            return
        if len(jobs) == 1:
            self._publish_auto_job(jobs[0])
            return
        self._publish_auto_jobs(jobs)

    def _publish_auto_jobs(self, jobs: list[dict]) -> None:
        replacements = [
            job for job in jobs
            if job.get("auto_replace_revision") or job.get("manual_republish_required")
        ]
        invalid = [
            job for job in jobs
            if str(job.get("status", "")) not in {"awaiting_delivery_review", "ready_to_publish"}
            and not auto.failed_job_publish_action(job)
        ]
        if invalid:
            shown = "\n".join(
                f"• {self._job_display_name(job, self.profiles)}"
                for job in invalid[:8]
            )
            messagebox.showwarning(
                APP_TITLE,
                "所选任务中包含不能烧录上传的状态；请取消选择以下任务后重试：\n\n"
                + shown,
            )
            return

        prepared: list[tuple[dict, list[str], list[str], int]] = []
        preparation_failures: list[str] = []
        collaboration_required = False
        for job in jobs:
            try:
                _review_dir, selected, titles, unreviewed = self._review_batch_for_job(job)
                project_state = core.load_project(Path(str(job.get("project", ""))))[1]
                collaboration = auto.collaboration_review_summary(project_state)
                if collaboration["required"]:
                    collaboration_required = True
                prepared.append((job, selected, titles, unreviewed))
            except Exception as exc:
                preparation_failures.append(
                    f"• {self._job_display_name(job, self.profiles)}：{exc}"
                )
        if preparation_failures:
            messagebox.showerror(
                APP_TITLE,
                "以下任务无法准备烧录并上传：\n\n" + "\n".join(preparation_failures[:8]),
            )
            return

        total_clips = sum(len(selected) for _job, selected, _titles, _unreviewed in prepared)
        total_unreviewed = sum(unreviewed for _job, _selected, _titles, unreviewed in prepared)
        shown = "\n".join(
            f"• {self._job_display_name(job, self.profiles)}（{len(selected)} 条，"
            f"{'替换原 BV' if job in replacements else '发布/续传'}）"
            for job, selected, _titles, _unreviewed in prepared[:10]
        )
        if len(prepared) > 10:
            shown += f"\n• 以及另外 {len(prepared) - 10} 个任务"
        collaboration_note = (
            "\n\n其中包含多人／同步试听素材；确认代表已核对参与者、说话人归属与标题关系。"
            if collaboration_required else ""
        )
        replacement_note = (
            "\n\n换源任务会按旧回执匹配原 BV，保留播放数据和评论；"
            "确认新素材成功后才移除旧素材。普通任务按发布/续传处理。"
            if replacements else ""
        )
        if not messagebox.askokcancel(
            APP_TITLE,
            f"将依次烧录并公开投稿所选 {len(prepared)} 个任务，共 {total_clips} 条稿件：\n\n"
            f"{shown}{collaboration_note}{replacement_note}\n\n"
            "人工标为不通过的稿件不会投稿；技术审计失败会拦截当前任务，但后续所选任务仍会继续。\n"
            "已成功或 B 站已存在完全一致标题的稿件会自动跳过。",
            icon="warning",
        ):
            return
        if total_unreviewed and not messagebox.askyesno(
            APP_TITLE,
            f"所选任务中共有 {total_unreviewed} 条切片尚未人工审核。\n\n"
            "确定不审核直接上传？未应用剪辑及技术审计失败仍会拦截。",
            icon="warning",
        ):
            return

        # Each confirmed job gets its own queue entry and completion callback.
        # A failure cannot discard the remaining jobs, and the queue can name them.
        for job, _selected, _titles, _unreviewed in prepared:
            job_id = str(job["id"])
            replacement = job in replacements
            extra = ["--job-id", job_id]
            if not replacement:
                extra.extend(["--allow-burn", "--allow-upload"])
                if collaboration_required:
                    extra.append("--confirm-collaboration")
            self._run_selected_auto_command(
                self.auto_command("replace-published-job" if replacement else "run-job", extra=extra),
                action_name="重新发布 · 替换原 BV" if replacement else "烧录并上传",
                after=lambda resume, target=job_id: self._after_selected_upload(target, resume),
            )

    def _publish_auto_job(self, job: dict, *, from_review: bool = False) -> None:
        if job.get("auto_replace_revision") or job.get(
            "manual_republish_required"
        ):
            self.replace_selected_published_job(job)
            return
        status = str(job.get("status", ""))
        normal = status in {"awaiting_delivery_review", "ready_to_publish"}
        safe_resume = auto.resumable_publish_failure(job)
        if not normal and not safe_resume:
            messagebox.showwarning(
                APP_TITLE,
                "当前任务不是“等待审核”“等待投稿”或通过安全门禁的投稿续传状态。",
            )
            return
        try:
            _review_dir, selected, titles, unreviewed = self._review_batch_for_job(job)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法准备烧录并上传：{exc}")
            return
        try:
            project_state = core.load_project(Path(str(job.get("project", ""))))[1]
            live_collaboration = auto.collaboration_review_summary(project_state)
            if live_collaboration["required"]:
                job["collaboration_review_required"] = True
                job["collaboration_clips"] = live_collaboration["clips"]
                job["collaboration_unverified"] = live_collaboration["unverified"]
        except Exception:
            pass
        preview = "\n".join(f"{index}. {title}" for index, title in enumerate(titles[:10], 1))
        if len(titles) > 10:
            preview += f"\n……另有 {len(titles) - 10} 条"
        label = self._job_display_name(job, self.profiles)
        verb = "再次投递失败稿件" if safe_resume else ("直接烧录并公开投稿" if status == "awaiting_delivery_review" else "继续公开投稿")
        review_note = (
            f"\n其中 {unreviewed} 条尚未人工审核；查看标题后还会要求一次明确确认。"
            if unreviewed else
            "\n这些切片均已有人工决定。"
        )
        collaboration_note = ""
        if job.get("collaboration_review_required"):
            collaboration_clips = list(job.get("collaboration_clips") or [])
            unverified_clips = list(job.get("collaboration_unverified") or [])
            collaboration_note = (
                "\n\n这是多人／同步试听素材。点击确认代表你已核对参与者、"
                "说话人归属与标题关系："
                + "、".join(collaboration_clips[:8])
            )
            if unverified_clips:
                collaboration_note += (
                    "\n尚未勾选嘉宾台词已核实："
                    + "、".join(unverified_clips[:8])
                    + "。仍可明确确认投稿，但封面不会叠加嘉宾人物图。"
                )
        if not messagebox.askokcancel(
            APP_TITLE,
            f"任务：{label}\n\n"
            f"将{verb} {len(selected)} 条可发布稿：\n{preview}\n"
            f"{review_note}{collaboration_note}\n\n"
            "人工标为不通过的稿件不会投稿；重做、未应用剪辑及技术审计失败仍会拦截。\n"
            "已成功或 B 站已存在完全一致标题的稿件会自动跳过，不会重复投稿。\n"
            "选择“确认”继续，选择“取消”返回审核台。",
            icon="warning",
        ):
            return
        if unreviewed and not messagebox.askyesno(
            APP_TITLE,
            f"以上有 {unreviewed} 条切片尚未人工审核。\n\n"
            "确定不审核直接上传？\n\n"
            "确认后会采用弹窗中列出的现有标题、字幕和封面；"
            "未应用剪辑及技术审计失败仍会拦截。",
            icon="warning",
        ):
            return
        job_id = str(job["id"])
        extra = ["--job-id", job_id, "--allow-burn", "--allow-upload"]
        if job.get("collaboration_review_required"):
            extra.append("--confirm-collaboration")
        self._run_selected_auto_command(
            self.auto_command(
                "run-job",
                extra=extra,
            ),
            action_name="烧录并上传",
            after=lambda resume: self._after_selected_upload(job_id, resume),
        )

    def save_current_review_suggestions(self, *, silent: bool = False) -> bool:
        if not self.current_review_dir or not self.current_review_video:
            if not silent:
                messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return False
        try:
            advice = self.vars["review_suggestions"].get().strip()
            review_workspace.set_review_suggestions(
                self.current_review_dir, self.current_review_video.name, advice
            )
            row = self._current_review_decision_row()
            for item in self.review_item_map.values():
                if Path(item["video"]) == self.current_review_video:
                    item["review_suggestions"] = advice
                    item["notes"] = row.get("notes", "")
                    item["revision_reason"] = review_workspace.review_revision_reason(row)
            self.refresh_review_revision_reason(row)
            if not silent:
                self.append_log(f"\n已保存人工审核建议：{self.current_review_video.name}\n")
            return True
        except Exception as exc:
            if not silent:
                messagebox.showerror(APP_TITLE, f"保存审核建议失败：{exc}")
            return False

    def _current_review_decision_row(self) -> dict:
        if not self.current_review_dir or not self.current_review_video:
            return {}
        try:
            decisions = review_workspace.load_decisions(
                self.current_review_dir, create=False
            )
            return dict(
                decisions.get("clips", {}).get(self.current_review_video.name, {})
            )
        except Exception:
            return {}

    def _set_current_review_row_status(self, status: str) -> None:
        selected = self.review_clip_tree.selection()
        if not selected:
            return
        iid = selected[0]
        item = self.review_item_map.get(iid)
        if item is not None:
            item["status"] = status
            item["replacement_requested"] = status == "revise"
        values = list(self.review_clip_tree.item(iid, "values"))
        if len(values) >= 4:
            values[3] = review_workspace.STATUS_LABELS.get(status, status)
        self.review_clip_tree.item(iid, values=values, tags=(status,))
        self.refresh_review_decision_status(status)
        self.refresh_review_primary_action()

    def refresh_review_decision_status(self, status: str | None = None) -> None:
        variable = self.vars.get("review_decision_status")
        label = self.__dict__.get("review_decision_status_label")
        if variable is None:
            return
        if status is None:
            selected = self.review_clip_tree.selection()
            item = self.review_item_map.get(selected[0], {}) if selected else {}
            status = str(item.get("status") or "")
        status = str(status or "")
        if not status:
            variable.set("审核状态：未选择切片")
            if label is not None:
                label.configure(foreground="#555555")
            return
        text = review_workspace.STATUS_LABELS.get(status, status)
        variable.set(f"审核状态：{text}")
        if label is not None:
            label.configure(
                foreground={
                    "pending": "#9A6700",
                    "approved": "#137333",
                    "revise": "#B3261E",
                    "skipped": "#5F6368",
                }.get(status, "#555555")
            )

    def _review_cover_values(self) -> tuple[str, str]:
        primary_var = self.vars.get("review_cover_primary")
        secondary_var = self.vars.get("review_cover_secondary")
        if primary_var is not None or secondary_var is not None:
            return (
                str(primary_var.get() if primary_var is not None else "").strip(),
                str(secondary_var.get() if secondary_var is not None else "").strip(),
            )
        combined_var = self.vars.get("review_subtitle")
        combined = str(combined_var.get() if combined_var is not None else "").strip()
        primary, separator, secondary = combined.partition("｜")
        if separator:
            return primary.strip(), secondary.strip()
        legacy_lines = [line.strip() for line in combined.splitlines() if line.strip()]
        if len(legacy_lines) == 2:
            return legacy_lines[0], legacy_lines[1]
        return combined, ""

    def _set_review_cover_values(self, primary: str, secondary: str) -> None:
        primary = str(primary or "").strip()
        secondary = str(secondary or "").strip()
        primary_var = self.vars.get("review_cover_primary")
        secondary_var = self.vars.get("review_cover_secondary")
        if primary_var is not None:
            primary_var.set(primary)
        if secondary_var is not None:
            secondary_var.set(secondary)
        combined_var = self.vars.get("review_subtitle")
        if combined_var is not None:
            combined_var.set(primary + (f"｜{secondary}" if secondary else ""))

    def _restore_current_review_copy(self, row: dict) -> None:
        primary = str(row.get("cover_text_primary", "")).strip()
        secondary = str(row.get("cover_text_secondary", "")).strip()
        self.vars["review_title"].set(str(row.get("title", "")).strip())
        self._set_review_cover_values(primary, secondary)
        selected = self.review_clip_tree.selection()
        current_item = self.review_item_map.get(selected[0], {}) if selected else {}
        description_item = dict(row)
        description_item["creator"] = str(current_item.get("creator") or "")
        self.vars["review_description"].set(
            composed_review_description(description_item, self.profiles)
        )
        for variable, field in (
            ("review_tags", "tags"),
            ("review_tag_evidence", "tag_evidence"),
            ("review_collection", "collection"),
            ("review_season_id", "season_id"),
            ("review_section_id", "section_id"),
            ("review_participants", "participants"),
            ("review_speaker_evidence", "speaker_evidence"),
        ):
            self.vars[variable].set(str(row.get(field, "")).strip())
        collaboration_type = str(
            row.get("collaboration_type", "single") or "single"
        ).strip().lower()
        self.vars["review_collaboration_type"].set(
            COLLABORATION_TYPE_NAMES.get(collaboration_type, "单人")
        )
        self.vars["review_guest_dialogue_verified"].set(
            local_publish.truthy(row.get("guest_dialogue_verified"))
        )
        self.refresh_review_collaboration_status()
        self.refresh_review_tag_status()

    def _current_review_source_publication(self) -> tuple[bool, dict | None]:
        review_dir = self.__dict__.get("current_review_dir")
        if not review_dir:
            return False, None
        try:
            job = self.auto_job_for_review_directory(Path(review_dir))
        except Exception:
            job = None
        job_status = str((job or {}).get("status") or "")
        published = job_status in {"published", "cleaned_published"} or bool(
            (job or {}).get("manual_republish_required")
            or (job or {}).get("auto_replace_revision")
            or (job or {}).get("publication_history")
        )
        if not published:
            project = review_workspace._workflow_project_root(Path(review_dir))
            try:
                if project is not None:
                    _, project_state = core.load_project(project)
                    published = bool(project_state.get("published_at")) or (
                        project_state.get("steps", {}).get("flow5", {}).get("status")
                        == "published"
                    )
            except Exception:
                pass
        return published, job

    def refresh_review_primary_action(self) -> None:
        button = self.__dict__.get("review_approve_button")
        if button is None:
            return
        decision = self._current_review_decision_row()
        status = str(decision.get("status") or "pending")
        published, _job = self._current_review_source_publication()
        replacing_source = bool(decision.get("replacement_was_published")) or (
            published and status in {"approved", "revise"}
        )
        button.configure(
            text="重新烧录并替换源" if replacing_source else "写入为通过"
        )

    def _ensure_current_approved_revision(self, action: str) -> bool:
        """Ask once before the first material edit to an approved clip."""
        review_dir = self.__dict__.get("current_review_dir")
        video = self.__dict__.get("current_review_video")
        if not review_dir or not video:
            return True
        decision = self._current_review_decision_row()
        if decision.get("replacement_requested"):
            return True
        if str(decision.get("status", "pending")) != "approved":
            return True
        published, job = self._current_review_source_publication()
        if published and job is not None:
            effect = (
                "点击“重新烧录并替换源”后，只重做这条切片的 Flow 4，随后"
                "自动替换原 BV 稿件；视频、字幕、封面、标题、简介和 Tag 会一起更新。\n"
                "新版确认成功前不会移除线上旧素材。"
            )
        elif published:
            effect = (
                "该切片来自已发布的手动项目。修改会生成本地修订版，但目前找不到"
                "对应自动队列任务，无法自动确定原 BV 号；通过后需人工处理线上替换。"
            )
        else:
            effect = (
                "这条切片会保存当前修改并重新烧录，"
                "之后的自动投稿会使用新版视频、封面和 Tag。"
            )
        if not messagebox.askyesno(
            APP_TITLE,
            f"这是一条已经通过的切片。要修改{action}并替换原版本吗？\n\n"
            f"{effect}\n\n选择“否”不会写入本次修改。",
            icon="warning",
            parent=self,
        ):
            self.vars["review_line_status"].set("已取消；原切片没有被修改")
            return False
        review_workspace.begin_approved_revision(
            self.current_review_dir,
            self.current_review_video.name,
            action=action,
            job_id=str((job or {}).get("id") or ""),
            published=published,
        )
        self._set_current_review_row_status("revise")
        self.vars["review_line_status"].set(
            "已进入原稿替换修订；点击“重新烧录并替换源”即可完成"
        )
        return True

    def _run_ready_approved_replacement(
        self,
        job_id: str,
        review_dir: Path,
        video_name: str,
        *,
        resume_monitor: bool,
    ) -> None:
        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool(
                select_video_path=str(review_dir / video_name)
            )
            updated = self.auto_job_by_id(job_id)
            if updated and updated.get("status") == "published":
                self.vars["auto_status"].set("新版已换入原 BV；队列继续执行")
                self.append_log("\n新版换源完成并回读确认，播放数据和评论已保留。\n")
            return False

        self._run_selected_auto_command(
            self.auto_command(
                "replace-published-job",
                extra=["--job-id", job_id],
            ),
            action_name="替换已投稿原稿素材",
            after=completed,
            resume_monitor=resume_monitor,
        )
        self.vars["auto_status"].set(
            "新版已烧录完成；正在安全替换原 BV，确认成功前保留线上旧素材"
        )

    def _finish_approved_revision(
        self, review_dir: Path, video_name: str, decision: dict
    ) -> bool:
        if not decision.get("replacement_requested"):
            return False
        job = self.auto_job_for_review_directory(review_dir)
        was_published = bool(decision.get("replacement_was_published"))
        if not was_published:
            self.maybe_auto_burn_review_batch(review_dir)
            return True
        if not job:
            self.vars["auto_status"].set(
                "历史修订已通过，但找不到原自动任务；未自动改动线上稿件"
            )
            messagebox.showwarning(
                APP_TITLE,
                "修订已保存并通过，但找不到包含原投稿回执的自动队列任务，"
                "因此没有自动替换线上稿件。",
            )
            return True
        job_id = str(job["id"])
        job_status = str(job.get("status") or "")
        ready_to_replace = job_status == "ready_to_publish" and bool(
            job.get("manual_republish_required")
            or job.get("auto_replace_revision")
        )
        if ready_to_replace:
            self._run_ready_approved_replacement(
                job_id,
                review_dir,
                video_name,
                resume_monitor=bool(self.auto_upload.get()),
            )
            return True
        if job_status == "awaiting_delivery_review" and job.get(
            "auto_replace_revision"
        ):
            self.maybe_auto_burn_review_batch(review_dir)
            return True
        if job_status == "running":
            self.vars["auto_status"].set(
                "历史修订已通过；当前任务运行结束后会接收自动替换"
            )
            return True
        request_key = f"revision:{job_id}"
        requests = self.__dict__.setdefault("_review_revision_requests", {})
        if request_key in requests:
            self.vars["auto_status"].set("这条历史修订已经在重做或排队")
            return True
        requests[request_key] = time.monotonic()

        def completed(_resume: bool) -> bool:
            requests.pop(request_key, None)
            self.refresh_auto_status(silent=True)
            self.load_global_review_pool(
                select_video_path=str(review_dir / video_name)
            )
            updated = self.auto_job_by_id(job_id)
            if updated and updated.get("status") == "ready_to_publish" and bool(
                updated.get("manual_republish_required")
                or updated.get("auto_replace_revision")
            ):
                self._run_ready_approved_replacement(
                    job_id,
                    review_dir,
                    video_name,
                    resume_monitor=_resume,
                )
                return True
            return False

        self._run_selected_auto_command(
            self.auto_command(
                "redo-job",
                extra=[
                    "--job-id", job_id,
                    "--from-flow", "flow4",
                    "--auto-replace",
                    "--clip-name", video_name,
                ],
            ),
            action_name="历史切片重烧并准备自动替换",
            after=completed,
            resume_monitor=bool(self.auto_upload.get()),
        )
        self.vars["auto_status"].set(
            "历史修订已通过；正在重烧，完成后会自动替换原 BV"
        )
        return True

    def auto_job_for_review_directory(self, review_dir: Path) -> dict | None:
        target = review_dir.expanduser().resolve()
        config = auto.load_config(self.auto_config_path)
        for job in auto.load_state(config).get("jobs", {}).values():
            try:
                _, project_state = core.load_project(Path(str(job.get("project", ""))))
                active = str(project_state.get("active_review_dir") or "").strip()
                if active and Path(active).expanduser().resolve() == target:
                    return job
            except Exception:
                continue
        return None

    @staticmethod
    def _review_directory_key(review_dir: Path) -> str:
        try:
            return str(review_dir.expanduser().resolve())
        except OSError:
            return str(review_dir)

    def _cancel_scheduled_review_continuation(self, review_dir: Path) -> bool:
        key = self._review_directory_key(review_dir)
        scheduled = self.__dict__.setdefault("_review_continuation_after_ids", {})
        after_id = scheduled.pop(key, None)
        if not after_id:
            return False
        try:
            self.after_cancel(after_id)
        except Exception:
            pass
        return True

    def _schedule_review_batch_continuation(self, review_dir: Path) -> None:
        """Leave a short undo window before the batch can burn or upload."""
        directory = review_dir.expanduser().resolve()
        self._cancel_scheduled_review_continuation(directory)
        key = self._review_directory_key(directory)
        scheduled = self.__dict__.setdefault("_review_continuation_after_ids", {})

        def continue_batch() -> None:
            scheduled.pop(key, None)
            self.maybe_auto_burn_review_batch(directory)

        scheduled[key] = self.after(REVIEW_APPROVAL_UNDO_GRACE_MS, continue_batch)
        self.vars["auto_status"].set(
            "审核决定已保存；8 秒内可用“回退上次通过”，随后才会检查烧录与投稿"
        )

    def _review_batch_has_started(self, review_dir: Path) -> tuple[bool, str]:
        """Detect a burn/upload request that can no longer be safely undone."""
        requests = self.__dict__.get("_review_burn_requests", {})
        job = self.auto_job_for_review_directory(review_dir)
        if job:
            status = str(job.get("status") or "")
            request_key = f"job:{job.get('id', '')}"
            if request_key in requests or status in {
                "running", "ready_to_publish", "published",
                "cleaned_published", "cleaned_discarded"
            }:
                return True, status or "已进入队列"
        project = review_workspace._workflow_project_root(review_dir)
        if project is not None:
            request_key = f"project:{project.resolve()}"
            if request_key in requests:
                return True, "已进入烧录队列"
        return False, ""

    def undo_last_review_approval(self) -> None:
        """Restore the last still-current approval and select it for editing."""
        candidate = self.__dict__.get("_last_review_approval_undo")
        if not candidate:
            candidate = review_workspace.latest_undoable_approval(
                core.WORKSPACE_ROOT / "workflow-projects"
            )
        if not candidate:
            messagebox.showinfo(APP_TITLE, "当前没有可以回退的‘上次通过’。")
            return
        review_dir = Path(str(candidate.get("review_dir") or ""))
        video_name = Path(str(candidate.get("video_name") or "")).name
        if not review_dir.is_dir() or not video_name:
            self._last_review_approval_undo = None
            messagebox.showinfo(APP_TITLE, "上次通过记录已不存在，无法回退。")
            return
        started, stage = self._review_batch_has_started(review_dir)
        if started:
            messagebox.showwarning(
                APP_TITLE,
                f"这条稿件已经{stage}，不能直接回退审核状态。\n"
                "请从已通过历史中打开并按修订/替换流程处理。",
            )
            return
        try:
            result = review_workspace.undo_approval(
                review_dir,
                video_name=video_name,
                action_id=str(candidate.get("action_id") or ""),
            )
        except Exception as exc:
            self._last_review_approval_undo = None
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self._cancel_scheduled_review_continuation(review_dir)
        self._last_review_approval_undo = None
        video_path = str(review_dir / result["video_name"])
        if self.review_global_mode:
            self.load_global_review_pool(select_video_path=video_path)
        else:
            self.load_review_directory(review_dir, select_video_path=video_path)
        restored_label = review_workspace.STATUS_LABELS.get(
            result.get("status", "pending"), result.get("status", "pending")
        )
        self.vars["review_line_status"].set(
            f"已回退：{result['video_name']} 恢复为{restored_label}，可继续修改"
        )
        self.vars["auto_status"].set("已取消本批次待执行的烧录检查")

    def maybe_auto_burn_review_batch(self, review_dir: Path) -> None:
        """Continue a fully human-reviewed batch through burn, audit, and upload."""
        try:
            decisions = review_workspace.load_decisions(review_dir, create=False)
            clips = decisions.get("clips", {})
            unresolved = [
                name for name, row in clips.items()
                if row.get("status") in {"pending", "revise"} and not row.get("missing")
            ]
            if unresolved:
                self.vars["auto_status"].set(
                    f"本批次还剩 {len(unresolved)} 条待审核；全部决定后自动烧录并上传"
                )
                return
            approved = [
                name for name, row in clips.items()
                if row.get("status") == "approved" and (review_dir / name).is_file()
            ]
            job = self.auto_job_for_review_directory(review_dir)
            if not approved:
                if job and str(job.get("status", "")) == "awaiting_delivery_review":
                    job_id = str(job["id"])
                    self._run_selected_auto_command(
                        self.auto_command(
                            "run-job",
                            extra=["--job-id", job_id, "--allow-burn"],
                        ),
                        action_name="完成无通过稿审核",
                    )
                    self.vars["auto_status"].set(
                        "审核已完成且没有通过稿；正在从待审核队列移出，不会烧录或投稿"
                    )
                else:
                    self.vars["auto_status"].set("本批次没有通过稿，无需烧录或投稿")
                return
            if job:
                job_id = str(job["id"])
                job_status = str(job.get("status", ""))
                if job_status in {
                    "published", "cleaned_published", "cleaned_discarded"
                }:
                    self.vars["auto_status"].set(
                        "该任务已经投稿或清理；旧审核条目已从列表移除，不会重复烧录"
                    )
                    self.load_global_review_pool()
                    return
                if job_status == "running":
                    self.vars["auto_status"].set("该任务已经在运行或排队，不会重复提交")
                    return
                if job_status not in {"awaiting_delivery_review", "ready_to_publish"}:
                    self.vars["auto_status"].set(
                        f"任务当前状态为 {job_status or '未知'}；未重复提交烧录或投稿"
                    )
                    return
                request_key = f"job:{job_id}"
                requests = self.__dict__.setdefault("_review_burn_requests", {})
                self._review_burn_requests = requests
                requested_at = float(requests.get(request_key, 0.0) or 0.0)
                if requested_at and time.monotonic() - requested_at < 600.0:
                    self.vars["auto_status"].set("该批次烧录已经在运行或排队")
                    return
                requests[request_key] = time.monotonic()

                def completed(_resume: bool) -> bool:
                    requests.pop(request_key, None)
                    self.refresh_auto_status(silent=True)
                    self.load_global_review_pool()
                    return False

                self._run_selected_auto_command(
                    self.auto_command(
                        "run-job",
                        extra=[
                            "--job-id",
                            job_id,
                            "--allow-burn",
                            "--allow-upload",
                            "--confirm-collaboration",
                        ],
                    ),
                    action_name="人工审核完成后自动烧录并上传",
                    after=completed,
                )
                self.vars["auto_status"].set(
                    "人工审核已完成；烧录、媒体审计和投稿已进入同一队列"
                )
                return
            project = review_workspace._workflow_project_root(review_dir)
            if project is None:
                messagebox.showwarning(
                    APP_TITLE, "审核已完成，但找不到对应项目，无法自动烧录。"
                )
                return
            request_key = f"project:{project.resolve()}"
            requests = self.__dict__.setdefault("_review_burn_requests", {})
            self._review_burn_requests = requests
            requested_at = float(requests.get(request_key, 0.0) or 0.0)
            if requested_at and time.monotonic() - requested_at < 600.0:
                self.vars["auto_status"].set("该批次烧录已经在运行或排队")
                return
            requests[request_key] = time.monotonic()

            def manual_completed() -> None:
                requests.pop(request_key, None)
                self.load_global_review_pool()

            self.run_core(
                ["step4", "--project", str(project), "--confirm-reviewed"],
                after=manual_completed,
                save_first=False,
            )
            self.vars["auto_status"].set("审核已完成；烧录与媒体审计已进入队列")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"审核完成后的自动烧录准备失败：{exc}")

    def open_selected_publish_preview(self, *, job_id: str | None = None) -> None:
        try:
            job = self.auto_job_by_id(job_id) if job_id else self._selected_auto_job_or_warn()
            if not job:
                return
            project = Path(str(job.get("project", "")))
            _, project_state = core.load_project(project)
            delivery = Path(str(project_state.get("delivery_dir", "")))
            if not delivery.is_dir():
                messagebox.showwarning(APP_TITLE, "这个任务还没有生成交付文件。")
                return
            self.open_path(str(delivery))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法打开交付文件夹：{exc}")

    def _after_selected_upload(self, job_id: str, resume_monitor: bool) -> bool:
        self.refresh_auto_status(silent=True)
        job = self.auto_job_by_id(job_id)
        if not job or job.get("status") != "published":
            return False
        self.vars["auto_status"].set(
            "稿件已上传并完成合集回读｜源文件已保留，队列继续执行"
        )
        self.append_log(
            "\n投稿完成；未自动弹出清理确认。"
            "需要释放空间时，请选择任务并点击“废弃并清理”。\n"
        )
        return False

    def _after_selected_uploads(self, job_ids: list[str], resume_monitor: bool) -> bool:
        self.refresh_auto_status(silent=True)
        published = sum(
            bool((job := self.auto_job_by_id(job_id)) and job.get("status") == "published")
            for job_id in job_ids
        )
        self.vars["auto_status"].set(
            f"批量烧录上传完成 {published}/{len(job_ids)}｜源文件已保留，队列继续执行"
        )
        self.append_log(
            f"\n所选任务批处理完成：{published}/{len(job_ids)} 个已发布。"
            "需要释放空间时，请选择任务并点击“废弃并清理”。\n"
        )
        return False

    def retry_selected_auto_codex(self) -> None:
        job = self._selected_auto_job_or_warn()
        if not job:
            return
        allowed = {"needs_codex", "awaiting_selection_review", "failed", "no_candidates"}
        if job.get("status") not in allowed:
            messagebox.showwarning(APP_TITLE, "当前任务不处于可重新调用 Codex 的失败/等待状态。")
            return
        try:
            config = auto.load_config(self.auto_config_path)
            model = str(config.get("codex_model", auto.DEFAULT_CODEX_MODEL))
            effort = str(
                config.get("codex_reasoning_effort", auto.DEFAULT_CODEX_REASONING_EFFORT)
            )
        except Exception:
            model, effort = auto.DEFAULT_CODEX_MODEL, auto.DEFAULT_CODEX_REASONING_EFFORT
        if not messagebox.askyesno(
            APP_TITLE,
            "软件会归档旧的无效选片 JSON，保留 Flow 1 转写，然后明确发起一次新的 Codex 调用。\n"
            f"模型：{model}，推理强度：{effort}。这会产生新的 token 用量。继续吗？",
        ):
            return
        job_id = str(job["id"])
        self._run_selected_auto_command(
            self.auto_command("retry-codex", extra=["--job-id", job_id]),
            action_name="重新调用 Codex",
        )

    def _start_cleanup_job(
        self, job: dict, *, reason: str, resume_monitor: bool = False
    ) -> bool:
        try:
            config = auto.load_config(self.auto_config_path)
            plan = auto.cleanup_job_plan(config, job)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法生成安全清理清单：{exc}")
            return False
        size_gib = float(plan["bytes"]) / (1024 ** 3)
        expected = auto.cleanup_confirmation(str(job["id"]))
        action = "上传成功后清理" if reason == "published" else "废弃稿件并清理"
        scope_note = (
            "仅删除已验证的独立审核发布包；原录播、原项目和审核批次记录保留。"
            if plan.get("provenance", {}).get("scope") == "review_package"
            else "仅包括清单中的录播、弹幕/SC XML 和这场任务的项目目录；不会扫描其他场次。"
        )
        label = self._job_display_name(job, self.profiles)
        if not messagebox.askokcancel(
            APP_TITLE,
            f"任务：{label}\n\n"
            f"{action}将永久删除 {plan['existing_count']} 个精确目标，约 {size_gib:.2f} GiB。\n"
            f"{scope_note}\n\n"
            "选择“确认”删除，选择“取消”保留。",
            icon="warning",
        ):
            return False
        job_id = str(job["id"])
        self._run_selected_auto_command(
            self.auto_command(
                "cleanup-job",
                extra=[
                    "--job-id",
                    job_id,
                    "--reason",
                    reason,
                    "--confirm",
                    expected,
                ],
            ),
            action_name=action,
            resume_monitor=resume_monitor,
        )
        return True

    def _start_cleanup_jobs(self, jobs: list[dict]) -> bool:
        try:
            config = auto.load_config(self.auto_config_path)
            reasons = [auto.cleanup_reason_for_job(job) for job in jobs]
            plan = auto.cleanup_jobs_plan(config, jobs)
            confirmation = auto.cleanup_jobs_confirmation(jobs)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法生成批量安全清理清单：{exc}")
            return False
        size_gib = float(plan["bytes"]) / (1024 ** 3)
        labels = [self._job_display_name(job, self.profiles) for job in jobs]
        shown = "\n".join(f"• {label}" for label in labels[:8])
        if len(labels) > 8:
            shown += f"\n• 以及另外 {len(labels) - 8} 项"
        published_count = reasons.count("published")
        discarded_count = reasons.count("discard")
        package_count = int(plan.get("review_package_count", 0))
        scope_note = (
            f"其中 {package_count} 个独立审核发布包只清理包目录，保留其原录播、原项目和审核批次记录。\n"
            if package_count else ""
        )
        if not messagebox.askokcancel(
            APP_TITLE,
            f"将清理所选 {len(jobs)} 个任务：\n\n{shown}\n\n"
            f"已发布后清理 {published_count} 项；废弃并清理 {discarded_count} 项。\n"
            f"共 {plan['existing_count']} 个不重复的精确目标，约 {size_gib:.2f} GiB。\n"
            "只删除经过归属核验的精确目标。\n"
            f"{scope_note}\n"
            "确认后整批进入同一安全队列，逐项清理并保留审计记录。",
            icon="warning",
        ):
            return False
        extra: list[str] = []
        for job in jobs:
            extra.extend(["--job-id", str(job["id"])])
        extra.extend(["--confirm", confirmation])

        def completed(_resume: bool) -> bool:
            self.refresh_auto_status(silent=True)
            return False

        self._run_selected_auto_command(
            self.auto_command("cleanup-jobs", extra=extra),
            action_name=f"清理 {len(jobs)} 个所选任务",
            after=completed,
        )
        return True

    def discard_selected_auto_job(self) -> None:
        jobs = self._selected_auto_jobs_or_warn()
        if not jobs:
            return
        if len(jobs) > 1:
            self._start_cleanup_jobs(jobs)
            return
        job = jobs[0]
        try:
            reason = auto.cleanup_reason_for_job(job)
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self._start_cleanup_job(job, reason=reason)
    def open_selected_auto_review(self, _event=None) -> None:
        try:
            job = self.selected_auto_job()
            if not job:
                messagebox.showwarning(APP_TITLE, "请先在自动任务表格中选择一场直播。")
                return
            project = Path(job["project"])
            state_path = project / core.PROJECT_FILENAME
            if state_path.is_file():
                _, project_state = core.load_project(state_path)
                review = project_state.get("active_review_dir")
                review_path = Path(review) if review else None
                if review_path and review_path.is_dir() and any(review_path.glob("*.mp4")):
                    self.load_review_directory(review_path)
                    return
            self.open_path(project)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def open_current_session_clipper(self) -> None:
        selected = self.review_clip_tree.selection()
        item = self.review_item_map.get(selected[0]) if selected else None
        if not item:
            messagebox.showwarning(APP_TITLE, "请先选择一条属于目标录播的切片。")
            return
        self.open_session_clipper(item)

    def open_session_clipper(self, item: dict) -> None:
        """Open the complete recording and create a human-authored clip from it."""
        project_value = item.get("project")
        if not project_value:
            messagebox.showwarning(APP_TITLE, "当前审核批次无法定位原始录播项目。")
            return
        try:
            project = Path(str(project_value)).expanduser().resolve()
            state_path, state = core.load_project(project)
            source = Path(str(state["config"].get("source", ""))).expanduser()
            if not source.is_file():
                raise FileNotFoundError(f"整场录播不存在：{source}")
            transcript_path = core.transcript_csv(state_path, state)
            with transcript_path.open("r", encoding="utf-8-sig", newline="") as handle:
                transcript_rows = [
                    {
                        "start_seconds": float(row["start_seconds"]),
                        "end_seconds": float(row["end_seconds"]),
                        "text": str(row.get("text", "")).strip(),
                    }
                    for row in csv.DictReader(handle)
                    if str(row.get("start_seconds", "")).strip()
                    and str(row.get("end_seconds", "")).strip()
                ]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法打开整场录播：{exc}")
            return

        default_start = 0.0
        default_end = 30.0
        try:
            metadata = review_workspace.review_proxy_metadata(Path(item["video"]))
            regions = metadata.get("initial_segments") or metadata.get("regions") or []
            if regions:
                default_start = float(regions[0].get("source_start", 0.0))
                default_end = float(regions[-1].get("source_end", default_start + 30.0))
        except Exception:
            pass

        window = Toplevel(self)
        window.title(f"整场录播预览与切片｜{item.get('session') or source.name}")
        window.geometry("1480x900")
        window.minsize(1100, 720)
        window.transient(self)

        position = StringVar(value="00:00:00.000 / 载入后显示总时长")
        active_text = StringVar(value="点击右侧转写可跳到对应时间")
        query = StringVar()
        start_value = StringVar(value=core.format_clock(default_start))
        end_value = StringVar(value=core.format_clock(default_end))
        title_value = StringVar()
        participant_value = StringVar(value=str(item.get("participants", "")))
        status_value = StringVar(value=f"转写：{transcript_path.name}")
        player_holder: dict[str, review_workspace.EmbeddedVlcPlayer | None] = {
            "player": None
        }

        outer = ttk.Frame(window, padding=10)
        outer.pack(fill=BOTH, expand=True)
        paned = ttk.PanedWindow(outer, orient="horizontal")
        paned.pack(fill=BOTH, expand=True)
        left = ttk.LabelFrame(paned, text=f"整场录播｜{source.name}", padding=8)
        right = ttk.LabelFrame(paned, text="整场转写检索", padding=8)
        paned.add(left, weight=4)
        paned.add(right, weight=3)

        player_host = ttk.Frame(left, width=820, height=462)
        player_host.pack(fill=BOTH, expand=True)
        player_host.pack_propagate(False)
        ttk.Label(
            player_host,
            text="点击“播放整场”后在这里预览；没有 VLC 时可用外部播放器",
            anchor="center",
        ).place(x=0, y=0, relwidth=1, relheight=1)
        Label(
            left,
            textvariable=active_text,
            anchor="center",
            justify="center",
            background="#111111",
            foreground="#FFFFFF",
            font=(UI_FONT, 15, "bold"),
            wraplength=790,
            height=2,
        ).pack(fill=X, pady=(4, 0))
        ttk.Label(left, textvariable=position, foreground="#555555").pack(
            anchor="w", pady=(5, 3)
        )

        def ensure_player() -> review_workspace.EmbeddedVlcPlayer:
            player = player_holder["player"]
            if player is None:
                library = review_workspace.find_libvlc()
                if library is None:
                    raise RuntimeError("未找到 VLC；可点击“外部播放器”预览整场")
                player = review_workspace.EmbeddedVlcPlayer(library)
                player_holder["player"] = player
            return player

        def selected_transcript_row() -> dict | None:
            selected = transcript_tree.selection()
            if not selected:
                return None
            try:
                return transcript_rows[int(selected[0].split("-", 1)[1])]
            except (IndexError, ValueError):
                return None

        def current_seconds(*, end_fallback: bool = False) -> float:
            player = player_holder["player"]
            if player is not None and player.player:
                return player.current_seconds()
            row = selected_transcript_row()
            if row:
                return float(row["end_seconds" if end_fallback else "start_seconds"])
            return core.parse_clock(end_value.get() if end_fallback else start_value.get())

        def play_at(seconds: float | None = None) -> None:
            try:
                player = ensure_player()
                value = current_seconds() if seconds is None else float(seconds)
                player_host.update_idletasks()
                player.play(source, player_host.winfo_id(), start_seconds=max(0.0, value))
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc), parent=window)

        controls = ttk.Frame(left)
        controls.pack(fill=X, pady=(6, 0))
        ttk.Button(controls, text="播放整场", command=lambda: play_at()).pack(side=LEFT)
        ttk.Button(
            controls,
            text="暂停/继续",
            command=lambda: player_holder["player"].toggle_pause()
            if player_holder["player"] else None,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            controls,
            text="后退 5 秒",
            command=lambda: player_holder["player"].seek(-5)
            if player_holder["player"] else None,
        ).pack(side=LEFT)
        ttk.Button(
            controls,
            text="前进 5 秒",
            command=lambda: player_holder["player"].seek(5)
            if player_holder["player"] else None,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            controls, text="外部播放器", command=lambda: self.open_path(source)
        ).pack(side=LEFT, padx=(8, 0))

        marks = ttk.LabelFrame(left, text="人工切片范围与文案", padding=8)
        marks.pack(fill=X, pady=(8, 0))
        time_row = ttk.Frame(marks)
        time_row.pack(fill=X)
        ttk.Label(time_row, text="开始").pack(side=LEFT)
        ttk.Entry(time_row, textvariable=start_value, width=18).pack(
            side=LEFT, padx=(5, 4)
        )
        ttk.Button(
            time_row,
            text="用播放位置标开始",
            command=lambda: start_value.set(core.format_clock(current_seconds())),
        ).pack(side=LEFT)
        ttk.Label(time_row, text="结束").pack(side=LEFT, padx=(12, 0))
        ttk.Entry(time_row, textvariable=end_value, width=18).pack(
            side=LEFT, padx=(5, 4)
        )
        ttk.Button(
            time_row,
            text="用播放位置标结束",
            command=lambda: end_value.set(
                core.format_clock(current_seconds(end_fallback=True))
            ),
        ).pack(side=LEFT)
        title_row = ttk.Frame(marks)
        title_row.pack(fill=X, pady=(6, 0))
        ttk.Label(title_row, text="标题", width=7).pack(side=LEFT)
        ttk.Entry(title_row, textvariable=title_value).pack(
            side=LEFT, fill=X, expand=True
        )
        subtitle_row = ttk.Frame(marks)
        subtitle_row.pack(fill=X, pady=(6, 0))
        ttk.Label(subtitle_row, text="副标题", width=7).pack(side=LEFT)
        subtitle_editor = ScrolledText(
            subtitle_row, wrap="word", height=2, font=(UI_FONT, 12)
        )
        subtitle_editor.pack(side=LEFT, fill=X, expand=True)
        participant_row = ttk.Frame(marks)
        participant_row.pack(fill=X, pady=(6, 0))
        ttk.Label(participant_row, text="参与嘉宾", width=7).pack(side=LEFT)
        ttk.Entry(participant_row, textvariable=participant_value).pack(
            side=LEFT, fill=X, expand=True
        )
        ttk.Label(
            marks,
            text="标题与副标题由你填写；创建后会重新生成审核素材，旧审核批次仍保留。",
            foreground="#555555",
        ).pack(anchor="w", pady=(6, 0))

        search_row = ttk.Frame(right)
        search_row.pack(fill=X)
        ttk.Label(search_row, text="检索").pack(side=LEFT)
        ttk.Entry(search_row, textvariable=query).pack(
            side=LEFT, fill=X, expand=True, padx=(5, 0)
        )
        transcript_frame = ttk.Frame(right)
        transcript_frame.pack(fill=BOTH, expand=True, pady=(6, 0))
        transcript_tree = ttk.Treeview(
            transcript_frame,
            columns=("start", "end", "text"),
            show="headings",
            selectmode="browse",
        )
        for name, label, width in (
            ("start", "开始", 105), ("end", "结束", 105), ("text", "转写", 500)
        ):
            transcript_tree.heading(name, text=label)
            transcript_tree.column(name, width=width, anchor="w")
        transcript_scroll = ttk.Scrollbar(
            transcript_frame, orient="vertical", command=transcript_tree.yview
        )
        transcript_tree.configure(yscrollcommand=transcript_scroll.set)
        transcript_tree.pack(side=LEFT, fill=BOTH, expand=True)
        transcript_scroll.pack(side=RIGHT, fill=Y)

        def refresh_transcript(*_args) -> None:
            needle = query.get().strip().casefold()
            transcript_tree.delete(*transcript_tree.get_children())
            matches = 0
            for index, row in enumerate(transcript_rows):
                if needle and needle not in str(row["text"]).casefold():
                    continue
                matches += 1
                transcript_tree.insert(
                    "", "end", iid=f"line-{index}",
                    values=(
                        core.format_clock(float(row["start_seconds"])),
                        core.format_clock(float(row["end_seconds"])),
                        row["text"],
                    ),
                )
            status_value.set(
                f"转写检索命中 {matches} / {len(transcript_rows)} 条｜{transcript_path.name}"
            )

        def jump_to_transcript(_event=None) -> None:
            row = selected_transcript_row()
            if row:
                play_at(float(row["start_seconds"]))

        transcript_tree.bind("<Double-1>", jump_to_transcript)
        transcript_tree.bind("<Return>", jump_to_transcript)
        query.trace_add("write", refresh_transcript)
        ttk.Label(right, textvariable=status_value, foreground="#555555").pack(
            anchor="w", pady=(4, 0)
        )
        refresh_transcript()

        starts = [float(row["start_seconds"]) for row in transcript_rows]

        def tick() -> None:
            if not window.winfo_exists():
                return
            player = player_holder["player"]
            if player is not None and player.player:
                current = player.current_seconds()
                duration = player.duration_seconds()
                position.set(
                    f"{core.format_clock(current)} / {core.format_clock(duration)}"
                )
                index = bisect_right(starts, current) - 1
                if 0 <= index < len(transcript_rows):
                    row = transcript_rows[index]
                    active_text.set(
                        row["text"]
                        if current <= float(row["end_seconds"]) + 0.4 else " "
                    )
            window.after(250, tick)

        def close_window() -> None:
            player = player_holder["player"]
            if player is not None:
                player.close()
            window.destroy()

        def create_clip() -> None:
            try:
                start = core.parse_clock(start_value.get())
                end = core.parse_clock(end_value.get())
                names = split_participant_names(participant_value.get())
                creator = str(state["config"].get("creator") or "")
                host_name = str(
                    self.profiles.get(creator, {}).get("display_name") or creator
                ).strip()
                if host_name and host_name.casefold() not in {
                    name.casefold() for name in names
                }:
                    names.insert(0, host_name)
                core.append_manual_selection(
                    project,
                    title=title_value.get(),
                    subtitle=subtitle_editor.get("1.0", "end-1c"),
                    timestamps=[{"开始秒": start, "结束秒": end}],
                    content_type=(
                        "song"
                        if str(state["config"].get("mode", "narrative")) == "song"
                        else "narrative"
                    ),
                    participants=names,
                    collaboration_type=("collaboration" if len(names) > 1 else "single"),
                )
                close_window()
                self.vars["status"].set("人工切片已加入选片表，正在重新生成审核素材")
                self.run_command(
                    self.core_command(["step3", "--project", str(project)]),
                    after=lambda: self.load_global_review_pool(select_tab=True),
                    flow_key="manual:flow3",
                )
            except Exception as exc:
                messagebox.showerror(APP_TITLE, f"无法创建人工切片：{exc}", parent=window)

        action_row = ttk.Frame(marks)
        action_row.pack(fill=X, pady=(8, 0))
        ttk.Button(
            action_row,
            text="创建切片并进入审核台",
            command=create_clip,
        ).pack(side=LEFT)
        ttk.Button(action_row, text="关闭", command=close_window).pack(side=RIGHT)
        window.protocol("WM_DELETE_WINDOW", close_window)
        window.after(250, tick)

    def choose_review_directory(self) -> None:
        value = filedialog.askdirectory(title="选择 Flow 3 审核目录")
        if value:
            self.load_review_directory(Path(value))

    def reload_review_directory(self) -> None:
        selected = str(self.current_review_video or "")
        if self.review_global_mode:
            self.load_global_review_pool(select_video_path=selected)
        elif self.current_review_dir:
            self.load_review_directory(self.current_review_dir, select_video_path=selected)

    def _populate_review_items(
        self, items: list[dict], *, summary: str,
        select_video_path: str = "", select_tab: bool = True,
    ) -> None:
        self.review_clip_tree.delete(*self.review_clip_tree.get_children())
        self.review_item_map = {}
        selected_iid = ""
        for index, item in enumerate(items, 1):
            iid = f"review-{index:05d}"
            self.review_item_map[iid] = item
            video = Path(item["video"])
            creator_key = str(item.get("creator", ""))
            creator = self.profiles.get(creator_key, {}).get(
                "display_name", creator_key or "未知主播"
            )
            status = str(item.get("status", "pending"))
            self.review_clip_tree.insert(
                "", "end", iid=iid,
                values=(
                    creator,
                    str(item.get("session", "未知场次")),
                    str(item.get("clip_id", video.stem.split("_", 1)[0])),
                    review_workspace.STATUS_LABELS.get(status, status),
                    str(item.get("title", video.stem)),
                ),
                tags=(status,),
            )
            if select_video_path and str(video) == select_video_path:
                selected_iid = iid
        self.vars["review_summary"].set(summary)
        if items:
            target = selected_iid or self.review_clip_tree.get_children()[0]
            self.review_clip_tree.selection_set(target)
            self.review_clip_tree.focus(target)
            self.review_clip_tree.see(target)
            self.review_clip_selected()
        else:
            self.stop_embedded_preview(show_cover=True)
            if self._subtitle_autosave_after is not None:
                try:
                    self.after_cancel(self._subtitle_autosave_after)
                except Exception:
                    pass
            self._subtitle_autosave_after = None
            self._review_edit_line = None
            self._subtitle_preserve_editor_line = None
            self.timeline_selection_kind = None
            self.current_review_dir = None
            self.current_review_video = None
            self.current_review_media = None
            self.current_review_ass = None
            self.current_review_original_ass = None
            self.review_dialogue_rows = []
            self.review_subtitle_tree.delete(*self.review_subtitle_tree.get_children())
            self._subtitle_loading = True
            try:
                self.vars["review_start"].set("")
                self.vars["review_end"].set("")
                self.vars["review_speaker"].set("待确认")
                self.vars["review_subtitle_query"].set("")
                self.vars["review_subtitle_match_status"].set("全部字幕")
                self.review_text_editor.delete("1.0", END)
                self.review_text_editor.edit_modified(False)
            finally:
                self._subtitle_loading = False
            self.vars["review_notes"].set("")
            self.vars["review_suggestions"].set("")
            self.refresh_review_revision_reason({})
            self.vars["review_title"].set("")
            self._set_review_cover_values("", "")
            self.vars["review_description"].set("")
            self.vars["review_tags"].set("")
            self.vars["review_collection"].set("")
            self.vars["review_season_id"].set("")
            self.vars["review_section_id"].set("")
            self.vars["review_collaboration_type"].set("单人")
            self.vars["review_participants"].set("")
            self.vars["review_speaker_evidence"].set("")
            self.vars["review_guest_dialogue_verified"].set(False)
            self.vars["review_collaboration_status"].set("单人素材")
            self.vars["review_tag_evidence"].set("")
            self.vars["review_tag_status"].set("当前没有可编辑的投稿 Tag")
            self.refresh_review_decision_status("")
            self.vars["review_line_status"].set("当前没有待审核或人工重做的切片")
            self.review_waveform_data = None
            self.draw_review_waveform(message="审核池已清空")
        if select_tab:
            self.notebook.select(self.review_tab)

    def visible_review_statuses(self) -> set[str]:
        statuses = set(REVIEW_QUEUE_STATUSES)
        variable = self.vars.get("review_show_approved")
        if variable is not None and bool(variable.get()):
            statuses.add("approved")
        return statuses

    def load_global_review_pool(
        self, *, select_video_path: str = "", select_tab: bool = True
    ) -> None:
        try:
            root = core.WORKSPACE_ROOT / "workflow-projects"
            visible_statuses = self.visible_review_statuses()
            show_approved = "approved" in visible_statuses
            items = review_workspace.discover_review_items(
                root,
                statuses=visible_statuses,
                include_published_projects=show_approved,
            )
            self.review_global_mode = True
            self.vars["review_dir"].set(str(root))
            pending = sum(item.get("status") == "pending" for item in items)
            revise = sum(item.get("status") == "revise" for item in items)
            approved = sum(item.get("status") == "approved" for item in items)
            sessions = len({str(item.get("project") or item.get("session")) for item in items})
            creators = len({str(item.get("creator")) for item in items})
            history_text = f"｜历史审核通过 {approved}" if show_approved else ""
            summary = (
                f"全局审核 {len(items)} 条｜待审核 {pending}｜人工重做 {revise}"
                f"{history_text}｜{creators} 位主播｜{sessions} 场直播"
            )
            self._populate_review_items(
                items, summary=summary, select_video_path=select_video_path,
                select_tab=select_tab,
            )
        except Exception as exc:
            if select_tab:
                messagebox.showerror(APP_TITLE, str(exc))
            else:
                self.vars["review_summary"].set(f"全局审核池载入失败：{exc}")

    def load_review_directory(
        self, path: Path, *, select_video_path: str = ""
    ) -> None:
        path = path.expanduser().resolve()
        if not path.is_dir():
            messagebox.showwarning(APP_TITLE, f"审核目录不存在：{path}")
            return
        videos = sorted(path.glob("*.mp4"))
        if not videos:
            messagebox.showwarning(APP_TITLE, f"审核目录没有 MP4：{path}")
            return
        decisions = review_workspace.load_decisions(path)
        project_root = core.WORKSPACE_ROOT / "workflow-projects"
        visible_statuses = self.visible_review_statuses()
        discovered = review_workspace.discover_review_items(
            project_root,
            statuses=visible_statuses,
            include_published_projects="approved" in visible_statuses,
        )
        items = [
            item for item in discovered if Path(item["review_dir"]).resolve() == path
        ]
        if not items:
            copy_map = {}
            copy_csv = path / "titles-and-covers.csv"
            if copy_csv.is_file():
                try:
                    copy_map = {
                        Path(row.get("video", "")).name: dict(row)
                        for row in core.copy_rows(copy_csv)
                    }
                except Exception:
                    copy_map = {}
            for video in videos:
                decision = decisions.get("clips", {}).get(video.name, {})
                status = str(decision.get("status", "pending"))
                if status not in visible_statuses:
                    continue
                copy_row = copy_map.get(video.name, {})
                primary = str(copy_row.get("cover_text_primary", "")).strip()
                secondary = str(copy_row.get("cover_text_secondary", "")).strip()
                items.append({
                    "review_dir": path,
                    "video": video,
                    "ass": video.with_suffix(".ass"),
                    "creator": "",
                    "session": path.parent.name,
                    "project": None,
                    "status": status,
                    "notes": str(decision.get("notes", "")),
                    "review_suggestions": str(decision.get("review_suggestions", "")),
                    "revision_reason": review_workspace.review_revision_reason(decision),
                    "title": str(copy_row.get("title", "")).strip() or video.stem,
                    "subtitle": "｜".join(
                        value for value in (primary, secondary) if value
                    ),
                    "cover_text_primary": primary,
                    "cover_text_secondary": secondary,
                    "content_type": str(copy_row.get("content_type", "narrative")).strip().lower(),
                    "tags": str(copy_row.get("tags", "")).strip(),
                    "tag_evidence": str(copy_row.get("tag_evidence", "")).strip(),
                    "description": str(copy_row.get("description", "")).strip(),
                    "collection": str(copy_row.get("collection", "")).strip(),
                    "season_id": str(copy_row.get("season_id", "")).strip(),
                    "section_id": str(copy_row.get("section_id", "")).strip(),
                    "vr_topic": str(copy_row.get("vr_topic", "")).strip(),
                    "clip_id": video.stem.split("_", 1)[0],
                })
        self.review_global_mode = False
        self.vars["review_dir"].set(str(path))
        counts = {name: 0 for name in review_workspace.VALID_STATUSES}
        for item in items:
            status = str(item.get("status", "pending"))
            counts[status] = counts.get(status, 0) + 1
        summary = (
            f"当前批次共 {len(items)} 条｜待审核 {counts.get('pending', 0)}｜"
            f"审核通过 {counts.get('approved', 0)}｜人工重做 {counts.get('revise', 0)}｜"
            f"不通过 {counts.get('skipped', 0)}"
        )
        self._populate_review_items(
            items, summary=summary, select_video_path=select_video_path
        )

    def review_clip_clicked(self, event) -> None:
        """Load a row immediately; only a second click starts the heavy player."""
        row_id = str(self.review_clip_tree.identify_row(event.y) or "")
        if not row_id or row_id not in self.review_item_map:
            return
        self.review_clip_tree.selection_set(row_id)
        self.review_clip_tree.focus(row_id)
        self.review_clip_tree.see(row_id)
        identify_column = getattr(self.review_clip_tree, "identify_column", None)
        if (
            callable(identify_column)
            and str(identify_column(getattr(event, "x", 0))) == "#2"
        ):
            self.open_session_clipper(self.review_item_map[row_id])
            return "break"

        def load_and_play(expected_iid: str = row_id) -> None:
            selected = self.review_clip_tree.selection()
            if not selected or selected[0] != expected_iid:
                return
            item = self.review_item_map.get(expected_iid)
            if not item:
                return
            expected_video = Path(str(item.get("video", "")))
            already_loaded = self.current_review_video == expected_video
            if not already_loaded:
                # Clicking the already-highlighted first row emits no
                # <<TreeviewSelect>>. Explicitly load it so the row can always
                # recover from a previous failed/slow selection callback.
                self.review_clip_selected()
            elif self.current_review_video == expected_video:
                self.play_current_embedded()

        self.after_idle(load_and_play)
    def review_clip_selected(self, _event=None) -> None:
        previous_video = getattr(self, "current_review_video", None)
        if self._review_edit_line is not None and not self._flush_pending_subtitle_autosave():
            # Treeview changes its visual selection before this callback runs. If
            # saving the previous subtitle fails, restore that row; otherwise the
            # UI points at a clip that was never loaded and clicking it again emits
            # no <<TreeviewSelect>> event.
            previous_iid = next(
                (
                    iid
                    for iid, value in self.review_item_map.items()
                    if previous_video is not None
                    and Path(str(value.get("video", ""))) == previous_video
                ),
                "",
            )
            if previous_iid:
                self.review_clip_tree.selection_set(previous_iid)
                self.review_clip_tree.focus(previous_iid)
                self.review_clip_tree.see(previous_iid)
            return
        self._review_edit_line = None
        self._subtitle_preserve_editor_line = None

        selected = self.review_clip_tree.selection()
        if not selected:
            return
        item = self.review_item_map.get(selected[0])
        if not item:
            return
        self.refresh_review_decision_status(str(item.get("status") or "pending"))
        video_path = Path(item["video"])
        if not video_path.is_file():
            self.vars["review_line_status"].set(
                "这条旧审核记录的素材已不存在，正在从待办列表移除"
            )
            missing_review_dir = Path(str(item.get("review_dir", video_path.parent)))
            if self.review_global_mode:
                self.after_idle(lambda: self.load_global_review_pool(select_tab=False))
            else:
                self.after_idle(
                    lambda directory=missing_review_dir: self.load_review_directory(directory)
                )
            return
        self.stop_embedded_preview(show_cover=False)
        self.current_review_dir = Path(item["review_dir"])
        self.current_review_video = video_path
        exact_ass = self.current_review_video.with_suffix(".ass")
        self.current_review_original_ass = exact_ass if exact_ass.is_file() else None
        streaming_ready = review_workspace.has_streaming_review(
            self.current_review_video
        )
        # A legacy padded proxy uses a different zero point. Never expose it to
        # the current editor/player while it is being migrated.
        self.current_review_media = (
            review_workspace.review_media_path(self.current_review_video)
            if streaming_ready else self.current_review_video
        )
        self.current_review_ass = (
            review_workspace.review_ass_path(self.current_review_video)
            if streaming_ready
            else self.current_review_original_ass
        )
        creator_key = str(item.get("creator") or "").strip()
        palette_changes = 0
        for ass_path in {
            path for path in (
                self.current_review_ass,
                self.current_review_original_ass,
            ) if path is not None
        }:
            palette_changes += review_workspace.refresh_ass_subtitle_palette(
                ass_path, creator_key, self.profiles
            )
        # Paint a useful first frame before subtitle normalization, cover decoding,
        # or any media probing begins. Tk otherwise appears unresponsive until the
        # whole selection callback returns.
        self.review_cover_image = None
        self.review_cover.configure(
            image="",
            text=f"已打开 {video_path.name}\n正在载入字幕和时间轴…",
        )
        self.review_cover.place(x=0, y=0, relwidth=1, relheight=1)
        self.review_cover.lift()
        self.review_waveform_data = None
        self.draw_review_waveform(message="切片已打开；正在载入波形…")
        self.update_idletasks()
        self.refresh_review_revision_reason(item)
        self.vars["review_notes"].set(str(item.get("notes", "")))
        self.vars["review_suggestions"].set(str(item.get("review_suggestions", "")))
        self.vars["review_title"].set(str(item.get("title", "")))
        primary = str(item.get("cover_text_primary", "")).strip()
        secondary = str(item.get("cover_text_secondary", "")).strip()
        if not primary and not secondary:
            combined = str(item.get("subtitle", "")).strip()
            primary, separator, secondary = combined.partition("｜")
            if not separator:
                legacy_lines = [
                    line.strip() for line in combined.splitlines() if line.strip()
                ]
                if len(legacy_lines) == 2:
                    primary, secondary = legacy_lines
                else:
                    primary, secondary = combined, ""
        self._set_review_cover_values(primary, secondary)
        self.vars["review_description"].set(
            composed_review_description(item, self.profiles)
        )
        self.vars["review_tags"].set(str(item.get("tags", "")))
        self.vars["review_collection"].set(str(item.get("collection", "")))
        self.vars["review_season_id"].set(str(item.get("season_id", "")))
        self.vars["review_section_id"].set(str(item.get("section_id", "")))
        self.vars["review_tag_evidence"].set(str(item.get("tag_evidence", "")))
        collaboration_type = str(
            item.get("collaboration_type", "single") or "single"
        ).strip().lower()
        self.vars["review_collaboration_type"].set(
            COLLABORATION_TYPE_NAMES.get(collaboration_type, "单人")
        )
        self.vars["review_participants"].set(str(item.get("participants", "")))
        self.vars["review_speaker_evidence"].set(
            str(item.get("speaker_evidence", ""))
        )
        self.vars["review_guest_dialogue_verified"].set(
            local_publish.truthy(item.get("guest_dialogue_verified"))
        )
        self.refresh_review_collaboration_status()
        self.refresh_review_tag_status()
        self.after_idle(self.refresh_review_primary_action)
        self.review_subtitle_tree.delete(*self.review_subtitle_tree.get_children())
        self.review_dialogue_rows = []
        sc_normalized = 0
        if self.current_review_ass:
            if str(item.get("status", "pending")) != "approved":
                sc_normalized = review_workspace.normalize_paid_messages_ass(
                    self.current_review_ass
                )
            if sc_normalized and self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(
                    self.current_review_video
                )
            self.review_dialogue_rows = review_workspace.read_dialogues(
                self.current_review_ass
            )
            self.refresh_review_speaker_choices()
            self.refresh_review_collaboration_status()
            self.filter_review_subtitles()
            visible_ids = list(self.review_subtitle_tree.get_children())
            if visible_ids:
                first = visible_ids[0]
                self.review_subtitle_tree.selection_set(first)
                self.review_subtitle_tree.focus(first)
                self.review_subtitle_selected()
            sc_note = f"；已修正 {sc_normalized} 条旧 SC/钢镚误识别" if sc_normalized else ""
            palette_note = (
                f"；已更新 {palette_changes} 个字幕配色样式"
                if palette_changes else ""
            )
            self.vars["review_line_status"].set(
                f"已载入 {len(self.review_dialogue_rows)} 条字幕；下轨点选，拖中间移动，"
                f"拖两边调时长{sc_note}{palette_note}"
            )
        else:
            self._subtitle_loading = True
            try:
                self.vars["review_start"].set("")
                self.vars["review_end"].set("")
                self.vars["review_speaker"].set("待确认")
                self.review_text_editor.delete("1.0", END)
                self.review_text_editor.edit_modified(False)
            finally:
                self._subtitle_loading = False
            self.vars["review_line_status"].set("当前视频缺少同名 ASS")
        self.review_cover.place(x=0, y=0, relwidth=1, relheight=1)
        self.review_cover.lift()
        video = self.current_review_video
        cover = self.current_review_dir / f"{video.stem}-cover.jpg"
        self.review_cover_image = None
        if Image is not None and ImageTk is not None and cover.is_file():
            try:
                with Image.open(cover) as source:
                    preview = source.convert("RGB")
                    preview.thumbnail((620, 349))
                    self.review_cover_image = ImageTk.PhotoImage(preview)
                self.review_cover.configure(image=self.review_cover_image, text="")
            except Exception:
                self.review_cover.configure(image="", text=cover.name)
        else:
            self.review_cover.configure(
                image="", text=cover.name if cover.is_file() else "当前切片没有封面"
            )
        timeline_key = str(video.resolve())
        self.timeline_state = self.timeline_state_by_video.get(timeline_key)
        self.refresh_timeline_apply_button()
        self.timeline_undo = self.timeline_undo_by_video.setdefault(timeline_key, [])
        self.timeline_redo = self.timeline_redo_by_video.setdefault(timeline_key, [])
        self.timeline_selected_segment = None
        self.timeline_selection_kind = None
        self.timeline_drag = None
        metadata = review_workspace.review_proxy_metadata(video)
        source_duration = float(metadata.get("source_duration", 0.0) or 0.0)
        source_start = review_workspace.review_default_source_start(video)
        # Opening a clip must begin at its original selected range. Previously a
        # revealed pre-context segment could make the default play action seek
        # to source second 0.
        self._review_requested_source_start = source_start
        if (
            review_workspace.supports_review_proxy(video)
            and not review_workspace.has_streaming_review(video)
        ):
            self.draw_review_waveform(message="正在移除旧版一分钟代理并统一源时间轴…")
            self.ensure_current_streaming_review_async()
        else:
            self.ensure_timeline_state()
            removed_context = (
                review_workspace.remove_automatic_streaming_context(
                    video, self.timeline_state
                )
                if self.timeline_state
                and review_workspace.has_streaming_review(video)
                else 0
            )
            if removed_context:
                self.reload_current_ass_dialogues()
                self.vars["review_line_status"].set(
                    f"已移除 {removed_context} 段旧版自动前后文；今后只按按钮增加"
                )
            self.load_current_waveform()

    def _subtitle_text_modified(self, _event=None) -> None:
        if not hasattr(self, "review_text_editor"):
            return
        if not self.review_text_editor.edit_modified():
            return
        self.review_text_editor.edit_modified(False)
        self._schedule_subtitle_autosave()

    def _subtitle_field_changed(self, *_args) -> None:
        self._schedule_subtitle_autosave()

    def _schedule_subtitle_autosave(self) -> None:
        if self._subtitle_loading or self._subtitle_autosave_busy or self._review_edit_line is None:
            return
        if self._subtitle_autosave_after is not None:
            try:
                self.after_cancel(self._subtitle_autosave_after)
            except Exception:
                pass
        line_number = int(self._review_edit_line)
        self._subtitle_autosave_after = self.after(
            180, lambda value=line_number: self._autosave_subtitle_line(value)
        )
        self.vars["review_line_status"].set("修改待写入…")

    def _flush_pending_subtitle_autosave(self) -> bool:
        if self._subtitle_autosave_after is not None:
            try:
                self.after_cancel(self._subtitle_autosave_after)
            except Exception:
                pass
            self._subtitle_autosave_after = None
        if self._review_edit_line is None:
            return True
        if not self.current_review_ass:
            # The review pool may have been emptied after this line was selected.
            # There is no longer a valid ASS target, so discard the stale editor
            # pointer instead of permanently blocking every newly arrived clip.
            self._review_edit_line = None
            self._subtitle_preserve_editor_line = None
            return True
        return self._autosave_subtitle_line(int(self._review_edit_line))

    def _speaker_label_for_name(self, name: str) -> str:
        raw_name = str(name or "").strip()
        normalized = raw_name.casefold()
        label_map = self.__dict__.get("speaker_profile_labels") or self.__dict__.get(
            "profile_labels", {}
        )
        for label, key in label_map.items():
            profile = self.profiles.get(key, {})
            aliases = {
                str(profile.get("display_name") or "").strip().casefold(),
                str(profile.get("title_tag") or "").strip().casefold(),
                key.casefold(),
            }
            if normalized and normalized in aliases:
                return label
        return raw_name or "待确认"

    def _review_participants_changed(self) -> None:
        self.refresh_review_speaker_choices()
        self.refresh_review_collaboration_status()

    def open_participant_selector(self) -> None:
        """Let a collaboration keep any number of configured or free-form guests."""
        creator = self._current_review_creator_key()
        if creator not in self.profiles:
            messagebox.showwarning(APP_TITLE, "请先在审核台选择一条切片。")
            return
        host_name = str(
            self.profiles[creator].get("display_name") or creator
        ).strip()
        current = split_participant_names(
            self.vars["review_participants"].get()
        )
        selected_keys: set[str] = set()
        matched_names: set[str] = {host_name.casefold()}
        for key, profile in self.profiles.items():
            display = str(profile.get("display_name") or key).strip()
            aliases = {
                key.casefold(),
                display.casefold(),
                str(profile.get("title_tag") or "").strip().casefold(),
            }
            if any(name.casefold() in aliases for name in current):
                selected_keys.add(key)
                matched_names.update(aliases)
        extras = [
            name for name in current
            if name.casefold() not in matched_names and name != "待确认嘉宾"
        ]

        window = Toplevel(self)
        window.title("选择参与嘉宾（可多选）")
        window.geometry("620x620")
        window.minsize(520, 480)
        window.transient(self)
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer,
            text=(
                f"主主播固定为 {host_name}。按 Ctrl / Shift 可一次选择多位嘉宾；"
                "保存后，说话人下拉只优先显示这些参与者。"
            ),
            wraplength=580,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        choices: list[tuple[str, str]] = []
        for key, profile in self.profiles.items():
            if key == creator or profile.get("archived"):
                continue
            choices.append((key, str(profile.get("display_name") or key).strip()))
        choices.sort(key=lambda item: item[1].casefold())
        listbox = Listbox(
            outer,
            selectmode=MULTIPLE,
            exportselection=False,
            height=18,
            font=(UI_FONT, 12),
        )
        listbox.pack(fill=BOTH, expand=True)
        for index, (key, display) in enumerate(choices):
            listbox.insert(END, f"{display}  （{key}）")
            if key in selected_keys:
                listbox.selection_set(index)

        custom = StringVar(value="、".join(extras))
        custom_row = ttk.Frame(outer)
        custom_row.pack(fill=X, pady=(8, 0))
        ttk.Label(custom_row, text="未配置嘉宾").pack(side=LEFT)
        ttk.Entry(custom_row, textvariable=custom).pack(
            side=LEFT, fill=X, expand=True, padx=(6, 0)
        )

        def apply() -> None:
            names = [host_name]
            names.extend(choices[index][1] for index in listbox.curselection())
            names.extend(split_participant_names(custom.get()))
            names = split_participant_names("、".join(names))
            self.vars["review_participants"].set("、".join(names))
            if len(names) > 1 and self.vars[
                "review_collaboration_type"
            ].get() == "单人":
                self.vars["review_collaboration_type"].set("多人连麦")
            window.destroy()

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X, pady=(10, 0))
        ttk.Button(buttons, text="应用参与嘉宾", command=apply).pack(side=LEFT)
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=RIGHT)

    def open_speaker_role_mapping(self) -> None:
        if self.timeline_rendering:
            self.vars["review_line_status"].set("请等剪辑稿应用完成后再调整角色")
            return
        if not self.current_review_ass or not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先选择有字幕的切片。")
            return
        if not self._flush_pending_subtitle_autosave():
            return
        groups = review_speaker_roles.speaker_groups(
            review_workspace.read_dialogues(self.current_review_ass)
        )
        if not groups:
            messagebox.showinfo(APP_TITLE, "当前切片没有可分配的字幕。")
            return
        owner = self.current_review_video
        labels = {
            f"{profile.get('display_name') or key}（{key}）": key
            for key, profile in self.profiles.items() if not profile.get("archived")
        }
        by_key = {key: label for label, key in labels.items()}
        window = Toplevel(self)
        window.title("说话人对应角色")
        window.geometry("780x500")
        window.minsize(660, 340)
        window.transient(self)
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer, text="把同一说话人的全部字幕对应到角色，统一名称与配色。仅修改当前切片，Ctrl+Z 可撤销。",
            wraplength=730, justify="left",
        ).pack(anchor="w", pady=(0, 10))
        area = ttk.Frame(outer)
        area.pack(fill=BOTH, expand=True)
        canvas = Canvas(area, highlightthickness=0)
        scroll = ttk.Scrollbar(area, orient="vertical", command=canvas.yview)
        scroll.pack(side=RIGHT, fill=Y)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        table = ttk.Frame(canvas)
        table_id = canvas.create_window((0, 0), window=table, anchor="nw")
        table.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(table_id, width=event.width))
        table.columnconfigure(2, weight=1)
        for column, heading in enumerate(("当前说话人", "字幕数", "对应角色")):
            ttk.Label(table, text=heading).grid(row=0, column=column, sticky="w", padx=6, pady=5)
        choices = ["保持现状", *labels]
        variables = {}
        for index, group in enumerate(groups):
            line = 1 + index * 2
            ttk.Label(table, text=group["label"]).grid(row=line, column=0, sticky="w", padx=6)
            ttk.Label(table, text=str(group["count"])).grid(row=line, column=1, sticky="w", padx=6)
            known_label = self._speaker_label_for_name(group["key"][0])
            known_key = self.speaker_profile_labels.get(known_label, "")
            variable = StringVar(value=by_key.get(known_key, "保持现状"))
            variables[group["key"]] = variable
            ttk.Combobox(table, textvariable=variable, values=choices, state="readonly", width=32).grid(
                row=line, column=2, sticky="ew", padx=6, pady=3,
            )
            ttk.Label(table, text="示例：" + group["sample"][:70], foreground="#666666", wraplength=700).grid(
                row=line + 1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 12),
            )
        ttk.Label(
            outer, text="未标记字幕按原有样式分组；同组混有不同声音时，可先在字幕行中分别指定说话人。",
            foreground="#555555", wraplength=730,
        ).pack(anchor="w", pady=(8, 0))

        def apply() -> None:
            if self.current_review_video != owner:
                messagebox.showwarning(APP_TITLE, "当前切片已改变，请重新打开角色对应窗口。", parent=window)
                return
            assignments = {group: labels[value.get()] for group, value in variables.items() if value.get() in labels}
            if self.apply_review_speaker_roles(assignments):
                window.destroy()

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X, pady=(12, 0))
        ttk.Button(buttons, text="应用到对应说话人的全部字幕", command=apply).pack(side=LEFT)
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=RIGHT)
        window.grab_set()

    def apply_review_speaker_roles(self, assignments: dict) -> bool:
        if self.timeline_rendering or not self.current_review_ass:
            return False
        if not assignments:
            return True
        if not self._flush_pending_subtitle_autosave():
            return False
        if not self._ensure_current_approved_revision("说话人角色对应"):
            return False
        selected_index = next((index for index, row in enumerate(self.review_dialogue_rows)
                               if row["line_number"] == self._review_edit_line), 0)
        undo_count = len(self.timeline_undo)
        try:
            self.push_timeline_undo(mark_video_edit=False)
            changed = review_speaker_roles.apply_speaker_roles(
                self.current_review_ass, assignments, self.profiles,
            )
            if self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(self.current_review_video)
            rows = review_workspace.read_dialogues(self.current_review_ass)
            preferred = str(rows[min(selected_index, len(rows) - 1)]["line_number"]) if rows else ""
            self._review_edit_line = None
            self.reload_current_ass_dialogues(preferred)
            self.draw_review_waveform()
            self.vars["review_line_status"].set(f"已更新 {changed} 行说话人名称与角色配色；Ctrl+Z 可撤销")
            return True
        except Exception as exc:
            if len(self.timeline_undo) > undo_count:
                self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, f"角色对应未完成：{exc}")
            return False

    def refresh_review_speaker_choices(self) -> None:
        if not hasattr(self, "review_speaker_box"):
            return
        choices = ["待确认"]
        participants = split_participant_names(
            self.vars["review_participants"].get()
        )
        if not participants:
            creator = self._current_review_creator_key()
            if creator in self.profiles:
                participants.append(
                    str(self.profiles[creator].get("display_name") or creator)
                )
        choices.extend(self._speaker_label_for_name(name) for name in participants)
        seen = {value.casefold() for value in choices}
        for row in getattr(self, "review_dialogue_rows", []):
            name = str(row.get("name") or "").strip()
            if name and name.casefold() not in seen:
                choices.append(name)
                seen.add(name.casefold())
        current = self.vars["review_speaker"].get().strip()
        if current and current.casefold() not in seen:
            choices.append(current)
        self.review_speaker_box.configure(values=choices)

    def _capture_subtitle_editor_state(self) -> dict[str, object]:
        state: dict[str, object] = {}
        try:
            state["insert"] = str(self.review_text_editor.index("insert"))
        except Exception:
            pass
        try:
            selection = self.review_text_editor.tag_ranges("sel")
            if len(selection) == 2:
                state["selection"] = (str(selection[0]), str(selection[1]))
        except Exception:
            pass
        return state

    def _restore_subtitle_editor_state(self, state: dict[str, object]) -> None:
        if not state:
            return
        try:
            self.review_text_editor.tag_remove("sel", "1.0", "end")
            selection = state.get("selection")
            if isinstance(selection, tuple) and len(selection) == 2:
                self.review_text_editor.tag_add("sel", selection[0], selection[1])
            insert = state.get("insert")
            if insert:
                self.review_text_editor.mark_set("insert", str(insert))
                self.review_text_editor.see(str(insert))
        except Exception:
            pass

    def refresh_review_collaboration_status(self) -> None:
        kind = COLLABORATION_TYPE_LABELS.get(
            self.vars["review_collaboration_type"].get(), "single"
        )
        if kind == "single":
            text = "单人素材：不叠加嘉宾人物图"
        elif self.vars["review_guest_dialogue_verified"].get():
            text = "已核实：重生成封面后叠加有配置的嘉宾人物图"
            if kind == "collaboration":
                participants = [
                    value.strip()
                    for value in re.split(
                        r"[,，、;；|\n]+", self.vars["review_participants"].get()
                    )
                    if value.strip()
                ]
                assigned = {
                    str(row.get("name") or "").strip().casefold()
                    for row in getattr(self, "review_dialogue_rows", [])
                }
                missing = [
                    name for name in participants[1:]
                    if name.casefold() not in assigned
                ]
                if missing:
                    text = "已核实，但这些人物尚未分配字幕行：" + "、".join(missing)
        else:
            text = "待核实：可制作审核，但不会叠加嘉宾图或无人值守投稿"
        self.vars["review_collaboration_status"].set(text)

    def filter_review_subtitles(self) -> None:
        if not hasattr(self, "review_subtitle_tree"):
            return
        selected = self.review_subtitle_tree.selection()
        selected_line = selected[0] if selected else str(
            self.__dict__.get("_review_edit_line") or ""
        )
        variables = self.__dict__.get("vars", {})
        query_variable = variables.get("review_subtitle_query")
        query = query_variable.get() if query_variable is not None else ""
        visible = filtered_subtitle_rows(
            getattr(self, "review_dialogue_rows", []),
            query,
        )
        self.review_subtitle_tree.delete(*self.review_subtitle_tree.get_children())
        for row in visible:
            self.review_subtitle_tree.insert(
                "", "end", iid=str(row["line_number"]),
                values=(
                    row.get("name") or "未标记",
                    row["start"], row["end"], row["text"],
                ),
            )
        visible_ids = set(self.review_subtitle_tree.get_children())
        if selected_line and selected_line in visible_ids:
            self.review_subtitle_tree.selection_set(selected_line)
            self.review_subtitle_tree.focus(selected_line)
            self.review_subtitle_tree.see(selected_line)
        status_variable = variables.get("review_subtitle_match_status")
        if status_variable is not None:
            status_variable.set(
                f"命中 {len(visible)} / {len(self.review_dialogue_rows)} 条"
                if str(query).strip() else f"全部 {len(self.review_dialogue_rows)} 条"
            )

    def select_next_subtitle_match(self) -> None:
        if not hasattr(self, "review_subtitle_tree"):
            return
        rows = list(self.review_subtitle_tree.get_children())
        if not rows:
            self.vars["review_line_status"].set("字幕检索没有匹配项")
            return
        selected = self.review_subtitle_tree.selection()
        if selected and selected[0] in rows:
            target = rows[(rows.index(selected[0]) + 1) % len(rows)]
        else:
            target = rows[0]
        self.review_subtitle_tree.selection_set(target)
        self.review_subtitle_tree.focus(target)
        self.review_subtitle_tree.see(target)
        self.review_subtitle_selected()
        self.play_selected_subtitle()

    def review_subtitle_selected(self, _event=None) -> None:
        selected = self.review_subtitle_tree.selection()
        if not selected:
            return
        line_number = int(selected[0])
        # Autosave can rebuild the Treeview when ASS line numbers change. Tk
        # then emits a delayed <<TreeviewSelect>> for the row already being
        # edited. Reloading that same row deletes/reinserts the Text content and
        # moves its caret to the end, so the active editor stays authoritative.
        preserve_line = self.__dict__.get("_subtitle_preserve_editor_line")
        if preserve_line == line_number and self._review_edit_line == line_number:
            self.timeline_selection_kind = "subtitle"
            self.draw_review_waveform()
            return
        self._subtitle_preserve_editor_line = None
        if self._review_edit_line is not None and self._review_edit_line != line_number:
            previous_line = str(self._review_edit_line)
            if not self._flush_pending_subtitle_autosave():
                # Treeview has already highlighted the clicked row. Restore the
                # row that is still loaded in the editor so one failed autosave
                # cannot leave the list and edit fields permanently out of sync.
                try:
                    self.review_subtitle_tree.selection_set(previous_line)
                    self.review_subtitle_tree.focus(previous_line)
                    self.review_subtitle_tree.see(previous_line)
                except Exception:
                    pass
                return
            # Saving the previous line rebuilds the Treeview and used to select
            # that previous row again. Re-assert the originally clicked row so
            # the visible selection and fields change together.
            try:
                target_line = str(line_number)
                self.review_subtitle_tree.selection_set(target_line)
                self.review_subtitle_tree.focus(target_line)
                self.review_subtitle_tree.see(target_line)
            except Exception:
                pass
        row = next(
            (item for item in self.review_dialogue_rows if item["line_number"] == line_number),
            None,
        )
        if not row:
            return
        self._subtitle_loading = True
        try:
            self._review_edit_line = line_number
            self.vars["review_start"].set(row["start"])
            self.vars["review_end"].set(row["end"])
            self.vars["review_speaker"].set(
                self._speaker_label_for_name(str(row.get("name") or ""))
            )
            self.review_text_editor.delete("1.0", END)
            self.review_text_editor.insert("1.0", row["text"])
            self.review_text_editor.edit_modified(False)
        finally:
            self._subtitle_loading = False
        self.timeline_selection_kind = "subtitle"
        self.draw_review_waveform()

    def play_selected_subtitle(self, event=None) -> None:
        """Seek to the clicked subtitle start and begin or resume playback."""
        if event is not None:
            row_id = str(self.review_subtitle_tree.identify_row(event.y) or "")
            if not row_id:
                return
            self.review_subtitle_tree.selection_set(row_id)
            self.review_subtitle_tree.focus(row_id)
        selected = self.review_subtitle_tree.selection()
        if not selected:
            return
        try:
            line_number = int(selected[0])
        except (TypeError, ValueError):
            return
        if self._review_edit_line != line_number:
            self.review_subtitle_selected()
            if self._review_edit_line != line_number:
                return
        row = next(
            (
                item for item in self.review_dialogue_rows
                if item["line_number"] == line_number
            ),
            None,
        )
        if row is None:
            return
        try:
            start = review_workspace.parse_ass_time(row["start"])
            self.seek_timeline_seconds(start, autoplay=True)
            self.vars["review_line_status"].set(
                f"已跳到字幕 {row['start']} 并开始播放：{row['text']}"
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法跳到所选字幕：{exc}")

    def _refresh_subtitle_rows_after_autosave(self, line_number: int) -> None:
        """Refresh saved row values without touching the active Text editor."""
        if not self.current_review_ass:
            return
        fresh_rows = review_workspace.read_dialogues(self.current_review_ass)
        old_ids = {str(row["line_number"]) for row in self.review_dialogue_rows}
        new_ids = {str(row["line_number"]) for row in fresh_rows}
        if old_ids != new_ids:
            # Adding the first speaker style can shift ASS source line numbers.
            # Rebuild the tree only for that structural case and suppress its
            # synthetic same-row selection event.
            self.reload_current_ass_dialogues(
                str(line_number), preserve_editor=True
            )
            return
        self.review_dialogue_rows = fresh_rows
        self.refresh_review_speaker_choices()
        self.refresh_review_collaboration_status()
        variables = self.__dict__.get("vars", {})
        query_variable = variables.get("review_subtitle_query")
        if query_variable is not None and query_variable.get().strip():
            self.filter_review_subtitles()
            return
        for row in fresh_rows:
            iid = str(row["line_number"])
            if self.review_subtitle_tree.exists(iid):
                self.review_subtitle_tree.item(
                    iid,
                    values=(
                        row.get("name") or "未标记",
                        row["start"], row["end"], row["text"],
                    ),
                )

    def _autosave_subtitle_line(self, line_number: int) -> bool:
        self._subtitle_autosave_after = None
        if self._subtitle_autosave_busy or not self.current_review_ass:
            return self.current_review_ass is not None
        if self._review_edit_line != line_number:
            return True
        row = next(
            (item for item in self.review_dialogue_rows if item["line_number"] == line_number),
            None,
        )
        if row is None:
            # A delete/reload can invalidate the remembered ASS line number.
            # There is nothing left to save, so clear the stale editor identity
            # and allow the newly clicked row to load instead of getting stuck.
            self._review_edit_line = None
            return True
        start = self.vars["review_start"].get()
        end = self.vars["review_end"].get()
        text = review_workspace.single_line_text(
            self.review_text_editor.get("1.0", END)
        )
        speaker_label = self.vars["review_speaker"].get().strip() or "待确认"
        speaker_key = self.speaker_profile_labels.get(speaker_label, "")
        speaker_profile = self.profiles.get(speaker_key, {})
        desired_name = (
            str(speaker_profile.get("display_name") or speaker_key)
            if speaker_key
            else speaker_label
        )
        desired_style = f"Speaker_{speaker_key}" if speaker_key else row["style"]
        if (start, end, text, desired_name, desired_style) == (
            row["start"], row["end"], row["text"], row.get("name", ""), row["style"]
        ):
            return True
        if not self._ensure_current_approved_revision("字幕或字幕时间"):
            self._subtitle_loading = True
            try:
                self.vars["review_start"].set(row["start"])
                self.vars["review_end"].set(row["end"])
                self.vars["review_speaker"].set(
                    self._speaker_label_for_name(str(row.get("name") or ""))
                )
                self.review_text_editor.delete("1.0", END)
                self.review_text_editor.insert("1.0", row["text"])
                self.review_text_editor.edit_modified(False)
            finally:
                self._subtitle_loading = False
            return True
        context_edit = row.get("effect") == review_workspace.CONTEXT_SUBTITLE_EFFECT
        self._subtitle_autosave_busy = True
        try:
            self.push_timeline_undo(mark_video_edit=context_edit)
            if speaker_key:
                style_exists = (
                    f"Style: {desired_style},"
                    in self.current_review_ass.read_text(encoding="utf-8-sig")
                )
                review_workspace.ensure_speaker_style(
                    self.current_review_ass,
                    speaker_key,
                    speaker_profile,
                    base_style=str(row.get("style") or ""),
                )
                if not style_exists:
                    line_number += 1
                    self._review_edit_line = line_number
            review_workspace.update_dialogue(
                self.current_review_ass,
                line_number,
                start=start,
                end=end,
                text=text,
                style=desired_style,
                name=desired_name,
            )
            review_workspace.force_single_line_ass(self.current_review_ass)
            repaired = review_workspace.resolve_dialogue_overlaps(self.current_review_ass)
            if context_edit and self.timeline_state:
                self.persist_timeline_state()
            if self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(self.current_review_video)
            self._refresh_subtitle_rows_after_autosave(line_number)
            updated_row = next(
                (item for item in self.review_dialogue_rows if item["line_number"] == line_number),
                None,
            )
            if updated_row is not None:
                self._subtitle_loading = True
                try:
                    self.vars["review_start"].set(updated_row["start"])
                    self.vars["review_end"].set(updated_row["end"])
                    self.vars["review_speaker"].set(
                        self._speaker_label_for_name(updated_row.get("name", ""))
                    )
                    # Never rewrite the active Text widget during autosave. The
                    # keystroke buffer and its caret/selection remain untouched.
                    self.review_text_editor.edit_modified(False)
                finally:
                    self._subtitle_loading = False
            detail = "已即时写入；无需点击保存"
            if context_edit:
                detail += "；已展开内容纳入剪辑稿，请应用剪辑稿"
            if repaired:
                detail += f"；修复 {repaired} 处重叠"
            self.vars["review_line_status"].set(detail)
            self.draw_review_waveform()
            return True
        except Exception as exc:
            self.vars["review_line_status"].set(f"即时写入失败：{exc}")
            return False
        finally:
            self._subtitle_autosave_busy = False

    def save_current_subtitle(self, *, silent: bool = False) -> bool:
        if not self.current_review_ass:
            if not silent:
                messagebox.showwarning(APP_TITLE, "当前切片没有可编辑 ASS。")
            return False
        return self._flush_pending_subtitle_autosave()

    def _apply_active_corrections_to_current_ass(self) -> int:
        """Apply every enabled exact-phrase correction to every cue in this clip."""
        current_ass = self.__dict__.get("current_review_ass")
        if not current_ass or not current_ass.is_file():
            return 0
        if self.__dict__.get("_review_edit_line") is not None and not self._flush_pending_subtitle_autosave():
            raise ValueError("当前字幕行尚未成功保存，不能继续全量纠错")
        selected = self.review_subtitle_tree.selection()
        preferred = selected[0] if selected else str(
            self.__dict__.get("_review_edit_line") or ""
        )
        changed = review_workspace.normalize_paid_messages_ass(
            current_ass
        )
        if changed and self.current_review_video:
            review_workspace.sync_review_proxy_ass_to_original(
                self.current_review_video
            )
            self.reload_current_ass_dialogues(preferred)
            self.draw_review_waveform()
        return changed

    def add_review_fuzzy_correction(self) -> None:
        """Add one exact-phrase correction and apply it to the whole current ASS."""
        wrong = self.vars["review_fuzzy_wrong"].get().strip()
        replacement = self.vars["review_fuzzy_replacement"].get().strip()
        if not wrong:
            try:
                wrong = self.review_text_editor.get("sel.first", "sel.last").strip()
            except Exception:
                wrong = ""
            if wrong:
                self.vars["review_fuzzy_wrong"].set(wrong)
        current_before = self.review_text_editor.get("1.0", "end-1c")
        current_ass = self.__dict__.get("current_review_ass")
        ass_before = (
            current_ass.read_text(encoding="utf-8-sig")
            if current_ass and current_ass.is_file()
            else ""
        )
        if (
            wrong
            and (wrong in current_before or wrong in ass_before)
            and not self._ensure_current_approved_revision("字幕")
        ):
            return
        try:
            row = glossary.upsert_correction(wrong, replacement, status="active")
            replaced_current = wrong in current_before
            if replaced_current:
                self.review_text_editor.delete("1.0", END)
                self.review_text_editor.insert(
                    "1.0", current_before.replace(wrong, replacement)
                )
                self.review_text_editor.edit_modified(True)
                self._schedule_subtitle_autosave()
            changed = self._apply_active_corrections_to_current_ass()
            self.vars["review_fuzzy_wrong"].set("")
            self.vars["review_fuzzy_replacement"].set("")
            detail = f"整词纠正已启用：{row['wrong']}→{row['replacement']}"
            if replaced_current:
                detail += "；当前字幕行已替换并写入"
            detail += f"；当前切片全部字幕另更新 {changed} 行"
            self.vars["review_line_status"].set(detail)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法加入纠错词组：{exc}")

    def add_current_subtitle(self) -> None:
        if not self.current_review_ass:
            messagebox.showwarning(APP_TITLE, "当前切片没有可编辑 ASS。")
            return
        duration = review_workspace.timeline_duration(self.timeline_segments())
        if duration <= 0:
            messagebox.showwarning(APP_TITLE, "尚未载入视频时间轴。")
            return
        if not self._ensure_current_approved_revision("字幕"):
            return
        if self.preview_player is not None and self.preview_player.player:
            start = self.current_edited_seconds()
        else:
            try:
                start = review_workspace.parse_ass_time(self.vars["review_end"].get())
            except ValueError:
                start = 0.0
        start = max(0.0, min(duration, start))
        if duration - start < 0.04:
            start = max(0.0, duration - 2.5)
        end = min(duration, start + 2.5)
        try:
            self.push_timeline_undo(mark_video_edit=False)
            line_number = review_workspace.insert_dialogue(
                self.current_review_ass,
                start_seconds=start,
                end_seconds=end,
                text="新字幕",
            )
            self.reload_current_ass_dialogues(str(line_number))
            self.review_text_editor.focus_set()
            self.review_text_editor.tag_add("sel", "1.0", "end-1c")
            self.vars["review_line_status"].set(
                "已在当前播放位置新增 2.5 秒字幕；输入文字后保存，可在下轨拖动"
            )
            self.draw_review_waveform()
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, str(exc))

    def delete_current_subtitle(self) -> None:
        if not self.current_review_ass:
            messagebox.showwarning(APP_TITLE, "当前切片没有可编辑 ASS。")
            return
        selected = self.review_subtitle_tree.selection()
        line_number = int(selected[0]) if selected else self._review_edit_line
        if line_number is None:
            messagebox.showwarning(APP_TITLE, "请先选择一条字幕。")
            return
        if not self._ensure_current_approved_revision("字幕"):
            return
        if not self._flush_pending_subtitle_autosave():
            return
        try:
            self.push_timeline_undo(mark_video_edit=False)
            preferred = review_workspace.delete_dialogue(
                self.current_review_ass, int(line_number)
            )
            if self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(
                    self.current_review_video
                )
            self._review_edit_line = None
            self.reload_current_ass_dialogues(str(preferred or ""))
            self.vars["review_line_status"].set(
                "已删除选中字幕；Ctrl+Z 可撤回"
            )
            self.draw_review_waveform()
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, str(exc))
    def force_current_ass_single_line(self) -> None:
        if not self.current_review_ass:
            messagebox.showwarning(APP_TITLE, "当前切片没有可编辑 ASS。")
            return
        if not self._ensure_current_approved_revision("字幕排版"):
            return
        try:
            count = review_workspace.force_single_line_ass(self.current_review_ass)
            self.review_clip_selected()
            self.vars["review_line_status"].set(f"已整理，修改 {count} 处")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def repair_current_subtitle_overlaps(self, *, silent: bool = False) -> int:
        if not self.current_review_ass:
            if not silent:
                messagebox.showwarning(APP_TITLE, "当前切片没有可编辑 ASS。")
            return -1
        if not self._ensure_current_approved_revision("字幕时间"):
            return -1
        try:
            count = review_workspace.resolve_dialogue_overlaps(
                self.current_review_ass
            )
            if self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(
                    self.current_review_video
                )
            self.review_clip_selected()
            self.vars["review_line_status"].set(
                f"重叠检查完成：修复 {count} 处；当前无重叠字幕"
            )
            return count
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return -1

    def nudge_review_time(self, field: str, delta: float) -> None:
        variable = self.vars["review_start" if field == "start" else "review_end"]
        try:
            value = review_workspace.parse_ass_time(variable.get()) + delta
            variable.set(review_workspace.format_ass_time(max(0.0, value)))
            self.draw_review_waveform()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def current_review_creator_key(self) -> str:
        selected = self.review_clip_tree.selection()
        item = self.review_item_map.get(selected[0]) if selected else None
        creator = str((item or {}).get("creator", "")).strip()
        if creator in self.profiles:
            return creator
        title = self.vars["review_title"].get().strip()
        for key, profile in self.profiles.items():
            if str(profile.get("title_tag", "")) in title:
                return key
        return ""

    @staticmethod
    def _translate_tag_error(error: str) -> str:
        translations = {
            "requires at least 1 clip-specific tag": "至少填写 1 个片段专属 Tag",
            "allows at most 5 clip-specific tags": "片段专属 Tag 最多 5 个",
            "requires at least 1 non-generic content tag": "至少保留 1 个具体内容 Tag",
            "tag_evidence is required": "请填写 Tag 依据",
        }
        if error in translations:
            return translations[error]
        if error.startswith("clip-specific tags duplicate profile tags:"):
            return "不要重复固定 Tag：" + error.split(":", 1)[1].strip()
        if "vr_topic=yes" in error:
            return "昼夜使用 VR 相关 Tag 时需确认本片确实讨论 VR"
        return error

    def current_review_tag_validation(self) -> tuple[list[str], list[str], list[str]]:
        tags = selection_tags.split_tags(self.vars["review_tags"].get())
        creator = self.current_review_creator_key()
        profile_tags = (
            self.profiles.get(creator, {}).get("upload", {}).get("tags", [])
            if creator else []
        )
        selected = self.review_clip_tree.selection()
        item = self.review_item_map.get(selected[0]) if selected else {}
        row = {
            "tags": self.vars["review_tags"].get(),
            "tag_evidence": self.vars["review_tag_evidence"].get().strip()
            or ("人工审核确认" if tags else ""),
            "vr_topic": str((item or {}).get("vr_topic", "")),
        }
        errors = [
            self._translate_tag_error(value)
            for value in selection_tags.validate_tag_row(row, creator, profile_tags)
        ]
        warnings: list[str] = []
        if not errors and len(tags) < 3:
            warnings.append("数量较少，但人工编辑允许投稿")
        sentence_like = [
            tag for tag in tags
            if selection_tags.TAG_SENTENCE_PUNCTUATION.search(tag) or len(tag) > 20
        ]
        if sentence_like:
            warnings.append("建议改成更短的搜索词：" + "、".join(sentence_like))
        return tags, errors, warnings

    def refresh_review_tag_status(self, *_args) -> None:
        tags, errors, warnings = self.current_review_tag_validation()
        if errors:
            text = f"{len(tags)}/5｜" + "；".join(errors)
            color = "#B42318"
        elif warnings:
            text = f"{len(tags)}/5｜可投稿；" + "；".join(warnings)
            color = "#A15C00"
        else:
            text = f"{len(tags)}/5｜人工 Tag 可投稿"
            color = "#137333"
        self.vars["review_tag_status"].set(text)
        if hasattr(self, "review_tag_status_label"):
            self.review_tag_status_label.configure(foreground=color)

    def _current_review_creator_key(self) -> str:
        selected = self.review_clip_tree.selection()
        if selected:
            key = str(self.review_item_map.get(selected[0], {}).get("creator") or "")
            if key in self.profiles:
                return key
        if self.current_review_dir:
            current = self.current_review_dir.resolve()
            while current != current.parent:
                state_path = current / core.PROJECT_FILENAME
                if state_path.is_file():
                    try:
                        return str(core.load_project(current)[1]["config"]["creator"])
                    except Exception:
                        break
                current = current.parent
        return self.profile_labels.get(self.vars["creator"].get(), "")

    def _normalized_review_participants(self) -> tuple[str, str, str, bool]:
        creator = self._current_review_creator_key()
        if creator not in self.profiles:
            raise ValueError("无法确定当前切片的主主播")
        host = self.profiles[creator]
        host_name = str(host.get("display_name") or creator).strip()
        kind = COLLABORATION_TYPE_LABELS.get(
            self.vars["review_collaboration_type"].get(), "single"
        )
        aliases: dict[str, tuple[str, str]] = {}
        for key, profile in self.profiles.items():
            for alias in (
                key,
                profile.get("display_name", ""),
                profile.get("title_tag", ""),
                *profile.get("hotwords", []),
            ):
                normalized = re.sub(r"\s+", "", str(alias)).casefold()
                if normalized:
                    aliases.setdefault(
                        normalized,
                        (key, str(profile.get("display_name") or key).strip()),
                    )
        names = [host_name]
        keys = [creator]
        for raw in re.split(
            r"[,，、;；|\n]+", self.vars["review_participants"].get()
        ):
            value = raw.strip()
            if not value:
                continue
            matched = aliases.get(re.sub(r"\s+", "", value).casefold())
            if matched:
                key, value = matched
                if key not in keys:
                    keys.append(key)
            if value.casefold() not in {name.casefold() for name in names}:
                names.append(value)
        verified = bool(self.vars["review_guest_dialogue_verified"].get())
        if kind == "single":
            names = [host_name]
            keys = [creator]
            verified = False
        elif len(names) < 2:
            raise ValueError("多人连麦或同步试听至少要填写一位嘉宾／被观看者")
        return kind, ",".join(names), ",".join(keys), verified

    def open_cover_maker(self) -> None:
        if not self.current_review_dir or not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先在审核台选择一条切片。")
            return
        copy_row = review_workspace.review_copy_row(
            self.current_review_dir, self.current_review_video.name
        )
        if not copy_row:
            messagebox.showerror(APP_TITLE, "当前切片缺少 titles-and-covers.csv 记录。")
            return
        owner = self.current_review_video
        content_type = str(copy_row.get("content_type") or "narrative")
        primary, secondary = self._review_cover_values()
        creator_key = self._current_review_creator_key()
        creator_name = str(
            self.profiles.get(creator_key, {}).get("display_name")
            or creator_key or "未知主播"
        )
        creator_catalog = cover_emotes.load_catalog(creator=creator_key)

        mode_labels = {
            "原话冲击": "quote-impact",
            "证据反应": "evidence-reaction",
            "冲突挑战": "clash-challenge",
            "歌曲封面": "song",
        }
        source_labels = {
            "自动布局": "auto",
            "全画面": "full",
            "电台左画面": "radio-left",
        }
        current_mode = str(copy_row.get("cover_mode") or "quote-impact")
        if current_mode == "viridis-song":
            current_mode = "song"
        current_source = str(copy_row.get("cover_source_region") or "auto")
        fields = {
            "primary": StringVar(value=primary),
            "secondary": StringVar(value=secondary),
            "emotion": StringVar(value=str(
                copy_row.get("cover_emotion") or cover_emotes.AUTO_EMOTION
            )),
            "emote": StringVar(value=str(copy_row.get("cover_emote") or "自动挑选")),
            "mode": StringVar(value=next(
                (label for label, value in mode_labels.items() if value == current_mode),
                "原话冲击",
            )),
            "source": StringVar(value=next(
                (label for label, value in source_labels.items() if value == current_source),
                "自动布局",
            )),
            "time": StringVar(value=str(copy_row.get("cover_time_seconds") or "")),
            "reference": StringVar(value=str(copy_row.get("reference_image") or "")),
            "status": StringVar(value="可使用视频当前播放位置取帧，或选择一张自定义背景图。"),
        }
        window = Toplevel(self)
        window.title("封面制作工具")
        width = min(1120, max(480, self.winfo_screenwidth() - 80))
        height = min(760, max(360, self.winfo_screenheight() - 100))
        window.geometry(f"{width}x{height}")
        window.minsize(min(640, width), min(400, height))
        window.transient(self)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        area = ttk.Frame(window, padding=(12, 12, 12, 0))
        area.grid(row=0, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        canvas = Canvas(area, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(area, orient="vertical", command=canvas.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=canvas.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        body = ttk.Frame(canvas)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.columnconfigure(0, weight=1)
        form = ttk.LabelFrame(body, text="封面文字、模板与画面", padding=10)
        form.grid(row=0, column=0, sticky="new")
        preview_box = ttk.LabelFrame(body, text="当前封面", padding=8)
        preview_box.grid(row=1, column=0, sticky="nw", pady=(10, 0))

        def resize_content(event=None) -> None:
            available = event.width if event is not None else canvas.winfo_width()
            form_width = form.winfo_reqwidth()
            preview_width = preview_box.winfo_reqwidth()
            if available >= form_width + preview_width + 16:
                preview_box.grid(row=0, column=1, sticky="ne", padx=(12, 0), pady=0)
                content_width = available
            else:
                preview_box.grid(row=1, column=0, sticky="nw", padx=0, pady=(10, 0))
                content_width = max(available, form_width, preview_width)
            canvas.itemconfigure(body_id, width=content_width)

        def update_content(_event) -> None:
            resize_content()
            canvas.configure(scrollregion=canvas.bbox("all"))

        body.bind("<Configure>", update_content)
        canvas.bind("<Configure>", resize_content)

        def scroll_content(event):
            if isinstance(event.widget, ttk.Combobox):
                return None
            if canvas.yview() != (0.0, 1.0) and event.delta:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            return None

        window.bind("<MouseWheel>", scroll_content)

        def form_row(index: int, label: str, widget) -> None:
            ttk.Label(form, text=label).grid(
                row=index, column=0, sticky="w", padx=(0, 10), pady=4
            )
            widget.grid(row=index, column=1, sticky="ew", pady=4)

        form.columnconfigure(1, weight=1)
        primary_editor = ScrolledText(form, wrap="word", height=2, font=(UI_FONT, 12))
        self._bind_multiline_variable(primary_editor, fields["primary"])
        form_row(0, "封面上区", primary_editor)
        secondary_editor = ScrolledText(
            form, wrap="word", height=2, font=(UI_FONT, 12)
        )
        self._bind_multiline_variable(secondary_editor, fields["secondary"])
        form_row(1, "封面下区", secondary_editor)
        ttk.Label(
            form,
            text=(
                f"普通封面每句 3–22 个有效字，上下区字号一致；长句按文字宽度均衡换行，每区最多两行。"
                f"单句可手动换一行；下区两句用｜分隔时会合并均衡排为最多两行。"
                f"表情图只从"
                f"{creator_name}专属库选择（当前 {len(creator_catalog)} 张）。"
            ),
            foreground="#555555",
            wraplength=320,
            justify="left",
        ).grid(row=2, column=1, sticky="w", pady=(0, 4))
        emotion_values = [
            cover_emotes.AUTO_EMOTION,
            cover_emotes.NO_EMOTE,
            *cover_emotes.EMOTION_CATEGORIES,
        ]
        emotion_box = ttk.Combobox(
            form, textvariable=fields["emotion"], values=emotion_values,
            state="readonly",
        )
        form_row(3, "表情包情绪", emotion_box)
        emote_box = ttk.Combobox(form, textvariable=fields["emote"], state="readonly")

        def refresh_emote_choices(*_args) -> None:
            emotion = fields["emotion"].get()
            labels = cover_emotes.emote_labels(
                emotion=emotion if emotion in cover_emotes.EMOTION_CATEGORIES else "",
                creator=creator_key,
            )
            values = ["自动挑选", *labels]
            current = fields["emote"].get().strip()
            emote_box.configure(values=values)
            if current not in values:
                fields["emote"].set("自动挑选")

        emotion_box.bind("<<ComboboxSelected>>", refresh_emote_choices)
        refresh_emote_choices()
        form_row(4, "具体表情包", emote_box)
        form_row(5, "封面模板", ttk.Combobox(
            form, textvariable=fields["mode"], values=list(mode_labels),
            state="readonly",
        ))
        form_row(6, "画面布局", ttk.Combobox(
            form, textvariable=fields["source"], values=list(source_labels),
            state="readonly",
        ))

        time_row = ttk.Frame(form)
        ttk.Entry(time_row, textvariable=fields["time"], width=8).pack(side=LEFT)
        ttk.Label(time_row, text="秒").pack(side=LEFT, padx=(3, 8))

        def use_current_time() -> None:
            seconds = self.current_edited_seconds()
            fields["time"].set(f"{max(0.0, seconds):.3f}")
            fields["reference"].set("")
            fields["status"].set("将从当前播放位置提取画面。")

        ttk.Button(
            time_row, text="使用当前播放位置", command=use_current_time
        ).pack(side=LEFT)
        form_row(7, "视频取帧", time_row)

        reference_row = ttk.Frame(form)
        ttk.Entry(reference_row, textvariable=fields["reference"]).pack(
            side=LEFT, fill=X, expand=True
        )

        def choose_reference() -> None:
            selected = filedialog.askopenfilename(
                parent=window,
                title="选择封面背景图",
                filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.webp"), ("所有文件", "*.*")],
            )
            if selected:
                fields["reference"].set(selected)
                fields["status"].set("将使用选择的图片作为封面背景。")

        ttk.Button(reference_row, text="选择图片", command=choose_reference).pack(
            side=LEFT, padx=(4, 0)
        )
        form_row(8, "自选背景", reference_row)
        ttk.Button(
            form,
            text="清空自选图，改用视频取帧",
            command=lambda: fields["reference"].set(""),
        ).grid(row=9, column=1, sticky="w", pady=(2, 8))
        ttk.Label(
            form, textvariable=fields["status"], foreground="#555555",
            wraplength=430, justify="left",
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 8))

        cover = self.current_review_dir / f"{self.current_review_video.stem}-cover.jpg"
        cover_photo = {"value": None}
        cover_preview = ttk.Label(preview_box, text="当前还没有封面", wraplength=300)
        cover_preview.pack()

        def refresh_cover_preview(*, revised: bool = False) -> None:
            if revised:
                preview_box.configure(text="修订后封面预览")
            if (
                cover.is_file()
                and Image is not None
                and ImageDraw is not None
                and ImageTk is not None
            ):
                try:
                    with Image.open(cover) as source:
                        image = source.convert("RGB")
                        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
                        guide = ImageDraw.Draw(overlay)
                        safe_left = round(image.width / 8)
                        safe_right = round(image.width * 7 / 8)
                        shade = (0, 0, 0, 72)
                        guide.rectangle((0, 0, safe_left, image.height), fill=shade)
                        guide.rectangle(
                            (safe_right, 0, image.width, image.height), fill=shade
                        )
                        guide.rectangle(
                            (safe_left, 1, safe_right - 1, image.height - 2),
                            outline=(255, 225, 0, 235),
                            width=max(3, image.width // 500),
                        )
                        image = Image.alpha_composite(
                            image.convert("RGBA"), overlay
                        ).convert("RGB")
                        image.thumbnail((300, 180))
                        cover_photo["value"] = ImageTk.PhotoImage(image)
                    cover_preview.configure(image=cover_photo["value"], text="")
                    return
                except Exception:
                    pass
            cover_photo["value"] = None
            cover_preview.configure(
                image="", text=cover.name if cover.is_file() else "当前还没有封面"
            )

        refresh_cover_preview()
        ttk.Label(
            preview_box,
            text="黄框 = 居中 4:3 安全区；16:9 显示完整画面",
            foreground="#555555",
            justify="left",
            wraplength=300,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Separator(preview_box, orient="horizontal").pack(fill=X, pady=(10, 7))
        ttk.Label(preview_box, text="表情包预览").pack(anchor="w")
        emote_preview_photo = {"value": None}
        emote_preview = ttk.Label(preview_box, text="自动按文案情绪选择", wraplength=300)
        emote_preview.pack(pady=(4, 2))

        def refresh_emote_preview(*_args) -> None:
            selected = cover_emotes.select_emote(
                "｜".join([
                    str(copy_row.get("title") or ""),
                    fields["primary"].get(),
                    fields["secondary"].get(),
                ]),
                emotion=fields["emotion"].get(),
                label=fields["emote"].get(),
                creator=creator_key,
            )
            if not selected or Image is None or ImageTk is None:
                emote_preview_photo["value"] = None
                empty_text = (
                    f"未找到{creator_name}自己的可用表情，不叠加图片"
                    if not creator_catalog
                    else "不叠加表情包"
                )
                emote_preview.configure(image="", text=empty_text)
                return
            try:
                with Image.open(selected["path"]) as source:
                    image = source.convert("RGBA")
                    image.thumbnail((180, 180))
                    emote_preview_photo["value"] = ImageTk.PhotoImage(image)
                emote_preview.configure(
                    image=emote_preview_photo["value"],
                    text=f"{selected['category']}｜{selected['label']}",
                    compound="top",
                )
            except Exception as exc:
                emote_preview.configure(image="", text=f"预览失败：{exc}")

        fields["emotion"].trace_add("write", refresh_emote_preview)
        fields["emote"].trace_add("write", refresh_emote_preview)
        refresh_emote_preview()
        ttk.Label(
            preview_box,
            text="生成后会替换当前切片封面，\n并进入正常烧录/投稿或历史换源流程。",
            justify="left", wraplength=300,
        ).pack(anchor="w", pady=(10, 0))

        def render_cover() -> None:
            if self.current_review_video != owner:
                fields["status"].set("当前切片已改变，请重新打开封面制作工具。")
                return
            try:
                mode = mode_labels[fields["mode"].get()]
                source_region = source_labels[fields["source"].get()]
                primary = fields["primary"].get().strip()
                secondary = fields["secondary"].get().strip()
                content_type = "song" if mode == "song" else "narrative"
                if content_type == "song":
                    primary, secondary = core.split_cover_copy(primary, content_type)
                else:
                    try:
                        primary, secondary = cover_emotes.validate_cover_lines(
                            primary, secondary
                        )
                    except ValueError as exc:
                        raise core.WorkflowError(str(exc)) from exc
                time_text = fields["time"].get().strip()
                if time_text:
                    seconds = float(time_text)
                    if not 0.0 <= seconds < float("inf"):
                        raise ValueError("视频取帧秒数必须是不小于 0 的数字")
                    time_text = f"{seconds:.3f}"
                reference = fields["reference"].get().strip()
                if reference:
                    reference_path = Path(reference).expanduser()
                    if not reference_path.is_absolute():
                        reference_path = self.current_review_dir / reference_path
                    reference_path = reference_path.resolve()
                    if not reference_path.is_file():
                        raise FileNotFoundError(f"自选背景图不存在：{reference_path}")
                    reference = str(reference_path)
                if not self._ensure_current_approved_revision("封面"):
                    return
                review_workspace.update_review_copy(
                    self.current_review_dir,
                    self.current_review_video.name,
                    title=self.vars["review_title"].get().strip(),
                    cover_text_primary=primary,
                    cover_text_secondary=secondary,
                    cover_emotion=fields["emotion"].get().strip(),
                    cover_emote=(
                        "" if fields["emote"].get() == "自动挑选"
                        else fields["emote"].get().strip()
                    ),
                    cover_mode=mode,
                    cover_source_region=source_region,
                    cover_time_seconds=time_text,
                    reference_image=reference,
                )
                self._set_review_cover_values(primary, secondary)
                fields["status"].set("正在生成修订后封面预览…")

                def show_revised_cover() -> None:
                    if self.current_review_video == owner:
                        self.review_clip_selected()
                    if not window.winfo_exists():
                        return
                    refresh_cover_preview(revised=True)
                    fields["status"].set("修订后封面已生成；请在封面预览区完成视觉复核。")

                self.rerender_current_review_cover(after=show_revised_cover)
            except Exception as exc:
                fields["status"].set(f"无法生成：{exc}")

        actions = ttk.Frame(window, padding=12)
        actions.grid(row=1, column=0, sticky="ew")
        ttk.Button(actions, text="取消", command=window.destroy).pack(side=RIGHT)
        ttk.Button(actions, text="生成修订并预览", command=render_cover).pack(
            side=RIGHT, padx=(0, 6)
        )

    def rerender_current_review_cover(self, *, after=None) -> None:
        if not self._ensure_current_approved_revision("封面"):
            return
        if not self.save_current_review_copy(silent=True):
            return
        if not self.current_review_dir or not self.current_review_video:
            return
        creator = self._current_review_creator_key()
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(core.SCRIPTS / "local_publish.py"),
            "render-local",
            "--copy",
            str(review_workspace.review_copy_path(self.current_review_dir)),
            "--clips-dir",
            str(self.current_review_dir),
            "--creator",
            creator,
            "--force",
            "--only-video",
            self.current_review_video.name,
        ]
        self.vars["review_line_status"].set("正在后台重生成当前封面…")
        self.run_command(
            command,
            flow_key="manual:cover",
            after=after or (lambda: self.review_clip_selected()),
        )
    def save_current_review_copy(
        self,
        *,
        silent: bool = False,
        require_valid_tags: bool = False,
        draft: bool = False,
    ) -> bool:
        if not self.current_review_dir or not self.current_review_video:
            if not silent:
                messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return False
        title = self.vars["review_title"].get().strip()
        primary, secondary = self._review_cover_values()
        description = self.vars["review_description"].get().strip()
        tags_text = self.vars["review_tags"].get().strip()
        tag_evidence = self.vars["review_tag_evidence"].get().strip()
        collection = self.vars["review_collection"].get().strip()
        season_id = self.vars["review_season_id"].get().strip()
        section_id = self.vars["review_section_id"].get().strip()
        collaboration_type = "single"
        participants = ""
        participant_keys = ""
        guest_verified = False
        try:
            (
                collaboration_type,
                participants,
                participant_keys,
                guest_verified,
            ) = self._normalized_review_participants()
            if not draft:
                if not title:
                    raise ValueError("标题不能为空")
                if len(title) > 80:
                    raise ValueError("标题不能超过 80 个字符")
                if (season_id and not section_id) or (section_id and not season_id):
                    raise ValueError("合集 season_id 和 section_id 必须同时填写或同时留空")
                for value, label in (
                    (season_id, "season_id"), (section_id, "section_id")
                ):
                    if value and (not value.isdigit() or int(value) <= 0):
                        raise ValueError(f"合集 {label} 必须是正整数")
                if (season_id or section_id) and not collection:
                    raise ValueError("填写合集 ID 时也必须填写合集名称")
            _tags, tag_errors, _tag_warnings = self.current_review_tag_validation()
            if require_valid_tags and tag_errors:
                raise ValueError("Tag 尚不能投稿：" + "；".join(tag_errors))
            copy_row = review_workspace.review_copy_row(
                self.current_review_dir, self.current_review_video.name
            )
            if tags_text and (
                tags_text != str(copy_row.get("tags", "")).strip() or not tag_evidence
            ):
                tag_evidence = "人工审核确认：" + "、".join(
                    selection_tags.split_tags(tags_text)
                )
                self.vars["review_tag_evidence"].set(tag_evidence)
            content_type = (copy_row.get("content_type") or "narrative").strip().lower()
            if content_type not in {"narrative", "song"}:
                content_type = "narrative"
            if draft:
                primary, secondary = primary.strip(), secondary.strip()
            else:
                if content_type == "song":
                    if secondary:
                        raise core.WorkflowError("歌切封面只填写上区歌名，下区必须留空")
                    primary, secondary = core.split_cover_copy(primary, content_type)
                else:
                    try:
                        primary, secondary = cover_emotes.validate_cover_lines(
                            primary, secondary
                        )
                    except ValueError as exc:
                        raise core.WorkflowError(str(exc)) from exc
            subtitle = primary + (f"｜{secondary}" if secondary else "")
            material_values = {
                "title": title,
                "cover_text_primary": primary,
                "cover_text_secondary": secondary,
                "tags": tags_text,
                "tag_evidence": tag_evidence,
                "description": description,
                "collection": collection,
                "season_id": season_id,
                "section_id": section_id,
                "collaboration_type": collaboration_type,
                "participants": participants,
                "participant_profile_keys": participant_keys,
                "speaker_evidence": self.vars[
                    "review_speaker_evidence"
                ].get().strip(),
                "guest_dialogue_verified": "1" if guest_verified else "0",
            }
            changed_fields = [
                field
                for field, value in material_values.items()
                if str(copy_row.get(field, "")).strip() != str(value).strip()
            ]
            if changed_fields:
                action_parts = []
                if any(
                    field in changed_fields
                    for field in (
                        "title", "cover_text_primary", "cover_text_secondary"
                    )
                ):
                    action_parts.append("标题或封面")
                if "tags" in changed_fields or "tag_evidence" in changed_fields:
                    action_parts.append("Tag")
                if any(
                    field in changed_fields
                    for field in (
                        "description", "collection", "season_id", "section_id"
                    )
                ):
                    action_parts.append("投稿信息")
                if any(
                    field in changed_fields
                    for field in (
                        "collaboration_type", "participants",
                        "participant_profile_keys", "speaker_evidence",
                        "guest_dialogue_verified",
                    )
                ):
                    action_parts.append("多人素材信息")
                action = "、".join(action_parts) or "投稿信息"
                if not self._ensure_current_approved_revision(action):
                    self._restore_current_review_copy(copy_row)
                    return False
            review_workspace.update_review_copy(
                self.current_review_dir,
                self.current_review_video.name,
                title=title,
                cover_text_primary=primary,
                cover_text_secondary=secondary,
                tags=tags_text,
                tag_evidence=tag_evidence,
                description=description,
                collection=collection,
                season_id=season_id,
                section_id=section_id,
                collaboration_type=collaboration_type,
                participants=participants,
                participant_profile_keys=participant_keys,
                speaker_evidence=self.vars["review_speaker_evidence"].get().strip(),
                guest_dialogue_verified="1" if guest_verified else "0",
            )
            delivery_copy = review_workspace.sync_review_tags_to_delivery(
                self.current_review_dir,
                self.current_review_video.name,
                tags=tags_text,
                tag_evidence=tag_evidence,
                description=description,
                collection=collection,
                season_id=season_id,
                section_id=section_id,
                collaboration_type=collaboration_type,
                participants=participants,
                participant_profile_keys=participant_keys,
                speaker_evidence=self.vars["review_speaker_evidence"].get().strip(),
                guest_dialogue_verified="1" if guest_verified else "0",
            )
            selected = self.review_clip_tree.selection()
            if selected:
                iid = selected[0]
                item = self.review_item_map.get(iid)
                if item is not None:
                    item["title"] = title
                    item["subtitle"] = subtitle
                    item["cover_text_primary"] = primary
                    item["cover_text_secondary"] = secondary
                    item["description"] = description
                    item["tags"] = tags_text
                    item["collection"] = collection
                    item["season_id"] = season_id
                    item["section_id"] = section_id
                    item["tag_evidence"] = tag_evidence
                    item["collaboration_type"] = collaboration_type
                    item["participants"] = participants
                    item["participant_profile_keys"] = participant_keys
                    item["speaker_evidence"] = self.vars[
                        "review_speaker_evidence"
                    ].get().strip()
                    item["guest_dialogue_verified"] = (
                        "1" if guest_verified else "0"
                    )
                values = list(self.review_clip_tree.item(iid, "values"))
                if len(values) >= 5:
                    values[4] = title
                    self.review_clip_tree.item(iid, values=values)
            self.refresh_review_tag_status()
            if not silent:
                detail = "标题、副标题、简介、Tag、合集与多人素材信息已保存"
                if delivery_copy is not None:
                    detail += "；已同步到现有交付，投稿前会重新预览"
                self.vars["review_line_status"].set(detail)
            return True
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return False
    def ensure_current_streaming_review_async(self, *, autoplay: bool = False) -> None:
        video = self.current_review_video
        if video is None or not review_workspace.supports_review_proxy(video):
            return
        key = str(video.resolve())
        if autoplay:
            self._streaming_autoplay_video = key
        if review_workspace.has_streaming_review(video):
            if autoplay:
                self.after(10, self.play_current_embedded)
            return
        if key in self.review_proxy_jobs:
            return
        self.review_proxy_jobs.add(key)
        self.vars["review_line_status"].set(
            "正在准备整场可延伸时间轴（无需重编码前后分钟素材）…"
        )

        def worker() -> None:
            try:
                with self.review_proxy_semaphore:
                    metadata = review_workspace.prepare_streaming_review(
                        video, core.bundled_ffmpeg()
                    )
                payload = {
                    "video": str(video),
                    "source_duration": float(metadata.get("source_duration", 0.0)),
                }
            except Exception as exc:
                payload = {"video": str(video), "error": str(exc)}
            self.output_queue.put(("review_stream", payload))

        threading.Thread(target=worker, daemon=True).start()

    def finish_streaming_review(self, payload: dict) -> None:
        video = Path(str(payload.get("video", "")))
        try:
            key = str(video.resolve())
        except OSError:
            key = str(video)
        self.review_proxy_jobs.discard(key)
        autoplay = getattr(self, "_streaming_autoplay_video", "") == key
        if autoplay:
            self._streaming_autoplay_video = ""
        if payload.get("error"):
            pending_extension = self.__dict__.get("_pending_stream_extension")
            if pending_extension and pending_extension[0] == key:
                self._pending_stream_extension = None
            self.vars["review_line_status"].set(
                f"整场时间轴准备失败：{payload['error']}"
            )
            return
        if self.current_review_video and video == self.current_review_video:
            self.timeline_state_by_video.pop(key, None)
            self.timeline_undo_by_video[key] = []
            self.timeline_redo_by_video[key] = []
            self.review_clip_selected()
            self.vars["review_line_status"].set(
                "整场时间轴已就绪；不会自动增加前后内容，使用按钮时才会扩展"
            )
            pending_extension = self.__dict__.get("_pending_stream_extension")
            if pending_extension and pending_extension[0] == key:
                self._pending_stream_extension = None
                _pending_key, direction, seconds = pending_extension
                self.after(
                    80,
                    lambda value=direction, amount=seconds:
                    self.extend_timeline_context(value, seconds=amount),
                )
            elif autoplay:
                self.after(80, self.play_current_embedded)


    def approve_or_replace_current_review(self) -> None:
        if not self.current_review_dir or not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return
        decision = self._current_review_decision_row()
        published, _job = self._current_review_source_publication()
        replacing_source = bool(decision.get("replacement_was_published")) or (
            published and str(decision.get("status") or "pending") == "approved"
        )
        if (
            replacing_source
            and not decision.get("replacement_requested")
            and not self._ensure_current_approved_revision("内容")
        ):
            return
        self.set_current_review_decision("approved")

    def set_current_review_decision(self, status: str) -> None:
        if not self.current_review_dir or not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return
        if status == "revise" and not (self.vars["review_notes"].get().strip() or self.vars["review_suggestions"].get().strip()):
            self.review_editor_notebook.select(self.review_feedback_tab)
            self.review_reason_editor.focus_set()
            messagebox.showwarning(APP_TITLE, "请填写人工重做理由或审核建议，再标记人工重做。")
            return
        decision_before = self._current_review_decision_row()
        if (
            status == "approved"
            and str(decision_before.get("status") or "") == "approved"
            and not decision_before.get("replacement_requested")
        ):
            external_changes = review_workspace.published_review_changes(
                self.current_review_dir, self.current_review_video.name
            )
            if external_changes:
                if not self._ensure_current_approved_revision(
                    "、".join(external_changes)
                ):
                    return
                decision_before = self._current_review_decision_row()
        if status == "approved" and self.timeline_rendering:
            try:
                current_key = str(self.current_review_video.resolve())
            except OSError:
                current_key = str(self.current_review_video)
            if current_key == self.timeline_rendering_video_key:
                if not self._flush_pending_subtitle_autosave():
                    return
                if not self.save_current_review_copy(
                    silent=True, require_valid_tags=True
                ):
                    return
                self._timeline_approval_request = {
                    "key": current_key,
                    "review_dir": str(self.current_review_dir),
                    "video_name": self.current_review_video.name,
                    "notes": self.vars["review_notes"].get(),
                    "revision": dict(decision_before),
                }
                self.vars["review_line_status"].set(
                    "剪辑稿正在生成；完成后会自动通过并开始烧录"
                )
                return
        try:
            if not self._flush_pending_subtitle_autosave():
                return
            if not self.save_current_review_copy(
                silent=True, require_valid_tags=status == "approved"
            ):
                return
            if status == "approved":
                edit_path = review_workspace.timeline_edit_path(self.current_review_video)
                edit_state = review_workspace._read_json(edit_path) if edit_path.is_file() else {}
                if edit_state.get("status") == "pending":
                    current_key = str(self.current_review_video.resolve())
                    self._timeline_approval_request = {
                        "key": current_key,
                        "review_dir": str(self.current_review_dir),
                        "video_name": self.current_review_video.name,
                        "notes": self.vars["review_notes"].get(),
                        "revision": dict(decision_before),
                    }
                    if not self.apply_timeline_edit(confirm=False):
                        self._timeline_approval_request = None
                    return
                review_workspace.sync_review_proxy_ass_to_original(self.current_review_video)
            review_dir = self.current_review_dir
            video = self.current_review_video
            current_path = str(video)
            global_mode = self.review_global_mode
            record_undo = (
                status == "approved"
                and str(decision_before.get("status") or "pending") != "approved"
                and not decision_before.get("replacement_requested")
            )
            if status == "revise" and not self.save_current_review_suggestions(silent=True):
                return
            decision_state = review_workspace.set_decision(
                review_dir,
                video.name,
                status,
                self.vars["review_notes"].get(),
                record_undo=record_undo,
            )
            if record_undo:
                history = decision_state.get(
                    review_workspace.DECISION_UNDO_HISTORY_KEY, []
                )
                if history:
                    entry = dict(history[-1])
                    entry["review_dir"] = str(review_dir)
                    self._last_review_approval_undo = entry
            removed_count = 0
            if status == "skipped":
                self.stop_embedded_preview(show_cover=True)
                removed_count = len(review_workspace.discard_review_clip(review_dir, video.name))
            if global_mode:
                self.load_global_review_pool(select_video_path=current_path)
            elif any(review_dir.glob("*.mp4")):
                self.load_review_directory(review_dir, select_video_path=current_path)
            else:
                self._populate_review_items(
                    [], summary="当前批次审核完成；没有剩余本地切片"
                )
            if status == "skipped":
                self.vars["auto_status"].set(
                    f"不通过稿已移入回收站（{removed_count} 个生成文件）；原始录播未删除"
                )
            if status == "approved" and decision_before.get(
                "replacement_requested"
            ):
                self.after(
                    150,
                    lambda directory=review_dir, name=video.name, row=dict(decision_before):
                    self._finish_approved_revision(directory, name, row),
                )
            elif status in {"approved", "skipped"}:
                self._schedule_review_batch_continuation(review_dir)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
    def load_current_waveform(self) -> None:
        self.review_waveform_token += 1
        token = self.review_waveform_token
        owner = self.current_review_video
        self.review_waveform_data = None
        self.draw_review_waveform(message="正在读取切片波形…")
        if owner is None:
            return
        owner_key = str(owner.resolve())
        pending_autoplay = self.__dict__.get("_review_waveform_autoplay_video")
        if pending_autoplay and pending_autoplay != owner_key:
            self._review_waveform_autoplay_video = ""
        self._review_waveform_loading_owner = owner_key
        try:
            spec = review_workspace.review_waveform_spec(owner)
            video = Path(spec["media"])
            stat = video.stat()
            key = (str(video.resolve()), stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError) as exc:
            if self.__dict__.get("_review_waveform_loading_owner") == owner_key:
                self._review_waveform_loading_owner = ""
            if self.__dict__.get("_review_waveform_autoplay_video") == owner_key:
                self._review_waveform_autoplay_video = ""
            self.draw_review_waveform(message=str(exc))
            return
        mapping = {
            "coordinate_space": spec.get("coordinate_space", "source"),
            "regions": list(spec.get("regions") or []),
            "source_duration": float(spec.get("source_duration", 0.0) or 0.0),
        }
        cached = self.review_waveform_cache.get(key)
        if cached is not None:
            self._review_waveform_loading_owner = ""
            self.review_waveform_data = {**cached, **mapping}
            self.ensure_timeline_state()
            self.draw_review_waveform()
            if self.__dict__.get("_review_waveform_autoplay_video") == owner_key:
                self._review_waveform_autoplay_video = ""
                if self.timeline_segments():
                    self.after(20, self.play_current_embedded)
            return
        cache_dir = review_workspace.review_proxy_directory(owner) / "_waveforms"

        def worker() -> None:
            try:
                with self.review_waveform_semaphore:
                    if token != self.review_waveform_token:
                        return
                    data = review_workspace.cached_waveform_peaks(
                        video,
                        core.bundled_ffmpeg(),
                        cache_dir=cache_dir,
                        columns=1100,
                    )
                payload = {
                    "token": token,
                    "owner": str(owner),
                    "video": str(video),
                    "key": key,
                    "data": data,
                    "mapping": mapping,
                }
            except Exception as exc:
                payload = {
                    "token": token,
                    "owner": str(owner),
                    "video": str(video),
                    "key": key,
                    "error": str(exc),
                }
            self.output_queue.put(("waveform", payload))

        threading.Thread(target=worker, daemon=True).start()

    def finish_review_waveform(self, payload: dict) -> None:
        owner_key = str(Path(str(payload.get("owner", ""))).resolve())
        if int(payload.get("token", -1)) != self.review_waveform_token:
            return
        if self.__dict__.get("_review_waveform_loading_owner") == owner_key:
            self._review_waveform_loading_owner = ""
        if (
            not self.current_review_video
            or payload.get("owner") != str(self.current_review_video)
        ):
            return
        if payload.get("error"):
            if self.__dict__.get("_review_waveform_autoplay_video") == owner_key:
                self._review_waveform_autoplay_video = ""
            self.review_waveform_data = None
            self.draw_review_waveform(message=f"波形生成失败：{payload['error']}")
            self.vars["review_line_status"].set(
                f"视频时间轴载入失败：{payload['error']}"
            )
            return
        data = payload.get("data")
        if isinstance(data, dict):
            key = payload.get("key")
            if isinstance(key, tuple):
                self.review_waveform_cache[key] = data
            mapping = payload.get("mapping")
            self.review_waveform_data = {
                **data,
                **(mapping if isinstance(mapping, dict) else {}),
            }
            self.ensure_timeline_state()
            self.draw_review_waveform()
            if self.__dict__.get("_review_waveform_autoplay_video") == owner_key:
                self._review_waveform_autoplay_video = ""
                if self.timeline_segments():
                    self.after(20, self.play_current_embedded)
                else:
                    self.vars["review_line_status"].set(
                        "素材已载入，但没有可播放的保留片段"
                    )

    def ensure_timeline_state(self) -> None:
        if not self.current_review_video:
            return
        key = str(self.current_review_video.resolve())
        waveform = self.review_waveform_data or {}
        metadata = review_workspace.review_proxy_metadata(self.current_review_video)
        duration = float(
            waveform.get("source_duration")
            or metadata.get("source_duration")
            or waveform.get("duration")
            or 0.0
        )
        if duration <= 0:
            return
        state = self.timeline_state_by_video.get(key)
        if state is None:
            state = review_workspace.load_timeline_edit(
                self.current_review_video, duration
            )
            self.timeline_state_by_video[key] = state
        self.timeline_state = state
        self.timeline_undo = self.timeline_undo_by_video.setdefault(key, [])
        self.timeline_redo = self.timeline_redo_by_video.setdefault(key, [])
        if self.timeline_selected_segment is None and state.get("segments"):
            self.timeline_selected_segment = 0
        self.refresh_timeline_apply_button()

    def refresh_timeline_apply_button(self) -> None:
        """Enable Apply only while the selected clip has a pending edit."""
        button = self.__dict__.get("timeline_apply_button")
        if button is None:
            return
        if self.timeline_rendering:
            button.configure(text="正在应用…", state="disabled")
            return
        pending = bool(
            self.current_review_video
            and self.timeline_state
            and self.timeline_state.get("status") == "pending"
        )
        button.configure(
            text="应用剪辑稿",
            state="normal" if pending else "disabled",
        )

    def timeline_segments(self) -> list[dict[str, float]]:
        self.ensure_timeline_state()
        if not self.timeline_state:
            return []
        return self.timeline_state.get("segments", [])

    def _reported_preview_source_seconds(self) -> float:
        if self.preview_player is None:
            return 0.0
        reported = self.preview_player.current_seconds()
        if self.current_review_video is None or self.current_review_media is None:
            return reported
        return review_workspace.review_media_to_source_seconds(
            self.current_review_video,
            self.current_review_media,
            reported,
        )

    def _preview_source_seconds(self) -> float:
        if self.preview_player is None:
            return 0.0
        reported = self._reported_preview_source_seconds()
        now = time.monotonic()
        for attribute, until_attribute, tolerance in (
            ("_review_jump_source", "_review_jump_until", 0.20),
            ("_review_starting_source", "_review_starting_until", 1.00),
        ):
            target = getattr(self, attribute)
            until = float(getattr(self, until_attribute))
            if target is None:
                continue
            if now < until and abs(reported - float(target)) > tolerance:
                return float(target)
            setattr(self, attribute, None)
        return reported

    def _set_preview_source_time(self, source_seconds: float, *, jump: bool) -> None:
        if self.preview_player is None:
            return
        target = max(0.0, float(source_seconds))
        media = self.current_review_media or self.current_review_video
        media_target = (
            review_workspace.source_to_review_media_seconds(
                self.current_review_video, media, target
            )
            if self.current_review_video is not None and media is not None
            else target
        )
        if media_target is None:
            raise RuntimeError("当前精确切片不包含该源时间，需切换到整场源播放")
        if jump:
            self._review_jump_source = target
            self._review_jump_until = time.monotonic() + 1.2
        else:
            self._review_starting_source = target
            self._review_starting_until = time.monotonic() + 0.9
        self._preview_at_timeline_end = False
        self.preview_player.set_time(media_target)

    def _next_preview_generation(self) -> int:
        generation = int(
            self.__dict__.get("_preview_playback_generation", 0) or 0
        ) + 1
        self._preview_playback_generation = generation
        return generation

    def _preview_callback_current(self, player, generation: int) -> bool:
        return bool(
            self.preview_player is player
            and player.player
            and int(self.__dict__.get("_preview_playback_generation", 0) or 0)
            == int(generation)
        )

    def _settle_preview_jump(
        self,
        player,
        generation: int,
        target: float,
        keep_paused: bool,
        attempt: int = 0,
    ) -> None:
        """Finish an in-place VLC seek without leaking audio from a deleted range."""
        if not self._preview_callback_current(player, generation):
            return
        reported = player.current_seconds()
        tolerance = max(0.35, self._review_playback_rate() * 0.12)
        settled = abs(float(reported) - float(target)) <= tolerance
        if not settled and attempt in {0, 3, 7}:
            player.set_time(target)
        if keep_paused:
            player.pause()
        elif not player.is_playing():
            player.resume()
        if settled or attempt >= 12:
            player.set_muted(False)
            return
        self.after(
            50,
            lambda: self._settle_preview_jump(
                player, generation, target, keep_paused, attempt + 1
            ),
        )

    def _pause_preview_at_timeline_end(self, index: int, source_end: float) -> None:
        if self.preview_player is None or not self.preview_player.player:
            return
        player = self.preview_player
        generation = self._next_preview_generation()
        target = max(
            float(self.timeline_segments()[index]["source_start"]),
            float(source_end) - 0.002,
        )
        media = self.current_review_media or self.current_review_video
        media_target = (
            review_workspace.source_to_review_media_seconds(
                self.current_review_video, media, target
            )
            if self.current_review_video is not None and media is not None
            else target
        )
        if media_target is None:
            media_target = target
        self._preview_segment_index = int(index)
        self._preview_at_timeline_end = True
        self._review_jump_source = target
        self._review_jump_until = time.monotonic() + 0.5
        player.set_muted(True)
        player.pause()
        player.set_time(media_target)
        self.after(
            80,
            lambda: (
                player.set_muted(False)
                if self._preview_callback_current(player, generation)
                else None
            ),
        )

    def _play_preview_segment(
        self, index: int, source_seconds: float, *, keep_paused: bool = False
    ) -> None:
        """Open the source once, then seek in-place across retained ranges."""
        if self.preview_player is None or not self.current_review_video:
            return
        segments = self.timeline_segments()
        if not 0 <= int(index) < len(segments):
            return
        segment = segments[int(index)]
        start = max(
            float(segment["source_start"]),
            min(float(segment["source_end"]) - 0.001, float(source_seconds)),
        )
        end = float(segment["source_end"])
        player = self.preview_player
        generation = self._next_preview_generation()
        self._preview_segment_index = int(index)
        self._preview_segment_started_at = time.monotonic()
        self._preview_at_timeline_end = False
        rate = self._review_playback_rate()
        if review_workspace.has_streaming_review(self.current_review_video):
            media = review_workspace.review_playback_media(
                self.current_review_video,
                float(segment["source_start"]),
                float(segment["source_end"]),
            )
        else:
            media = self.current_review_media or self.current_review_video
        self.current_review_media = media
        playback_start = review_workspace.source_to_review_media_seconds(
            self.current_review_video, media, start
        )
        if playback_start is None:
            raise RuntimeError("审核媒体不包含所选时间")
        media_key = str(media.resolve())
        reuse_player = bool(
            player.player
            and self.__dict__.get("_preview_media_path", "") == media_key
        )
        if reuse_player:
            player.set_muted(True)
            self._set_preview_source_time(start, jump=True)
            if keep_paused:
                player.pause()
            else:
                player.resume()
            player.set_rate(rate)
            self.after(
                25,
                lambda: self._settle_preview_jump(
                    player, generation, playback_start, keep_paused
                ),
            )
            return
        player.play(
            media,
            self.review_player_host.winfo_id(),
            start_seconds=playback_start,
        )
        self._preview_media_path = media_key
        player.set_muted(False)
        player.set_rate(rate)
        for delay in (180, 600):
            self.after(
                delay,
                lambda active=player, value=rate, token=generation: (
                    active.set_rate(value)
                    if self._preview_callback_current(active, token)
                    and self._review_playback_rate() == value else None
                ),
            )
        self._review_starting_source = start
        self._review_starting_until = time.monotonic() + 1.0
        self.after(
            120,
            lambda active=player, value=playback_start, token=generation: (
                active.set_time(value)
                if self._preview_callback_current(active, token) else None
            ),
        )
        if keep_paused:
            for delay in (180, 360):
                self.after(
                    delay,
                    lambda active=player, token=generation: (
                        active.pause()
                        if self._preview_callback_current(active, token) else None
                    ),
                )

    def current_edited_seconds(self) -> float:
        segments = self.timeline_segments()
        if not segments or self.preview_player is None:
            return 0.0
        value, _index = review_workspace.source_to_edited_nearest(
            segments, self._preview_source_seconds()
        )
        return float(value)

    def seek_timeline_seconds(
        self, edited_seconds: float, *, autoplay: bool = False
    ) -> None:
        segments = self.timeline_segments()
        if not segments:
            return
        source, index = review_workspace.edited_to_source(segments, edited_seconds)
        if self.preview_player is None or not self.preview_player.player:
            self._review_requested_source_start = source
            self.play_current_embedded()
        else:
            active_index = self.__dict__.get("_preview_segment_index")
            if active_index is not None and int(active_index) != int(index):
                keep_paused = not autoplay and not self.preview_player.is_playing()
                self._play_preview_segment(index, source, keep_paused=keep_paused)
            else:
                self._set_preview_source_time(source, jump=False)
                if autoplay:
                    restarted = self.preview_player.resume()
                    if restarted:
                        self.after(
                            120,
                            lambda value=source: self._set_preview_source_time(
                                value, jump=False
                            ),
                        )
        self.draw_review_playhead()

    def _ensure_timeline_ass_backup(self) -> None:
        if not self.timeline_state or not self.current_review_video:
            return
        if self.timeline_state.get("ass_backup_ready"):
            metadata = review_workspace.review_proxy_metadata_path(
                self.current_review_video
            )
            metadata_backup = review_workspace.timeline_backup_metadata_path(
                self.current_review_video
            )
            if metadata.is_file() and not metadata_backup.is_file():
                metadata_backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(metadata, metadata_backup)
            return
        if self.current_review_ass and self.current_review_ass.is_file():
            backup = review_workspace.timeline_backup_ass_path(
                self.current_review_video
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.current_review_ass, backup)
        metadata = review_workspace.review_proxy_metadata_path(
            self.current_review_video
        )
        if metadata.is_file():
            metadata_backup = review_workspace.timeline_backup_metadata_path(
                self.current_review_video
            )
            metadata_backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(metadata, metadata_backup)
        self.timeline_state["ass_backup_ready"] = True

    def _capture_timeline_snapshot(self) -> dict:
        if not self.current_review_video:
            return {}
        selected = self.review_subtitle_tree.selection()
        return {
            "state": copy.deepcopy(self.timeline_state),
            "selected_segment": self.timeline_selected_segment,
            "selected_subtitle": (
                str(selected[0]) if selected else str(self._review_edit_line or "")
            ),
            "ass_bytes": self.current_review_ass.read_bytes()
            if self.current_review_ass and self.current_review_ass.is_file()
            else None,
            "metadata_bytes": review_workspace.review_proxy_metadata_path(
                self.current_review_video
            ).read_bytes()
            if review_workspace.review_proxy_metadata_path(
                self.current_review_video
            ).is_file()
            else None,
            "had_edit_file": review_workspace.timeline_edit_path(
                self.current_review_video
            ).is_file(),
        }

    def push_timeline_undo(
        self, *, mark_video_edit: bool, clear_redo: bool = True
    ) -> None:
        self.ensure_timeline_state()
        if not self.current_review_video:
            return
        if mark_video_edit and self.timeline_state:
            self._ensure_timeline_ass_backup()
        snapshot = self._capture_timeline_snapshot()
        if not snapshot:
            return
        self.timeline_undo.append(snapshot)
        del self.timeline_undo[:-50]
        if clear_redo:
            self.timeline_redo.clear()

    def _restore_timeline_snapshot(self, snapshot: dict) -> None:
        if not self.current_review_video:
            return
        key = str(self.current_review_video.resolve())
        restored_state = copy.deepcopy(snapshot.get("state"))
        self.timeline_state = restored_state
        if restored_state is None:
            self.timeline_state_by_video.pop(key, None)
        else:
            self.timeline_state_by_video[key] = restored_state
        self.timeline_selected_segment = snapshot.get("selected_segment")
        metadata_path = review_workspace.review_proxy_metadata_path(
            self.current_review_video
        )
        if snapshot.get("metadata_bytes") is not None:
            metadata_path.write_bytes(snapshot["metadata_bytes"])
        if self.current_review_ass and snapshot.get("ass_bytes") is not None:
            self.current_review_ass.write_bytes(snapshot["ass_bytes"])
            review_workspace.sync_review_proxy_ass_to_original(
                self.current_review_video
            )
        if snapshot.get("had_edit_file") and restored_state is not None:
            review_workspace.save_timeline_edit(
                self.current_review_video, restored_state
            )
        else:
            review_workspace.clear_timeline_edit(self.current_review_video)
        self.reload_current_ass_dialogues(
            str(snapshot.get("selected_subtitle") or "")
        )
        self.draw_review_waveform()
        self.refresh_timeline_apply_button()

    def persist_timeline_state(self) -> None:
        if not self.current_review_video or not self.timeline_state:
            return
        # A delayed subtitle autosave can run while/after FFmpeg is applying the
        # same state. It must not recreate the just-cleared pending edit file.
        if self.timeline_state.get("status") in {"applying", "applied"}:
            return
        self.timeline_state["status"] = "pending"
        review_workspace.save_timeline_edit(
            self.current_review_video, self.timeline_state
        )
        self.refresh_timeline_apply_button()

    def cut_timeline_at_playhead(self) -> None:
        if self.timeline_rendering:
            return
        if not self.current_review_video or self.preview_player is None or not self.preview_player.player:
            self.vars["review_line_status"].set("请先播放视频，再按 C 切开")
            return
        try:
            self.push_timeline_undo(mark_video_edit=True)
            segments, selected = review_workspace.split_timeline_segment(
                self.timeline_segments(), self._preview_source_seconds()
            )
            self.timeline_state["segments"] = segments
            self.timeline_selected_segment = selected
            self.persist_timeline_state()
            self.draw_review_waveform()
            self.vars["review_line_status"].set(
                f"已在播放头切开；当前选中片段 {selected + 1}，Delete 可做波纹删除"
            )
        except Exception as exc:
            if self.timeline_undo:
                self.timeline_undo.pop()
            self.vars["review_line_status"].set(str(exc))

    def delete_selected_timeline_segment(self) -> None:
        if self.timeline_rendering:
            return
        segments = self.timeline_segments()
        index = self.timeline_selected_segment
        if index is None or not 0 <= index < len(segments):
            self.vars["review_line_status"].set("请先在视频上轨点击选择一个片段")
            return
        if len(segments) <= 1:
            self.vars["review_line_status"].set("不能删除唯一片段；请先按 C 切开")
            return
        try:
            spans = review_workspace.timeline_segment_spans(segments)
            span = spans[index]
            self.push_timeline_undo(mark_video_edit=True)

            del segments[index]
            self.timeline_state["segments"] = segments
            if self.current_review_ass:
                result = review_workspace.delete_ass_interval(
                    self.current_review_ass,
                    float(span["edited_start"]),
                    float(span["edited_end"]),
                )
            else:
                result = {"removed": 0, "shifted": 0, "clipped": 0}
            self.timeline_selected_segment = min(index, len(segments) - 1)
            self.persist_timeline_state()
            self.reload_current_ass_dialogues()
            self._preview_segment_index = -1
            self.seek_timeline_seconds(
                min(
                    float(span["edited_start"]),
                    review_workspace.timeline_duration(segments),
                )
            )
            self.draw_review_waveform()
            self.vars["review_line_status"].set(
                "已波纹删除并自动粘合；字幕同步"
                f"移除 {result['removed']}、前移 {result['shifted']}、缩短 {result['clipped']} 行"
            )
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, str(exc))

    def undo_timeline_action(self, *, silent: bool = False) -> None:
        if self.timeline_rendering:
            return
        if not self.timeline_undo or not self.current_review_video:
            if not silent:
                self.vars["review_line_status"].set("没有可撤销的审核动作")
            return
        current = self._capture_timeline_snapshot()
        snapshot = self.timeline_undo.pop()
        if not silent and current:
            self.timeline_redo.append(current)
            del self.timeline_redo[:-50]
        self._restore_timeline_snapshot(snapshot)
        if not silent:
            self.vars["review_line_status"].set(
                "已撤销上一动作；Ctrl+Y 可重做"
            )

    def redo_timeline_action(self) -> None:
        if self.timeline_rendering:
            return
        if not self.timeline_redo or not self.current_review_video:
            self.vars["review_line_status"].set("没有可重做的审核动作")
            return
        current = self._capture_timeline_snapshot()
        snapshot = self.timeline_redo.pop()
        if current:
            self.timeline_undo.append(current)
            del self.timeline_undo[:-50]
        self._restore_timeline_snapshot(snapshot)
        self.vars["review_line_status"].set("已重做上一动作；Ctrl+Z 可撤销")

    def discard_timeline_edit(self) -> None:
        if not self.current_review_video or not self.timeline_state:
            return
        if self.timeline_state.get("status") != "pending":
            self.vars["review_line_status"].set("当前没有未应用的视频剪辑")
            return
        if not messagebox.askyesno(
            APP_TITLE, "放弃当前视频所有未应用的切开和删除，并恢复剪辑前字幕吗？"
        ):
            return
        backup_ass = review_workspace.timeline_backup_ass_path(
            self.current_review_video
        )
        if self.current_review_ass and backup_ass.is_file():
            shutil.copy2(backup_ass, self.current_review_ass)
        backup_metadata = review_workspace.timeline_backup_metadata_path(
            self.current_review_video
        )
        metadata_path = review_workspace.review_proxy_metadata_path(
            self.current_review_video
        )
        if backup_metadata.is_file():
            shutil.copy2(backup_metadata, metadata_path)
        if self.current_review_ass and self.current_review_ass.is_file():
            review_workspace.sync_review_proxy_ass_to_original(
                self.current_review_video
            )
        review_workspace.clear_timeline_edit(self.current_review_video)
        key = str(self.current_review_video.resolve())
        self.timeline_state_by_video.pop(key, None)
        self.timeline_undo_by_video[key] = []
        self.timeline_undo = self.timeline_undo_by_video[key]
        self.timeline_redo_by_video[key] = []
        self.timeline_redo = self.timeline_redo_by_video[key]
        self.timeline_state = None
        self.timeline_selected_segment = None
        self.ensure_timeline_state()
        self.reload_current_ass_dialogues()
        self.draw_review_waveform()
        self.refresh_timeline_apply_button()
        self.vars["review_line_status"].set("已放弃未应用剪辑并恢复字幕")

    def apply_timeline_edit(self, *, confirm: bool = True) -> bool:
        if self.timeline_rendering:
            self.vars["review_line_status"].set(
                "剪辑稿仍在后台生成；完成后按钮会恢复，并显示“剪辑稿已应用”"
            )
            return False
        if self.current_review_video and not self.timeline_state:
            edit_path = review_workspace.timeline_edit_path(self.current_review_video)
            edit_state = review_workspace._read_json(edit_path) if edit_path.is_file() else {}
            if edit_state.get("segments"):
                key = str(self.current_review_video.resolve())
                self.timeline_state = edit_state
                self.timeline_state_by_video[key] = edit_state
        if not self.current_review_video or not self.timeline_state:
            self.vars["review_line_status"].set("时间轴尚未载入，暂不能应用剪辑稿")
            return False
        if not self._flush_pending_subtitle_autosave():
            self.vars["review_line_status"].set("字幕尚未写入成功，暂不能应用剪辑稿")
            return False
        if self.timeline_state.get("status") != "pending":
            self.vars["review_line_status"].set("当前没有需要应用的视频删减")
            return False
        if not self._ensure_current_approved_revision("视频剪辑"):
            return False
        approval_request = getattr(self, "_timeline_approval_request", None)
        if isinstance(approval_request, dict):
            approval_request["revision"] = self._current_review_decision_row()
        if confirm and not messagebox.askyesno(
            APP_TITLE,
            "将按上轨保留片段生成新的审核 MP4，并保留剪辑前视频备份。继续吗？",
        ):
            return False
        video = self.current_review_video
        segments = copy.deepcopy(self.timeline_segments())
        try:
            source_video, render_segments = review_workspace.timeline_render_plan(video, segments)
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return False
        edited_ass = self.current_review_ass
        render_dir = video.parent / review_workspace.TIMELINE_EDIT_DIR / "rendering"
        render_dir.mkdir(parents=True, exist_ok=True)
        temporary = render_dir / f"{video.stem}.tmp.mp4"
        backup_video = review_workspace.timeline_backup_video_path(video)
        self.stop_embedded_preview(show_cover=True)
        self.timeline_state["status"] = "applying"
        self.timeline_rendering = True
        self.timeline_rendering_video_key = str(video.resolve())
        self.refresh_timeline_apply_button()
        started = time.monotonic()
        duration = review_workspace.timeline_duration(segments)
        render_key = str(video.resolve())
        source_label = "当前切片" if source_video == video else "整场录播"
        self.vars["review_line_status"].set(f"正在应用剪辑稿：从{source_label}生成 {duration:.1f} 秒成片…")
        self._update_timeline_render_elapsed(render_key, started, duration)

        def worker() -> None:
            try:
                backup_video.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(video, backup_video)
                review_workspace.render_timeline_edit(
                    source_video, temporary, render_segments, core.bundled_ffmpeg()
                )
                payload = {
                    "video": str(video),
                    "temporary": str(temporary),
                    "backup": str(backup_video),
                    "used_proxy": source_video != video,
                    "segments": segments,
                    "ass": str(edited_ass or ""),
                }
            except Exception as exc:
                payload = {
                    "video": str(video),
                    "temporary": str(temporary),
                    "error": str(exc),
                }
            self.output_queue.put(("timeline_render", payload))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _update_timeline_render_elapsed(self, key: str, started: float, duration: float) -> None:
        if not self.timeline_rendering or self.timeline_rendering_video_key != key:
            return
        elapsed = time.monotonic() - started
        if elapsed >= 1.0:
            waiting = bool(self.__dict__.get("_timeline_approval_request"))
            detail = "；完成后继续审核通过" if waiting else ""
            self.vars["review_line_status"].set(
                f"正在生成 {duration:.1f} 秒剪辑稿，已用 {elapsed:.0f} 秒{detail}"
            )
        self.after(1000, lambda: self._update_timeline_render_elapsed(key, started, duration))

    def finish_timeline_render(self, payload: dict) -> None:
        key = str(Path(str(payload.get("video", ""))).resolve())
        try:
            self._finish_timeline_render_result(payload)
        except Exception as exc:
            video = Path(str(payload.get("video", "")))
            pending = review_workspace.timeline_edit_path(video).is_file()
            state = self.timeline_state_by_video.get(key)
            if state is not None:
                state["status"] = "pending" if pending else "applied"
            self._timeline_approval_request = None
            self.report_callback_exception(*sys.exc_info())
            detail = (
                "应用未完成，剪辑草稿与已生成视频已保留；请重试应用"
                if pending else "剪辑已保存，后续界面刷新或审核未完成；请刷新审核列表"
            )
            self.vars["review_line_status"].set(f"{detail}：{exc}")
        finally:
            self.timeline_rendering = False
            self.timeline_rendering_video_key = ""
            self.refresh_timeline_apply_button()

    def _finish_timeline_render_result(self, payload: dict) -> None:
        temporary = Path(str(payload.get("temporary", "")))
        video = Path(str(payload.get("video", "")))
        try:
            key = str(video.resolve())
        except OSError:
            key = str(video)
        applying_state = self.timeline_state_by_video.get(key)
        if payload.get("error"):
            if applying_state is not None:
                applying_state["status"] = "pending"
            if temporary.is_file():
                temporary.unlink()
            self.timeline_rendering = False
            self.timeline_rendering_video_key = ""
            if (
                getattr(self, "_timeline_approval_request", None)
                and self._timeline_approval_request.get("key") == key
            ):
                self._timeline_approval_request = None
            self.refresh_timeline_apply_button()
            messagebox.showerror(APP_TITLE, f"应用剪辑稿失败：{payload['error']}")
            return
        if not temporary.is_file():
            if applying_state is not None:
                applying_state["status"] = "pending"
            self.timeline_rendering = False
            self.timeline_rendering_video_key = ""
            if (
                getattr(self, "_timeline_approval_request", None)
                and self._timeline_approval_request.get("key") == key
            ):
                self._timeline_approval_request = None
            self.refresh_timeline_apply_button()
            messagebox.showerror(APP_TITLE, "应用剪辑稿失败：没有生成临时视频")
            return
        # Flush before clearing the edit file. The old order reloaded the row
        # after clearing, allowing its delayed autosave to write pending back.
        if not self._flush_pending_subtitle_autosave():
            raise ValueError("字幕修改尚未保存")
        self._review_edit_line = None
        if self.current_review_video == video:
            # The user may have resumed playback while FFmpeg was running.
            self.stop_embedded_preview(show_cover=True)
        review_workspace.commit_timeline_render(
            video, temporary, Path(str(payload["ass"])) if payload.get("ass") else None,
            list(payload.get("segments") or (applying_state or {}).get("segments") or []),
        )
        if applying_state is not None:
            applying_state["status"] = "applied"
        self.timeline_state_by_video.pop(key, None)
        self.timeline_undo_by_video[key] = []
        self.timeline_redo_by_video[key] = []
        approval_request = (
            self._timeline_approval_request
            if getattr(self, "_timeline_approval_request", None)
            and self._timeline_approval_request.get("key") == key
            else None
        )
        if approval_request:
            self._timeline_approval_request = None
            review_dir = Path(approval_request["review_dir"])
            revision = dict(approval_request.get("revision") or {})
            record_undo = (
                str(revision.get("status") or "pending") != "approved"
                and not revision.get("replacement_requested")
            )
            decision_state = review_workspace.set_decision(
                review_dir,
                approval_request["video_name"],
                "approved",
                approval_request.get("notes", ""),
                record_undo=record_undo,
            )
            if record_undo:
                history = decision_state.get(
                    review_workspace.DECISION_UNDO_HISTORY_KEY, []
                )
                if history:
                    entry = dict(history[-1])
                    entry["review_dir"] = str(review_dir)
                    self._last_review_approval_undo = entry
        else:
            notes = self.vars["review_notes"].get().strip()
            suffix = "已应用时间轴剪辑，需重新审核"
            current_decision = review_workspace.load_decisions(
                video.parent, create=False
            ).get("clips", {}).get(video.name, {})
            next_status = (
                "revise" if current_decision.get("replacement_requested")
                else "pending"
            )
            review_workspace.set_decision(
                video.parent, video.name, next_status,
                f"{notes}；{suffix}".strip("；"),
            )
        self.review_waveform_cache = {
            cache_key: value for cache_key, value in self.review_waveform_cache.items()
            if cache_key[0] != key
        }
        if self.review_global_mode:
            self.load_global_review_pool(select_video_path=str(video))
        else:
            self.load_review_directory(video.parent, select_video_path=str(video))
        self.timeline_rendering = False
        self.timeline_rendering_video_key = ""
        self.refresh_timeline_apply_button()
        if approval_request:
            self.vars["review_line_status"].set(
                "剪辑稿已应用并通过；正在准备烧录或替换原稿"
            )
            if revision.get("replacement_requested"):
                self.after(
                    150,
                    lambda directory=Path(approval_request["review_dir"]),
                    name=approval_request["video_name"], row=revision:
                    self._finish_approved_revision(directory, name, row),
                )
            else:
                self._schedule_review_batch_continuation(
                    Path(approval_request["review_dir"])
                )
        else:
            self.vars["review_line_status"].set(
                f"剪辑稿已应用；原视频备份：{payload.get('backup', '')}"
            )

    def reload_current_ass_dialogues(
        self, preferred_line: str = "", *, preserve_editor: bool = False
    ) -> None:
        if not self.current_review_ass or not self.current_review_ass.is_file():
            return
        selected = preferred_line
        if not selected:
            current = self.review_subtitle_tree.selection()
            selected = current[0] if current else ""
        self.review_dialogue_rows = review_workspace.read_dialogues(
            self.current_review_ass
        )
        self.refresh_review_speaker_choices()
        self.refresh_review_collaboration_status()
        self.filter_review_subtitles()
        ids = set(self.review_subtitle_tree.get_children())
        fallback = next(iter(self.review_subtitle_tree.get_children()), "")
        target = selected if selected in ids else fallback
        if target:
            self._subtitle_preserve_editor_line = (
                int(target) if preserve_editor else None
            )
            self.review_subtitle_tree.selection_set(target)
            self.review_subtitle_tree.focus(target)
            self.review_subtitle_tree.see(target)
            if not preserve_editor:
                self.review_subtitle_selected()
        else:
            self._subtitle_loading = True
            try:
                self._review_edit_line = None
                self._subtitle_preserve_editor_line = None
                self.vars["review_start"].set("")
                self.vars["review_end"].set("")
                self.vars["review_speaker"].set("待确认")
                self.review_text_editor.delete("1.0", END)
                self.review_text_editor.edit_modified(False)
            finally:
                self._subtitle_loading = False

    def _timeline_time_from_x(self, event_x: float) -> float:
        x = self.review_waveform.canvasx(event_x)
        duration = review_workspace.timeline_duration(self.timeline_segments())
        return max(0.0, min(duration, (x - 64.0) / max(0.1, self.timeline_zoom)))

    def timeline_mouse_down(self, event):
        self.review_waveform.focus_set()
        self.ensure_timeline_state()
        edited = self._timeline_time_from_x(event.x)
        y = float(event.y)
        if 22 <= y <= 79:
            for span in review_workspace.timeline_segment_spans(self.timeline_segments()):
                if float(span["edited_start"]) <= edited <= float(span["edited_end"]):
                    self.timeline_selected_segment = int(span["index"])
                    self.timeline_selection_kind = "video"
                    break
            self.seek_timeline_seconds(edited)
            self.draw_review_waveform()
            return "break"
        if 90 <= y <= 128 and self.review_dialogue_rows:
            selected_lines = set(self.review_subtitle_tree.selection())
            candidates = []
            for row in self.review_dialogue_rows:
                try:
                    start = review_workspace.parse_ass_time(row["start"])
                    end = review_workspace.parse_ass_time(row["end"])
                except ValueError:
                    continue
                if start <= edited <= end:
                    line = str(row["line_number"])
                    candidates.append(
                        (0 if line in selected_lines else 1, end - start, row, start, end)
                    )
            if candidates:
                _selected, _length, row, start, end = min(
                    candidates, key=lambda item: (item[0], item[1])
                )
                line = str(row["line_number"])
                self.timeline_selection_kind = "subtitle"
                editor_tabs = self.__dict__.get("review_editor_notebook")
                if editor_tabs is not None:
                    editor_tabs.select(self.review_subtitle_tab)
                self.review_subtitle_tree.selection_set(line)
                self.review_subtitle_tree.focus(line)
                self.review_subtitle_tree.see(line)
                self.review_subtitle_selected()
                width_pixels = max(1.0, (end - start) * self.timeline_zoom)
                handle_pixels = max(4.0, min(9.0, width_pixels * 0.28))
                start_distance = abs(edited - start) * self.timeline_zoom
                end_distance = abs(edited - end) * self.timeline_zoom
                if start_distance <= handle_pixels:
                    mode = "start"
                elif end_distance <= handle_pixels:
                    mode = "end"
                else:
                    mode = "move"
                self.push_timeline_undo(
                    mark_video_edit=False, clear_redo=False
                )
                self.timeline_drag = {
                    "line": line,
                    "field": mode,
                    "changed": False,
                    "start": start,
                    "end": end,
                    "anchor": edited,
                }
                hint = {
                    "start": "拖动字幕左边缘：调整出现时间",
                    "end": "拖动字幕右边缘：调整消失时间",
                    "move": "拖动字幕块中间：整体移动所在位置",
                }[mode]
                self.vars["review_line_status"].set(hint + "；右侧可修改字幕文字")
            else:
                self.timeline_selection_kind = None
                self.vars["review_line_status"].set("当前位置没有字幕块；滚轮可横向移动时间轴")
            self.draw_review_waveform()
            return "break"
        self.timeline_selection_kind = None
        self.seek_timeline_seconds(edited)
        return "break"

    def timeline_context_menu(self, event):
        self.review_waveform.focus_set()
        y = float(event.y)
        edited = self._timeline_time_from_x(event.x)
        if 22 <= y <= 79:
            tolerance = max(0.04, 12.0 / max(0.1, self.timeline_zoom))
            gap = review_workspace.restorable_timeline_gap(
                self.timeline_segments(),
                edited,
                tolerance_seconds=tolerance,
            )
            if gap is None:
                self.vars["review_line_status"].set(
                    "请在两个视频片段的交界处右键；只有删掉的中间原片可以恢复"
                )
                return "break"
            menu = Menu(self, tearoff=False)
            menu.add_command(
                label=f"恢复两段中间的 {float(gap['duration']):g} 秒原片",
                command=lambda value=int(gap["left_index"]):
                self.restore_timeline_middle(value),
            )
            menu.tk_popup(event.x_root, event.y_root)
            return "break"
        if not (90 <= y <= 128):
            return "break"
        selected = self.review_subtitle_tree.selection()
        if self.timeline_selection_kind != "subtitle" or not selected:
            self.vars["review_line_status"].set(
                "请先左键选中一个字幕块，再在字幕轴上右键"
            )
            return "break"
        menu = Menu(self, tearoff=False)
        menu.add_command(
            label=f"将选中字幕结束设为 {self.format_review_clock(edited)}",
            command=lambda value=edited: self.set_selected_subtitle_end(value),
        )
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def restore_timeline_middle(self, left_index: int) -> None:
        if not self.current_review_video or not self.timeline_state:
            return
        if not review_workspace.has_streaming_review(self.current_review_video):
            self.vars["review_line_status"].set(
                "这条旧素材没有整场源时间轴，无法安全恢复已删除的中间原片"
            )
            return
        if not self._flush_pending_subtitle_autosave():
            return
        if not self._ensure_current_approved_revision("恢复中间原片"):
            return
        self.push_timeline_undo(mark_video_edit=True)
        try:
            restored = review_workspace.restore_streaming_gap(
                self.current_review_video,
                self.timeline_state,
                int(left_index),
            )
            self.timeline_selected_segment = int(restored["inserted_index"])
            self.persist_timeline_state()
            review_workspace.sync_review_proxy_ass_to_original(
                self.current_review_video
            )
            self.reload_current_ass_dialogues()
            self.draw_review_waveform()
            self.seek_timeline_seconds(float(restored["edited_boundary"]))
            self.vars["review_line_status"].set(
                f"已恢复两段中间 {float(restored['duration']):g} 秒原片和对应字幕；Ctrl+Z 可撤销"
            )
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, f"无法恢复中间原片：{exc}")

    def set_selected_subtitle_end(self, edited_seconds: float) -> None:
        selected = self.review_subtitle_tree.selection()
        if not selected or not self.current_review_ass:
            return
        line = int(selected[0])
        row = next(
            (item for item in self.review_dialogue_rows if item["line_number"] == line),
            None,
        )
        if row is None:
            return
        try:
            start = review_workspace.parse_ass_time(str(row["start"]))
            end = max(0.0, float(edited_seconds))
            if end - start < 0.04:
                raise ValueError("右键位置必须在字幕开始时间之后至少 0.04 秒")
            self.push_timeline_undo(mark_video_edit=False)
            review_workspace.update_dialogue(
                self.current_review_ass,
                line,
                start=str(row["start"]),
                end=review_workspace.format_ass_time(end),
                text=str(row["text"]),
            )
            repaired = review_workspace.resolve_dialogue_overlaps(
                self.current_review_ass
            )
            if self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(
                    self.current_review_video
                )
            self.reload_current_ass_dialogues(str(line))
            note = f"；同时修复 {repaired} 处重叠" if repaired else ""
            self.vars["review_line_status"].set(
                f"字幕结束时间已设到右键位置{note}；Ctrl+Z 可撤回"
            )
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, str(exc))

    def timeline_mouse_drag(self, event):
        if not self.timeline_drag:
            return "break"
        value = self._timeline_time_from_x(event.x)
        try:
            field = str(self.timeline_drag["field"])
            playhead = self.current_edited_seconds()
            snapped = False
            if field in {"start", "end"}:
                value, snapped = review_workspace.snap_subtitle_time(
                    value, playhead, self.timeline_zoom
                )
            start, end = review_workspace.subtitle_drag_times(
                float(self.timeline_drag["start"]),
                float(self.timeline_drag["end"]),
                float(self.timeline_drag["anchor"]),
                value,
                field,
                review_workspace.timeline_duration(self.timeline_segments()),
            )
            if field == "move":
                start_snap, start_snapped = review_workspace.snap_subtitle_time(
                    start, playhead, self.timeline_zoom
                )
                end_snap, end_snapped = review_workspace.snap_subtitle_time(
                    end, playhead, self.timeline_zoom
                )
                if start_snapped and (
                    not end_snapped or abs(start - playhead) <= abs(end - playhead)
                ):
                    end += start_snap - start
                    start = start_snap
                    snapped = True
                elif end_snapped:
                    start += end_snap - end
                    end = end_snap
                    snapped = True
            self.vars["review_start"].set(review_workspace.format_ass_time(start))
            self.vars["review_end"].set(review_workspace.format_ass_time(end))
            self.timeline_drag["changed"] = True
            self.timeline_drag["snapped"] = snapped
            if snapped:
                self.vars["review_line_status"].set("已吸附到红色播放线")
            self.draw_review_waveform()
        except ValueError:
            pass
        return "break"

    def timeline_mouse_up(self, _event):
        drag = self.timeline_drag
        self.timeline_drag = None
        if not drag or not self.current_review_ass:
            return "break"
        if not drag.get("changed"):
            if self.timeline_undo:
                self.timeline_undo.pop()
            return "break"
        try:
            self.timeline_redo.clear()
            review_workspace.update_dialogue(
                self.current_review_ass,
                int(drag["line"]),
                start=self.vars["review_start"].get(),
                end=self.vars["review_end"].get(),
                text=self.review_text_editor.get("1.0", END),
            )
            review_workspace.force_single_line_ass(self.current_review_ass)
            repaired = review_workspace.resolve_dialogue_overlaps(
                self.current_review_ass
            )
            if self.current_review_video:
                review_workspace.sync_review_proxy_ass_to_original(
                    self.current_review_video
                )
            self.reload_current_ass_dialogues(str(drag["line"]))
            action = "整体位置已移动" if drag["field"] == "move" else "持续时间已调整"
            overlap_note = f"；同时修复 {repaired} 处重叠" if repaired else ""
            snap_note = "；已吸附红线" if drag.get("snapped") else ""
            self.vars["review_line_status"].set(
                f"字幕块{action}{snap_note}{overlap_note}；Ctrl+Z 可撤回"
            )
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, str(exc))
        return "break"

    def timeline_mouse_wheel(self, event):
        self.ensure_timeline_state()
        duration = review_workspace.timeline_duration(self.timeline_segments())
        if duration <= 0:
            return "break"
        canvas = self.review_waveform
        if not (int(getattr(event, "state", 0)) & 0x0004):
            delta = int(getattr(event, "delta", 0))
            clicks = max(1, abs(delta) // 120) if delta else 1
            direction = -1 if delta > 0 else 1
            canvas.xview_scroll(direction * clicks * 2, "units")
            return "break"
        old_zoom = self.timeline_zoom
        edited_at_pointer = self._timeline_time_from_x(event.x)
        view_width = max(100, canvas.winfo_width())
        fit_zoom = max(0.5, (view_width - 70) / duration)
        factor = 1.25 if event.delta > 0 else 0.8
        self.timeline_zoom = max(fit_zoom, min(120.0, old_zoom * factor))
        self.draw_review_waveform()
        content_width = 64 + duration * self.timeline_zoom + 20
        desired_left = 64 + edited_at_pointer * self.timeline_zoom - event.x
        canvas.xview_moveto(max(0.0, desired_left / max(content_width, 1.0)))
        return "break"
    def timeline_space_shortcut(self, _event=None):
        if not self._review_shortcut_allowed():
            return None
        if self.preview_player is None or not self.preview_player.player:
            self.play_current_embedded()
        else:
            self.pause_current_embedded()
        return "break"

    def timeline_cut_shortcut(self, _event=None):
        if not self._review_shortcut_allowed():
            return None
        self.cut_timeline_at_playhead()
        return "break"

    def timeline_delete_shortcut(self, _event=None):
        focus = self.focus_get()
        if focus not in {self.review_waveform, self.review_subtitle_tree}:
            return None
        if self.timeline_selection_kind == "subtitle" or focus == self.review_subtitle_tree:
            self.delete_current_subtitle()
            return "break"
        if self.timeline_selection_kind == "video":
            self.delete_selected_timeline_segment()
            return "break"
        self.vars["review_line_status"].set(
            "请先选择上轨视频片段或下轨字幕块"
        )
        return "break"

    def timeline_undo_shortcut(self, _event=None):
        if not self._review_shortcut_allowed():
            return None
        self.undo_timeline_action()
        return "break"

    def timeline_redo_shortcut(self, _event=None):
        if not self._review_shortcut_allowed():
            return None
        self.redo_timeline_action()
        return "break"

    def draw_review_waveform(self, message: str = "") -> None:
        if not hasattr(self, "review_waveform"):
            return
        canvas = self.review_waveform
        canvas.delete("all")
        view_width = max(100, canvas.winfo_width())
        height = max(130, canvas.winfo_height())
        data = self.review_waveform_data
        if not data or not data.get("peaks"):
            canvas.configure(scrollregion=(0, 0, view_width, height))
            canvas.create_text(
                view_width / 2, height / 2,
                text=message or "选择切片后显示视频轨与字幕轨",
                fill="#D7DEE9", font=(UI_FONT, 12),
            )
            return
        self.ensure_timeline_state()
        segments = self.timeline_segments()
        duration = review_workspace.timeline_duration(segments)
        if duration <= 0:
            return
        left = 64.0
        fit_zoom = max(0.5, (view_width - left - 10) / duration)
        self.timeline_zoom = max(fit_zoom, self.timeline_zoom)
        content_width = max(view_width, left + duration * self.timeline_zoom + 20)
        canvas.configure(scrollregion=(0, 0, content_width, height))
        canvas.create_rectangle(left, 22, content_width, 79, fill="#172033", outline="#42516B")
        canvas.create_rectangle(left, 90, content_width, 128, fill="#20263A", outline="#56647D")
        canvas.create_text(7, 50, text="视频", anchor="w", fill="#D7DEE9", font=(UI_FONT, 11))
        canvas.create_text(
            7, 109, text=f"字幕\n{len(self.review_dialogue_rows)} 条",
            anchor="w", fill="#FFFFFF", font=(UI_FONT, 10), justify="left",
        )

        tick = 0.1
        for candidate in (0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300):
            tick = candidate
            if tick * self.timeline_zoom >= 68:
                break
        marker = 0.0
        while marker <= duration + 1e-6:
            x = left + marker * self.timeline_zoom
            canvas.create_line(x, 12, x, 128, fill="#38445B")
            canvas.create_text(
                x + 3, 10, text=self.format_review_clock(marker),
                anchor="sw", fill="#AAB5C8", font=(UI_FONT, 9),
            )
            marker += tick

        peaks = data["peaks"]
        waveform_duration = float(data.get("duration") or 0.0)
        step = max(2, int(max(1.0, content_width - left) / 2200))
        for x in range(int(left), int(content_width), step):
            edited = (x - left) / self.timeline_zoom
            source, _index = review_workspace.edited_to_source(segments, edited)
            sample_seconds = review_workspace.waveform_sample_seconds(data, source)
            if sample_seconds is None:
                amplitude = 1.0
                color = "#465064"
            else:
                peak_index = min(
                    len(peaks) - 1,
                    max(
                        0,
                        int(
                            sample_seconds
                            / max(waveform_duration, 0.001)
                            * (len(peaks) - 1)
                        ),
                    ),
                )
                amplitude = max(1.0, float(peaks[peak_index]) * 20)
                color = "#61D6C4"
            canvas.create_line(
                x, 50 - amplitude, x, 50 + amplitude,
                fill=color, tags=("wave",),
            )

        for span in review_workspace.timeline_segment_spans(segments):
            index = int(span["index"])
            segment = segments[index]
            segment_label = str(segment.get("label") or f"视频片段 {index + 1}")
            x1 = left + float(span["edited_start"]) * self.timeline_zoom
            x2 = left + float(span["edited_end"]) * self.timeline_zoom
            selected = index == self.timeline_selected_segment
            canvas.create_rectangle(
                x1 + 1, 24, max(x1 + 3, x2 - 1), 77,
                outline="#FFD166" if selected else "#7AA7D9",
                width=3 if selected else 1,
                tags=(f"segment-{index}", "video-segment"),
            )
            if x2 - x1 > 55:
                canvas.create_text(
                    (x1 + x2) / 2, 28,
                    text=segment_label,
                    anchor="n", fill="#FFFFFF", font=(UI_FONT, 10),
                )

        selected_lines = set(self.review_subtitle_tree.selection())
        visible_subtitles = 0
        for row in self.review_dialogue_rows:
            line = str(row["line_number"])
            try:
                start_value = self.vars["review_start"].get() if line in selected_lines else row["start"]
                end_value = self.vars["review_end"].get() if line in selected_lines else row["end"]
                start = review_workspace.parse_ass_time(start_value)
                end = review_workspace.parse_ass_time(end_value)
            except ValueError:
                continue
            if end <= 0 or start >= duration or end <= start:
                continue
            visible_subtitles += 1
            clipped_start = max(0.0, start)
            clipped_end = min(duration, end)
            x1 = left + clipped_start * self.timeline_zoom
            x2 = left + clipped_end * self.timeline_zoom
            selected = line in selected_lines
            display_x2 = max(x1 + 6, x2)
            canvas.create_rectangle(
                x1 + 1, 94, display_x2 - 1, 124,
                fill="#F59E0B" if selected else "#356AC3",
                outline="#FFFFFF" if selected else "#9DB7E8",
                width=3 if selected else 1,
                tags=(f"subtitle-{line}", "subtitle-cue"),
            )
            actual_width = x2 - x1
            if actual_width > 34:
                max_chars = max(2, int((actual_width - 12) / 8))
                label = f"{line}  {row['text']}"[:max_chars]
                canvas.create_text(
                    x1 + 6, 109, text=label,
                    anchor="w", fill="#FFFFFF", font=(UI_FONT, 10),
                    tags=(f"subtitle-{line}", "subtitle-cue"),
                )
            if selected:
                for handle_x in (x1, x2):
                    canvas.create_rectangle(
                        handle_x - 4, 96, handle_x + 4, 122,
                        fill="#FFF7D6", outline="#7A4B00", width=1,
                        tags=(f"subtitle-{line}", "subtitle-handle"),
                    )
        if not self.review_dialogue_rows:
            canvas.create_text(
                left + 14, 109,
                text="没有载入同名 ASS 字幕，因此下轨暂无字幕块",
                anchor="w", fill="#FFB4B4", font=(UI_FONT, 11),
            )
        elif visible_subtitles == 0:
            canvas.create_text(
                left + 14, 109,
                text="ASS 有字幕，但所有时间都超出当前视频时长",
                anchor="w", fill="#FFCF70", font=(UI_FONT, 11),
            )
        self.draw_review_playhead()
    def draw_review_playhead(self) -> None:
        if not hasattr(self, "review_waveform"):
            return
        self.review_waveform.delete("playhead")
        segments = self.timeline_segments()
        if not segments:
            return
        current = self.current_edited_seconds()
        x = 64.0 + current * self.timeline_zoom
        self.review_waveform.create_line(
            x, 12, x, 128, fill="#FF5A5F", width=2, tags=("playhead",),
        )

    def _keep_review_playhead_visible(self, edited_seconds: float) -> None:
        """Follow a growing streaming timeline without fighting nearby manual scrolls."""
        canvas = self.review_waveform
        view_width = max(100.0, float(canvas.winfo_width()))
        view_left = float(canvas.canvasx(0))
        view_right = view_left + view_width
        x = 64.0 + max(0.0, float(edited_seconds)) * self.timeline_zoom
        if view_left + 40.0 <= x <= view_right - 70.0:
            return
        duration = review_workspace.timeline_duration(self.timeline_segments())
        content_width = max(
            view_width, 64.0 + duration * self.timeline_zoom + 20.0
        )
        desired_left = max(0.0, min(content_width - view_width, x - view_width * 0.35))
        canvas.xview_moveto(desired_left / max(content_width, 1.0))

    @staticmethod
    def format_review_clock(seconds: float) -> str:
        centiseconds = max(0, round(seconds * 100))
        minutes, remainder = divmod(centiseconds, 6000)
        secs, cs = divmod(remainder, 100)
        return f"{minutes:02d}:{secs:02d}.{cs:02d}"

    def _preview_dialogue_rows(self) -> list[dict]:
        """Include unsaved keystrokes in the low-cost video subtitle overlay."""
        rows = [dict(row) for row in self.review_dialogue_rows]
        if self._review_edit_line is None:
            return rows
        for row in rows:
            if row["line_number"] != self._review_edit_line:
                continue
            row["start"] = self.vars["review_start"].get()
            row["end"] = self.vars["review_end"].get()
            row["text"] = review_workspace.single_line_text(
                self.review_text_editor.get("1.0", END)
            )
            break
        return rows

    def _update_review_video_subtitle(
        self, edited_seconds: float, *, visible: bool
    ) -> None:
        if not hasattr(self, "review_video_subtitle"):
            return
        text = (
            review_workspace.active_dialogue_text(
                self._preview_dialogue_rows(), edited_seconds
            )
            if visible
            else ""
        )
        self.review_video_subtitle.configure(text=text or " ")

    def extend_timeline_context(
        self, direction: str, *, seconds: float = 5.0
    ) -> None:
        """Explicitly add source time before or after the current cut."""
        if direction not in {"before", "after"}:
            raise ValueError(f"未知扩展方向：{direction}")
        if not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return
        video = self.current_review_video
        if (
            review_workspace.supports_review_proxy(video)
            and not review_workspace.has_streaming_review(video)
        ):
            key = str(video.resolve())
            self._pending_stream_extension = (key, direction, float(seconds))
            self.ensure_current_streaming_review_async(autoplay=False)
            self.vars["review_line_status"].set(
                "正在准备整场源时间轴；完成后会增加时间，但不会自动播放"
            )
            return
        if not review_workspace.has_streaming_review(video):
            self.vars["review_line_status"].set("当前素材没有可供前后增加的整场源视频")
            return
        self.ensure_timeline_state()
        if not self.timeline_state or not self.current_review_ass:
            self.vars["review_line_status"].set("时间轴或字幕尚未载入")
            return
        if not self._flush_pending_subtitle_autosave():
            return
        if not self._ensure_current_approved_revision("视频前后范围"):
            return
        selected_signature = None
        selected_source = None
        selected_row = next(
            (
                row for row in self.review_dialogue_rows
                if row["line_number"] == self._review_edit_line
            ),
            None,
        )
        old_segments = copy.deepcopy(self.timeline_segments())
        if selected_row is not None:
            selected_signature = (
                selected_row.get("text", ""),
                selected_row.get("effect", ""),
                selected_row.get("name", ""),
                selected_row.get("style", ""),
            )
            selected_source, _ = review_workspace.edited_to_source(
                old_segments,
                review_workspace.parse_ass_time(selected_row["start"]),
            )
        self.push_timeline_undo(mark_video_edit=True)
        try:
            source_edge = (
                float(old_segments[0]["source_start"])
                if direction == "before"
                else float(old_segments[-1]["source_end"])
            )
            changed = review_workspace.reveal_streaming_context(
                video,
                self.timeline_state,
                source_edge,
                chunk_seconds=float(seconds),
                direction=direction,
            )
            if not changed:
                if self.timeline_undo:
                    self.timeline_undo.pop()
                label = "最前面" if direction == "before" else "最后面"
                self.vars["review_line_status"].set(
                    f"已经到源视频{label}，无法继续增加"
                )
                return
            self.persist_timeline_state()
            preferred = ""
            if selected_signature is not None and selected_source is not None:
                candidates = []
                for row in review_workspace.read_dialogues(self.current_review_ass):
                    signature = (
                        row.get("text", ""), row.get("effect", ""),
                        row.get("name", ""), row.get("style", ""),
                    )
                    if signature != selected_signature:
                        continue
                    candidate_source, _ = review_workspace.edited_to_source(
                        self.timeline_segments(),
                        review_workspace.parse_ass_time(row["start"]),
                    )
                    candidates.append(
                        (abs(candidate_source - selected_source), row["line_number"])
                    )
                if candidates:
                    preferred = str(min(candidates)[1])
            self.reload_current_ass_dialogues(preferred)
            self.draw_review_waveform()
            label = "前面" if direction == "before" else "后面"
            self.vars["review_line_status"].set(
                f"已往{label}增加 {float(seconds):g} 秒；没有自动播放，可继续点击增加"
            )
        except Exception as exc:
            self.undo_timeline_action(silent=True)
            messagebox.showerror(APP_TITLE, f"无法增加前后时间：{exc}")
    def _review_playback_tick(self) -> None:
        try:
            segments = self.timeline_segments()
            duration = review_workspace.timeline_duration(segments)
            current = 0.0
            source = 0.0
            subtitle_visible = False
            if self.preview_player is not None and self.preview_player.player and segments:
                reported_source = self._reported_preview_source_seconds()
                source = self._preview_source_seconds()
                settling = abs(source - reported_source) > 0.05
                edited, index = review_workspace.source_to_edited(segments, source)
                active_index = self.__dict__.get("_preview_segment_index")
                if active_index is not None and 0 <= int(active_index) < len(segments):
                    active_index = int(active_index)
                    active = segments[active_index]
                    active_start = float(active["source_start"])
                    active_end = float(active["source_end"])
                    started_at = float(
                        self.__dict__.get("_preview_segment_started_at", 0.0) or 0.0
                    )
                    boundary_lead = max(
                        0.015, self._review_playback_rate() * 0.025
                    )
                    reached_boundary = bool(
                        not settling
                        and self.preview_player.is_playing()
                        and source >= active_end - boundary_lead
                    )
                    if (
                        reached_boundary
                        and active_index + 1 < len(segments)
                    ):
                        next_index = active_index + 1
                        next_source = float(segments[next_index]["source_start"])
                        self._play_preview_segment(next_index, next_source)
                        source = next_source
                        edited = sum(
                            float(item["source_end"]) - float(item["source_start"])
                            for item in segments[:next_index]
                        )
                        index = next_index
                    elif (
                        reached_boundary
                        and active_index + 1 >= len(segments)
                        and not self.__dict__.get("_preview_at_timeline_end", False)
                    ):
                        self._pause_preview_at_timeline_end(
                            active_index, active_end
                        )
                        source = max(active_start, active_end - 0.002)
                        edited = duration
                        index = active_index
                if edited is None and not settling:
                    next_span = next(
                        (
                            span
                            for span in review_workspace.timeline_segment_spans(segments)
                            if float(span["source_start"]) > source + 0.001
                        ),
                        None,
                    )
                    if next_span is not None:
                        next_index = int(next_span["index"])
                        target = float(next_span["source_start"])
                        self._play_preview_segment(next_index, target)
                        source = target
                        edited = float(next_span["edited_start"])
                        index = next_index
                if edited is None:
                    edited, _nearest_index = review_workspace.source_to_edited_nearest(
                        segments, source
                    )
                    subtitle_visible = False
                else:
                    # Wait for VLC to reach the new media time before showing
                    # the target cue over audio/video from the previous position.
                    subtitle_visible = abs(source - reported_source) <= 0.05
                current = float(edited)
            metadata = (
                review_workspace.review_proxy_metadata(self.current_review_video)
                if self.current_review_video else {}
            )
            full_duration = float(metadata.get("source_duration", 0.0) or 0.0)
            self.vars["review_playback"].set(
                f"成片播放 {self.format_review_clock(current)} / 已载入 {self.format_review_clock(duration)}"
                + (
                    f"｜源视频 {self.format_review_clock(source)} / {self.format_review_clock(full_duration)}"
                    if full_duration else ""
                )
                + "｜只播放保留片段｜空格播放/暂停｜C切开｜Delete删所选轨"
            )
            self.draw_review_playhead()
            self._update_review_video_subtitle(current, visible=subtitle_visible)
            self._keep_review_playhead_visible(current)
        finally:
            if self.winfo_exists():
                self.after(25, self._review_playback_tick)
    def _review_shortcut_allowed(self) -> bool:
        if not hasattr(self, "notebook") or self.notebook.select() != str(self.review_tab):
            return False
        focus = self.focus_get()
        if focus is None:
            return True
        return focus.winfo_class() not in {
            "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"
        }

    def mark_review_start_shortcut(self, _event=None):
        if not self._review_shortcut_allowed():
            return None
        self.mark_review_boundary("start")
        return "break"

    def mark_review_end_shortcut(self, _event=None):
        if not self._review_shortcut_allowed():
            return None
        self.mark_review_boundary("end")
        return "break"

    def mark_review_boundary(self, field: str) -> None:
        if self.preview_player is None or not self.preview_player.player:
            self.vars["review_line_status"].set("请先播放视频，再用 ← / → 标记时间")
            return
        seconds = self.current_edited_seconds()
        key = "review_start" if field == "start" else "review_end"
        self.vars[key].set(review_workspace.format_ass_time(seconds))
        label = "开始" if field == "start" else "结束"
        self.vars["review_line_status"].set(
            f"已把当前播放头标为{label}；时间修改会立即写入 ASS"
        )
        self.draw_review_waveform()

    def play_current_embedded(self) -> None:
        if not self.current_review_video:
            messagebox.showwarning(APP_TITLE, "请先选择一条切片。")
            return
        requested_source = self._review_requested_source_start
        if (
            review_workspace.supports_review_proxy(self.current_review_video)
            and not review_workspace.has_streaming_review(self.current_review_video)
        ):
            self.ensure_current_streaming_review_async(autoplay=True)
            self.vars["review_line_status"].set(
                "正在连接整场源时间轴并启用严格剪辑预览，完成后播放"
            )
            return
        segments = self.timeline_segments()
        if not segments:
            if self.review_waveform_data is None:
                video_key = str(self.current_review_video.resolve())
                self._review_waveform_autoplay_video = video_key
                self.vars["review_line_status"].set(
                    "正在读取视频时长与时间轴；完成后会自动播放"
                )
                if self.__dict__.get("_review_waveform_loading_owner") != video_key:
                    self.load_current_waveform()
            else:
                self.vars["review_line_status"].set(
                    "当前剪辑稿没有可播放的保留片段"
                )
            return
        library = review_workspace.find_libvlc()
        if library is None:
            messagebox.showwarning(
                APP_TITLE,
                "未找到 VLC 的 libvlc.dll，无法内嵌播放；仍可使用外部播放器。",
            )
            return
        try:
            if self.preview_player is None:
                self.preview_player = review_workspace.EmbeddedVlcPlayer(library)
            self.update_idletasks()
            self.review_cover.place_forget()
            if requested_source is None:
                try:
                    start = review_workspace.parse_ass_time(self.vars["review_start"].get())
                except ValueError:
                    start = 0.0
                source_start, segment_index = review_workspace.edited_to_source(
                    segments, start
                )
            else:
                source_start = float(requested_source)
                _edited, segment_index = review_workspace.source_to_edited(
                    segments, source_start
                )
                if segment_index is None:
                    next_span = next(
                        (
                            span
                            for span in review_workspace.timeline_segment_spans(
                                segments
                            )
                            if float(span["source_start"]) >= source_start
                        ),
                        None,
                    )
                    if next_span is None:
                        next_span = review_workspace.timeline_segment_spans(segments)[-1]
                    segment_index = int(next_span["index"])
                    source_start = float(next_span["source_start"])
            self._play_preview_segment(segment_index, source_start)
            self._review_requested_source_start = None
            self.vars["review_line_status"].set(
                f"正在左侧播放：{(self.current_review_media or self.current_review_video).name}"
            )
        except Exception as exc:
            self.review_cover.place(x=0, y=0, relwidth=1, relheight=1)
            self.review_cover.lift()
            messagebox.showerror(APP_TITLE, f"内嵌播放失败：{exc}")

    def _review_playback_rate(self) -> float:
        raw = self.vars["review_speed"].get().strip().lower().rstrip("x×")
        try:
            return max(0.5, min(2.0, float(raw)))
        except (TypeError, ValueError):
            return 1.0

    def set_review_playback_rate(self, _event=None) -> None:
        rate = self._review_playback_rate()
        self.vars["review_speed"].set(f"{rate:g}x" if rate != 1.0 else "1.0x")
        if self.preview_player is not None:
            playback_player = self.preview_player
            playback_player.set_rate(rate)
            for delay in (180, 600):
                self.after(
                    delay,
                    lambda player=playback_player, value=rate: (
                        player.set_rate(value)
                        if self.preview_player is player and self._review_playback_rate() == value else None
                    ),
                )
        self.vars["review_line_status"].set(f"播放倍速已调整为 {rate:g}x")

    def pause_current_embedded(self) -> None:
        if self.preview_player:
            if self.__dict__.get("_preview_at_timeline_end", False):
                segments = self.timeline_segments()
                if segments:
                    self._play_preview_segment(
                        0, float(segments[0]["source_start"])
                    )
                return
            self.preview_player.toggle_pause()

    def seek_current_embedded(self, delta_seconds: float) -> None:
        if self.preview_player:
            self.seek_timeline_seconds(self.current_edited_seconds() + delta_seconds)

    def stop_embedded_preview(self, show_cover: bool = True) -> None:
        if self.preview_player:
            self.preview_player.stop()
        self._next_preview_generation()
        self._preview_segment_index = None
        self._preview_media_path = ""
        self._preview_at_timeline_end = False
        self._review_jump_source = None
        self._review_starting_source = None
        if hasattr(self, "review_video_subtitle"):
            self.review_video_subtitle.configure(text=" ")
        if show_cover and hasattr(self, "review_cover"):
            self.review_cover.place(x=0, y=0, relwidth=1, relheight=1)
            self.review_cover.lift()
        self.draw_review_playhead()

    def open_current_video(self) -> None:
        if self.current_review_video:
            self.open_path(self.current_review_media or self.current_review_video)

    def open_loaded_review_folder(self) -> None:
        if self.current_review_dir:
            self.open_path(self.current_review_dir)
            return
        configured = self.vars["review_dir"].get().strip()
        if configured:
            self.open_path(Path(configured))
            return
        messagebox.showwarning(APP_TITLE, "尚未载入审核文件夹。")

    def open_selection_folder(self) -> None:
        self.open_path(Path(self.project_dir()) / "selection")

    def open_review_folder(self) -> None:
        state = self.read_state()
        if not state or not state.get("active_review_dir"):
            messagebox.showwarning(APP_TITLE, "当前项目尚未生成 Flow 3 审核素材。")
            return
        self.load_review_directory(Path(state["active_review_dir"]))

    def open_delivery_folder(self) -> None:
        state = self.read_state()
        path = (
            Path(state["delivery_dir"])
            if state and state.get("delivery_dir")
            else Path(self.project_dir()) / "deliveries"
        )
        self.open_path(path)

    def open_path(self, path: str | Path) -> None:
        value = Path(path)
        if not value.exists():
            messagebox.showwarning(APP_TITLE, f"路径尚不存在：{value}")
            return
        if os.name == "nt":
            os.startfile(str(value))
        else:
            subprocess.Popen(["xdg-open", str(value)])

    def on_close(self) -> None:
        if self.timeline_rendering:
            messagebox.showwarning(APP_TITLE, "正在应用剪辑稿，请等待生成完成后再关闭。")
            return
        save_choice = messagebox.askyesnocancel(
            APP_TITLE,
            "退出前要保存当前页面的修改吗？\n\n"
            "是：保存并退出\n否：不保存直接退出\n取消：返回工作台",
        )
        if save_choice is None:
            return
        if save_choice and not self._save_active_context():
            return
        if (
            self.task_processes
            or self.pending_commands
            or self.monitor_process is not None
        ) and not messagebox.askyesno(
            APP_TITLE, "后台任务仍在运行。确定终止所有任务树并退出吗？"
        ):
            return
        if self.preview_player is not None:
            self.preview_player.close()
            self.preview_player = None
        self.pending_commands.clear()
        for process in list(self.task_processes.values()):
            self._kill_tree(process)
        self._clear_manual_activity_marker()
        if self.monitor_process is not None:
            self.stop_monitor(ask=False)
        self.destroy()


def main() -> int:
    configure_windows_dpi()
    instance_target = core.WORKSPACE_ROOT / "workflow-app.instance"
    try:
        with auto.queue_lock(instance_target):
            Workbench().mainloop()
    except auto.AutomationError:
        notice = Tk()
        notice.withdraw()
        messagebox.showwarning(APP_TITLE, "工作台已经在运行，请切换到已打开的窗口。")
        notice.destroy()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())










