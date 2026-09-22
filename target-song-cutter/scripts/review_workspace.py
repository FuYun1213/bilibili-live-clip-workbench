#!/usr/bin/env python3
"""Review-batch decisions and safe single-line ASS editing helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from array import array
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any
import creator_profiles
from virtuareal_glossary import compact_paid_thanks, load_replacements, normalize_text



DECISIONS_FILENAME = "review-decisions.json"
DECISION_UNDO_HISTORY_KEY = "review_undo_history"
DECISION_UNDO_HISTORY_LIMIT = 25
VALID_STATUSES = {"pending", "approved", "revise", "skipped"}
STATUS_LABELS = {
    "pending": "待审核",
    "approved": "审核通过",
    "revise": "人工重做",
    "skipped": "不通过",
}


def background_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def decisions_path(review_dir: Path) -> Path:
    return review_dir / DECISIONS_FILENAME


def load_decisions(review_dir: Path, *, create: bool = True) -> dict[str, Any]:
    path = decisions_path(review_dir)
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        value = {"schema_version": 1, "updated_at": now_iso(), "clips": {}}
    clips = value.setdefault("clips", {})
    changed = False
    discovered = {video.name for video in review_dir.glob("*.mp4")}
    for name in sorted(discovered):
        if name not in clips:
            clips[name] = {"status": "pending", "notes": ""}
            changed = True
    for name in list(clips):
        if name not in discovered:
            clips[name]["missing"] = True
        else:
            clips[name].pop("missing", None)
        status = str(clips[name].get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            clips[name]["status"] = "pending"
            changed = True
    if create and (changed or not path.is_file()):
        value["updated_at"] = now_iso()
        _atomic_json(path, value)
    return value


def set_review_suggestions(review_dir: Path, video_name: str, suggestions: str) -> dict[str, Any]:
    """Save editorial advice independently of the approval decision and its undo history."""
    video = review_dir / Path(video_name).name
    if not video.is_file():
        raise FileNotFoundError(video)
    value = load_decisions(review_dir)
    row = value["clips"][video.name]
    row["review_suggestions"] = suggestions.strip()
    row["suggestions_updated_at"] = now_iso()
    value["updated_at"] = now_iso()
    _atomic_json(decisions_path(review_dir), value)
    return value


def set_review_notes(review_dir: Path, video_name: str, notes: str) -> dict[str, Any]:
    """Save a review note without creating or changing an approval decision."""
    video = review_dir / Path(video_name).name
    if not video.is_file():
        raise FileNotFoundError(video)
    value = load_decisions(review_dir)
    row = value["clips"][video.name]
    row["notes"] = notes.strip()
    row["notes_updated_at"] = now_iso()
    value["updated_at"] = now_iso()
    _atomic_json(decisions_path(review_dir), value)
    return value


def review_revision_reason(row: dict[str, Any]) -> str:
    """Describe a revision using recorded advice, notes and replacement actions.

    Detailed editorial advice comes first, followed by nonduplicated notes.
    Legacy pointers to the advice and repeated review boilerplate are omitted.
    This is a display value: never persist a missing-reason message as a finding.
    """
    if str(row.get("status") or "").strip().lower() != "revise":
        return ""
    advice = str(row.get("review_suggestions") or "").strip()
    notes = str(row.get("notes") or "").strip()
    reason = advice or notes
    if advice and notes:
        # Existing automatic reviews repeat the same finding in both fields,
        # sometimes adding a timing failure only to notes. Keep that extra
        # evidence and any manually added explanation without echoing the
        # shared finding or a bare "see editorial advice" pointer.
        note_content = re.sub(
            r"^(?:Codex 自动审核：\s*)?(?:严重问题：\s*)?人工复核[。:：]\s*",
            "", notes,
        )
        compact_advice = re.sub(r"\s+", "", advice)
        additions = []
        for part in re.split(r"(?<=[。！？])\s*|[\r\n]+", note_content):
            part = part.strip()
            if not part or re.fullmatch(
                r"(?:严重内容问题[，,。；;\s]*)?请?(?:查看|参见|见)"
                r"(?:独立)?(?:人工)?审核建议[。.!！\s]*", part,
            ):
                continue
            comparable = re.sub(r"\s+", "", part).rstrip("。！？.!?")
            if comparable and comparable not in compact_advice:
                additions.append(part)
        if additions:
            reason += "\n补充说明：" + "\n".join(additions)
    action = str(row.get("replacement_action") or "").strip()
    if row.get("replacement_requested") and action:
        return f"原稿替换修订：{action}\n{reason or '未填写具体重做理由。'}"
    return reason or "未填写人工重做理由。请补充具体问题和需要修改的内容。"


def set_decision(
    review_dir: Path,
    video_name: str,
    status: str,
    notes: str = "",
    *,
    record_undo: bool = False,
) -> dict[str, Any]:
    status = status.strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"Unsupported review status: {status}")
    video = review_dir / Path(video_name).name
    if not video.is_file():
        raise FileNotFoundError(video)
    value = load_decisions(review_dir)
    row = dict(value["clips"].get(video.name, {}))
    previous_row = dict(row)
    action_id = uuid.uuid4().hex if record_undo else ""
    changed_at = now_iso()
    row.update(
        {
            "status": status,
            "notes": notes.strip(),
            "updated_at": changed_at,
        }
    )
    if action_id:
        row["decision_action_id"] = action_id
    value["clips"][video.name] = row
    if record_undo:
        history = list(value.get(DECISION_UNDO_HISTORY_KEY, []))
        history.append(
            {
                "action_id": action_id,
                "video_name": video.name,
                "before": previous_row,
                "after_status": status,
                "after_updated_at": changed_at,
                "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(
                    timespec="microseconds"
                ),
                "recorded_at_ns": time.time_ns(),
            }
        )
        value[DECISION_UNDO_HISTORY_KEY] = history[-DECISION_UNDO_HISTORY_LIMIT:]
    value["updated_at"] = changed_at
    _atomic_json(decisions_path(review_dir), value)
    return value


def _undoable_approval_entry(
    review_dir: Path,
    value: dict[str, Any],
    *,
    video_name: str = "",
    action_id: str = "",
) -> dict[str, Any] | None:
    clips = value.get("clips", {})
    history = value.get(DECISION_UNDO_HISTORY_KEY, [])
    if not isinstance(clips, dict) or not isinstance(history, list):
        return None
    wanted_name = Path(video_name).name if video_name else ""
    for raw_entry in reversed(history):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        name = Path(str(entry.get("video_name") or "")).name
        if not name or (wanted_name and name != wanted_name):
            continue
        if action_id and str(entry.get("action_id") or "") != action_id:
            continue
        if str(entry.get("after_status") or "") != "approved":
            continue
        before = entry.get("before")
        if not isinstance(before, dict):
            continue
        if str(before.get("status") or "pending") == "approved":
            continue
        current = clips.get(name)
        if not isinstance(current, dict):
            continue
        if str(current.get("status") or "") != "approved":
            continue
        if str(current.get("decision_action_id") or "") != str(
            entry.get("action_id") or ""
        ):
            continue
        if not (review_dir / name).is_file():
            continue
        entry["review_dir"] = str(review_dir)
        return entry
    return None


def latest_undoable_approval(search_root: Path) -> dict[str, Any] | None:
    """Return the latest still-current UI approval below ``search_root``."""
    root = search_root.expanduser().resolve()
    if not root.is_dir():
        return None
    candidates: list[dict[str, Any]] = []
    for path in root.rglob(DECISIONS_FILENAME):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            entry = _undoable_approval_entry(path.parent, value)
        except (OSError, ValueError, TypeError):
            continue
        if entry is not None:
            candidates.append(entry)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            int(item.get("recorded_at_ns") or 0),
            str(item.get("recorded_at") or ""),
        ),
    )


def undo_approval(
    review_dir: Path,
    *,
    video_name: str = "",
    action_id: str = "",
) -> dict[str, Any]:
    """Restore the exact decision row that preceded a still-current approval."""
    review_dir = review_dir.expanduser().resolve()
    value = load_decisions(review_dir, create=False)
    entry = _undoable_approval_entry(
        review_dir,
        value,
        video_name=video_name,
        action_id=action_id,
    )
    if entry is None:
        raise ValueError("上一次通过已经发生后续变化，不能直接回退")
    name = str(entry["video_name"])
    restored = dict(entry["before"])
    for key in ("review_suggestions", "suggestions_updated_at"):
        if key in value["clips"][name]:
            restored[key] = value["clips"][name][key]
    restored.setdefault("status", "pending")
    restored.setdefault("notes", "")
    restored["updated_at"] = now_iso()
    restored["approval_undone_at"] = now_iso()
    value["clips"][name] = restored
    history = value.get(DECISION_UNDO_HISTORY_KEY, [])
    value[DECISION_UNDO_HISTORY_KEY] = [
        row
        for row in history
        if not (
            isinstance(row, dict)
            and str(row.get("action_id") or "") == str(entry["action_id"])
        )
    ]
    undone = list(value.get("undone_review_actions", []))
    undone.append(
        {
            **entry,
            "undone_at": now_iso(),
            "restored_status": restored.get("status", "pending"),
        }
    )
    value["undone_review_actions"] = undone[-DECISION_UNDO_HISTORY_LIMIT:]
    value["updated_at"] = now_iso()
    _atomic_json(decisions_path(review_dir), value)
    return {
        "review_dir": str(review_dir),
        "video_name": name,
        "status": str(restored.get("status") or "pending"),
        "notes": str(restored.get("notes") or ""),
    }


def begin_approved_revision(
    review_dir: Path,
    video_name: str,
    *,
    action: str,
    job_id: str = "",
    published: bool = False,
) -> dict[str, Any]:
    """Turn an approved clip into an explicit, traceable replacement revision."""
    video = review_dir / Path(video_name).name
    if not video.is_file():
        raise FileNotFoundError(video)
    value = load_decisions(review_dir)
    row = dict(value["clips"].get(video.name, {}))
    status = str(row.get("status", "pending")).strip().lower()
    if status == "revise" and row.get("replacement_requested"):
        return value
    if status != "approved":
        raise ValueError("只有已经通过的切片可以开始原稿替换修订")
    row.update(
        {
            "status": "revise",
            "replacement_requested": True,
            "replacement_action": str(action or "内容").strip(),
            "replacement_job_id": str(job_id or "").strip(),
            "replacement_was_published": bool(published),
            "revision_started_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    value["clips"][video.name] = row
    value["updated_at"] = now_iso()
    _atomic_json(decisions_path(review_dir), value)
    return value


def complete_approved_revisions(
    review_dir: Path, video_names: list[str] | tuple[str, ...]
) -> dict[str, Any]:
    """Clear active replacement flags after the platform replacement succeeds."""
    value = load_decisions(review_dir, create=False)
    changed = False
    for raw_name in video_names:
        name = Path(raw_name).name
        if name not in value.get("clips", {}):
            continue
        row = dict(value["clips"][name])
        if not row.get("replacement_requested"):
            continue
        row["replacement_requested"] = False
        row["replacement_completed_at"] = now_iso()
        row["updated_at"] = now_iso()
        value["clips"][name] = row
        changed = True
    if changed:
        value["updated_at"] = now_iso()
        _atomic_json(decisions_path(review_dir), value)
    return value


def selected_video_names(
    review_dir: Path,
    *,
    require_human_decisions: bool,
) -> tuple[list[str], dict[str, Any]]:
    value = load_decisions(review_dir)
    clips = value.get("clips", {})
    pending = [name for name, row in clips.items() if row.get("status") == "pending"]
    revise = [name for name, row in clips.items() if row.get("status") == "revise"]
    if revise:
        raise ValueError("仍有等待人工重做的切片：" + "、".join(sorted(revise)))
    if require_human_decisions and pending:
        raise ValueError("仍有待审核切片：" + "、".join(sorted(pending)))
    selected = [
        name
        for name, row in clips.items()
        if row.get("status") == "approved"
        or (not require_human_decisions and row.get("status") == "pending")
    ]
    pending_edits = [
        name for name in selected
        if timeline_edit_path(review_dir / name).is_file()
        and _read_json(timeline_edit_path(review_dir / name)).get("status") == "pending"
    ]
    if pending_edits:
        raise ValueError("仍有未应用的时间轴剪辑：" + "、".join(sorted(pending_edits)))
    if not selected:
        raise ValueError("没有通过审核的切片；“不通过”不会被烧录或投稿")
    return sorted(selected), value


def _trash_path(path: Path) -> None:
    """Move a generated artifact to the Windows Recycle Bin when possible."""
    if not path.exists():
        return
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", wintypes.WORD),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            operation = SHFILEOPSTRUCTW()
            operation.wFunc = 3  # FO_DELETE
            operation.pFrom = str(path.resolve()) + "\0\0"
            operation.fFlags = 0x40 | 0x10 | 0x04 | 0x400  # recycle, quiet, no UI
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
            if result == 0 and not operation.fAnyOperationsAborted:
                return
        except Exception:
            pass
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_copy_row(path: Path, video_name: str) -> bool:
    if not path.is_file():
        return False
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    target = Path(video_name).name
    kept = [
        row for row in rows
        if Path(str(row.get("video", ""))).name != target
    ]
    if len(kept) == len(rows):
        return False
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)
    os.replace(temporary, path)
    return True


def discard_review_clip(review_dir: Path, video_name: str) -> list[Path]:
    """Recycle only generated artifacts for one rejected clip; never source recordings."""
    review_dir = review_dir.expanduser().resolve()
    video = (review_dir / Path(video_name).name).resolve()
    if video.parent != review_dir:
        raise ValueError("拒绝清理目标不在当前审核目录")

    candidates = {
        video,
        video.with_suffix(".ass"),
        review_proxy_video_path(video),
        review_proxy_ass_path(video),
        review_proxy_metadata_path(video),
        timeline_edit_path(video),
        timeline_backup_video_path(video),
        timeline_backup_ass_path(video),
    }
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidates.add(review_dir / f"{video.stem}-cover{suffix}")

    delivery_dir: Path | None = None
    project_root = _workflow_project_root(review_dir)
    if project_root is not None:
        project_state = _read_json(project_root / "workflow-project.json")
        delivery_value = str(project_state.get("delivery_dir") or "").strip()
        if delivery_value:
            delivery_dir = Path(delivery_value).expanduser().resolve()
            candidates.add(delivery_dir / video.name)
            candidates.add(delivery_dir / video.with_suffix(".ass").name)
            for suffix in (".jpg", ".jpeg", ".png", ".webp"):
                candidates.add(delivery_dir / f"{video.stem}-cover{suffix}")

    removed: list[Path] = []
    for path in sorted(candidates, key=lambda item: len(str(item)), reverse=True):
        if path.is_file():
            _trash_path(path)
            removed.append(path)
    _remove_copy_row(review_copy_path(review_dir), video.name)
    if delivery_dir is not None:
        _remove_copy_row(delivery_dir / "titles-and-covers.csv", video.name)
    for directory in (
        review_proxy_directory(video),
        timeline_edit_path(video).parent / "sources",
        timeline_edit_path(video).parent,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed

ASS_TIME_RE = re.compile(r"^(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})[.](?P<cs>\d{2})$")


def parse_ass_time(value: str) -> float:
    match = ASS_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"无效 ASS 时间：{value}")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("cs")) / 100
    )


def format_ass_time(seconds: float) -> str:
    centiseconds = max(0, round(float(seconds) * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def single_line_text(text: str) -> str:
    value = text.replace(r"\N", " ").replace(r"\n", " ")
    value = value.replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())


def read_dialogues(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        rows.append(
            {
                "line_number": line_number,
                "start": fields[1],
                "effect": fields[8].strip(),
                "end": fields[2],
                "style": fields[3],
                "name": fields[4],
                "text": single_line_text(fields[9]),
            }
        )
    return rows



def active_dialogue_text(
    rows: list[dict[str, Any]], edited_seconds: float
) -> str:
    """Return visible ASS text at a clip-relative time using half-open cues."""
    value = max(0.0, float(edited_seconds))
    active: list[str] = []
    for row in rows:
        try:
            start = parse_ass_time(str(row.get("start", "")))
            end = parse_ass_time(str(row.get("end", "")))
        except ValueError:
            continue
        if start - 0.001 <= value < end - 0.001:
            text = single_line_text(str(row.get("text", "")))
            if text and text not in active:
                active.append(text)
    return "\n".join(active)


def _ass_bgr_color(value: str) -> str:
    rgb = str(value).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", rgb):
        raise ValueError("说话人主题色必须是 #RRGGBB")
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def _ass_uses_radio_layout(lines: list[str]) -> bool:
    """Radio speaker clones share the document's fixed right-panel geometry."""
    return any(
        re.fullmatch(r"Title:\s*Vertical livestream radio layout", line.strip(), re.IGNORECASE)
        for line in lines
    ) or any(
        line.startswith("Style:")
        and len(fields := line.split(":", 1)[1].lstrip().split(",")) >= 22
        and fields[0].casefold().endswith("radio")
        and fields[18:21] == ["5", "720", "80"]
        for line in lines
    )


def ensure_speaker_style(
    path: Path,
    speaker_key: str,
    profile: dict[str, Any],
    *,
    base_style: str = "",
) -> str:
    """Create/update one ASS style by cloning the active subtitle style."""
    safe_key = re.sub(r"[^A-Za-z0-9_]", "_", speaker_key.strip()) or "Unknown"
    style_name = f"Speaker_{safe_key}"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    style_rows: list[tuple[int, list[str]]] = []
    for index, line in enumerate(lines):
        if not line.startswith("Style:") or "," not in line:
            continue
        fields = line.split(":", 1)[1].lstrip().split(",")
        if fields:
            style_rows.append((index, fields))
    if not style_rows:
        raise ValueError("ASS 缺少 [V4+ Styles] 样式定义")
    target = next((item for item in style_rows if item[1][0].strip() == style_name), None)
    base = next(
        (item for item in style_rows if item[1][0].strip() == base_style),
        style_rows[0],
    )
    index, fields = target if target is not None else (base[0], list(base[1]))
    if len(fields) < 6:
        raise ValueError("ASS 样式字段不完整，无法创建多人说话人样式")
    fill_color, outline_color = creator_profiles.subtitle_colors(profile)
    fields[0] = style_name
    fields[1] = str(profile.get("dialogue_font") or fields[1]).strip()
    fields[2] = "65" if _ass_uses_radio_layout(lines) else str(
        int(profile.get("dialogue_font_size", fields[2]))
    )
    fields[3] = _ass_bgr_color(fill_color)
    fields[4] = _ass_bgr_color(fill_color)
    fields[5] = _ass_bgr_color(outline_color)
    if len(fields) >= 18:
        width = float(
            profile.get(
                "subtitle_outline_width",
                creator_profiles.DEFAULT_SUBTITLE_OUTLINE_WIDTH,
            )
        )
        fields[16] = f"{width:g}"
        fields[17] = f"{max(2.0, min(3.0, width / 2)):g}"
    style_line = "Style: " + ",".join(fields)
    if target is not None:
        lines[index] = style_line
    else:
        lines.insert(base[0] + 1, style_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return style_name


def refresh_ass_subtitle_palette(
    path: Path,
    host_key: str,
    profiles: dict[str, dict[str, Any]],
) -> int:
    """Migrate every known speaker style in one ASS to the current safe palette."""
    if not path.is_file():
        return 0
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    aliases: dict[str, str] = {}
    style_aliases: dict[str, str] = {}
    for key, profile in profiles.items():
        values = {
            key,
            str(profile.get("display_name") or ""),
            str(profile.get("title_tag") or ""),
            str(profile.get("subtitle_style") or ""),
        }
        display = str(profile.get("display_name") or "")
        values.update(re.findall(r"[A-Za-z][A-Za-z0-9_]*", display))
        for value in values:
            normalized = value.strip().casefold()
            if normalized:
                aliases[normalized] = key
                style_aliases[normalized] = key

    dialogue_styles: dict[str, str] = {}
    for line in lines:
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        speaker_key = aliases.get(fields[4].strip().casefold())
        if speaker_key:
            dialogue_styles[fields[3].strip().casefold()] = speaker_key

    changed = 0
    for index, line in enumerate(lines):
        if not line.startswith("Style:") or "," not in line:
            continue
        prefix, body = line.split(":", 1)
        fields = body.lstrip().split(",")
        if len(fields) < 18:
            continue
        style_name = fields[0].strip()
        normalized = style_name.casefold()
        base = re.sub(r"overlap$", "", normalized, flags=re.IGNORECASE)
        profile_key = dialogue_styles.get(normalized)
        if base.startswith("speaker_"):
            profile_key = base[len("speaker_"):]
        if profile_key not in profiles:
            profile_key = style_aliases.get(base)
        if profile_key not in profiles and base == "regular" and host_key in profiles:
            profile_key = host_key
        if profile_key not in profiles:
            continue
        profile = profiles[profile_key]
        fill_color, outline_color = creator_profiles.subtitle_colors(profile)
        width = float(
            profile.get(
                "subtitle_outline_width",
                creator_profiles.DEFAULT_SUBTITLE_OUTLINE_WIDTH,
            )
        )
        updated = list(fields)
        updated[1] = str(profile.get("dialogue_font") or updated[1]).strip()
        # The ordinary profile size must not undo the radio panel's 65px
        # subtitle contract, including cloned Speaker_* styles.
        updated[2] = "65" if _ass_uses_radio_layout(lines) else str(
            int(profile.get("dialogue_font_size", updated[2]))
        )
        updated[3] = _ass_bgr_color(fill_color)
        updated[4] = _ass_bgr_color(fill_color)
        updated[5] = _ass_bgr_color(outline_color)
        updated[16] = f"{width:g}"
        updated[17] = f"{max(2.0, min(3.0, width / 2)):g}"
        updated_line = f"{prefix}: " + ",".join(updated)
        if updated_line != line:
            lines[index] = updated_line
            changed += 1
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return changed

def update_dialogue(
    path: Path,
    line_number: int,
    *,
    start: str,
    end: str,
    text: str,
    style: str | None = None,
    name: str | None = None,
) -> None:
    start_seconds = parse_ass_time(start)
    end_seconds = parse_ass_time(end)
    if end_seconds <= start_seconds:
        raise ValueError("字幕结束时间必须晚于开始时间")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(line_number) - 1
    if not 0 <= index < len(lines) or not lines[index].startswith("Dialogue:"):
        raise ValueError("选中的字幕行已经变化，请重新加载")
    fields = lines[index].split(",", 9)
    if len(fields) != 10:
        raise ValueError("ASS 字幕行格式损坏")
    fields[1] = format_ass_time(start_seconds)
    fields[2] = format_ass_time(end_seconds)
    if style is not None:
        fields[3] = style.strip()
    if name is not None:
        fields[4] = name.strip()
    fields[9] = single_line_text(text)
    lines[index] = ",".join(fields)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def delete_dialogue(path: Path, line_number: int) -> int | None:
    """Delete exactly one ASS Dialogue row and return the nearest remaining line."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(line_number) - 1
    if not 0 <= index < len(lines) or not lines[index].startswith("Dialogue:"):
        raise ValueError("选中的字幕行已经变化，请重新加载")
    del lines[index]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    dialogue_lines = [
        position + 1 for position, line in enumerate(lines) if line.startswith("Dialogue:")
    ]
    if not dialogue_lines:
        return None
    return min(dialogue_lines, key=lambda value: (abs(value - int(line_number)), value))


def insert_dialogue(
    path: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    text: str = "新字幕",
) -> int:
    """Insert a single-line cue in chronological order and return its line number."""
    start = max(0.0, float(start_seconds))
    end = float(end_seconds)
    if end - start < 0.04:
        raise ValueError("新增字幕至少需要持续 0.04 秒")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    dialogue_fields: list[list[str]] = []
    dialogue_indices: list[int] = []
    for index, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) == 10:
            dialogue_fields.append(fields)
            dialogue_indices.append(index)
    if dialogue_fields:
        fields = list(dialogue_fields[0])
    else:
        style = "Default"
        for line in lines:
            if line.startswith("Style:"):
                values = line.split(":", 1)[1].split(",")
                if values and values[0].strip():
                    style = values[0].strip()
                break
        fields = ["Dialogue: 0", "", "", style, "", "0", "0", "0", "", ""]
    fields[1] = format_ass_time(start)
    fields[2] = format_ass_time(end)
    fields[9] = single_line_text(text) or "新字幕"
    new_line = ",".join(fields)

    insertion = None
    for index, existing in zip(dialogue_indices, dialogue_fields):
        try:
            if parse_ass_time(existing[1]) > start:
                insertion = index
                break
        except ValueError:
            continue
    if insertion is None and dialogue_indices:
        insertion = dialogue_indices[-1] + 1
    if insertion is None:
        events_index = next(
            (index for index, line in enumerate(lines) if line.strip() == "[Events]"),
            None,
        )
        if events_index is None:
            raise ValueError("ASS 缺少 [Events] 区域")
        insertion = events_index + 1
        while insertion < len(lines) and lines[insertion].startswith("Format:"):
            insertion += 1
    lines.insert(insertion, new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return insertion + 1


def force_single_line_ass(path: Path) -> int:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    replacements = load_replacements()
    changed = 0
    for index, line in enumerate(lines):
        if line.startswith("WrapStyle:") and line.strip() != "WrapStyle: 2":
            lines[index] = "WrapStyle: 2"
            changed += 1
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        normalized = single_line_text(fields[9])
        if normalized != fields[9]:
            fields[9] = normalized
            lines[index] = ",".join(fields)
            changed += 1
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return changed




def normalize_paid_messages_ass(path: Path) -> int:
    """Apply the active correction dictionary and paid-message cleanup to an ASS."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    replacements = load_replacements()
    changed = 0
    for index, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        normalized = normalize_text(single_line_text(fields[9]), replacements)
        if normalized == fields[9]:
            continue
        fields[9] = normalized
        lines[index] = ",".join(fields)
        changed += 1
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return changed

def find_dialogue_overlaps(
    path: Path, *, tolerance_seconds: float = 0.001
) -> list[dict[str, Any]]:
    """Return chronologically adjacent ASS cues whose visible times overlap."""
    cues: list[dict[str, Any]] = []
    for row in read_dialogues(path):
        try:
            start = parse_ass_time(str(row["start"]))
            end = parse_ass_time(str(row["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        cues.append({**row, "start_seconds": start, "end_seconds": end})
    cues.sort(
        key=lambda row: (
            row["start_seconds"], row["end_seconds"], row["line_number"]
        )
    )
    overlaps: list[dict[str, Any]] = []
    for previous, current in zip(cues, cues[1:]):
        overlap = float(previous["end_seconds"]) - float(current["start_seconds"])
        if overlap > float(tolerance_seconds):
            overlaps.append(
                {
                    "previous": previous,
                    "current": current,
                    "overlap_seconds": overlap,
                }
            )
    return overlaps


def resolve_dialogue_overlaps(
    path: Path, *, min_duration: float = 0.12, gap_seconds: float = 0.02
) -> int:
    """Remove visible cue overlap while preserving text and changing as little timing as possible."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    cues: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        try:
            start = parse_ass_time(fields[1])
            end = parse_ass_time(fields[2])
        except ValueError:
            continue
        if end <= start:
            continue
        cues.append(
            {
                "index": index,
                "fields": fields,
                "start": start,
                "end": end,
                "text": single_line_text(fields[9]),
                "removed": False,
            }
        )
    cues.sort(key=lambda cue: (cue["start"], cue["end"], cue["index"]))
    repaired = 0
    kept: list[dict[str, Any]] = []
    minimum = max(0.04, float(min_duration))
    gap = max(0.0, float(gap_seconds))
    for current in cues:
        if not kept:
            kept.append(current)
            continue
        previous = kept[-1]
        if current["start"] + 0.001 >= previous["end"] + gap:
            kept.append(current)
            continue
        if current["text"] and current["text"] == previous["text"]:
            previous["end"] = max(previous["end"], current["end"])
            current["removed"] = True
            repaired += 1
            continue
        shortened_end = current["start"] - gap
        if shortened_end - previous["start"] >= minimum:
            previous["end"] = shortened_end
            repaired += 1
            kept.append(current)
            continue
        delayed_start = previous["end"] + gap
        if current["end"] - delayed_start >= minimum:
            current["start"] = delayed_start
            repaired += 1
            kept.append(current)
            continue
        union_start = previous["start"]
        union_end = max(previous["end"], current["end"])
        if union_end - union_start >= minimum * 2 + gap:
            boundary = max(
                union_start + minimum,
                min(
                    (previous["end"] + current["start"]) / 2,
                    union_end - minimum - gap,
                ),
            )
            previous["end"] = boundary
            current["start"] = boundary + gap
            repaired += 1
            kept.append(current)
            continue
        previous_duration = previous["end"] - previous["start"]
        current_duration = current["end"] - current["start"]
        if current_duration > previous_duration:
            previous["removed"] = True
            kept[-1] = current
        else:
            current["removed"] = True
        repaired += 1
    if repaired:
        removed_indices = {cue["index"] for cue in cues if cue["removed"]}
        for cue in cues:
            if cue["removed"]:
                continue
            fields = list(cue["fields"])
            fields[1] = format_ass_time(cue["start"])
            fields[2] = format_ass_time(cue["end"])
            fields[9] = cue["text"]
            lines[cue["index"]] = ",".join(fields)
        lines = [
            line for index, line in enumerate(lines) if index not in removed_indices
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return repaired

def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _project_root_for(review_dir: Path, projects_root: Path) -> Path | None:
    boundary = projects_root.resolve()
    current = review_dir.resolve()
    while current != current.parent:
        if (current / "workflow-project.json").is_file():
            return current
        if current == boundary:
            break
        current = current.parent
    return None


def review_copy_path(review_dir: Path) -> Path:
    return review_dir / "titles-and-covers.csv"


def _copy_map(review_dir: Path) -> dict[str, dict[str, str]]:
    path = review_copy_path(review_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {
                Path(str(row.get("video", ""))).name: dict(row)
                for row in csv.DictReader(handle)
                if str(row.get("video", "")).strip()
            }
    except (OSError, csv.Error):
        return {}


def review_copy_row(review_dir: Path, video_name: str) -> dict[str, str]:
    return dict(_copy_map(review_dir).get(Path(video_name).name, {}))


def update_review_copy(
    review_dir: Path,
    video_name: str,
    *,
    title: str,
    cover_text_primary: str,
    cover_text_secondary: str,
    tags: str | None = None,
    tag_evidence: str | None = None,
    description: str | None = None,
    collection: str | None = None,
    season_id: str | None = None,
    section_id: str | None = None,
    collaboration_type: str | None = None,
    participants: str | None = None,
    participant_profile_keys: str | None = None,
    speaker_evidence: str | None = None,
    guest_dialogue_verified: str | None = None,
    guest_character_images: str | None = None,
    cover_emotion: str | None = None,
    cover_emote: str | None = None,
    cover_mode: str | None = None,
    cover_source_region: str | None = None,
    cover_time_seconds: str | None = None,
    reference_image: str | None = None,
) -> dict[str, str]:
    path = review_copy_path(review_dir)
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    required = {"video", "title", "cover_text_primary", "cover_text_secondary"}
    if not required.issubset(fieldnames):
        raise ValueError("titles-and-covers.csv 缺少标题或副标题字段")
    metadata_values = (
        ("tags", tags),
        ("tag_evidence", tag_evidence),
        ("description", description),
        ("collection", collection),
        ("season_id", season_id),
        ("section_id", section_id),
        ("collaboration_type", collaboration_type),
        ("participants", participants),
        ("participant_profile_keys", participant_profile_keys),
        ("speaker_evidence", speaker_evidence),
        ("guest_dialogue_verified", guest_dialogue_verified),
        ("guest_character_images", guest_character_images),
        ("cover_emotion", cover_emotion),
        ("cover_emote", cover_emote),
        ("cover_mode", cover_mode),
        ("cover_source_region", cover_source_region),
        ("cover_time_seconds", cover_time_seconds),
        ("reference_image", reference_image),
    )
    for field, value in metadata_values:
        if value is not None and field not in fieldnames:
            fieldnames.append(field)
            for row in rows:
                row[field] = ""
    target_name = Path(video_name).name
    updated: dict[str, str] | None = None
    for row in rows:
        if Path(str(row.get("video", ""))).name != target_name:
            continue
        row["title"] = title.strip()
        row["cover_text_primary"] = cover_text_primary.strip()
        row["cover_text_secondary"] = cover_text_secondary.strip()
        if tags is not None:
            row["tags"] = tags.strip()
        if tag_evidence is not None:
            row["tag_evidence"] = tag_evidence.strip()
        if description is not None:
            row["description"] = description.strip()
        if collection is not None:
            row["collection"] = collection.strip()
        if season_id is not None:
            row["season_id"] = season_id.strip()
        if section_id is not None:
            row["section_id"] = section_id.strip()
        if collaboration_type is not None:
            row["collaboration_type"] = collaboration_type.strip()
        if participants is not None:
            row["participants"] = participants.strip()
        if participant_profile_keys is not None:
            row["participant_profile_keys"] = participant_profile_keys.strip()
        if speaker_evidence is not None:
            row["speaker_evidence"] = speaker_evidence.strip()
        if guest_dialogue_verified is not None:
            row["guest_dialogue_verified"] = guest_dialogue_verified.strip()
        if guest_character_images is not None:
            row["guest_character_images"] = guest_character_images.strip()
        if cover_emotion is not None:
            row["cover_emotion"] = cover_emotion.strip()
        if cover_emote is not None:
            row["cover_emote"] = cover_emote.strip()
        if cover_mode is not None:
            row["cover_mode"] = cover_mode.strip()
        if cover_source_region is not None:
            row["cover_source_region"] = cover_source_region.strip()
        if cover_time_seconds is not None:
            row["cover_time_seconds"] = cover_time_seconds.strip()
        if reference_image is not None:
            row["reference_image"] = reference_image.strip()
        updated = row
        break
    if updated is None:
        raise ValueError(f"titles-and-covers.csv 找不到视频：{target_name}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)
    return dict(updated)

def published_review_changes(review_dir: Path, video_name: str) -> list[str]:
    """Detect edited cover/copy fields against the currently published delivery."""
    project_root = _workflow_project_root(review_dir)
    if project_root is None:
        return []
    state = _read_json(project_root / "workflow-project.json")
    if not (
        state.get("published_at")
        or state.get("steps", {}).get("flow5", {}).get("status") == "published"
    ):
        return []
    delivery_value = str(state.get("delivery_dir") or "").strip()
    if not delivery_value:
        return []
    delivery = Path(delivery_value)
    review_row = review_copy_row(review_dir, video_name)
    delivery_row = _copy_map(delivery).get(Path(video_name).name, {})
    if not review_row or not delivery_row:
        return []
    changed: list[str] = []
    field_labels = {
        "title": "标题",
        "cover_text_primary": "封面",
        "cover_text_secondary": "封面",
        "cover_emotion": "封面",
        "cover_emote": "封面",
        "tags": "Tag",
        "tag_evidence": "Tag",
        "description": "简介",
        "collection": "合集",
        "season_id": "合集",
        "section_id": "合集",
        "cover_mode": "封面",
        "cover_source_region": "封面",
        "cover_time_seconds": "封面",
        "reference_image": "封面",
    }
    for field, label in field_labels.items():
        if str(review_row.get(field, "")).strip() != str(
            delivery_row.get(field, "")
        ).strip() and label not in changed:
            changed.append(label)
    stem = Path(video_name).stem
    review_cover = review_dir / f"{stem}-cover.jpg"
    delivery_cover = delivery / f"{stem}-cover.jpg"
    if review_cover.is_file() and delivery_cover.is_file():
        if hashlib.sha256(review_cover.read_bytes()).digest() != hashlib.sha256(
            delivery_cover.read_bytes()
        ).digest() and "封面" not in changed:
            changed.append("封面")
    return changed


def sync_review_tags_to_delivery(
    review_dir: Path,
    video_name: str,
    *,
    tags: str,
    tag_evidence: str,
    description: str | None = None,
    collection: str | None = None,
    season_id: str | None = None,
    section_id: str | None = None,
    collaboration_type: str | None = None,
    participants: str | None = None,
    participant_profile_keys: str | None = None,
    speaker_evidence: str | None = None,
    guest_dialogue_verified: str | None = None,
    guest_character_images: str | None = None,
) -> Path | None:
    """Mirror review metadata into an existing Flow 4 delivery without reburning video."""
    project_root = review_dir.expanduser().resolve()
    while project_root != project_root.parent:
        state_path = project_root / "workflow-project.json"
        if state_path.is_file():
            break
        project_root = project_root.parent
    else:
        return None
    state = _read_json(state_path)
    if state.get("published_at") or (
        state.get("steps", {}).get("flow5", {}).get("status") == "published"
    ):
        # A stale review row must never invalidate or rewrite an already
        # published delivery. Published metadata can only be changed through a
        # dedicated post-publication workflow with platform read-back.
        return None
    delivery_value = str(state.get("delivery_dir") or "").strip()
    if not delivery_value:
        return None
    delivery = Path(delivery_value)
    copy_path = delivery / "titles-and-covers.csv"
    if not copy_path.is_file():
        return None
    with copy_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    metadata_values = (
        ("tags", tags),
        ("tag_evidence", tag_evidence),
        ("description", description),
        ("collection", collection),
        ("season_id", season_id),
        ("section_id", section_id),
        ("collaboration_type", collaboration_type),
        ("participants", participants),
        ("participant_profile_keys", participant_profile_keys),
        ("speaker_evidence", speaker_evidence),
        ("guest_dialogue_verified", guest_dialogue_verified),
        ("guest_character_images", guest_character_images),
    )
    for field, value in metadata_values:
        if value is not None and field not in fieldnames:
            fieldnames.append(field)
            for row in rows:
                row[field] = ""
    target_name = Path(video_name).name
    updated = False
    for row in rows:
        if Path(str(row.get("video", ""))).name != target_name:
            continue
        row["tags"] = tags.strip()
        row["tag_evidence"] = tag_evidence.strip()
        if description is not None:
            row["description"] = description.strip()
        if collection is not None:
            row["collection"] = collection.strip()
        if season_id is not None:
            row["season_id"] = season_id.strip()
        if section_id is not None:
            row["section_id"] = section_id.strip()
        if collaboration_type is not None:
            row["collaboration_type"] = collaboration_type.strip()
        if participants is not None:
            row["participants"] = participants.strip()
        if participant_profile_keys is not None:
            row["participant_profile_keys"] = participant_profile_keys.strip()
        if speaker_evidence is not None:
            row["speaker_evidence"] = speaker_evidence.strip()
        if guest_dialogue_verified is not None:
            row["guest_dialogue_verified"] = guest_dialogue_verified.strip()
        if guest_character_images is not None:
            row["guest_character_images"] = guest_character_images.strip()
        updated = True
        break
    if not updated:
        return None
    temporary = copy_path.with_name(f".{copy_path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, copy_path)
    state.pop("publish_preview_digest", None)
    state.pop("publish_preview_at", None)
    flow5 = state.setdefault("steps", {}).setdefault("flow5", {})
    flow5.update({
        "status": "pending",
        "updated_at": now_iso(),
        "detail": "审核台修改投稿元数据后需重新生成投稿预览",
        "outputs": [],
    })
    _atomic_json(state_path, state)
    return copy_path


def _session_metadata(project_root: Path | None) -> tuple[str, str]:
    if project_root is None:
        return "", "未知场次"
    state = _read_json(project_root / "workflow-project.json")
    creator = str(state.get("config", {}).get("creator", "")).strip()
    manifest = _read_json(project_root / "continuity" / "segments.json")
    segments = manifest.get("segments") if isinstance(manifest.get("segments"), list) else []
    first = segments[0] if segments and isinstance(segments[0], dict) else {}
    started = str(first.get("started_at", "")).replace("T", " ")[:16]
    source_stem = Path(str(first.get("path", ""))).stem
    match = re.match(r"^录制-\d+-\d{8}-\d{6}-\d+-(.+)$", source_stem)
    subject = match.group(1).strip() if match else source_stem
    details = "｜".join(part for part in (started, subject) if part)
    return creator, details or project_root.name


def discover_review_items(
    projects_root: Path,
    *,
    statuses: set[str] | None = None,
    include_published_projects: bool = False,
) -> list[dict[str, Any]]:
    """Collect clips across every project while preserving each batch decision file."""
    root = projects_root.expanduser().resolve()
    accepted = statuses or {"pending", "revise"}
    items: list[dict[str, Any]] = []
    if not root.is_dir():
        return items
    decision_files: list[Path] = []
    grouped: dict[Path, list[Path]] = {}
    unowned: list[Path] = []
    for decision_file in root.rglob(DECISIONS_FILENAME):
        review_dir = decision_file.parent
        try:
            relative_parts = {
                part.casefold() for part in review_dir.relative_to(root).parts
            }
        except ValueError:
            relative_parts = set()
        if "redo-archives" in relative_parts:
            continue
        project_root = _project_root_for(review_dir, root)
        if project_root is None:
            unowned.append(decision_file)
        else:
            grouped.setdefault(project_root, []).append(decision_file)

    # Flow 3 retries leave complete older review folders on disk. One workflow
    # project has exactly one active review batch, so choose that batch here
    # instead of allowing an old pending copy to reappear after the newer copy
    # was approved. Legacy states without active_review_dir fall back to the
    # active run, Flow 3 outputs, then the newest decision file.
    decision_files.extend(unowned)
    for project_root, candidates in grouped.items():
        project_state = _read_json(project_root / "workflow-project.json")
        preferred_dirs: list[Path] = []
        active_value = str(project_state.get("active_review_dir") or "").strip()
        if active_value:
            preferred_dirs.append(Path(active_value).expanduser())
        active_run = str(project_state.get("active_run_dir") or "").strip()
        if active_run:
            preferred_dirs.append(Path(active_run).expanduser() / "export" / "clips")
        flow3_outputs = (
            project_state.get("steps", {}).get("flow3", {}).get("outputs", [])
        )
        for raw_output in reversed(flow3_outputs if isinstance(flow3_outputs, list) else []):
            output = Path(str(raw_output)).expanduser()
            preferred_dirs.append(
                output.parent if output.name == DECISIONS_FILENAME else output
            )
        selected: Path | None = None
        candidate_by_dir = {
            str(path.parent.resolve()).casefold(): path for path in candidates
        }
        for preferred in preferred_dirs:
            try:
                selected = candidate_by_dir.get(
                    str(preferred.resolve()).casefold()
                )
            except OSError:
                selected = None
            if selected is not None:
                break
        if selected is None:
            selected = max(
                candidates,
                key=lambda path: (
                    path.stat().st_mtime_ns if path.is_file() else 0,
                    str(path),
                ),
            )
        decision_files.append(selected)

    for decision_file in decision_files:
        review_dir = decision_file.parent
        decisions = load_decisions(review_dir, create=False)
        copy_rows = _copy_map(review_dir)
        project_root = _project_root_for(review_dir, root)
        published_project = False
        if project_root is not None:
            project_state = _read_json(project_root / "workflow-project.json")
            published_project = bool(project_state.get("published_at")) or (
                project_state.get("steps", {}).get("flow5", {}).get("status")
                == "published"
            )
            if published_project and not include_published_projects:
                continue
        creator, session = _session_metadata(project_root)
        for video in sorted(review_dir.glob("*.mp4")):
            decision = decisions.get("clips", {}).get(video.name, {})
            status = str(decision.get("status", "pending"))
            if status not in accepted:
                continue
            if published_project and not (
                status == "approved"
                or (
                    status == "revise"
                    and bool(decision.get("replacement_requested"))
                )
            ):
                continue
            copy_row = copy_rows.get(video.name, {})
            primary = str(copy_row.get("cover_text_primary", "")).strip()
            secondary = str(copy_row.get("cover_text_secondary", "")).strip()
            items.append(
                {
                    "review_dir": review_dir,
                    "video": video,
                    "ass": video.with_suffix(".ass"),
                    "creator": creator,
                    "session": session,
                    "project": project_root,
                    "status": status,
                    "notes": str(decision.get("notes", "")),
                    "revision_reason": review_revision_reason(decision),
                    "review_suggestions": str(decision.get("review_suggestions", "")),
                    "replacement_requested": bool(
                        decision.get("replacement_requested")
                    ),
                    "replacement_action": str(
                        decision.get("replacement_action", "")
                    ),
                    "title": str(copy_row.get("title", "")).strip() or video.stem,
                    "subtitle": "｜".join(value for value in (primary, secondary) if value),
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
                    "collaboration_type": str(
                        copy_row.get("collaboration_type", "single")
                    ).strip().lower(),
                    "participants": str(copy_row.get("participants", "")).strip(),
                    "participant_profile_keys": str(
                        copy_row.get("participant_profile_keys", "")
                    ).strip(),
                    "speaker_evidence": str(
                        copy_row.get("speaker_evidence", "")
                    ).strip(),
                    "guest_dialogue_verified": str(
                        copy_row.get("guest_dialogue_verified", "0")
                    ).strip(),
                    "guest_character_images": str(
                        copy_row.get("guest_character_images", "")
                    ).strip(),
                    "clip_id": video.stem.split("_", 1)[0],
                }
            )
    priority = {"revise": 0, "pending": 1, "approved": 2, "skipped": 3}
    items.sort(
        key=lambda item: (
            priority.get(str(item["status"]), 9),
            str(item["session"]),
            str(item["video"]),
        )
    )
    return items


def waveform_peaks(
    video: Path,
    ffmpeg: Path,
    *,
    columns: int = 900,
    sample_rate: int = 2000,
) -> dict[str, Any]:
    """Decode a low-rate mono stream and return normalized peaks for a Tk canvas."""
    if columns < 8 or sample_rate < 100:
        raise ValueError("波形采样参数无效")
    flags = background_creationflags()
    result = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "s16le", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
        timeout=180,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"FFmpeg 无法提取音轨：{video.name}")
    samples = array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return {"duration": 0.0, "peaks": [0.0] * columns}
    stride = max(1, ceil(len(samples) / columns))
    raw_peaks = [
        max((abs(value) for value in samples[start : start + stride]), default=0)
        for start in range(0, len(samples), stride)
    ][:columns]
    raw_peaks.extend([0] * (columns - len(raw_peaks)))
    ceiling = max(raw_peaks) or 1
    return {
        "duration": len(samples) / float(sample_rate),
        "peaks": [min(1.0, value / ceiling) for value in raw_peaks],
    }


WAVEFORM_CACHE_SCHEMA = 1


def cached_waveform_peaks(
    video: Path,
    ffmpeg: Path,
    *,
    cache_dir: Path,
    columns: int = 900,
    sample_rate: int = 2000,
) -> dict[str, Any]:
    """Reuse a compact disk waveform when the media signature is unchanged."""
    media = video.expanduser().resolve()
    stat = media.stat()
    signature = {
        "path": str(media),
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
        "columns": int(columns),
        "sample_rate": int(sample_rate),
    }
    digest = hashlib.sha256(
        json.dumps(signature, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    cache_path = cache_dir / f"{digest}.json"
    try:
        cached = _read_json(cache_path)
        if (
            cached.get("schema_version") == WAVEFORM_CACHE_SCHEMA
            and cached.get("signature") == signature
            and isinstance(cached.get("data", {}).get("peaks"), list)
        ):
            return dict(cached["data"])
    except (OSError, ValueError, TypeError):
        pass
    data = waveform_peaks(
        media, ffmpeg, columns=columns, sample_rate=sample_rate
    )
    _atomic_json(
        cache_path,
        {
            "schema_version": WAVEFORM_CACHE_SCHEMA,
            "signature": signature,
            "data": data,
        },
    )
    return data


TIMELINE_EDIT_DIR = ".timeline-edits"
REVIEW_PROXY_DIR = ".review-proxies"
CONTEXT_SUBTITLE_SCHEMA_VERSION = 2
CONTEXT_SUBTITLE_EFFECT = "review-context"


def review_proxy_directory(video: Path) -> Path:
    return video.parent / REVIEW_PROXY_DIR


def review_proxy_video_path(video: Path) -> Path:
    return review_proxy_directory(video) / video.name


def review_proxy_ass_path(video: Path) -> Path:
    return review_proxy_directory(video) / f"{video.stem}.ass"


def review_proxy_metadata_path(video: Path) -> Path:
    return review_proxy_directory(video) / f"{video.name}.json"


def review_proxy_metadata(video: Path) -> dict[str, Any]:
    return _read_json(review_proxy_metadata_path(video))


def review_default_source_start(video: Path) -> float:
    """Return the first original clip point, ignoring revealed pre-context."""
    metadata = review_proxy_metadata(video)
    regions = list(metadata.get("regions") or [])
    if regions:
        return float(regions[0].get("source_start", 0.0) or 0.0)
    initial = list(metadata.get("initial_segments") or [])
    if initial:
        return float(initial[0].get("source_start", 0.0) or 0.0)
    return 0.0

def has_review_proxy(video: Path) -> bool:
    return review_proxy_video_path(video).is_file() and bool(
        review_proxy_metadata(video).get("regions")
    )


def has_streaming_review(video: Path) -> bool:
    metadata = review_proxy_metadata(video)
    source = Path(str(metadata.get("source", "")))
    return bool(
        metadata.get("streaming") and metadata.get("regions") and source.is_file()
        and _streaming_export_is_current(video, metadata)
    )


def _streaming_export_inputs(video: Path) -> dict[str, Any] | None:
    """Read only the export inputs that determine this clip's source mapping."""
    project = _workflow_project_root(video.parent)
    if project is None:
        return None  # Standalone/legacy review folders have no export plan.
    plan = _edit_plan_for_review(video.parent, project)
    if plan is None:
        return None
    state = _read_json(project / "workflow-project.json")
    source = Path(str(state.get("config", {}).get("source", ""))).expanduser()
    clip_id = str(review_copy_row(video.parent, video.name).get("clip_id", "")).strip()
    clip_id = clip_id or video.stem.split("_", 1)[0]
    with plan.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle)
                if str(row.get("slice_id", "")).strip() == clip_id
                and _truthy_csv(row.get("keep"))]
    rows.sort(key=lambda row: int(float(row.get("order") or 0)))
    def identity(path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return {
        "source": identity(source), "video": identity(video),
        "ranges": [[float(row["start_seconds"]), float(row["end_seconds"])] for row in rows],
    }


def _streaming_export_is_current(video: Path, metadata: dict[str, Any]) -> bool:
    try:
        current = _streaming_export_inputs(video)
        if current is None:
            return True
        recorded = metadata.get("export_inputs")
        if recorded is not None:
            return recorded == current
        # Older caches predate export fingerprints. Preserve applied manual cuts;
        # their ranges intentionally differ from the original edit plan.
        if video.stat().st_mtime_ns > review_proxy_metadata_path(video).stat().st_mtime_ns:
            return False
        if not _same_media(Path(current["source"]["path"]), Path(str(metadata.get("source", "")))):
            return False
        if metadata.get("applied_at"):
            return True
        duration = float(metadata["source_duration"])
        ranges = []
        for start, end in current["ranges"]:
            start = max(0.0, min(duration, start))
            end = max(start, min(duration, end))
            if end - start >= 0.02:
                ranges.append((start, end))
        regions = metadata.get("regions", [])
        return len(ranges) == len(regions) and all(
            abs(start - float(region["source_start"])) < 0.001
            and abs(end - float(region["source_end"])) < 0.001
            for (start, end), region in zip(ranges, regions)
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def review_media_path(video: Path) -> Path:
    video = video.resolve()
    metadata = review_proxy_metadata(video)
    if has_streaming_review(video):
        # The exported MP4 is frame-accurate and already uses the same edited
        # clock as its ASS. Seeking directly in a multi-hour FLV can land on a
        # distant keyframe even when VLC reports the requested source time.
        # Use the exact MP4 for normal review; context outside its regions is
        # switched to the full source on demand by review_playback_media().
        return video
    return review_proxy_video_path(video) if has_review_proxy(video) else video


def _same_media(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return str(left).casefold() == str(right).casefold()


def _streaming_region_for_source(
    video: Path,
    source_start: float,
    source_end: float | None = None,
) -> dict[str, Any] | None:
    start = float(source_start)
    end = start if source_end is None else float(source_end)
    for region in review_proxy_metadata(video).get("regions") or []:
        left = float(region.get("source_start", 0.0))
        right = float(region.get("source_end", 0.0))
        if left - 0.001 <= start and end <= right + 0.001:
            return dict(region)
    return None


def review_playback_media(
    video: Path,
    source_start: float,
    source_end: float | None = None,
) -> Path:
    """Prefer the exact clip, falling back to the full source for added context."""
    video = video.resolve()
    if not has_streaming_review(video):
        return review_media_path(video)
    if _streaming_region_for_source(video, source_start, source_end) is not None:
        return video
    return Path(str(review_proxy_metadata(video)["source"])).resolve()


def source_to_review_media_seconds(
    video: Path,
    media: Path,
    source_seconds: float,
) -> float | None:
    """Map the full-source clock to the currently selected playback media."""
    video = video.resolve()
    media = media.resolve()
    source = float(source_seconds)
    metadata = review_proxy_metadata(video)
    full_source_value = str(metadata.get("source", "")).strip()
    if full_source_value and _same_media(media, Path(full_source_value)):
        return source
    if _same_media(media, video):
        if not metadata.get("streaming") or not metadata.get("regions"):
            return source
        region = _streaming_region_for_source(video, source)
        if region is None:
            return None
        return float(region.get("exact_start", 0.0)) + source - float(
            region.get("source_start", 0.0)
        )
    proxy = review_proxy_video_path(video)
    if _same_media(media, proxy):
        for region in metadata.get("regions") or []:
            left = float(region.get("source_start", 0.0))
            right = float(region.get("source_end", 0.0))
            if left - 0.001 <= source <= right + 0.001:
                return float(region.get("proxy_start", 0.0)) + source - left
        return None
    return source


def review_media_to_source_seconds(
    video: Path,
    media: Path,
    media_seconds: float,
) -> float:
    """Map VLC's current media clock back to the full-source clock."""
    video = video.resolve()
    media = media.resolve()
    value = float(media_seconds)
    metadata = review_proxy_metadata(video)
    full_source_value = str(metadata.get("source", "")).strip()
    if full_source_value and _same_media(media, Path(full_source_value)):
        return value
    if _same_media(media, video):
        regions = list(metadata.get("regions") or [])
        for index, region in enumerate(regions):
            left = float(region.get("exact_start", 0.0))
            right = float(region.get("exact_end", 0.0))
            is_last = index == len(regions) - 1
            if left - 0.001 <= value and (value < right - 0.001 or is_last):
                return float(region.get("source_start", 0.0)) + max(
                    0.0, min(right - left, value - left)
                )
    proxy = review_proxy_video_path(video)
    if _same_media(media, proxy):
        regions = list(metadata.get("regions") or [])
        for index, region in enumerate(regions):
            left = float(region.get("proxy_start", 0.0))
            right = float(region.get("proxy_end", 0.0))
            is_last = index == len(regions) - 1
            if left - 0.001 <= value and (value < right - 0.001 or is_last):
                return float(region.get("source_start", 0.0)) + max(
                    0.0, min(right - left, value - left)
                )
    return value


def review_ass_path(video: Path) -> Path | None:
    proxy = review_proxy_ass_path(video)
    if (has_streaming_review(video) or has_review_proxy(video)) and proxy.is_file():
        return proxy
    exact = video.with_suffix(".ass")
    return exact if exact.is_file() else None


def review_waveform_spec(video: Path) -> dict[str, Any]:
    """Choose the smallest accurate audio source for an editable review timeline."""
    metadata = review_proxy_metadata(video)
    regions = list(metadata.get("regions") or [])
    if has_streaming_review(video):
        return {
            "media": video.resolve(),
            "coordinate_space": "exact_regions",
            "regions": regions,
            "source_duration": float(metadata.get("source_duration", 0.0) or 0.0),
        }
    if has_review_proxy(video):
        return {
            "media": review_proxy_video_path(video).resolve(),
            "coordinate_space": "proxy_regions",
            "regions": regions,
            "source_duration": float(metadata.get("source_duration", 0.0) or 0.0),
        }
    return {
        "media": video.resolve(),
        "coordinate_space": "source",
        "regions": [],
        "source_duration": 0.0,
    }


def waveform_sample_seconds(data: dict[str, Any], source_seconds: float) -> float | None:
    """Map a source-timeline point into the compact waveform media."""
    coordinate_space = str(data.get("coordinate_space", "source"))
    source = float(source_seconds)
    if coordinate_space == "source":
        return source
    target_key = "exact_start" if coordinate_space == "exact_regions" else "proxy_start"
    for region in data.get("regions") or []:
        start = float(region.get("source_start", 0.0))
        end = float(region.get("source_end", 0.0))
        if start - 0.001 <= source <= end + 0.001:
            return float(region.get(target_key, 0.0)) + max(0.0, source - start)
    return None


def _workflow_project_root(review_dir: Path) -> Path | None:
    current = review_dir.resolve()
    while current != current.parent:
        if (current / "workflow-project.json").is_file():
            return current
        current = current.parent
    return None


def supports_review_proxy(video: Path) -> bool:
    project_root = _workflow_project_root(video.parent)
    if project_root is None:
        return False
    state = _read_json(project_root / "workflow-project.json")
    return not bool(state.get("radio_layout")) and _edit_plan_for_review(video.parent, project_root) is not None


def _edit_plan_for_review(review_dir: Path, project_root: Path) -> Path | None:
    current = review_dir.resolve()
    boundary = project_root.resolve()
    while True:
        candidate = current / "edit-plan.csv"
        if candidate.is_file():
            return candidate
        if current == boundary or current == current.parent:
            return None
        current = current.parent


def _media_duration_with_ffmpeg(media: Path, ffmpeg: Path) -> float:
    flags = background_creationflags()
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(media)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=flags,
        timeout=60,
    )
    detail = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", detail)
    if not match:
        raise RuntimeError(f"FFmpeg 无法读取素材时长：{media.name}")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _truthy_csv(value: Any) -> bool:
    return str(value or "1").strip().lower() not in {"0", "false", "no", "off"}


def build_review_proxy_spec(video: Path, padding_seconds: float, ffmpeg: Path) -> dict[str, Any]:
    """Describe a padded review proxy while retaining exact-output time mappings."""
    project_root = _workflow_project_root(video.parent)
    if project_root is None:
        raise ValueError(f"无法定位项目根目录：{video.parent}")
    state = _read_json(project_root / "workflow-project.json")
    source_value = str(state.get("config", {}).get("source", "")).strip()
    source = Path(source_value).expanduser() if source_value else Path()
    if not source_value or not source.is_file():
        raise FileNotFoundError(f"项目源视频不存在：{source_value or '未配置'}")
    edit_plan = _edit_plan_for_review(video.parent, project_root)
    if edit_plan is None:
        raise FileNotFoundError(f"审核批次找不到 edit-plan.csv：{video.parent}")
    copy_row = review_copy_row(video.parent, video.name)
    clip_id = str(copy_row.get("clip_id", "")).strip() or video.stem.split("_", 1)[0]
    with edit_plan.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            dict(row) for row in csv.DictReader(handle)
            if str(row.get("slice_id", "")).strip() == clip_id and _truthy_csv(row.get("keep"))
        ]
    if not rows:
        raise ValueError(f"edit-plan.csv 找不到片段 {clip_id}")
    rows.sort(key=lambda row: int(float(str(row.get("order", "0") or "0"))))
    source_duration = _media_duration_with_ffmpeg(source, ffmpeg)
    padding = max(0.0, float(padding_seconds))
    exact_parts: list[dict[str, float]] = []
    exact_cursor = 0.0
    for row in rows:
        start = max(0.0, min(source_duration, float(row["start_seconds"])))
        end = max(start, min(source_duration, float(row["end_seconds"])))
        if end - start < 0.02:
            continue
        exact_parts.append({
            "source_start": start,
            "source_end": end,
            "exact_start": exact_cursor,
            "exact_end": exact_cursor + end - start,
        })
        exact_cursor += end - start
    if not exact_parts:
        raise ValueError(f"片段 {clip_id} 没有有效时间范围")

    blocks: list[dict[str, Any]] = []
    for part in exact_parts:
        window_start = max(0.0, part["source_start"] - padding)
        window_end = min(source_duration, part["source_end"] + padding)
        if (
            blocks
            and part["source_start"] >= blocks[-1]["last_original_start"]
            and window_start <= blocks[-1]["source_end"] + 0.001
        ):
            block = blocks[-1]
            block["source_end"] = max(float(block["source_end"]), window_end)
            block["last_original_start"] = part["source_start"]
            block["parts"].append(part)
        else:
            blocks.append({
                "source_start": window_start,
                "source_end": window_end,
                "last_original_start": part["source_start"],
                "parts": [part],
            })

    proxy_cursor = 0.0
    regions: list[dict[str, float]] = []
    initial_segments: list[dict[str, Any]] = []
    padding_label = "5分钟" if padding >= 299 else "1分钟"
    for block_index, block in enumerate(blocks, 1):
        block_start = float(block["source_start"])
        block_end = float(block["source_end"])
        block_proxy_start = proxy_cursor
        block_regions: list[dict[str, float]] = []
        for part in block["parts"]:
            proxy_start = block_proxy_start + float(part["source_start"]) - block_start
            region = {
                "exact_start": float(part["exact_start"]),
                "exact_end": float(part["exact_end"]),
                "proxy_start": proxy_start,
                "proxy_end": proxy_start + float(part["source_end"]) - float(part["source_start"]),
                "source_start": float(part["source_start"]),
                "source_end": float(part["source_end"]),
            }
            regions.append(region)
            block_regions.append(region)
        boundaries = {block_proxy_start, block_proxy_start + block_end - block_start}
        for region in block_regions:
            boundaries.update({region["proxy_start"], region["proxy_end"]})
        points = sorted(boundaries)
        first_original = min(region["proxy_start"] for region in block_regions)
        last_original = max(region["proxy_end"] for region in block_regions)
        for start, end in zip(points, points[1:]):
            if end - start < 0.02:
                continue
            midpoint = (start + end) / 2
            if any(region["proxy_start"] <= midpoint <= region["proxy_end"] for region in block_regions):
                kind, label = "original", "原片"
            elif midpoint < first_original:
                kind, label = "pre", f"前置{padding_label}"
            elif midpoint > last_original:
                kind, label = "post", f"后置{padding_label}"
            else:
                kind, label = "context", "段间上下文"
            if len(blocks) > 1:
                label = f"{label} {block_index}"
            initial_segments.append({
                "source_start": start,
                "source_end": end,
                "kind": kind,
                "label": label,
            })
        proxy_cursor += block_end - block_start

    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "video": video.name,
        "clip_id": clip_id,
        "padding_seconds": padding,
        "source": str(source.resolve()),
        "source_duration": source_duration,
        "exact_duration": exact_cursor,
        "proxy_duration": proxy_cursor,
        "render_segments": [
            {"source_start": float(block["source_start"]), "source_end": float(block["source_end"])}
            for block in blocks
        ],
        "regions": regions,
        "initial_segments": initial_segments,
    }


def _transform_ass_regions(
    source_ass: Path,
    destination_ass: Path,
    regions: list[dict[str, Any]],
    *,
    forward: bool,
) -> None:
    lines = source_ass.read_text(encoding="utf-8-sig").splitlines()
    output: list[str] = []
    for line in lines:
        if not line.startswith("Dialogue:"):
            output.append(line)
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        cue_start = parse_ass_time(fields[1])
        cue_end = parse_ass_time(fields[2])
        mapped_lines: list[tuple[float, str]] = []
        for region in regions:
            input_start = float(region["exact_start"] if forward else region["proxy_start"])
            input_end = float(region["exact_end"] if forward else region["proxy_end"])
            output_start = float(region["proxy_start"] if forward else region["exact_start"])
            overlap_start = max(cue_start, input_start)
            overlap_end = min(cue_end, input_end)
            if overlap_end - overlap_start < 0.02:
                continue
            mapped_start = output_start + overlap_start - input_start
            mapped_end = output_start + overlap_end - input_start
            mapped = list(fields)
            mapped[1] = format_ass_time(mapped_start)
            mapped[2] = format_ass_time(mapped_end)
            mapped[9] = single_line_text(mapped[9])
            mapped_lines.append((mapped_start, ",".join(mapped)))
        output.extend(value for _start, value in sorted(mapped_lines))
    destination_ass.parent.mkdir(parents=True, exist_ok=True)
    destination_ass.write_text("\n".join(output) + "\n", encoding="utf-8-sig")


def review_proxy_has_context_subtitles(video: Path) -> bool:
    details = review_proxy_metadata(video).get("context_subtitles", {})
    return bool(
        isinstance(details, dict)
        and int(details.get("version", 0) or 0) >= CONTEXT_SUBTITLE_SCHEMA_VERSION
        and review_proxy_ass_path(video).is_file()
    )


def _context_transcript_path(project_root: Path) -> Path | None:
    candidate = project_root / "analysis" / "transcript" / "transcript.csv"
    if candidate.is_file():
        from asr_hotword_guard import require_clean_artifact
        require_clean_artifact(candidate)
        return candidate
    return None


def _subtract_intervals(
    start: float,
    end: float,
    blockers: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    pieces = [(start, end)]
    for blocked_start, blocked_end in blockers:
        updated: list[tuple[float, float]] = []
        for left, right in pieces:
            if blocked_end <= left or blocked_start >= right:
                updated.append((left, right))
                continue
            if blocked_start > left:
                updated.append((left, min(right, blocked_start)))
            if blocked_end < right:
                updated.append((max(left, blocked_end), right))
        pieces = updated
    return [(left, right) for left, right in pieces if right - left >= 0.04]


def _context_proxy_spans(
    spec: dict[str, Any],
    source_start: float,
    source_end: float,
) -> list[tuple[float, float]]:
    blockers = sorted(
        (float(region["source_start"]), float(region["source_end"]))
        for region in spec.get("regions", [])
    )
    proxy_cursor = 0.0
    mapped: list[tuple[float, float]] = []
    for segment in spec.get("render_segments", []):
        block_start = float(segment["source_start"])
        block_end = float(segment["source_end"])
        overlap_start = max(source_start, block_start)
        overlap_end = min(source_end, block_end)
        if overlap_end - overlap_start >= 0.04:
            for left, right in _subtract_intervals(
                overlap_start, overlap_end, blockers
            ):
                mapped.append(
                    (
                        proxy_cursor + left - block_start,
                        proxy_cursor + right - block_start,
                    )
                )
        proxy_cursor += max(0.0, block_end - block_start)
    return mapped


def _proxy_dialogue_template(lines: list[str]) -> list[str]:
    for line in lines:
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) == 10 and fields[8].strip() != CONTEXT_SUBTITLE_EFFECT:
            return fields
    style = "Default"
    for line in lines:
        if line.startswith("Style:"):
            values = line.split(":", 1)[1].split(",")
            if values and values[0].strip():
                style = values[0].strip()
                break
    return ["Dialogue: 0", "", "", style, "", "0", "0", "0", "", ""]


def _merge_context_dialogues(proxy_ass: Path, context_lines: list[str]) -> None:
    lines = proxy_ass.read_text(encoding="utf-8-sig").splitlines()
    events_start = next(
        (index for index, line in enumerate(lines) if line.strip() == "[Events]"),
        None,
    )
    if events_start is None:
        raise ValueError(f"ASS 缺少 [Events] 区域：{proxy_ass}")
    events_end = next(
        (
            index
            for index in range(events_start + 1, len(lines))
            if lines[index].startswith("[") and lines[index].endswith("]")
        ),
        len(lines),
    )
    static_lines: list[str] = []
    dialogue_lines: list[str] = []
    for line in lines[events_start + 1 : events_end]:
        if not line.startswith("Dialogue:"):
            static_lines.append(line)
            continue
        fields = line.split(",", 9)
        if len(fields) == 10 and fields[8].strip() == CONTEXT_SUBTITLE_EFFECT:
            continue
        dialogue_lines.append(line)
    dialogue_lines.extend(context_lines)

    def sort_key(line: str) -> tuple[float, float, str]:
        fields = line.split(",", 9)
        try:
            return parse_ass_time(fields[1]), parse_ass_time(fields[2]), fields[8]
        except (IndexError, ValueError):
            return float("inf"), float("inf"), ""

    dialogue_lines.sort(key=sort_key)
    updated = (
        lines[: events_start + 1]
        + static_lines
        + dialogue_lines
        + lines[events_end:]
    )
    proxy_ass.write_text("\n".join(updated) + "\n", encoding="utf-8-sig")


def _bounded_context_span(
    start: float, end: float, text: str
) -> tuple[float, float]:
    """Keep coarse-ASR review context readable and aligned to the spoken tail."""
    duration = max(0.0, float(end) - float(start))
    glyph_count = len(re.sub(r"\s+", "", text))
    expected = min(5.0, max(1.2, glyph_count / 4.5 + 0.8))
    if duration > 5.0:
        return max(float(start), float(end) - expected), float(end)
    return float(start), float(end)

def refresh_review_proxy_ass(
    video: Path,
    spec: dict[str, Any] | None = None,
    *,
    preserve_existing: bool = True,
) -> dict[str, Any]:
    """Add full-session subtitles to padded areas while exact-clip cues win."""
    video = video.resolve()
    metadata = dict(spec or review_proxy_metadata(video))
    if not metadata.get("regions"):
        raise ValueError(f"审核代理缺少时间轴映射：{video.name}")
    exact_ass = video.with_suffix(".ass")
    proxy_ass = review_proxy_ass_path(video)
    if not exact_ass.is_file():
        raise FileNotFoundError(f"精确切片字幕不存在：{exact_ass}")
    if not preserve_existing or not proxy_ass.is_file():
        _transform_ass_regions(
            exact_ass, proxy_ass, metadata["regions"], forward=True
        )

    base_lines = proxy_ass.read_text(encoding="utf-8-sig").splitlines()
    template = _proxy_dialogue_template(base_lines)
    project_root = _workflow_project_root(video.parent)
    transcript = _context_transcript_path(project_root) if project_root else None
    context_lines: list[str] = []
    if transcript is not None:
        with transcript.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            text = compact_paid_thanks(single_line_text(str(row.get("text", ""))))
            text = text.replace("{", "｛").replace("}", "｝")
            if not text:
                continue
            try:
                source_start = float(row["start_seconds"])
                source_end = float(row["end_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            for proxy_start, proxy_end in _context_proxy_spans(
                metadata, source_start, source_end
            ):
                proxy_start, proxy_end = _bounded_context_span(
                    proxy_start, proxy_end, text
                )
                fields = list(template)
                fields[1] = format_ass_time(proxy_start)
                fields[2] = format_ass_time(proxy_end)
                fields[8] = CONTEXT_SUBTITLE_EFFECT
                fields[9] = text
                context_lines.append(",".join(fields))
    _merge_context_dialogues(proxy_ass, context_lines)
    force_single_line_ass(proxy_ass)
    resolve_dialogue_overlaps(proxy_ass)
    metadata["context_subtitles"] = {
        "version": CONTEXT_SUBTITLE_SCHEMA_VERSION,
        "source": str(transcript.resolve()) if transcript else "",
        "cue_count": len(context_lines),
        "exact_clip_subtitles_preserved": True,
    }
    _atomic_json(review_proxy_metadata_path(video), metadata)
    return metadata

def generate_review_proxy(video: Path, padding_seconds: float, ffmpeg: Path) -> dict[str, Any]:
    """Create a hidden padded review asset without modifying the exact clip."""
    video = video.resolve()
    existing = review_proxy_metadata(video)
    if (
        review_proxy_video_path(video).is_file()
        and float(existing.get("padding_seconds", -1)) == float(padding_seconds)
        and existing.get("regions")
    ):
        if review_proxy_has_context_subtitles(video):
            return existing
        return refresh_review_proxy_ass(video, existing, preserve_existing=True)
    if existing.get("regions"):
        clear_timeline_edit(video)
    spec = build_review_proxy_spec(video, padding_seconds, ffmpeg)
    directory = review_proxy_directory(video)
    directory.mkdir(parents=True, exist_ok=True)
    destination = review_proxy_video_path(video)
    temporary = directory / f".{video.stem}.{os.getpid()}.tmp.mp4"
    if temporary.is_file():
        temporary.unlink()
    render_review_proxy(Path(spec["source"]), temporary, spec["render_segments"], ffmpeg)
    os.replace(temporary, destination)
    return refresh_review_proxy_ass(video, spec, preserve_existing=False)


def _legacy_proxy_segments_to_source(
    segments: list[dict[str, Any]],
    render_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate old padded-proxy coordinates onto the full source timeline."""
    mappings: list[dict[str, float]] = []
    proxy_cursor = 0.0
    for block in render_segments:
        source_start = float(block["source_start"])
        source_end = float(block["source_end"])
        length = max(0.0, source_end - source_start)
        if length >= 0.02:
            mappings.append(
                {
                    "proxy_start": proxy_cursor,
                    "proxy_end": proxy_cursor + length,
                    "source_start": source_start,
                }
            )
            proxy_cursor += length
    converted: list[dict[str, Any]] = []
    for segment in segments:
        old_start = float(segment.get("source_start", 0.0))
        old_end = float(segment.get("source_end", 0.0))
        for mapping in mappings:
            overlap_start = max(old_start, mapping["proxy_start"])
            overlap_end = min(old_end, mapping["proxy_end"])
            if overlap_end - overlap_start < 0.02:
                continue
            item: dict[str, Any] = {
                "source_start": mapping["source_start"]
                + overlap_start - mapping["proxy_start"],
                "source_end": mapping["source_start"]
                + overlap_end - mapping["proxy_start"],
            }
            for key in ("kind", "label"):
                if str(segment.get(key, "")).strip():
                    item[key] = str(segment[key]).strip()
            converted.append(item)
    return converted


def prepare_streaming_review(video: Path, ffmpeg: Path) -> dict[str, Any]:
    """Prepare a zero-render review timeline backed by the full source media."""
    video = video.resolve()
    existing = review_proxy_metadata(video)
    proxy_ass = review_proxy_ass_path(video)
    if (
        existing.get("streaming")
        and existing.get("regions")
        and Path(str(existing.get("source", ""))).is_file()
        and proxy_ass.is_file()
        and _streaming_export_is_current(video, existing)
    ):
        return existing

    stale_streaming = bool(existing.get("streaming") and not _streaming_export_is_current(video, existing))
    edit_path = timeline_edit_path(video)
    if stale_streaming and edit_path.is_file():
        raise ValueError("切片已重新导出，但仍有未应用的审核剪辑；原字幕和剪辑稿已保留，请先处理该剪辑稿")
    legacy_state = (
        _read_json(edit_path)
        if edit_path.is_file() and not existing.get("streaming")
        else {}
    )
    legacy_ass_bytes = (
        proxy_ass.read_bytes()
        if legacy_state and proxy_ass.is_file()
        else None
    )
    if legacy_state and legacy_ass_bytes is None:
        raise ValueError("旧版剪辑缺少字幕工作副本，已停止迁移以避免丢失修改")
    if existing.get("regions") and proxy_ass.is_file() and not legacy_state and not stale_streaming:
        sync_review_proxy_ass_to_original(video)

    spec = build_review_proxy_spec(video, 0.0, ffmpeg)
    converted_segments: list[dict[str, Any]] = []
    if legacy_state:
        converted_segments = _legacy_proxy_segments_to_source(
            list(legacy_state.get("segments") or []),
            list(existing.get("render_segments") or []),
        )
        if not converted_segments:
            raise ValueError("旧版未应用剪辑无法换算到源录播时间轴，原数据未改动")

    exact_ass = video.with_suffix(".ass")
    if not exact_ass.is_file():
        raise FileNotFoundError(f"精确切片字幕不存在：{exact_ass}")
    if stale_streaming:
        backup_dir = review_proxy_directory(video) / "stale-exports" / uuid.uuid4().hex
        backup_dir.mkdir(parents=True)
        for old_path in (proxy_ass, review_proxy_metadata_path(video)):
            if old_path.is_file():
                shutil.copy2(old_path, backup_dir / old_path.name)
    clear_review_proxy(video)
    spec["export_inputs"] = _streaming_export_inputs(video)
    spec["schema_version"] = 2
    spec["streaming"] = True
    spec["streaming_version"] = 1
    spec["padding_seconds"] = 0.0
    spec["initial_segments"] = [
        {
            "source_start": float(region["source_start"]),
            "source_end": float(region["source_end"]),
            "kind": "original",
            "label": "原片",
        }
        for region in spec["regions"]
    ]
    spec["revealed_context_ranges"] = []
    proxy_ass.parent.mkdir(parents=True, exist_ok=True)
    exact_ass = video.with_suffix(".ass")
    if not exact_ass.is_file():
        raise FileNotFoundError(f"精确切片字幕不存在：{exact_ass}")
    if legacy_ass_bytes is None:
        shutil.copy2(exact_ass, proxy_ass)
    else:
        proxy_ass.write_bytes(legacy_ass_bytes)
        legacy_state["segments"] = converted_segments
        legacy_state["source_duration"] = float(spec["source_duration"])
        legacy_state["streaming"] = True
        legacy_state["migrated_from_padded_proxy_at"] = now_iso()
        save_timeline_edit(video, legacy_state)
    _atomic_json(review_proxy_metadata_path(video), spec)
    return spec


def _shift_ass_from(path: Path, edited_seconds: float, delta_seconds: float) -> None:
    """Shift cues at an insertion point while preserving user-edited text."""
    if delta_seconds <= 0.0:
        return
    boundary = float(edited_seconds)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            continue
        start = parse_ass_time(fields[1])
        end = parse_ass_time(fields[2])
        if start >= boundary - 0.001:
            start += delta_seconds
            end += delta_seconds
        elif end > boundary:
            end += delta_seconds
        fields[1] = format_ass_time(start)
        fields[2] = format_ass_time(end)
        lines[index] = ",".join(fields)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def _streaming_context_lines(
    video: Path,
    metadata: dict[str, Any],
    source_start: float,
    source_end: float,
    edited_start: float,
    *,
    exclude_initial_regions: bool = True,
) -> list[str]:
    project_root = _workflow_project_root(video.parent)
    transcript = _context_transcript_path(project_root) if project_root else None
    if transcript is None:
        return []
    proxy_ass = review_proxy_ass_path(video)
    template = _proxy_dialogue_template(
        proxy_ass.read_text(encoding="utf-8-sig").splitlines()
    )
    blockers = (
        sorted(
            (float(region["source_start"]), float(region["source_end"]))
            for region in metadata.get("regions", [])
        )
        if exclude_initial_regions
        else []
    )
    result: list[str] = []
    with transcript.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        text = compact_paid_thanks(single_line_text(str(row.get("text", ""))))
        text = text.replace("{", "｛").replace("}", "｝")
        if not text:
            continue
        try:
            row_start = max(source_start, float(row["start_seconds"]))
            row_end = min(source_end, float(row["end_seconds"]))
        except (KeyError, TypeError, ValueError):
            continue
        if row_end - row_start < 0.04:
            continue
        for left, right in _subtract_intervals(row_start, row_end, blockers):
            cue_start = edited_start + left - source_start
            cue_end = edited_start + right - source_start
            cue_start, cue_end = _bounded_context_span(cue_start, cue_end, text)
            fields = list(template)
            fields[1] = format_ass_time(cue_start)
            fields[2] = format_ass_time(cue_end)
            fields[8] = CONTEXT_SUBTITLE_EFFECT
            fields[9] = text
            result.append(",".join(fields))
    return result


def restorable_timeline_gap(
    segments: list[dict[str, Any]],
    edited_seconds: float,
    *,
    tolerance_seconds: float = 0.25,
) -> dict[str, float | int] | None:
    """Return the closest forward source gap at an edited segment boundary."""
    spans = timeline_segment_spans(segments)
    candidates: list[tuple[float, dict[str, float | int]]] = []
    for left, right in zip(spans, spans[1:]):
        source_start = float(left["source_end"])
        source_end = float(right["source_start"])
        if source_end - source_start < 0.04:
            continue
        boundary = float(left["edited_end"])
        distance = abs(float(edited_seconds) - boundary)
        if distance <= max(0.01, float(tolerance_seconds)):
            candidates.append(
                (
                    distance,
                    {
                        "left_index": int(left["index"]),
                        "edited_boundary": boundary,
                        "source_start": source_start,
                        "source_end": source_end,
                        "duration": source_end - source_start,
                    },
                )
            )
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def restore_streaming_gap(
    video: Path,
    state: dict[str, Any],
    left_index: int,
) -> dict[str, float | int]:
    """Restore one removed source gap and its transcript into a streaming review."""
    metadata = review_proxy_metadata(video)
    if not metadata.get("streaming"):
        raise ValueError("当前素材没有整场源时间轴，无法恢复中间原片")
    source_duration = max(0.0, float(metadata.get("source_duration", 0.0)))
    segments = normalized_timeline_segments(
        list(state.get("segments") or []), source_duration
    )
    index = int(left_index)
    if not 0 <= index < len(segments) - 1:
        raise ValueError("右键位置不是两个视频片段的交界")
    left = float(segments[index]["source_end"])
    right = float(segments[index + 1]["source_start"])
    if right - left < 0.04:
        raise ValueError("这两段之间没有可恢复的原片")

    insertion_edited = sum(
        float(item["source_end"]) - float(item["source_start"])
        for item in segments[: index + 1]
    )
    added = right - left
    proxy_ass = review_proxy_ass_path(video)
    if not proxy_ass.is_file():
        raise FileNotFoundError(f"审核字幕工作副本不存在：{proxy_ass}")
    _shift_ass_from(proxy_ass, insertion_edited, added)
    for region in metadata.get("regions", []):
        if float(region.get("proxy_start", 0.0)) >= insertion_edited - 0.001:
            region["proxy_start"] = float(region["proxy_start"]) + added
            region["proxy_end"] = float(region["proxy_end"]) + added
    segments.insert(
        index + 1,
        {
            "source_start": left,
            "source_end": right,
            "kind": "restored-middle",
            "label": f"恢复中间 {added:g} 秒",
        },
    )
    _add_streaming_context_lines(
        proxy_ass,
        _streaming_context_lines(
            video,
            metadata,
            left,
            right,
            insertion_edited,
            exclude_initial_regions=False,
        ),
    )
    force_single_line_ass(proxy_ass)
    metadata.setdefault("revealed_context_ranges", []).append(
        {
            "source_start": left,
            "source_end": right,
            "kind": "restored-middle",
            "revealed_at": now_iso(),
        }
    )
    _atomic_json(review_proxy_metadata_path(video), metadata)
    state["segments"] = segments
    state["source_duration"] = source_duration
    state["streaming"] = True
    state["revealed_context"] = True
    state["updated_at"] = now_iso()
    return {
        "left_index": index,
        "inserted_index": index + 1,
        "edited_boundary": insertion_edited,
        "source_start": left,
        "source_end": right,
        "duration": added,
    }


def _add_streaming_context_lines(path: Path, new_lines: list[str]) -> None:
    if not new_lines:
        return
    existing = path.read_text(encoding="utf-8-sig").splitlines()
    context = []
    for line in existing:
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) == 10 and fields[8].strip() == CONTEXT_SUBTITLE_EFFECT:
            context.append(line)
    merged = list(dict.fromkeys([*context, *new_lines]))
    _merge_context_dialogues(path, merged)


def reveal_streaming_context(
    video: Path,
    state: dict[str, Any],
    source_seconds: float,
    *,
    chunk_seconds: float = 8.0,
    direction: str = "",
) -> bool:
    """Add one explicit before/after chunk to the editable timeline."""
    metadata = review_proxy_metadata(video)
    if not metadata.get("streaming"):
        return False
    source_duration = max(0.0, float(metadata.get("source_duration", 0.0)))
    segments = normalized_timeline_segments(
        list(state.get("segments", [])), source_duration
    )
    if not segments or direction not in {"before", "after"}:
        return False
    chunk = max(1.0, float(chunk_seconds))
    insert_at = 0
    if direction == "before":
        right = float(segments[0]["source_start"])
        left = max(0.0, right - chunk)
        kind, label = "manual-pre-context", f"往前增加 {chunk:g} 秒"
    else:
        left = float(segments[-1]["source_end"])
        right = min(source_duration, left + chunk)
        insert_at = len(segments)
        kind, label = "manual-post-context", f"往后增加 {chunk:g} 秒"
    if right - left < 0.04:
        return False

    insertion_edited = sum(
        float(item["source_end"]) - float(item["source_start"])
        for item in segments[:insert_at]
    )
    added = right - left
    proxy_ass = review_proxy_ass_path(video)
    if insert_at < len(segments):
        _shift_ass_from(proxy_ass, insertion_edited, added)
        for region in metadata.get("regions", []):
            if float(region["proxy_start"]) >= insertion_edited - 0.001:
                region["proxy_start"] = float(region["proxy_start"]) + added
                region["proxy_end"] = float(region["proxy_end"]) + added
    segments.insert(
        insert_at,
        {
            "source_start": left,
            "source_end": right,
            "kind": kind,
            "label": label,
        },
    )
    _add_streaming_context_lines(
        proxy_ass,
        _streaming_context_lines(video, metadata, left, right, insertion_edited),
    )
    force_single_line_ass(proxy_ass)

    metadata.setdefault("revealed_context_ranges", []).append(
        {"source_start": left, "source_end": right, "revealed_at": now_iso()}
    )
    _atomic_json(review_proxy_metadata_path(video), metadata)
    state["segments"] = segments
    state["source_duration"] = source_duration
    state["streaming"] = True
    state["revealed_context"] = True
    state["updated_at"] = now_iso()
    return True


AUTOMATIC_CONTEXT_KINDS = {
    "pre", "post", "context", "pre-context", "post-context"
}


def remove_automatic_streaming_context(video: Path, state: dict[str, Any]) -> int:
    """Remove legacy auto-expanded ranges while preserving explicit additions."""
    segments = list(state.get("segments") or [])
    remove_indices = [
        index for index, segment in enumerate(segments)
        if str(segment.get("kind") or "") in AUTOMATIC_CONTEXT_KINDS
    ]
    if not remove_indices or len(remove_indices) >= len(segments):
        return 0
    remove_set = set(remove_indices)
    spans = timeline_segment_spans(segments)
    proxy_ass = review_proxy_ass_path(video)
    metadata = review_proxy_metadata(video)
    regions = list(metadata.get("regions") or [])
    for index in reversed(remove_indices):
        span = spans[index]
        left = float(span["edited_start"])
        right = float(span["edited_end"])
        length = right - left
        if proxy_ass.is_file() and length > 0.0:
            delete_ass_interval(proxy_ass, left, right)
        for region in regions:
            if float(region.get("proxy_start", 0.0)) >= right - 0.001:
                region["proxy_start"] = float(region["proxy_start"]) - length
                region["proxy_end"] = float(region["proxy_end"]) - length
    state["segments"] = [
        segment for index, segment in enumerate(segments)
        if index not in remove_set
    ]
    state["updated_at"] = now_iso()
    metadata["regions"] = regions
    metadata["initial_segments"] = [
        {
            "source_start": float(region["source_start"]),
            "source_end": float(region["source_end"]),
            "kind": "original",
            "label": "原片",
        }
        for region in regions
    ]
    metadata["revealed_context_ranges"] = []
    _atomic_json(review_proxy_metadata_path(video), metadata)
    if timeline_edit_path(video).is_file():
        save_timeline_edit(video, state)
    return len(remove_indices)


def sync_review_proxy_ass_to_original(video: Path) -> bool:
    # A ripple-edited draft no longer shares the exported MP4 clock.
    # Keep its ASS local until the corresponding video is rendered.
    if timeline_edit_path(video).is_file():
        return False
    metadata = review_proxy_metadata(video)
    proxy_ass = review_proxy_ass_path(video)
    exact_ass = video.with_suffix(".ass")

    if not metadata.get("regions") or not proxy_ass.is_file():
        return False
    _transform_ass_regions(proxy_ass, exact_ass, metadata["regions"], forward=False)
    force_single_line_ass(exact_ass)
    resolve_dialogue_overlaps(exact_ass)
    return True


def clear_review_proxy(video: Path) -> None:
    for path in (
        review_proxy_video_path(video),
        review_proxy_ass_path(video),
        review_proxy_metadata_path(video),
    ):
        if path.is_file():
            path.unlink()


def timeline_edit_path(video: Path) -> Path:
    return video.parent / TIMELINE_EDIT_DIR / f"{video.name}.json"


def timeline_backup_ass_path(video: Path) -> Path:
    return video.parent / TIMELINE_EDIT_DIR / "sources" / f"{video.stem}.ass"


def timeline_backup_video_path(video: Path) -> Path:
    return video.parent / TIMELINE_EDIT_DIR / "sources" / video.name


def timeline_backup_metadata_path(video: Path) -> Path:
    return video.parent / TIMELINE_EDIT_DIR / "sources" / f"{video.name}.review.json"


def timeline_duration(segments: list[dict[str, float]]) -> float:
    return sum(
        max(0.0, float(segment["source_end"]) - float(segment["source_start"]))
        for segment in segments
    )


def normalized_timeline_segments(
    segments: list[dict[str, Any]], source_duration: float
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    limit = max(0.0, float(source_duration))
    for segment in segments:
        start = max(0.0, min(limit, float(segment.get("source_start", 0.0))))
        end = max(start, min(limit, float(segment.get("source_end", 0.0))))
        if end - start >= 0.02:
            normalized: dict[str, Any] = {"source_start": start, "source_end": end}
            for key in ("kind", "label"):
                if str(segment.get(key, "")).strip():
                    normalized[key] = str(segment[key]).strip()
            result.append(normalized)
    return result


def load_timeline_edit(video: Path, source_duration: float) -> dict[str, Any]:
    path = timeline_edit_path(video)
    if path.is_file():
        value = _read_json(path)
        segments = normalized_timeline_segments(
            value.get("segments", []), source_duration
        )
        if segments:
            value["segments"] = segments
            value.setdefault("status", "pending")
            return value
    metadata = review_proxy_metadata(video)
    initial = normalized_timeline_segments(
        metadata.get("initial_segments", []), source_duration
    )
    if not initial:
        initial = [
            {"source_start": 0.0, "source_end": max(0.0, float(source_duration)), "label": "原片"}
        ]
    return {
        "schema_version": 1,
        "status": "clean",
        "video": video.name,
        "source_duration": float(source_duration),
        "segments": initial,
        "updated_at": now_iso(),
    }


def save_timeline_edit(video: Path, state: dict[str, Any]) -> Path:
    path = timeline_edit_path(video)
    payload = dict(state)
    payload["schema_version"] = 1
    payload["status"] = "pending"
    payload["video"] = video.name
    payload["updated_at"] = now_iso()
    _atomic_json(path, payload)
    return path


def clear_timeline_edit(video: Path) -> None:
    path = timeline_edit_path(video)
    if path.is_file():
        path.unlink()


def timeline_segment_spans(
    segments: list[dict[str, float]],
) -> list[dict[str, float | int]]:
    spans: list[dict[str, float | int]] = []
    cursor = 0.0
    for index, segment in enumerate(segments):
        length = float(segment["source_end"]) - float(segment["source_start"])
        spans.append(
            {
                "index": index,
                "edited_start": cursor,
                "edited_end": cursor + length,
                "source_start": float(segment["source_start"]),
                "source_end": float(segment["source_end"]),
            }
        )
        cursor += length
    return spans


def edited_to_source(
    segments: list[dict[str, float]], edited_seconds: float
) -> tuple[float, int]:
    spans = timeline_segment_spans(segments)
    if not spans:
        return 0.0, -1
    value = max(0.0, min(timeline_duration(segments), float(edited_seconds)))
    for position, span in enumerate(spans):
        is_last = position == len(spans) - 1
        # A shared edited boundary belongs to the next source segment. This
        # avoids seeking into a removed source gap when playback starts there.
        if value < float(span["edited_end"]) - 1e-6 or is_last:
            offset = min(
                float(span["source_end"]) - float(span["source_start"]),
                max(0.0, value - float(span["edited_start"])),
            )
            return float(span["source_start"]) + offset, int(span["index"])
    last = spans[-1]
    return float(last["source_end"]), int(last["index"])


def source_to_edited(
    segments: list[dict[str, float]], source_seconds: float
) -> tuple[float | None, int | None]:
    value = float(source_seconds)
    for span in timeline_segment_spans(segments):
        if float(span["source_start"]) - 1e-6 <= value <= float(span["source_end"]) + 1e-6:
            offset = max(0.0, min(
                float(span["source_end"]) - float(span["source_start"]),
                value - float(span["source_start"]),
            ))
            return float(span["edited_start"]) + offset, int(span["index"])
    return None, None


def source_to_edited_nearest(
    segments: list[dict[str, float]], source_seconds: float
) -> tuple[float, int | None]:
    """Map a source gap/end to the nearest stable edited boundary, never zero-reset."""
    edited, index = source_to_edited(segments, source_seconds)
    if edited is not None:
        return float(edited), index
    spans = timeline_segment_spans(segments)
    if not spans:
        return 0.0, None
    value = float(source_seconds)
    if value < float(spans[0]["source_start"]):
        return 0.0, None
    for previous, following in zip(spans, spans[1:]):
        if (
            float(previous["source_end"]) < value
            < float(following["source_start"])
        ):
            return float(previous["edited_end"]), int(previous["index"])
    return float(spans[-1]["edited_end"]), int(spans[-1]["index"])

def split_timeline_segment(
    segments: list[dict[str, float]], source_seconds: float
) -> tuple[list[dict[str, float]], int]:
    value = float(source_seconds)
    result = [dict(segment) for segment in segments]
    for index, segment in enumerate(result):
        start = float(segment["source_start"])
        end = float(segment["source_end"])
        if start + 0.04 < value < end - 0.04:
            left = dict(segment)
            right = dict(segment)
            left.update({"source_start": start, "source_end": value})
            right.update({"source_start": value, "source_end": end})
            result[index : index + 1] = [left, right]
            return result, index + 1
    raise ValueError("播放头距离片段边缘太近，无法切开")


def subtitle_drag_times(
    start_seconds: float,
    end_seconds: float,
    pointer_start: float,
    pointer_current: float,
    mode: str,
    timeline_limit: float,
    *,
    minimum_duration: float = 0.04,
) -> tuple[float, float]:
    """Return clamped ASS cue bounds for an edge resize or whole-cue move."""
    limit = max(minimum_duration, float(timeline_limit))
    start = max(0.0, min(limit - minimum_duration, float(start_seconds)))
    end = min(limit, max(start + minimum_duration, float(end_seconds)))
    if mode == "start":
        return max(0.0, min(float(pointer_current), end - minimum_duration)), end
    if mode == "end":
        return start, min(limit, max(float(pointer_current), start + minimum_duration))
    if mode == "move":
        length = min(limit, max(minimum_duration, end - start))
        moved_start = start + float(pointer_current) - float(pointer_start)
        moved_start = max(0.0, min(limit - length, moved_start))
        return moved_start, moved_start + length
    raise ValueError(f"未知字幕拖动模式：{mode}")


def snap_subtitle_time(
    pointer_seconds: float,
    playhead_seconds: float,
    pixels_per_second: float,
    *,
    threshold_pixels: float = 10.0,
) -> tuple[float, bool]:
    """Snap a subtitle edge/pointer to the playhead within a pixel threshold."""
    pointer = max(0.0, float(pointer_seconds))
    playhead = max(0.0, float(playhead_seconds))
    distance = abs(pointer - playhead) * max(0.1, float(pixels_per_second))
    return (playhead, True) if distance <= max(0.0, float(threshold_pixels)) else (pointer, False)


def delete_ass_interval(path: Path, start_seconds: float, end_seconds: float) -> dict[str, int]:
    start_cut = max(0.0, float(start_seconds))
    end_cut = max(start_cut, float(end_seconds))
    removed_duration = end_cut - start_cut
    if removed_duration <= 0.0:
        return {"removed": 0, "shifted": 0, "clipped": 0}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    output: list[str] = []
    removed = shifted = clipped = 0
    for line in lines:
        if not line.startswith("Dialogue:"):
            output.append(line)
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            output.append(line)
            continue
        cue_start = parse_ass_time(fields[1])
        cue_end = parse_ass_time(fields[2])
        if cue_end <= start_cut:
            output.append(line)
            continue
        if cue_start >= end_cut:
            fields[1] = format_ass_time(cue_start - removed_duration)
            fields[2] = format_ass_time(cue_end - removed_duration)
            output.append(",".join(fields))
            shifted += 1
            continue
        if cue_start < start_cut and cue_end > end_cut:
            fields[2] = format_ass_time(cue_end - removed_duration)
            output.append(",".join(fields))
            clipped += 1
            continue
        if cue_start < start_cut < cue_end <= end_cut:
            if start_cut - cue_start >= 0.02:
                fields[2] = format_ass_time(start_cut)
                output.append(",".join(fields))
                clipped += 1
            else:
                removed += 1
            continue
        if start_cut <= cue_start < end_cut < cue_end:
            new_end = cue_end - removed_duration
            if new_end - start_cut >= 0.02:
                fields[1] = format_ass_time(start_cut)
                fields[2] = format_ass_time(new_end)
                output.append(",".join(fields))
                clipped += 1
            else:
                removed += 1
            continue
        removed += 1
    path.write_text("\n".join(output) + "\n", encoding="utf-8-sig")
    return {"removed": removed, "shifted": shifted, "clipped": clipped}


def timeline_render_source(video: Path) -> Path:
    """Timeline ranges use the full-source clock, independently of VLC's media."""
    metadata = review_proxy_metadata(video)
    if metadata.get("streaming"):
        source_value = str(metadata.get("source") or "").strip()
        source = Path(source_value)
        if not source_value or not source.is_file():
            raise FileNotFoundError("整场源视频不可用，无法应用剪辑稿")
        return source.resolve()
    return review_proxy_video_path(video) if has_review_proxy(video) else video


def timeline_render_plan(
    video: Path, segments: list[dict[str, Any]]
) -> tuple[Path, list[dict[str, Any]]]:
    """Use the short clip only when every retained source range maps into it."""
    metadata = review_proxy_metadata(video)
    regions = sorted(metadata.get("regions") or [], key=lambda row: float(row["source_start"]))
    if metadata.get("streaming") and regions and video.is_file() and segments:
        mapped = []
        for segment in segments:
            cursor, end = float(segment["source_start"]), float(segment["source_end"])
            for region in regions:
                start, stop = float(region["source_start"]), float(region["source_end"])
                if stop <= cursor or start >= end:
                    continue
                # A gap is not present in the MP4: extending into it needs the source.
                if start > cursor + 1e-6:
                    break
                exact_start, exact_end = float(region["exact_start"]), float(region["exact_end"])
                if abs((exact_end - exact_start) - (stop - start)) > 0.01:
                    break
                stop = min(end, stop)
                mapped.append({
                    **segment,
                    "source_start": exact_start + cursor - start,
                    "source_end": exact_start + stop - start,
                })
                cursor = stop
                if cursor >= end - 1e-6:
                    break
            if cursor < end - 1e-6:
                break
        else:
            return video, mapped
    return timeline_render_source(video), [dict(segment) for segment in segments]


def finish_review_timeline_metadata(
    video: Path, edited_ass: Path | None, segments: list[dict[str, Any]]
) -> None:
    """Retain the new cut-to-source mapping for the next review and edit."""
    metadata = review_proxy_metadata(video)
    exact_ass = video.with_suffix(".ass")
    if edited_ass is not None and edited_ass.is_file() and not _same_media(edited_ass, exact_ass):
        shutil.copy2(edited_ass, exact_ass)
    if not metadata.get("streaming"):
        clear_review_proxy(video)
        return
    if not segments:
        raise ValueError("应用后的剪辑时间轴不能为空")
    cursor = 0.0
    regions = []
    for segment in segments:
        start, end = float(segment["source_start"]), float(segment["source_end"])
        regions.append({
            "source_start": start, "source_end": end,
            "exact_start": cursor, "exact_end": cursor + end - start,
            "proxy_start": cursor, "proxy_end": cursor + end - start,
        })
        cursor += end - start
    metadata.update({
        "regions": regions,
        "render_segments": [
            {"source_start": item["source_start"], "source_end": item["source_end"]}
            for item in segments
        ],
        "initial_segments": [
            {"source_start": item["source_start"], "source_end": item["source_end"],
             "kind": "original", "label": "已应用剪辑"}
            for item in segments
        ],
        "exact_duration": cursor, "proxy_duration": cursor,
        "revealed_context_ranges": [], "applied_at": now_iso(),
        "export_inputs": _streaming_export_inputs(video),
    })
    if exact_ass.is_file():
        proxy_ass = review_proxy_ass_path(video)
        proxy_ass.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exact_ass, proxy_ass)
    _atomic_json(review_proxy_metadata_path(video), metadata)


def commit_timeline_render(
    video: Path, temporary: Path, edited_ass: Path | None,
    segments: list[dict[str, Any]],
) -> None:
    """Install video and its timeline together; keep the rendered file on failure."""
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise ValueError("剪辑临时视频不存在或为空")
    if video.resolve() == temporary.resolve():
        raise ValueError("临时视频不能覆盖自身")
    if not segments:
        raise ValueError("剪辑时间轴不能为空")
    if edited_ass is not None and not edited_ass.is_file():
        raise FileNotFoundError(f"剪辑字幕不存在：{edited_ass}")
    render_dir = video.parent / TIMELINE_EDIT_DIR / "rendering"
    render_dir.mkdir(parents=True, exist_ok=True)
    paths = list(dict.fromkeys([
        video.with_suffix(".ass"), review_proxy_ass_path(video),
        review_proxy_metadata_path(video), timeline_edit_path(video),
    ]))
    if not review_proxy_metadata(video).get("streaming"):
        paths.append(review_proxy_video_path(video))
    with tempfile.TemporaryDirectory(prefix="commit-", dir=render_dir) as folder:
        rollback = Path(folder)
        assert rollback.resolve().parent == render_dir.resolve()
        original = rollback / "original.mp4"
        shutil.copy2(video, original)
        saved = {}
        for index, path in enumerate(paths):
            backup = rollback / f"file-{index}"
            saved[path] = backup if path.is_file() else None
            if saved[path] is not None:
                shutil.copy2(path, backup)
        installed = False
        try:
            os.replace(temporary, video)
            installed = True
            finish_review_timeline_metadata(video, edited_ass, segments)
            clear_timeline_edit(video)
        except Exception:
            if installed:
                # Put the completed render back so a failed commit can be retried.
                os.replace(video, temporary)
                shutil.copy2(original, video)
                for path, backup in saved.items():
                    if backup is not None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, path)
                    elif path.is_file():
                        path.unlink()
            raise


def render_timeline_edit(
    source: Path,
    destination: Path,
    segments: list[dict[str, float]],
    ffmpeg: Path,
) -> None:
    if not segments:
        raise ValueError("时间轴不能删除全部视频")
    # Seek each retained range before opening the input. The previous trim-only
    # graph decoded from the beginning of a multi-hour livestream up to the last
    # retained timestamp, so the UI could remain pending for a very long time.
    command = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []
    inputs: list[str] = []
    for index, segment in enumerate(segments):
        start = float(segment["source_start"])
        end = float(segment["source_end"])
        duration = max(0.02, end - start)
        command.extend(
            [
                "-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
                "-i", str(source),
            ]
        )
        filters.extend(
            [
                (
                    f"[{index}:v]trim=start=0:duration={duration:.6f},"
                    f"settb=AVTB,setpts=PTS-STARTPTS[v{index}]"
                ),
                (
                    f"[{index}:a]atrim=start=0:duration={duration:.6f},"
                    f"asettb=1/1000000,asetpts=PTS-STARTPTS[a{index}]"
                ),
            ]
        )
        inputs.append(f"[v{index}][a{index}]")
    filters.append(
        "".join(inputs) + f"concat=n={len(segments)}:v=1:a=1[joinedv][joineda]"
    )
    filters.extend(
        [
            "[joinedv]settb=AVTB,setpts=PTS-STARTPTS[outv]",
            (
                "[joineda]aresample=async=1:first_pts=0,"
                "asettb=1/1000000,asetpts=PTS-STARTPTS[outa]"
            ),
        ]
    )
    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-avoid_negative_ts", "disabled",
            str(destination),
        ]
    )
    flags = background_creationflags()
    result = subprocess.run(
        command,
        capture_output=True,
        creationflags=flags,
    )
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "FFmpeg 生成剪辑稿失败")


def render_review_proxy(
    source: Path,
    destination: Path,
    segments: list[dict[str, float]],
    ffmpeg: Path,
) -> None:
    """Render a lightweight, low-priority review copy without consuming the machine."""
    if not segments:
        raise ValueError("审核上下文没有可渲染片段")
    filters: list[str] = []
    inputs: list[str] = []
    for index, segment in enumerate(segments):
        start = float(segment["source_start"])
        end = float(segment["source_end"])
        filters.extend(
            [
                f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,scale=w='min(iw,960)':h=-2:flags=fast_bilinear,fps=30[v{index}]",
                f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{index}]",
            ]
        )
        inputs.append(f"[v{index}][a{index}]")
    filters.append(
        "".join(inputs) + f"concat=n={len(segments)}:v=1:a=1[outv][outa]"
    )
    result = subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-filter_complex", ";".join(filters),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-threads", "2", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", str(destination),
        ],
        capture_output=True,
        creationflags=background_creationflags(),
    )
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "FFmpeg 生成低负载审核素材失败")


def registry_vlc_candidates() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    result: list[str] = []
    locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, location in locations:
        try:
            root = winreg.OpenKey(hive, location)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                    index += 1
                except OSError:
                    break
                try:
                    subkey = winreg.OpenKey(root, subkey_name)
                except OSError:
                    continue
                with subkey:
                    try:
                        display_name = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                    except OSError:
                        continue
                    if "vlc" not in display_name.casefold():
                        continue
                    try:
                        icon = str(winreg.QueryValueEx(subkey, "DisplayIcon")[0])
                        executable = Path(icon.strip().strip('"').split(",", 1)[0])
                        result.append(str(executable.parent / "libvlc.dll"))
                    except OSError:
                        pass
                    try:
                        install = Path(str(winreg.QueryValueEx(subkey, "InstallLocation")[0]))
                        result.append(str(install / "libvlc.dll"))
                    except OSError:
                        pass
    return result


def find_libvlc() -> Path | None:
    candidates = [
        r"C:\Program Files\VideoLAN\VLC\libvlc.dll",
        r"C:\Program Files (x86)\VideoLAN\VLC\libvlc.dll",
        *registry_vlc_candidates(),
    ]
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return None


class EmbeddedVlcPlayer:
    def __init__(self, library: Path) -> None:
        import ctypes

        self.ctypes = ctypes
        self.library_path = library.resolve()
        self._dll_directory = None
        if hasattr(os, "add_dll_directory"):
            self._dll_directory = os.add_dll_directory(str(self.library_path.parent))
        self.lib = ctypes.CDLL(str(self.library_path))
        self.lib.libvlc_new.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
        self.lib.libvlc_new.restype = ctypes.c_void_p
        self.lib.libvlc_release.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_add_option.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.libvlc_media_new_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.libvlc_media_new_path.restype = ctypes.c_void_p
        self.lib.libvlc_media_release.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_new_from_media.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_new_from_media.restype = ctypes.c_void_p
        self.lib.libvlc_media_player_release.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_set_hwnd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.libvlc_media_player_play.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_play.restype = ctypes.c_int
        self.lib.libvlc_media_player_set_pause.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.libvlc_media_player_set_rate.argtypes = [
            ctypes.c_void_p, ctypes.c_float
        ]
        self.lib.libvlc_media_player_set_rate.restype = ctypes.c_int
        self.lib.libvlc_media_player_is_playing.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_is_playing.restype = ctypes.c_int
        self.lib.libvlc_media_player_get_time.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_get_time.restype = ctypes.c_longlong
        self.lib.libvlc_media_player_set_time.argtypes = [ctypes.c_void_p, ctypes.c_longlong]
        self.lib.libvlc_media_player_get_length.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_media_player_get_length.restype = ctypes.c_longlong
        self.lib.libvlc_media_player_stop.argtypes = [ctypes.c_void_p]
        self.lib.libvlc_audio_set_mute.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.libvlc_audio_set_mute.restype = ctypes.c_int
        # The editor draws draft subtitles on its edited clock. Do not also
        # load the sibling ASS on VLC's different media clock.
        arguments = [b"--quiet", b"--no-video-title-show",
                     b"--no-sub-autodetect-file", b"--no-spu"]
        argv = (ctypes.c_char_p * len(arguments))(*arguments)
        self.instance = self.lib.libvlc_new(len(arguments), argv)
        if not self.instance:
            raise RuntimeError("VLC 初始化失败")
        self.player = None
        self.playback_rate = 1.0

    def play(
        self, video: Path, window_id: int, *, start_seconds: float = 0.0,
        stop_seconds: float | None = None,
    ) -> None:
        self.stop()
        media = self.lib.libvlc_media_new_path(
            self.instance, str(video.resolve()).encode("utf-8")
        )
        if not media:
            raise RuntimeError(f"VLC 无法读取视频：{video}")
        if start_seconds > 0.0:
            option = f":start-time={float(start_seconds):.3f}".encode("ascii")
            self.lib.libvlc_media_add_option(media, option)
        if stop_seconds is not None and float(stop_seconds) > float(start_seconds):
            option = f":stop-time={float(stop_seconds):.3f}".encode("ascii")
            self.lib.libvlc_media_add_option(media, option)
        try:
            self.player = self.lib.libvlc_media_player_new_from_media(media)
        finally:
            self.lib.libvlc_media_release(media)
        if not self.player:
            raise RuntimeError("VLC 无法创建内嵌播放器")
        self.lib.libvlc_media_player_set_hwnd(
            self.player, self.ctypes.c_void_p(int(window_id))
        )
        if self.lib.libvlc_media_player_play(self.player) != 0:
            self.stop()
            raise RuntimeError(f"VLC 播放失败：{video}")
        self.set_rate(self.playback_rate)

    def set_rate(self, rate: float) -> bool:
        value = max(0.25, min(4.0, float(rate)))
        self.playback_rate = value
        if not self.player:
            return True
        return self.lib.libvlc_media_player_set_rate(
            self.player, self.ctypes.c_float(value)
        ) == 0

    def toggle_pause(self) -> None:
        if not self.player:
            return
        pause = 1 if self.lib.libvlc_media_player_is_playing(self.player) else 0
        self.lib.libvlc_media_player_set_pause(self.player, pause)

    def is_playing(self) -> bool:
        return bool(
            self.player and self.lib.libvlc_media_player_is_playing(self.player)
        )

    def pause(self) -> None:
        if self.player:
            self.lib.libvlc_media_player_set_pause(self.player, 1)

    def set_muted(self, muted: bool) -> bool:
        if not self.player:
            return True
        return self.lib.libvlc_audio_set_mute(
            self.player, 1 if muted else 0
        ) == 0

    def resume(self) -> bool:
        """Ensure an existing player continues; return True when VLC had to restart."""
        if not self.player or self.lib.libvlc_media_player_is_playing(self.player):
            return False
        self.lib.libvlc_media_player_set_pause(self.player, 0)
        if self.lib.libvlc_media_player_is_playing(self.player):
            return False
        if self.lib.libvlc_media_player_play(self.player) != 0:
            raise RuntimeError("VLC 无法从所选字幕时间继续播放")
        return True

    def seek(self, delta_seconds: float) -> None:
        if not self.player:
            return
        current = int(self.lib.libvlc_media_player_get_time(self.player))
        self.lib.libvlc_media_player_set_time(
            self.player, self.ctypes.c_longlong(max(0, current + round(delta_seconds * 1000)))
        )


    def current_seconds(self) -> float:
        if not self.player:
            return 0.0
        return max(0.0, int(self.lib.libvlc_media_player_get_time(self.player)) / 1000.0)

    def duration_seconds(self) -> float:
        if not self.player:
            return 0.0
        return max(0.0, int(self.lib.libvlc_media_player_get_length(self.player)) / 1000.0)

    def set_time(self, seconds: float) -> None:
        if self.player:
            self.lib.libvlc_media_player_set_time(
                self.player, self.ctypes.c_longlong(max(0, round(seconds * 1000)))
            )

    def stop(self) -> None:
        if not self.player:
            return
        self.lib.libvlc_media_player_stop(self.player)
        self.lib.libvlc_media_player_release(self.player)
        self.player = None

    def close(self) -> None:
        self.stop()
        if self.instance:
            self.lib.libvlc_release(self.instance)
            self.instance = None
        if self._dll_directory is not None:
            self._dll_directory.close()
            self._dll_directory = None

def registry_aegisub_candidates() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    result: list[str] = []
    for hive, location in locations:
        try:
            root = winreg.OpenKey(hive, location)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                    index += 1
                except OSError:
                    break
                try:
                    subkey = winreg.OpenKey(root, subkey_name)
                except OSError:
                    continue
                with subkey:
                    try:
                        display_name = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                    except OSError:
                        continue
                    if "aegisub" not in display_name.casefold():
                        continue
                    try:
                        icon = str(winreg.QueryValueEx(subkey, "DisplayIcon")[0])
                        result.append(icon.strip().strip('"').split(",", 1)[0])
                    except OSError:
                        pass
                    try:
                        install = Path(str(winreg.QueryValueEx(subkey, "InstallLocation")[0]))
                        result.extend(
                            str(install / name)
                            for name in ("aegisub.exe", "aegisub64.exe", "aegisub32.exe")
                        )
                    except OSError:
                        pass
    return result

def find_aegisub(configured: str = "") -> Path | None:
    candidates = [configured] if configured.strip() else []
    candidates.extend(
        filter(
            None,
            [
                shutil.which("aegisub64.exe"),
                shutil.which("aegisub.exe"),
                shutil.which("aegisub32.exe"),
                r"C:\Program Files\Aegisub\aegisub64.exe",
                r"C:\Program Files\Aegisub\aegisub.exe",
                r"C:\Program Files (x86)\Aegisub\aegisub32.exe",
                *registry_aegisub_candidates(),
            ],
        )
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def bind_ass_media(ass: Path, video: Path) -> None:
    value = ass.read_text(encoding="utf-8-sig")
    media = str(video.resolve())
    section = (
        "[Aegisub Project Garbage]\n"
        f"Audio File: {media}\n"
        f"Video File: {media}\n"
        "Video AR Mode: 4\n"
        "Video AR Value: 1.777778\n\n"
    )
    pattern = re.compile(
        r"(?ms)^\[Aegisub Project Garbage\]\s*.*?(?=^\[[^\n]+\]\s*$)"
    )
    if pattern.search(value):
        value = pattern.sub(section, value, count=1)
    elif "[V4+ Styles]" in value:
        value = value.replace("[V4+ Styles]", section + "[V4+ Styles]", 1)
    else:
        value = section + value
    ass.write_text(value, encoding="utf-8-sig")


def launch_aegisub(executable: Path, ass: Path, video: Path) -> None:
    bind_ass_media(ass, video)
    subprocess.Popen([str(executable), str(ass)])



