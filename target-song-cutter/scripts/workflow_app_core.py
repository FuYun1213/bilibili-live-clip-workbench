#!/usr/bin/env python3
"""Local five-stage livestream clipping workbench core.

The Tkinter frontend runs this module as a subprocess so Whisper and FFmpeg
jobs remain cancelable and their output can be streamed into the application.
"""

from __future__ import annotations

import argparse
import copy
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


import cover_emotes
import media_packaging
from publish_diagnostics import PROGRESS_PREFIX, parse_progress_line, publish_error_summary
from campaign_rules import merge_campaign_tags
import review_workspace
from narrative_edit import align_authoritative_edit_ranges, refine_narrative_edit_ranges, write_narrative_refinement_audit
from windows_process import hidden_subprocess_kwargs, run_checked_with_transient_retries


SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
WORKSPACE_ROOT = SKILL_ROOT.parent
PROFILES_PATH = SKILL_ROOT / "assets" / "creator-profiles.json"
PROJECT_FILENAME = "workflow-project.json"
SCHEMA_VERSION = 1
DEFAULT_KEYWORDS = ["性暗示", "边界感", "点评", "评价", "破防", "红温"]
FLOW3_TAIL_PADDING_SECONDS = 0.0
MIN_GIFT_TRIM_FRAGMENT_SECONDS = 0.5
COPY_FIELDS = [
    "clip_id", "video", "title", "content_type", "cover_mode",
    "cover_source_region", "cover_time_seconds", "cover_text_primary",
    "cover_text_secondary", "cover_emotion", "cover_emote",
    "description", "tags", "campaign_tags", "tag_evidence",
    "vr_topic", "tid", "collection", "season_id", "section_id",
    "reference_image", "evidence_image", "collaboration_type", "participants",
    "participant_profile_keys", "speaker_evidence", "guest_dialogue_verified",
    "guest_character_images",
]
PLAN_FIELDS = [
    "slice_id", "order", "start_seconds", "end_seconds", "title",
    "outline", "hook", "reason", "keep",
]
REVIEWED_VALUES = {"1", "true", "yes", "y", "reviewed", "ok", "是", "已审核"}
SECRET_PATTERN = re.compile(
    r"(?i)(SESSDATA|bili_jct|access_token|refresh_token|cookie)\s*[:=]\s*[^\s,;]+"
)


class WorkflowError(RuntimeError):
    """A user-actionable workflow failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def atomic_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows readers can briefly deny replacement; keep the previous complete
    # document visible and give each writer its own temporary file.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_profiles() -> dict[str, dict[str, Any]]:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8-sig"))["profiles"]


def project_file(project: Path | str) -> Path:
    value = Path(project).expanduser().resolve()
    return value if value.name == PROJECT_FILENAME else value / PROJECT_FILENAME


def load_project(project: Path | str) -> tuple[Path, dict[str, Any]]:
    state_path = project_file(project)
    if not state_path.is_file():
        raise WorkflowError(f"项目文件不存在：{state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    if state.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError("不支持的项目版本")
    # Legacy projects could override the recording with a separate audio file.
    # The current workflow always uses the recording's own embedded audio.
    state.get("config", {}).pop("audio", None)
    return state_path, state


def save_project(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_json(state_path, state)


def init_project(
    project: Path,
    source: Path,
    xml: Path | None,
    creator: str,
    mode: str,
    model: str,
    device: str,
    compute_type: str,
    speaker_count: int | None = None,
) -> Path:
    profiles = load_profiles()
    if creator not in profiles:
        raise WorkflowError(f"未知主播配置：{creator}")
    source = source.expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"录播文件不存在：{source}")
    if xml:
        xml = xml.expanduser().resolve()
        if not xml.is_file():
            print(f"弹幕 XML 不存在，将使用录播音频和转写继续处理：{xml}")
    if mode not in {"narrative", "song", "mixed"}:
        raise WorkflowError(f"未知内容模式：{mode}")
    if device not in {"cuda", "cpu"}:
        raise WorkflowError(f"未知 ASR 设备：{device}")

    project = project.expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    state_path = project / PROJECT_FILENAME
    previous: dict[str, Any] = {}
    if state_path.is_file():
        previous = json.loads(state_path.read_text(encoding="utf-8-sig"))
    if speaker_count is None:
        speaker_count = previous.get("config", {}).get("asr", {}).get(
            "speaker_count"
        )
    if speaker_count is not None and int(speaker_count) <= 0:
        raise WorkflowError("说话人数必须是正整数")
    asr_config = {
        "model": model or "local:qwen3-asr-auto",
        "device": device,
        "compute_type": compute_type or ("float16" if device == "cuda" else "int8"),
    }
    if speaker_count is not None:
        asr_config["speaker_count"] = int(speaker_count)
    config = {
        "source": str(source),
        "xml": str(xml) if xml else "",
        "creator": creator,
        "mode": mode,
        "asr": asr_config,
        "publish": {
            "tid": 27,
            "song_tid": 130,
            "copyright": 1,
            "source": "",
            "no_reprint": False,
            "limit": 3,
            "cooldown": 0,
        },
    }
    identity_fields = ("source", "xml", "creator", "mode")
    old_config = previous.get("config", {})
    identity_changed = bool(previous) and any(
        old_config.get(field, "") != config[field] for field in identity_fields
    )
    state = previous or {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "steps": {},
        "approvals": {},
    }
    if "media_packaging" in old_config:
        config["media_packaging"] = media_packaging.normalize_media_packaging(old_config["media_packaging"], project)
    state["config"] = config
    if identity_changed:
        state["steps"] = {}
        state["approvals"] = {}
        for field in (
            "selection_file", "active_run_dir", "active_review_dir", "active_burn_source_dir",
            "delivery_dir", "publish_preview_digest",
        ):
            state.pop(field, None)
    save_project(state_path, state)
    return state_path


def mark_step(
    state_path: Path,
    state: dict[str, Any],
    name: str,
    status: str,
    detail: str = "",
    outputs: Sequence[Path | str] = (),
) -> None:
    state.setdefault("steps", {})[name] = {
        "status": status,
        "updated_at": now_iso(),
        "detail": detail,
        "outputs": [str(value) for value in outputs],
    }
    save_project(state_path, state)


def redact(value: str) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", value)


def console_print(value: object) -> None:
    """Print without letting a legacy Windows console encoding abort the workflow."""
    text = str(value)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        fallback = text.encode(encoding, errors="backslashreplace").decode(encoding)
        sys.stdout.write(fallback + "\n")
        sys.stdout.flush()


def script_command(name: str, *parts: object) -> list[str]:
    return [sys.executable, "-X", "utf8", str(SCRIPTS / name), *[str(part) for part in parts]]


def transient_flow2_router_error(stage: str, line: str) -> bool:
    """Hide recoverable Codex shell-policy retries unless the process really fails."""
    folded = line.casefold()
    return (
        stage.startswith("flow2-codex-selection")
        and "codex_core::tools::router" in folded
        and "blocked by policy" in folded
    )


def run_external(
    project_root: Path,
    stage: str,
    command: Sequence[str],
    *,
    stdin_text: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> None:
    logs = project_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{run_stamp()}-{stage}.log"
    shown = subprocess.list2cmdline([redact(str(part)) for part in command])
    console_print(f"\n[{stage}] {shown}")
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    deferred_console_errors: list[str] = []
    failure_reason = ""
    recent_output: list[str] = []
    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{now_iso()}] {shown}\n")
        handle.flush()
        process = subprocess.Popen(
            list(command),
            cwd=str(WORKSPACE_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        if stdin_text is not None:
            assert process.stdin is not None
            process.stdin.write(stdin_text)
            process.stdin.close()
        assert process.stdout is not None
        try:
            for line in process.stdout:
                safe = redact(line.rstrip("\r\n"))
                if re.match(r"^(?:Error:|错误[：:]|[\w.]*Error:|[\w.]*Exception:)", safe):
                    failure_reason = safe[:1200]
                if stage.startswith("flow5-"):
                    recent_output.append(safe)
                    recent_output = recent_output[-160:]
                handle.write(safe + "\n")
                handle.flush()
                if on_output is not None:
                    try:
                        on_output(safe)
                    except Exception as exc:
                        console_print(f"[{stage}] 无法更新显示状态：{redact(str(exc))}")
                if safe.startswith(PROGRESS_PREFIX):
                    continue
                if transient_flow2_router_error(stage, safe):
                    deferred_console_errors.append(safe)
                    continue
                console_print(safe)
        except KeyboardInterrupt:
            process.terminate()
            raise
        finally:
            process.stdout.close()
        return_code = process.wait()
    if deferred_console_errors:
        if return_code:
            for line in deferred_console_errors:
                console_print(line)
        else:
            console_print(
                f"[{stage}] 已自动跳过 {len(deferred_console_errors)} 次受限读取尝试；"
                "兼容读取已成功。"
            )
    if return_code:
        if stage.startswith("flow5-"):
            failure_reason = publish_error_summary("\n".join(recent_output))["detail"]
        detail = f"{failure_reason} " if failure_reason else ""
        raise WorkflowError(f"{stage} 失败，退出码 {return_code}。{detail}日志：{log_path}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_paths(paths: Iterable[Path], extra: Any | None = None) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(item for item in path.rglob("*") if item.is_file()))
        elif path.is_file():
            files.append(path)
    for path in sorted(set(files), key=lambda item: str(item).casefold()):
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    if extra is not None:
        digest.update(json.dumps(extra, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def parse_clock(value: Any) -> float:
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        raw = str(value).strip().replace(",", ".")
        if not raw:
            raise WorkflowError("空时间戳")
        if ":" not in raw:
            seconds = float(raw)
        else:
            parts = raw.split(":")
            if len(parts) == 2:
                seconds = float(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            else:
                raise WorkflowError(f"无法解析时间戳：{value}")
    if seconds < 0:
        raise WorkflowError(f"时间戳不能为负数：{value}")
    return round(seconds, 3)


def range_from_value(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        start = next(
            (value[key] for key in ("开始", "开始秒", "start", "start_seconds") if key in value),
            None,
        )
        end = next(
            (value[key] for key in ("结束", "结束秒", "end", "end_seconds") if key in value),
            None,
        )
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raw = str(value).strip()
        parts = re.split(r"\s*(?:-->|→|至|—|–)\s*", raw, maxsplit=1)
        if len(parts) == 1:
            parts = re.split(r"(?<=\d)\s*-\s*(?=\d{1,2}:)", raw, maxsplit=1)
        if len(parts) != 2:
            raise WorkflowError(f"时间段必须包含开始和结束：{value}")
        start, end = parts
    if start is None or end is None:
        raise WorkflowError(f"时间段缺少开始或结束：{value}")
    start_seconds = parse_clock(start)
    end_seconds = parse_clock(end)
    if end_seconds <= start_seconds:
        raise WorkflowError(f"结束时间必须晚于开始时间：{value}")
    return {"start_seconds": start_seconds, "end_seconds": end_seconds}


def split_cover_copy(value: str, mode: str) -> tuple[str, str]:
    text = value.strip()
    if not text:
        raise WorkflowError("副标题不能为空")
    if mode == "song":
        if "｜" in text or "|" in text or "\n" in text or "\r" in text:
            raise WorkflowError("歌切副标题只能是完整歌名，不能使用分区或换行")
        if not 1 <= len(text) <= 24:
            raise WorkflowError("歌名应为 1–24 个字符")
        return text, ""
    if re.search(r"[｜|]", text):
        parts = [part.strip() for part in re.split(r"[｜|]", text)]
    else:
        parts = [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]
    if len(parts) not in (2, 3) or not all(parts):
        raise WorkflowError(
            "普通切片副标题必须为 2–3 句，用｜分隔；第一句放上区，其余放下区"
        )
    try:
        return cover_emotes.validate_cover_lines(parts[0], "｜".join(parts[1:]))
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def title_with_creator(title: str, creator: str) -> str:
    profile = load_profiles()[creator]
    label = profile["title_tag"]
    title = title.strip()
    current = re.match(r"^【([^】]+)】", title)
    if current and current.group(1) != label:
        raise WorkflowError(f"标题主播标签与项目不一致：{current.group(1)} != {label}")
    if not current:
        title = f"【{label}】{title}"
    if not 1 <= len(title) <= 80:
        raise WorkflowError("标题必须为 1–80 个字符")
    if "'" in title:
        raise WorkflowError("标题不能包含英文单引号")
    return title


def normalize_publish_tags(value: Any) -> list[str]:
    """Normalize five model-authored content tags while preserving legacy selections."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = re.split(r"[,，;；|]", value)
    elif isinstance(value, list):
        values = value
    else:
        raise WorkflowError("标签必须是包含 5 个字符串的数组")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise WorkflowError("标签数组中的每一项都必须是字符串")
        tag = re.sub(r"\s+", " ", raw).strip(" ,，;；|")
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    if len(result) != 5:
        raise WorkflowError(f"每个片段必须提供 5 个互不重复的内容 Tag，当前为 {len(result)} 个")
    return result


COLLABORATION_TYPE_ALIASES = {
    "": "single",
    "single": "single",
    "单人": "single",
    "collaboration": "collaboration",
    "多人": "collaboration",
    "多人连麦": "collaboration",
    "连麦": "collaboration",
    "reaction": "reaction",
    "同步试听": "reaction",
    "同看反应": "reaction",
    "同看": "reaction",
    "reaction/watchalong": "reaction",
}


def normalize_collaboration_metadata(
    raw: dict[str, Any], creator: str
) -> dict[str, Any]:
    """Normalize conservative model hints; audible guests are never auto-verified."""
    profiles = load_profiles()
    host = profiles[creator]
    host_name = str(host.get("display_name") or creator).strip()
    alias_map: dict[str, tuple[str, str]] = {}
    for key, profile in profiles.items():
        if profile.get("archived"):
            continue
        display = str(profile.get("display_name") or key).strip()
        aliases = [key, display, str(profile.get("title_tag") or "")]
        aliases.extend(str(value) for value in profile.get("hotwords", []))
        for alias in aliases:
            normalized = re.sub(r"\s+", "", alias).casefold()
            if normalized:
                alias_map.setdefault(normalized, (key, display))

    value = raw.get("参与者", raw.get("participants"))
    if value in (None, ""):
        supplied = []
    elif isinstance(value, str):
        supplied = re.split(r"[,，、;；|\n]+", value)
    elif isinstance(value, list):
        supplied = value
    else:
        raise WorkflowError("参与者必须是人物名称数组或逗号分隔文本")
    participants = [host_name]
    participant_keys = [creator]
    seen = {host_name.casefold()}
    for item in supplied:
        name = str(item).strip()
        if not name:
            continue
        match = alias_map.get(re.sub(r"\s+", "", name).casefold())
        if match:
            key, display = match
            if key not in participant_keys:
                participant_keys.append(key)
            name = display
        if name.casefold() not in seen:
            seen.add(name.casefold())
            participants.append(name)

    raw_type = str(
        raw.get("多人类型", raw.get("collaboration_type", "single")) or "single"
    ).strip().casefold()
    collaboration_type = COLLABORATION_TYPE_ALIASES.get(raw_type)
    if collaboration_type is None:
        raise WorkflowError(f"未知多人类型：{raw_type}")
    if collaboration_type == "single" and len(participants) > 1:
        collaboration_type = "collaboration"
    if collaboration_type != "single" and len(participants) == 1:
        participants.append("待确认嘉宾")

    evidence = str(
        raw.get("说话人依据", raw.get("speaker_evidence", "")) or ""
    ).strip()
    if collaboration_type != "single" and not evidence:
        evidence = "模型仅标记为疑似多人，需在审核台核实可听台词"
    # Flow2 has no trusted voice anchors. Verification is deliberately a human
    # review field even if a model tries to set it to true.
    verified = False
    return {
        "collaboration_type": collaboration_type,
        "participants": participants,
        "participant_profile_keys": participant_keys,
        "speaker_evidence": evidence,
        "guest_dialogue_verified": verified,
    }

def normalize_selection(payload: Any, creator: str, mode: str) -> list[dict[str, Any]]:
    if mode == "mixed":
        if not isinstance(payload, dict):
            raise WorkflowError("混合切片 JSON 必须包含“普通切片”和“歌切”两个数组")
        narrative_items = payload.get("普通切片", payload.get("narrative", []))
        song_items = payload.get("歌切", payload.get("song", []))
        if not isinstance(narrative_items, list) or not isinstance(song_items, list):
            raise WorkflowError("混合切片的“普通切片”和“歌切”都必须是数组")
        typed_items = [
            *(("narrative", item) for item in narrative_items),
            *(("song", item) for item in song_items),
        ]
    else:
        if isinstance(payload, dict):
            raw_items = payload.get("clips") or payload.get("切片") or payload.get("items")
        else:
            raw_items = payload
        if not isinstance(raw_items, list):
            raise WorkflowError("选片 JSON 必须是数组，或包含 clips 数组")
        typed_items = [(mode, item) for item in raw_items]

    if not typed_items:
        raise WorkflowError("选片 JSON 没有任何片段")
    result: list[dict[str, Any]] = []
    for index, (item_mode, raw) in enumerate(typed_items, 1):
        if not isinstance(raw, dict):
            raise WorkflowError(f"第 {index} 项必须是 JSON 对象")
        title = raw.get("标题", raw.get("title", ""))
        subtitle = raw.get("副标题", raw.get("subtitle", ""))
        timestamps = raw.get("时间戳", raw.get("timestamps"))
        publish_tags = normalize_publish_tags(
            raw.get("标签", raw.get("tags", raw.get("publish_tags")))
        )
        tag_evidence = raw.get("标签依据", raw.get("tag_evidence", ""))
        vr_topic = "yes" if str(raw.get("vr_topic") or "").strip().casefold() in {
            "yes", "true", "1", "是", "有"
        } else ""
        collaboration = normalize_collaboration_metadata(raw, creator)
        if not isinstance(title, str) or not isinstance(subtitle, str):
            raise WorkflowError(f"第 {index} 项的标题和副标题必须是字符串")
        if publish_tags and (not isinstance(tag_evidence, str) or not tag_evidence.strip()):
            raise WorkflowError(f"第 {index} 项提供了标签但缺少标签依据")
        if timestamps is None:
            raise WorkflowError(f"第 {index} 项缺少时间戳")
        values = timestamps if isinstance(timestamps, list) else [timestamps]
        if not values:
            raise WorkflowError(f"第 {index} 项没有时间段")
        ranges = [range_from_value(value) for value in values]
        try:
            structure = media_packaging.normalize_narrative_structure(
                raw.get("叙事结构", raw.get("narrative_structure")), ranges, item_mode,
            )
        except (ValueError, TypeError) as exc:
            raise WorkflowError(f"第 {index} 项：{exc}") from exc
        total = sum(item["end_seconds"] - item["start_seconds"] for item in ranges)
        if item_mode == "narrative" and not 30 <= total + 5 <= 300:
            raise WorkflowError(
                f"第 {index} 项预计成片 {total + 5:.1f} 秒；普通切片必须为 30–300 秒"
            )
        if item_mode == "song" and total < 10:
            raise WorkflowError(f"第 {index} 项歌切短于 10 秒")
        full_title = title_with_creator(title, creator)
        primary, secondary = split_cover_copy(subtitle, item_mode)
        item = {
            "clip_id": f"{index:03d}",
            "title": full_title,
            "subtitle": primary + (f"｜{secondary}" if secondary else ""),
            "content_type": item_mode,
            "cover_text_primary": primary,
            "cover_text_secondary": secondary,
            "timestamps": ranges,
            "narrative_structure": structure,
            "vr_topic": vr_topic,
            **collaboration,
        }
        if publish_tags:
            item["publish_tags"] = publish_tags
            item["tag_evidence"] = tag_evidence.strip()
        result.append(item)
    return result


def canonical_selection(
    items: Sequence[dict[str, Any]], mode: str | None = None
) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
    def public_item(item: dict[str, Any]) -> dict[str, Any]:
        result = {
            "标题": item["title"],
            "副标题": item["subtitle"],
            "时间戳": [
                {"开始秒": value["start_seconds"], "结束秒": value["end_seconds"]}
                for value in item["timestamps"]
            ],
            "多人类型": {
                "single": "单人",
                "collaboration": "多人连麦",
                "reaction": "同步试听",
            }.get(str(item.get("collaboration_type")), "单人"),
            "参与者": list(item.get("participants") or []),
            "说话人依据": str(item.get("speaker_evidence") or ""),
            "嘉宾台词已核实": bool(item.get("guest_dialogue_verified", False)),
        }
        structure = item.get("narrative_structure")
        if structure:
            role_labels = {"cold_open": "冷开场", "setup": "前情", "return": "回归", "payoff": "结果", "body": "正文"}
            result["叙事结构"] = {
                "类型": "倒叙" if structure["type"] == "callback" else "顺叙",
                "段落角色": [role_labels[role] for role in structure["roles"]],
                "转场秒数": structure["transition_seconds"], "理由": structure["reason"],
            }
        if item.get("vr_topic"):
            result["vr_topic"] = str(item["vr_topic"])
        if item.get("publish_tags"):
            result["标签"] = list(item["publish_tags"])
            result["标签依据"] = str(item.get("tag_evidence") or "").strip()
        return result

    if mode == "mixed":
        return {
            "普通切片": [
                public_item(item)
                for item in items
                if item.get("content_type", "narrative") == "narrative"
            ],
            "歌切": [
                public_item(item)
                for item in items
                if item.get("content_type") == "song"
            ],
        }
    return [public_item(item) for item in items]


def load_canonical_selection(state: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(state.get("selection_file", ""))
    if not path.is_file():
        raise WorkflowError("尚未导入选片 JSON")
    config = state["config"]
    return normalize_selection(
        json.loads(path.read_text(encoding="utf-8-sig")),
        config["creator"],
        config["mode"],
    )

def format_clock(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def transcript_csv(state_path: Path, state: dict[str, Any]) -> Path:
    root = state_path.parent / "analysis" / "transcript"
    mode = state["config"]["mode"]
    if mode == "song":
        names = ("transcript_multilingual.csv",)
    elif mode == "mixed":
        names = ("transcript.filtered.csv", "transcript_multilingual.csv")
    else:
        names = ("transcript.filtered.csv", "transcript.csv")
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    raise WorkflowError("找不到与当前模式匹配的转写 CSV，请先运行流程 1")

def authoritative_transcript_files(
    state_path: Path, state: dict[str, Any]
) -> tuple[Path, Path, Path, Path]:
    """Require a completed Flow1 authority before any clip is exported."""
    csv_path = transcript_csv(state_path, state)
    root = state_path.parent / "analysis" / "transcript"
    json_path = (
        root / "transcript_multilingual.json"
        if csv_path.name == "transcript_multilingual.csv"
        else root / "transcript.json"
    )
    completeness = root / "transcript-completeness.json"
    speech = root / "speech-activity.json"
    missing = [path for path in (json_path, completeness, speech) if not path.is_file()]
    if missing:
        raise WorkflowError(
            "当前任务没有新版 Flow1 权威转写产物，请重做流程1；缺少："
            + "、".join(path.name for path in missing)
        )
    try:
        report = json.loads(completeness.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Flow1 完整性报告无法读取：{completeness}") from exc
    if report.get("status") != "PASS" or not report.get("authoritative"):
        unresolved = int(report.get("unresolved_window_count") or 0)
        raise WorkflowError(
            "Flow1 整场转写尚未通过完整性门禁"
            + (f"（仍有 {unresolved} 段语音未覆盖）" if unresolved else "")
            + "，请先重做流程1，不能在切片阶段补录"
        )
    return csv_path, json_path, completeness, speech

def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _row_float(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = str(row.get(name, "")).strip()
        if not value:
            continue
        try:
            return float(value)
        except ValueError:
            continue
    return None


def _write_csv_rows(
    path: Path, fieldnames: Sequence[str], rows: Sequence[dict[str, str]]
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _merge_time_ranges(ranges: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        start = max(0.0, float(start))
        end = max(start, float(end))
        if not merged or start > merged[-1][1] + 10.0:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def build_compact_selection_context(
    source_csv: Path,
    engagement_dir: Path,
    context_csv: Path,
    *,
    mode: str,
    max_context_seconds: float = 3600.0,
    max_source_seconds: float | None = None,
) -> dict[str, Any]:
    """Create a bounded semantic candidate transcript before any model call."""
    fields, transcript = _read_csv_rows(source_csv)
    from asr_hotword_guard import require_clean_artifact
    require_clean_artifact(source_csv)
    original_row_count = len(transcript)
    # Model input needs dialogue and source times, not ASR internals. Put text
    # before speaker metadata so long machine-generated fields cannot hide it.
    source_fields = list(fields)
    fields = [field for field in (
        "start_seconds", "end_seconds", "text", "speaker_id", "speaker_key", "speaker_label"
    ) if field in fields]
    if not {"start_seconds", "end_seconds", "text"}.issubset(fields):
        raise WorkflowError("选片转写缺少起止时间或正文，不能当作没有合格内容")
    transcript = [{field: row.get(field, "") for field in fields} for row in transcript]
    if max_source_seconds is not None:
        transcript = [
            row
            for row in transcript
            if (_row_float(row, "start_seconds") or 0.0) < max_source_seconds
        ]
    if not transcript:
        _write_csv_rows(context_csv, fields, [])
        return {
            "strategy": "no-transcript-in-source", "selected_rows": 0,
            "source_duration_seconds": max_source_seconds,
            "original_rows": original_row_count, "rows_within_source": 0,
            "original_bytes": source_csv.stat().st_size,
            "selected_bytes": context_csv.stat().st_size,
            "fields": fields, "omitted_fields": [field for field in source_fields if field not in fields],
        }

    candidates: list[tuple[float, float]] = []
    hotspot_fields, hotspots = _read_csv_rows(engagement_dir / "engagement-hotspots.csv")
    del hotspot_fields
    for row in hotspots[:12]:
        start = _row_float(row, "cause_search_start", "inspect_start", "start_seconds")
        end = _row_float(row, "payoff_search_end", "inspect_end", "end_seconds")
        if start is not None and end is not None and end > start:
            candidates.append((max(0.0, start - 120.0), end + 180.0))

    _, superchats = _read_csv_rows(engagement_dir / "superchats.csv")
    ranked_sc = sorted(
        superchats,
        key=lambda row: _row_float(row, "price") or 0.0,
        reverse=True,
    )
    for row in ranked_sc[:8]:
        anchor = _row_float(row, "aligned_time_seconds", "source_time_seconds")
        if anchor is None:
            continue
        response_end = _row_float(row, "response_end_seconds", "response_search_end_seconds")
        candidates.append((
            max(0.0, anchor - 120.0),
            (response_end if response_end is not None else anchor + 180.0) + 120.0,
        ))

    keyword_ranges: list[tuple[float, float]] = []
    last_keyword = -1e9
    for row in transcript:
        text = str(row.get("text", ""))
        start = _row_float(row, "start_seconds")
        end = _row_float(row, "end_seconds")
        if start is None or end is None or start - last_keyword < 120.0:
            continue
        if any(keyword in text for keyword in DEFAULT_KEYWORDS):
            keyword_ranges.append((max(0.0, start - 120.0), end + 180.0))
            last_keyword = start
            if len(keyword_ranges) >= 10:
                break
    candidates.extend(keyword_ranges)

    if not candidates:
        total_end = max(_row_float(row, "end_seconds") or 0.0 for row in transcript)
        for index in range(1, 11):
            anchor = total_end * index / 11
            candidates.append((max(0.0, anchor - 150.0), anchor + 150.0))

    if max_source_seconds is not None:
        candidates = [
            (start, min(end, max_source_seconds))
            for start, end in candidates
            if start < max_source_seconds and min(end, max_source_seconds) > start
        ]

    chosen: list[tuple[float, float]] = []
    budget = 0.0
    for start, end in candidates:
        proposed = _merge_time_ranges([*chosen, (start, end)])
        proposed_budget = sum(range_end - range_start for range_start, range_end in proposed)
        if chosen and proposed_budget > max_context_seconds:
            continue
        chosen = proposed
        budget = proposed_budget
    merged = chosen
    selected = [
        row
        for row in transcript
        if any(
            (_row_float(row, "end_seconds") or 0.0) >= start
            and (_row_float(row, "start_seconds") or 0.0) <= end
            for start, end in merged
        )
    ]
    if not selected:
        selected = transcript[: min(len(transcript), 500)]
        merged = []
    _write_csv_rows(context_csv, fields, selected)
    return {
        "strategy": "hotspots-sc-keywords" if mode in {"narrative", "mixed"} else "bounded-context",
        "original_rows": original_row_count,
        "rows_within_source": len(transcript),
        "selected_rows": len(selected),
        "source_duration_seconds": max_source_seconds,
        "context_padding_seconds": {"before": 120, "after": 180},
        "selected_unique_seconds": round(budget, 3),
        "original_bytes": source_csv.stat().st_size,
        "selected_bytes": context_csv.stat().st_size,
        "fields": fields,
        "omitted_fields": [field for field in source_fields if field not in fields],
        "time_ranges": [
            {"start_seconds": round(start, 3), "end_seconds": round(end, 3)}
            for start, end in merged
        ],
    }


def write_selection_signal_subset(
    source: Path,
    target: Path,
    *,
    limit: int = 20,
    max_source_seconds: float | None = None,
) -> None:
    fields, rows = _read_csv_rows(source)
    if not fields:
        shutil.copy2(source, target)
        return
    if max_source_seconds is not None:
        time_fields = (
            ("aligned_time_seconds", "source_time_seconds")
            if source.name == "superchats.csv"
            else ("start_seconds", "inspect_start")
        )
        rows = [
            row
            for row in rows
            if (_row_float(row, *time_fields) or 0.0) < max_source_seconds
        ]
    if source.name == "superchats.csv":
        rows = sorted(rows, key=lambda row: _row_float(row, "price") or 0.0, reverse=True)[:limit]
        rows.sort(key=lambda row: _row_float(row, "aligned_time_seconds", "source_time_seconds") or 0.0)
    else:
        rows = rows[:limit]
    _write_csv_rows(target, fields, rows)


def campaign_selection_instructions(config: dict[str, Any]) -> str:
    """Load a matched campaign's file-backed Flow 2 selection instructions."""
    configured = str(config.get("campaign_selection_prompt") or "").strip()
    if not configured:
        return ""
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = SKILL_ROOT / candidate
    resolved = candidate.resolve()
    references_root = (SKILL_ROOT / "references").resolve()
    try:
        resolved.relative_to(references_root)
    except ValueError as exc:
        raise WorkflowError(
            f"活动选片 Prompt 必须位于 references 目录：{resolved}"
        ) from exc
    if not resolved.is_file():
        raise WorkflowError(f"活动选片 Prompt 不存在：{resolved}")
    content = resolved.read_text(encoding="utf-8-sig").strip()
    if not content:
        raise WorkflowError(f"活动选片 Prompt 为空：{resolved}")
    campaign_name = str(config.get("campaign_name") or "本场活动").strip()
    return (
        f"\n## 本场活动专属选片规则：{campaign_name}\n\n"
        f"{content}\n"
    )


def build_selection_prompt(state_path: Path, state: dict[str, Any]) -> tuple[Path, Path, Path]:
    selection_dir = state_path.parent / "selection"
    selection_dir.mkdir(parents=True, exist_ok=True)
    source_csv = transcript_csv(state_path, state)
    context_csv = selection_dir / "selection-context.csv"
    engagement = state_path.parent / "analysis" / "engagement"
    source_duration: float | None = None
    try:
        from content_slicer import media_duration

        source_duration = media_duration(Path(state["config"]["source"]))
    except (OSError, RuntimeError, ValueError):
        pass
    mode = state["config"]["mode"]
    context_stats = build_compact_selection_context(
        source_csv,
        engagement,
        context_csv,
        mode=mode,
        max_source_seconds=source_duration,
    )
    stats_path = selection_dir / "selection-context-stats.json"
    atomic_json(stats_path, context_stats)
    attachments = [context_csv.name, stats_path.name]
    for name in ("engagement-hotspots.csv", "superchats.csv", "engagement-summary.json"):
        source = engagement / name
        if source.is_file():
            target = selection_dir / name
            if source.suffix.lower() == ".csv":
                write_selection_signal_subset(
                    source, target, max_source_seconds=source_duration
                )
            else:
                shutil.copy2(source, target)
            attachments.append(target.name)

    profile = load_profiles()[state["config"]["creator"]]
    fixed_publish_tags = [
        str(value).strip()
        for value in [
            *profile.get("upload", {}).get("tags", []),
            *state.get("config", {}).get("campaign_tags", []),
        ]
        if str(value).strip()
    ]
    subject_name = next(
        (word for word in profile.get("hotwords", []) if re.search(r"[\u4e00-\u9fff]", word)),
        profile["display_name"],
    )
    available_people = [
        str(value.get("display_name") or key)
        for key, value in load_profiles().items()
        if not value.get("archived")
    ]
    selection_notes = "\n".join(
        str(note).strip()
        for note in profile.get("selection_notes", [])
        if str(note).strip()
    )
    if selection_notes:
        selection_notes = f"\n主播专属补充：\n{selection_notes}\n"
    campaign_instructions = ""
    cover_copy_instructions = ""
    if mode in {"narrative", "mixed"}:
        campaign_instructions = campaign_selection_instructions(state.get("config", {}))
        cover_copy_instructions = (
            SKILL_ROOT / "references" / "narrative-cover-prompt.md"
        ).read_text(encoding="utf-8").strip()
    example_item = {
        "标题": f"{subject_name}说要露出肚皮道歉，低头才发现自己早就露着",
        "副标题": "准备露肚皮道歉｜低头一看，省了一步",
        "时间戳": [{"开始": "00:12:34.500", "结束": "00:13:20.000"}],
        "标签": ["具体人物", "作品或产品", "核心话题", "关系或动作", "事件类型"],
        "标签依据": "具体人物=12:38直接提及；作品或产品=12:44直接提及；其余标签=12:34-13:20围绕该事件展开",
        "多人类型": "单人",
        "参与者": [subject_name],
        "说话人依据": "选段中只能确认主播发言",
        "嘉宾台词已核实": False,
        "vr_topic": "",
    }
    song_example = {
        "标题": "完整歌名现场演唱",
        "副标题": "完整歌名",
        "时间戳": [{"开始": "00:24:10.000", "结束": "00:28:35.000"}],
        "标签": ["完整歌名", "原唱或作品", "翻唱", "歌切", "演唱语言"],
        "标签依据": "完整歌名=24:05报歌名；原唱或作品=上下文明确；翻唱、歌切、演唱语言=24:10-28:35连续演唱",
        "多人类型": "单人",
        "参与者": [subject_name],
        "说话人依据": "选段中只能确认主播演唱",
        "嘉宾台词已核实": False,
        "vr_topic": "",
    }

    if mode == "narrative":
        mode_text = "普通叙事切片"
        selection_goal = (
            "从热点候选上下文中找“陌生观众看到标题也会点开”的独立事件。"
            "弹幕流速只负责发现候选；SC 要连同它引导的主播回答一起判断，不能只截感谢。"
        )
        limit_rule = "输出全部达到标准且互不重复的候选；数量由本场有效内容自然决定，长直播可以多、弱内容可以少或为空，不得预设固定条数"
        duration_rule = "普通切片默认 60–300 秒；只有同一次连续交流在 30–60 秒内已经明确包含起因、变化和收尾时才可更短"
        subtitle_rule = (
            "普通切片副标题按下方专节写作；用 2–3 句完整表达具体事件与观众吐槽，每句 3–22 个有效文字，句间用｜分隔"
        )
        output_payload: Any = [example_item]
        template_payload: Any = [
            {
                "标题": "",
                "副标题": example_item["副标题"],
                "时间戳": [{"开始": "00:00:00.000", "结束": "00:00:30.000"}],
                "标签": ["", "", "", "", ""],
                "标签依据": "标签=时间戳与原文依据；标签=时间戳与原文依据",
                "多人类型": "单人",
                "参与者": [subject_name],
                "说话人依据": "",
                "嘉宾台词已核实": False,
                "vr_topic": "",
            }
        ]
    elif mode == "song":
        mode_text = "歌切"
        selection_goal = (
            "寻找连续演唱 attempt。优先结合报歌名、歌词重复段、伴奏持续区、重来／再来／错了"
            "等重启提示；弹幕和 SC 只能帮助定位，不能证明歌名或边界。"
        )
        limit_rule = "输出全部达到标准且互不重复的候选；数量由本场有效内容自然决定，长直播可以多、弱内容可以少或为空，不得预设固定条数"
        duration_rule = (
            "每条至少 10 秒，优先完整一首；从正式开唱或必要前奏开始，到最后尾音和伴奏释放后结束。"
            "不得把失败前半段与重开后的演唱拼在一起；只有短版时标题必须如实写“片段／一段／短版”"
        )
        subtitle_rule = "副标题只写完整歌名，1–24 字，不使用分隔符"
        output_payload = [song_example]
        template_payload = [
            {
                "标题": "",
                "副标题": "完整歌名",
                "时间戳": [{"开始": "00:00:00.000", "结束": "00:03:30.000"}],
                "标签": ["", "", "", "", ""],
                "标签依据": "标签=时间戳与原文依据；标签=时间戳与原文依据",
                "多人类型": "单人",
                "参与者": [subject_name],
                "说话人依据": "",
                "嘉宾台词已核实": False,
                "vr_topic": "",
            }
        ]
    else:
        mode_text = "混合切片（普通切片 + 歌切）"
        selection_goal = (
            "在同一次通读中同时寻找普通叙事爆点与连续演唱 attempt。普通切片按陌生观众可读的"
            "完整事件判断；歌切结合报歌名、歌词重复段、伴奏持续区和重启提示判断。弹幕流速、SC"
            "和音乐迹象都只负责发现候选，不能代替内容与边界核验。"
        )
        limit_rule = "普通切片和歌切都输出全部达到标准且互不重复的候选；数量由本场有效内容自然决定，不预设每类或总计条数"
        duration_rule = (
            "普通切片默认 60–300 秒，只有同一次连续交流在 30–60 秒内已包含起因、变化和收尾时才可更短；"
            "歌切至少 10 秒并优先完整一首，从必要前奏／正式开唱到最后尾音和伴奏释放。"
            "歌切不得跨越失败重启，短版必须明确标注"
        )
        subtitle_rule = (
            "普通切片副标题按下方专节写作；用 2–3 句完整表达具体事件与观众吐槽，每句 3–22 个有效文字，句间用｜分隔。"
            "歌切副标题仍只写完整歌名，1–24 字"
        )
        output_payload = {"普通切片": [example_item], "歌切": [song_example]}
        template_payload = {
            "普通切片": [
                {
                    "标题": "",
                    "副标题": example_item["副标题"],
                    "时间戳": [{"开始": "00:00:00.000", "结束": "00:00:30.000"}],
                    "标签": ["", "", "", "", ""],
                    "标签依据": "",
                    "多人类型": "单人",
                    "参与者": [subject_name],
                    "说话人依据": "",
                    "嘉宾台词已核实": False,
                    "vr_topic": "",
                }
            ],
            "歌切": [
                {
                    "标题": "",
                    "副标题": "完整歌名",
                    "时间戳": [{"开始": "00:00:00.000", "结束": "00:03:30.000"}],
                    "标签": ["", "", "", "", ""],
                    "标签依据": "",
                    "多人类型": "单人",
                    "参与者": [subject_name],
                    "说话人依据": "",
                    "嘉宾台词已核实": False,
                    "vr_topic": "",
                }
            ],
        }

    def add_structure_examples(payload):
        groups = payload.values() if isinstance(payload, dict) else [payload]
        for group in groups:
            for example in group:
                example["叙事结构"] = {"类型": "顺叙", "段落角色": ["正文"], "转场秒数": 0.5,
                                     "理由": "事件按因果顺序展开，前置结果不会增加必要悬念"}
    add_structure_examples(output_payload)
    add_structure_examples(template_payload)
    output_example = json.dumps(output_payload, ensure_ascii=False, indent=2)
    prompt = f"""# 直播选片任务

本批材料文件：{', '.join(attachments)}。若调用方已内嵌完整材料，直接分析内嵌数据，不要再次调用命令读取文件；否则只读取上述文件，不要搜索、列举或读取其他目录。
主播：{profile['display_name']}。模式：{mode_text}。CSV 使用整场录播原始时间。
已配置人物（只能辅助拼写，不能代替语音证据）：{", ".join(available_people)}。

## 先判断值不值得切

{selection_goal}
弹幕峰值、SC、关键词和转写中的怪话只负责告诉你“去哪里看”，它们本身不是入选理由。先按时间把 selection-context.csv 划成若干同一话题／同一因果链的故事岛，再从故事岛中选片；不要从孤立金句反推一个并不存在的故事。游戏直播按“当前讨论的问题／一次具体反应／一段连续笑点”切成更小的故事岛；游戏进程连续不代表话题连续，队友闲聊、突然读礼物、无因果关系的弹幕问答和下一件事都不能混进同一条。
{campaign_instructions}

值得切的普通片段必须让中立陌生观众在前 3–8 秒知道人物或问题，并且至少有一种真实内容强度：主张撞上现实、计划产生后果、自信翻车、自曝或意外信息、冲突／尴尬升级、明确怪话带来后续反应、对具体人事物的鲜明点评、SC 问题引出有变化的回答。故事中还必须出现新的信息、态度变化、升级或反应，最后有可以复述的落点。

以下内容即使容易写标题也不值得切：只有一个有趣话题但主播只是平铺直叙；普通问答或普通观点；只有可爱、夸奖、陪伴感或粉丝滤镜；没有后续反应的孤立怪话；同一意思反复说但没有升级；只有弹幕快、SC 金额高或关键词敏感；必须知道直播梗或人物关系才看得懂；靠补标题才能显得有反转。短笑话可以入选，但必须在自身范围内完整包含铺垫、转折和笑点，不能只是问题或半句回答。

普通爆点优先：真实反转、自爆、失败、尴尬、冲突、递进笑点、性暗示／双关、主播点评人与事、强烈反应、能单独引用的怪话或结论。
{selection_notes}
硬排除：外卖口令、外卖优惠口令、神秘数字、优惠码、兑换码及围绕这些口令的记忆／复述内容；即使弹幕很快也不得选入。
歌曲优先：一次连续且边界完整的演唱；转写歌词可能错误或缺失，不能据此编造歌名和歌词。

在内部按 1–5 分评估故事岛；保留 3–5 分，4–5 分优先。3 分只要已有直接证据、独立钩子和可理解的首尾，也应输出给审核台继续判断，不要因为它不是长篇四段式故事就提前丢弃；评分不要写进 JSON。普通切片必须满足事件或交流具体、爆点／态度／结论清楚、转写有直接证据、陌生观众无需直播背景也能理解。歌切必须有足够证据定位同一次连续演唱。{limit_rule}；没有就输出对应空数组，不用拿弱素材凑数。

## 把短爆点扩成完整叙事

1. 先标出真正吸引人的核心时刻，不要立刻把热点窗口当成成片范围。
2. 沿同一话题向前找：谁在说、起因是什么、问题／主张／计划从哪里开始。找到陌生观众能独立进入故事的最早完整句；再往前已经是另一话题时停止。
3. 沿同一话题向后找：回答如何发展、是否出现反驳／升级／结果／反应／补充限定。保留到最后一个真正完成当前意思的落点；后面明确换题时停止。
4. 核心时刻不足 90 秒时，必须实际查看它前至少 120 秒、后至少 180 秒的上下文，再判断哪些句子属于同一因果链。需要的是语义边界，不是机械把前后时长全部塞进成片。
5. 用一句话复述完整故事：起因 → 变化／升级 → 结果或反应。如果仍只能复述成“主播聊了 X”或只有问题没有落点，就淘汰；不得从另一个话题拼结果，也不得靠标题制造素材里没有的因果。
6. 只有故事岛确认完整且值得切后，才输出覆盖完整叙事的最短范围；顺叙默认按原时间升序，只有符合下一节“倒叙与回调”的片段才按最终播放顺序输出非升序范围。

## 倒叙与跨场回调

1. 每个候选都主动比较顺叙与倒叙的可读性，并在叙事结构的理由中说明选择。后段完整反应、后果、反转或回扣能先提出一个真实问题，而较早片段恰能回答时，可以倒叙；不要求主播必须说“之前”才允许。后段冷开场 → 最早且最短必要前情 → 回到后段继续到落点；不得跨事件制造因果。
2. 倒叙时间戳按“最终播放顺序”填写，可以不是数字升序。第一段冷开场通常只保留 3–12 秒完整钩子；第二段交代早先起因；最后一段从冷开场之后继续到真实反应或结果。各段不得重叠、不得重复播放同一句，也不得跨话题拼笑点。
3. 如果主播在当前录播中明确提到“上次直播／前几天／之前那场”的事件，可把这次提及当作跨场 callback 候选。只有当前素材自己已经讲清前情和本次的新变化时才入选，标题可写“上次……，这次……”；旧场视频不在当前 selection 文件夹时，不得伪造旧场时间戳或假装成片包含旧画面。
4. 如果同一场录播较早位置确有被回扣的原事件，可以按第 1 条加入实际旧片段；如果只是提到另一场直播，则保留当前口述，不跨项目搜索或擅自拼接素材。
5. 每项 JSON 都输出“叙事结构”：类型为“顺叙”或“倒叙”，段落角色数组与时间戳逐项对应，角色为“冷开场／前情／回归／结果／正文”，转场秒数为 0.5，理由说明为何当前素材更适合所选结构。倒叙示例：叙事结构={{"类型":"倒叙","段落角色":["冷开场","前情","回归"],"转场秒数":0.5,"理由":"先展示失败反应提出悬念，前情解释原因，回归保留后续结果"}}。时间戳按最终播放顺序填写，不反放任何段落。切片机读取该字段后会在冷开场→前情、前情→回归的非顺叙跳点自动加入总长约0.5秒的渐进高斯模糊，音频与成片时长不变；不要在时间戳中另加转场占位或重复对白。
6. 歌切保持顺叙，不使用倒叙；除非是同一演唱中主播明确停止、解释并从头重唱，此时仍只保留最终完整的一次，不做闪回。

## 多人直播与同步试听素材

1. 只有成片范围内确实保留另一人的可听台词，才标为“多人连麦”；原视频／节目中的人物发言且主播是在观看和反应时标为“同步试听”。只提到名字、画面出现头像、房间标题写着联动、弹幕提及或无法确定的声音，均不能直接当作嘉宾。
2. “参与者”先写主播，再写确实有可听台词的人物。已配置人物列表只用于统一名字拼写；列表之外的人物允许按素材原文填写，但必须在“说话人依据”给出时间和原句事实。
3. 无可靠说话人证据时保持“单人”；若能判断存在第二说话人但无法确认身份，可标“多人连麦”、参与者写“待确认嘉宾”，并明确说明疑点。不得猜人。
4. “嘉宾台词已核实”在模型阶段必须始终为 false；审核台会让人工逐句指定说话人。只有人工确认后才会在封面叠加嘉宾人物图。
5. 多人连麦标题必须让陌生观众看清“谁与谁发生了什么”，涉及双方发言时使用双方中文简称，不得用她／他代替；描述和 Tag 也只能写实际保留台词的人物。
6. 同步试听／reaction 中，原视频已有清晰原生字幕时，后续 ASS 只负责主播反应；标题必须写清“A 看 B 谈 X”的关系，不能把被观看者的话归给主播。

## 剪辑门禁

1. {duration_rule}。
2. 时间戳落在完整句意、歌词或音乐边界，结尾在最后音节后留 0.2–0.5 秒。时间戳数组必须按最终成片播放顺序填写：普通顺叙为升序，合格倒叙允许非升序。多个时间段必须属于同一事件或同一次连续演唱；普通事件相隔超过 180 秒时，除非后段明确回扣，否则放弃。歌切默认使用一个连续范围。
3. 普通切片优先使用最短的连续顺叙范围，不得为了“更刺激”随意前置爆点。两句之间超过约 2.5 秒且没有表情、动作或因果价值的空白应删掉；哪怕岔题不足 15 秒，只要与当前问题、反应或笑点没有语义关系也必须剔除，可用多个按原顺序的时间段绕开。不要为了审核方便额外保留首尾把手，也不要输出同一事件的改名版。审核台可继续沿整场源视频精剪，但查看用上下文不得自动写入成片范围。
4. 删除普通礼物感谢、无关岔题、重复解释和空转。只有原句同时具有感谢／收到／送达等收款语境，或能与附近 SC 事件核对时，才把钢镚、SC、Super Chat、醒目留言及常见同音误识别写成“（谢谢SC）”；普通讨论、游戏名、英文词或无法确认的同音词不得强行改写。确认是 SC 时一律只写“（谢谢SC）”，不保留发送者 ID、金额和重复的“谢谢／感谢”，但 SC 正文与主播针对正文的回答必须完整保留。
5. 普通切片必须有“可理解的前提／问题”和“明确的变化、态度、笑点、回答或落点”，但不强制同时具备背景、起因、升级、结果四个长篇环节。一个短交流只要首尾自洽、有直接证据并能独立成立，就应保留；不要因缺少长背景而淘汰。边界确实悬空时，再阅读前至少 120 秒和后至少 180 秒，把属于同一话题的必要原句扩入成片，不能只留在审核代理的上下文里；不得从另一个话题拼接虚假收尾。
6. 输出前单独检查所选范围的第一条和最后一条转写：首句必须让陌生观众知道代词和话题指什么，不能从无指代的“然后／所以／但是／他／这个／那”半路开场；末句不能停在尚未回答的问题、即将开始的解释、未列完的选项或“原因就是／要么就是／后来我就”等开放句。发现开放边界时继续向前或向后扩，直到出现自洽开场和明确落点。
7. 标题中的具体人、事、动作和结果必须能在所选时间范围正文中找到直接证据；严禁拿相邻但未选入的内容、另一个时间段的话题或模型猜测来写标题。

## 标题与封面文案

标题必须准确，但先把它写成观众会点开的 B 站切片标题，而不是会议纪要。优先使用四种结构：素材中的可引用原话或怪话；带具体对象的问题；人物关系中的误会／冲突；一个动作紧接着真实反差或后果。不要机械套用“{subject_name}解释 A，结果 B”，也不强制出现“结果／最后／却”等连接词。
程序会自动补主播标签，但标签不能代替句子主语。标题叙述只要需要指向主播的主语或代词，就必须写中文简称“{subject_name}”，不得用“她”“他”“TA”或泛称“主播”替代；自然省略主语且不会产生歧义时可以不硬加。多人标题写实际发生关系的双方中文简称。普通切片副标题按下方专节处理原话口吻、主语和节奏。不得使用“通过完整叙事”“主播聊了某事”“精彩切片”等占位话。歌切不得猜测素材没有证据的歌名。
标题只概括已经确认完整的故事岛，不负责拯救弱素材。具体数字、人物、作品、游戏、身体动作、生活经历和被回扣的旧事都比“破防、震惊、爆笑、太可爱”这类空评价更有点击依据；情绪词只能放大已经存在的事实，不能替代事实。

在输出 JSON 前先在内部做三遍处理（不要把过程写进输出）：
1. 逐个故事岛记录核心时刻、起因、发展、落点、可引用原话、是否回扣更早事件、首尾边界。
2. 为每个入选项各写三个标题草案：原话型、具体事件型、反差／后果型；选择事实最具体、陌生观众最易懂的一条，避免所有标题长成同一个模板。
3. 单独复核时间戳播放顺序、标题事实、封面 2–3 句合读是否清楚、具体且有吸引点、是否反复套用同一句式、自动换行后是否易读、emoji 是否语义匹配、五个 Tag 及其依据。

只学习下面这些公开切片常见的“结构”，不能照抄，也不能捏造素材中没有的事实：
- 原话加自证：几个月没洗澡，但{subject_name}坚持说脚不臭
- 疑问加反差：{subject_name}说要露肚皮道歉，低头才发现已经露着
- 明确对象加动作：{subject_name}看高手操作，现场学会什么叫闪屏
- 具体经历：{subject_name}靠写稿承担自己和妹妹的学费
- 合法回调：上次还立下保证，{subject_name}这次开播第一句就改口

标题允许是简洁的钩子、问题、怪话或状态变化，不要求写成“起因—结果”长句，但必须是独立可读的完整表达；不能是半句、纯情绪词或占位语。标题不超过 80 字，不使用英文单引号。{subtitle_rule}。

{cover_copy_instructions}

## Tag 生成门禁

程序会在发布时自动加入固定标签：{', '.join(fixed_publish_tags)}。这批固定标签不得再次写入“标签”，也不要用“主播、直播、切片、虚拟主播、VUP、VirtuaReal、杂谈、娱乐、音乐”等泛词占满名额。

每条必须输出恰好 5 个互不重复的片段专属 Tag：
1. 先写 1–2 个正文中真实出现的可搜索实体，例如人物、作品、游戏、歌名、平台、地点或事件；再写 1–2 个核心话题／关系／动作名词；最后仅在确有帮助时写 1 个内容类型词，如“歌切、翻唱、SC回应、游戏切片”。
2. 优先使用 B 站用户会直接搜索的规范名称。中文 Tag 通常 2–8 字；人名、歌名、游戏名、英文专名可更长。Tag 必须是名词或短名词短语，不得把标题截成半句话。
3. 禁止完整句、超过 12 字的情绪化标题碎片、杜撰热梗、无证据蹭热门人物、同义重复和纯评价词。例如“太搞笑了、当场破防了、精彩切片、直播日常、主播锐评”都不能用来凑数。
4. 普通切片优先覆盖“具体实体 + 核心话题 + 关系／动作 + 明确事件”；歌切优先覆盖“完整歌名 + 原唱／作品（有证据时）+ 翻唱 + 歌切 + 语言／曲风（能确认时）”。不确定歌名、人物或作品时宁可放弃该候选，禁止猜测。
5. “标签依据”必须用“Tag=时间戳和原文事实”的形式逐项或合并说明，5 个 Tag 都要能追溯到所选正文、报歌名、画面／直播主题或连续演唱事实。
6. 昼夜_Chilly 不属于 VirtuaReal，不能把 VR 当作其身份标签。只有保留的正文直接讨论 VR 时，才可使用相关话题 Tag，并把 vr_topic 设为 "yes"，在标签依据中写明对应原文和时间；其他情况 vr_topic 必须为空字符串。

好例：刘备文学、老福特、绿色网站、粉色网站、网络文学；Snow Halation、Love Live!、hololive、认错歌曲、口误；歌切可用“酒醉的蝴蝶、翻唱、歌切、女声、中文歌”。
坏例：枝堇Sumire、VirtuaReal、虚拟主播、直播切片（已自动加入）；“转头就把网站全列了出来”“当场喊救命”“太好笑了”（句子或纯评价）。

## 唯一允许的输出格式

只输出以下 JSON，不要 Markdown、解释、评分或额外字段。每个片段必须包含“标题”“副标题”“时间戳”“标签”“标签依据”“多人类型”“参与者”“说话人依据”“嘉宾台词已核实”“vr_topic”“叙事结构”十一个业务字段；歌切的叙事结构固定为顺叙：
{output_example}

没有合格内容时，普通／歌切模式输出 []；混合模式保留两个键并把对应数组写成 []。不要为了凑数降低标准。
"""
    prompt_path = selection_dir / "selection-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    schema_path = selection_dir / "selection-template.json"
    atomic_json(schema_path, template_payload)
    return prompt_path, context_csv, schema_path

EXECUTABLE_PLAN_VERSION = 4


class SubtitleTimingNeedsReview(WorkflowError):
    """A timing contract could not be proven; do not blindly retry rendering."""


def prepare_executable_plan(state_path: Path, state: dict[str, Any], items=None) -> dict[str, Any]:
    """Flow2 approval and Flow3 export share exactly this final, verified plan."""
    from authoritative_alignment import align_crossed_rows, AlignmentError, ALIGNMENT_VERSION, MODEL_REVISION
    from content_slicer import load_authoritative_segments, EditRange, reject_gift_overlaps
    project = state_path.parent
    items = load_canonical_selection(state) if items is None else items
    canonical_csv, canonical_json, completeness, speech = authoritative_transcript_files(state_path, state)
    gift = project / "analysis/transcript/gift-acknowledgements.csv"
    source = Path(state["config"]["source"])
    signature = [str(source.resolve()), source.stat().st_size, source.stat().st_mtime_ns]
    inputs = [canonical_csv, canonical_json, completeness, speech, gift]
    digest = digest_paths(inputs, extra=[EXECUTABLE_PLAN_VERSION, ALIGNMENT_VERSION, MODEL_REVISION, signature, items])
    root = project / "selection/executable-plans" / digest
    record = root / "plan.json"
    if record.is_file():
        try:
            cached = json.loads(record.read_text(encoding="utf-8-sig"))
            transcript = Path(cached["transcript_json"])
            seal = digest_paths([transcript], extra=cached["items"])
            if cached["input_digest"] == digest and cached["output_digest"] == seal:
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            pass
    plan_items, boundary_audit, trim_audit = copy.deepcopy(items), [], []
    if state["config"]["mode"] != "song":
        plan_items, boundary_audit = refine_narrative_edit_ranges(plan_items, canonical_csv)
        if gift.is_file():
            plan_items, trim_audit = trim_items_around_gift_acknowledgements(plan_items, gift)
    segments = load_authoritative_segments(canonical_json, canonical_csv)
    try:
        aligned, timing_audit = align_crossed_rows(source, segments, plan_items,
            project / "analysis/transcript/word-alignment-cache")
        plan_items, edge_audit = align_authoritative_edit_ranges(plan_items, aligned)
        parts = [EditRange(item["clip_id"], i, span["start_seconds"], span["end_seconds"], "", "", "", "")
                 for item in plan_items for i, span in enumerate(item["timestamps"], 1)]
        reject_gift_overlaps(parts, gift if gift.is_file() else None)
    except (AlignmentError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SubtitleTimingNeedsReview(f"字幕时间对齐需要处理：{exc}；保留选片，修复时间轴后可继续") from exc
    root.mkdir(parents=True, exist_ok=True)
    aligned_json = root / "transcript.json"
    atomic_json(aligned_json, dict(authoritative=True, origin="flow1-text-with-acoustic-word-alignment",
        source=str(source), source_signature=signature, segments=aligned,
        hotword_guard_terms=json.loads(canonical_json.read_text(encoding="utf-8-sig")).get("hotword_guard_terms", [])))
    prepared = dict(version=EXECUTABLE_PLAN_VERSION, input_digest=digest,
        items=plan_items, transcript_json=str(aligned_json),
        output_digest=digest_paths([aligned_json], extra=plan_items),
        boundary_audit=boundary_audit, gift_audit=trim_audit,
        timing_audit=timing_audit, edge_audit=edge_audit, record=str(record))
    atomic_json(record, prepared)
    return prepared


def import_selection(state_path: Path, state: dict[str, Any], source_json: Path) -> Path:
    source_json = source_json.expanduser().resolve()
    if not source_json.is_file():
        raise WorkflowError(f"选片 JSON 不存在：{source_json}")
    payload = json.loads(source_json.read_text(encoding="utf-8-sig"))
    items = normalize_selection(payload, state["config"]["creator"], state["config"]["mode"])
    # Do not stamp an executable approval before timing is actually verified.
    prepared = None
    if items and (state_path.parent / "analysis/transcript/transcript-completeness.json").is_file():
        prepared = prepare_executable_plan(state_path, state, items)
    selection_dir = state_path.parent / "selection"
    selection_dir.mkdir(parents=True, exist_ok=True)
    target = selection_dir / "selection.json"
    atomic_json(target, canonical_selection(items, state["config"]["mode"]))
    state["selection_file"] = str(target)
    state.setdefault("approvals", {})["editorial"] = {
        "digest": digest_paths([target], extra=state["config"]),
        "approved_at": now_iso(),
    }
    if prepared:
        state["approvals"]["editorial"].update(
            executable_plan=prepared["record"], timing_digest=prepared["input_digest"]
        )
    state.pop("active_run_dir", None)
    state.pop("active_review_dir", None)
    state.pop("active_burn_source_dir", None)
    state.pop("radio_layout", None)
    state.pop("delivery_dir", None)
    state.pop("publish_preview_digest", None)
    save_project(state_path, state)
    return target


def append_manual_selection(
    project: Path | str,
    *,
    title: str,
    subtitle: str,
    timestamps: Sequence[dict[str, Any] | Sequence[Any] | str],
    content_type: str = "narrative",
    participants: Sequence[str] | str | None = None,
    collaboration_type: str = "single",
    speaker_evidence: str = "人工从整场录播预览创建，待在审核台逐句核实说话人",
    publish_tags: Sequence[str] | None = None,
    tag_evidence: str = "",
) -> tuple[Path, dict[str, Any]]:
    """Append one human-authored clip without discarding the previous review run."""
    state_path, state = load_project(project)
    mode = str(state["config"].get("mode") or "narrative")
    item_mode = str(content_type or "narrative").strip().lower()
    if item_mode not in {"narrative", "song"}:
        raise WorkflowError(f"未知手动切片类型：{content_type}")
    if mode != "mixed" and item_mode != mode:
        raise WorkflowError(f"当前项目模式是 {mode}，不能新增 {item_mode} 切片")

    raw: dict[str, Any] = {
        "标题": str(title).strip(),
        "副标题": str(subtitle).strip(),
        "时间戳": list(timestamps),
        "多人类型": collaboration_type,
        "参与者": participants or [],
        "说话人依据": str(speaker_evidence).strip(),
        "嘉宾台词已核实": False,
        "vr_topic": "",
    }
    if publish_tags:
        raw["标签"] = list(publish_tags)
        raw["标签依据"] = str(tag_evidence).strip()

    existing = load_canonical_selection(state)
    new_item = normalize_selection(
        [raw], state["config"]["creator"], item_mode
    )[0]
    new_item["content_type"] = item_mode
    combined = [*existing, new_item]
    for index, item in enumerate(combined, 1):
        item["clip_id"] = f"{index:03d}"

    selection_dir = state_path.parent / "selection"
    selection_dir.mkdir(parents=True, exist_ok=True)
    target = selection_dir / "selection.json"
    atomic_json(target, canonical_selection(combined, mode))
    state["selection_file"] = str(target)
    state.setdefault("approvals", {})["editorial"] = {
        "digest": digest_paths([target], extra=state["config"]),
        "approved_at": now_iso(),
        "source": "human-full-session-preview",
    }
    for flow in ("flow3", "flow4", "flow5"):
        state.setdefault("steps", {}).setdefault(flow, {}).update(
            {
                "status": "pending",
                "updated_at": now_iso(),
                "detail": "整场预览新增了人工切片，等待重新生成审核素材",
                "outputs": [],
            }
        )
    state.pop("delivery_dir", None)
    state.pop("publish_preview_digest", None)
    save_project(state_path, state)
    return target, new_item


def expand_narrative_review_ranges(
    items: Sequence[dict[str, Any]],
    transcript_path: Path,
    *,
    pre_seconds: float = 20.0,
    post_seconds: float = 30.0,
    maximum_total_seconds: float = 300.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give narrative review drafts safe story handles without another model call."""
    expanded = copy.deepcopy(list(items))
    _fields, transcript_rows = _read_csv_rows(transcript_path)
    transcript_end = 0.0
    for row in transcript_rows:
        value = _row_float(row, "end_seconds", "end")
        if value is not None:
            transcript_end = max(transcript_end, value)
    selected_end = max(
        (
            float(span["end_seconds"])
            for item in expanded
            for span in item.get("timestamps", [])
        ),
        default=0.0,
    )
    source_end = max(transcript_end, selected_end)
    audit: list[dict[str, Any]] = []
    for item in expanded:
        if item.get("content_type", "narrative") != "narrative":
            continue
        ranges = item.get("timestamps", [])
        if not ranges:
            continue
        total_before = sum(
            float(span["end_seconds"]) - float(span["start_seconds"])
            for span in ranges
        )
        capacity = max(0.0, maximum_total_seconds - total_before)
        first = min(ranges, key=lambda span: float(span["start_seconds"]))
        last = max(ranges, key=lambda span: float(span["end_seconds"]))
        pre_added = min(
            max(0.0, float(pre_seconds)),
            max(0.0, float(first["start_seconds"])),
            capacity,
        )
        first["start_seconds"] = float(first["start_seconds"]) - pre_added
        capacity -= pre_added
        post_added = min(
            max(0.0, float(post_seconds)),
            max(0.0, source_end - float(last["end_seconds"])),
            capacity,
        )
        last["end_seconds"] = float(last["end_seconds"]) + post_added
        if pre_added > 0.001 or post_added > 0.001:
            audit.append(
                {
                    "clip_id": item.get("clip_id", ""),
                    "pre_added_seconds": round(pre_added, 3),
                    "post_added_seconds": round(post_added, 3),
                    "before_seconds": round(total_before, 3),
                    "review_draft_seconds": round(
                        total_before + pre_added + post_added, 3
                    ),
                }
            )
    return expanded, audit


def write_narrative_boundary_audit(
    path: Path, rows: Sequence[dict[str, Any]]
) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "clip_id",
                "pre_added_seconds",
                "post_added_seconds",
                "before_seconds",
                "review_draft_seconds",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

def trim_items_around_gift_acknowledgements(
    items: Sequence[dict[str, Any]],
    gift_acks: Path,
    *,
    minimum_fragment_seconds: float = MIN_GIFT_TRIM_FRAGMENT_SECONDS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split selected ranges around gift thanks while preserving editorial order."""
    with gift_acks.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    excluded: list[tuple[float, float, str]] = []
    for row in rows:
        try:
            start = float(row.get("start_seconds", ""))
            end = float(row.get("end_seconds", ""))
        except (TypeError, ValueError):
            continue
        if end > start:
            excluded.append((max(0.0, start - 0.01), end + 0.01, row.get("text", "")))
    excluded.sort(key=lambda value: (value[0], value[1]))
    trimmed = copy.deepcopy(list(items))
    audit: list[dict[str, Any]] = []
    for item in trimmed:
        if item.get("content_type", "narrative") == "song":
            continue
        original_ranges = list(item["timestamps"])
        cleaned: list[dict[str, float]] = []
        for value in original_ranges:
            fragments = [(float(value["start_seconds"]), float(value["end_seconds"]))]
            for excluded_start, excluded_end, _text in excluded:
                next_fragments: list[tuple[float, float]] = []
                for start, end in fragments:
                    if excluded_end <= start or excluded_start >= end:
                        next_fragments.append((start, end))
                        continue
                    if excluded_start - start >= minimum_fragment_seconds:
                        next_fragments.append((start, min(end, excluded_start)))
                    if end - excluded_end >= minimum_fragment_seconds:
                        next_fragments.append((max(start, excluded_end), end))
                fragments = next_fragments
            cleaned.extend(
                {"start_seconds": start, "end_seconds": end}
                for start, end in fragments
                if end - start >= minimum_fragment_seconds
            )
        if not cleaned:
            raise WorkflowError(
                f"{item['clip_id']} 全部落在礼物感谢区间，无法自动生成编辑计划"
            )
        before = sum(
            float(value["end_seconds"]) - float(value["start_seconds"])
            for value in original_ranges
        )
        after = sum(value["end_seconds"] - value["start_seconds"] for value in cleaned)
        if item.get("narrative_structure"):
            try:
                item["narrative_structure"] = media_packaging.remap_narrative_structure(
                    item["narrative_structure"], original_ranges, cleaned,
                )
            except ValueError as exc:
                raise WorkflowError(f"{item['clip_id']}：{exc}") from exc
        item["timestamps"] = cleaned
        if before - after > 0.001:
            audit.append(
                {
                    "clip_id": item["clip_id"],
                    "original_seconds": round(before, 3),
                    "kept_seconds": round(after, 3),
                    "removed_seconds": round(before - after, 3),
                    "parts_after_trim": len(cleaned),
                }
            )
    return trimmed, audit


def write_gift_trim_audit(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "clip_id",
                "original_seconds",
                "kept_seconds",
                "removed_seconds",
                "parts_after_trim",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

def write_edit_plan(path: Path, items: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        for item in items:
            ranges = item["timestamps"]
            reverse = any(
                ranges[index]["start_seconds"] < ranges[index - 1]["start_seconds"]
                for index in range(1, len(ranges))
            )
            for order, value in enumerate(ranges, 1):
                if reverse and order == 1:
                    reason = "cold open"
                elif reverse and order == 2:
                    reason = "minimum setup"
                elif order == len(ranges):
                    reason = "payoff"
                else:
                    reason = "minimum setup"
                writer.writerow(
                    {
                        "slice_id": item["clip_id"],
                        "order": order,
                        "start_seconds": f"{value['start_seconds']:.3f}",
                        "end_seconds": f"{value['end_seconds']:.3f}",
                        "title": item["title"],
                        "outline": item["subtitle"],
                        "hook": item["cover_text_primary"],
                        "reason": reason,
                        "keep": "1",
                    }
                )


def effective_media_packaging(state: dict[str, Any], project_root: Path) -> dict[str, Any]:
    config = state.get("config", {})
    if "media_packaging" in config:
        return media_packaging.normalize_media_packaging(config["media_packaging"], project_root, check_files=True)
    return media_packaging.load_media_packaging(WORKSPACE_ROOT / "media-packaging.json", check_files=True)


def delivery_narrative_transitions(run_root: Path, review_dir: Path, rows: list[dict]) -> dict[str, list]:
    plans = {}
    plan = run_root / "edit-plan.csv"
    if plan.is_file():
        for part in copy_rows(plan):
            if str(part.get("keep", "")).lower() not in {"1", "true", "yes", "y", "keep"}:
                continue
            plans.setdefault(str(part["slice_id"]), []).append(part)
    selection_path = run_root / "narrative-structures.json"
    structures = json.loads(selection_path.read_text(encoding="utf-8-sig")) if selection_path.is_file() else {}
    result = {}
    for row in rows:
        name = Path(row["video"]).name
        if (row.get("content_type") or "narrative") == "song":
            result[name] = []
            continue
        metadata = review_workspace.review_proxy_metadata(review_dir / name)
        regions = metadata.get("regions") or []
        if regions:
            ranges = [(float(part["source_start"]), float(part["source_end"]))
                      for part in sorted(regions, key=lambda part: float(part["exact_start"]))]
        else:
            parts = sorted(plans.get(str(row.get("clip_id", "")), []), key=lambda part: int(part["order"]))
            ranges = [(float(part["start_seconds"]), float(part["end_seconds"])) for part in parts]
        structure = structures.get(str(row.get("clip_id", "")), {})
        source_ranges = structure.get("source_ranges") or []
        source_roles = structure.get("roles") or []
        roles = []
        for start, end in ranges:
            overlaps = [max(0.0, min(end, span["end_seconds"]) - max(start, span["start_seconds"]))
                        for span in source_ranges]
            best = max(range(len(overlaps)), key=overlaps.__getitem__) if overlaps else None
            roles.append(source_roles[best] if best is not None and overlaps[best] > 0 and best < len(source_roles) else "")
        transitions = media_packaging.narrative_transitions(
            ranges, roles=roles, duration=structure.get("transition_seconds", 0.5),
        )
        if transitions and not regions and review_workspace.timeline_backup_video_path(review_dir / name).is_file():
            raise WorkflowError(f"{name} 人工剪辑后缺少源时间映射，无法可靠定位倒叙转场；请重新保存剪辑时间轴")
        result[name] = transitions
    return result


def derive_tags(item: dict[str, Any], mode: str) -> list[str]:
    provided = item.get("publish_tags")
    if provided:
        return normalize_publish_tags(provided)
    item_mode = str(item.get("content_type") or mode).strip().lower()
    if item_mode not in {"narrative", "song"}:
        item_mode = "narrative"
    candidates = [item["cover_text_primary"], item["cover_text_secondary"]]
    body = re.sub(r"^【[^】]+】", "", item["title"])
    candidates.extend(
        value.strip("《》“”\" ")
        for value in re.split(r"[，。！？：；、｜|（）()]", body)
    )
    keyword_tags = [
        ("点评", "主播点评"), ("评价", "主播锐评"), ("破防", "破防现场"),
        ("红温", "红温现场"), ("边界感", "边界感"), ("老公", "关系玩笑"),
        ("暗示", "双关玩笑"), ("唱", "现场演唱"), ("歌", "歌曲翻唱"),
    ]
    candidates.extend(tag for keyword, tag in keyword_tags if keyword in body)
    candidates.extend(
        ["现场演唱", "歌曲翻唱", "直播歌回"]
        if item_mode == "song"
        else ["直播反转", "搞笑名场面", "主播锐评"]
    )
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tag = re.sub(r"\s+", "", candidate)
        if not 2 <= len(tag) <= 20:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
        if len(result) == 5:
            break
    if len(result) < 3:
        raise WorkflowError(f"无法为 {item['clip_id']} 生成三个片段专属 Tag")
    return result


def collaboration_description(item: dict[str, Any]) -> str:
    kind = str(item.get("collaboration_type") or "single")
    participants = [str(value).strip() for value in item.get("participants") or [] if str(value).strip()]
    if kind == "single" or len(participants) < 2:
        return ""
    host, guests = participants[0], "、".join(participants[1:])
    if kind == "reaction":
        relation = f"本片为{host}观看并回应{guests}相关内容。"
    else:
        relation = f"本片保留{host}与{guests}的现场对话。"
    evidence = str(item.get("speaker_evidence") or "").strip()
    return relation + (f"说话人依据：{evidence}" if evidence else "")

def write_copy_csv(
    path: Path,
    items: Sequence[dict[str, Any]],
    slices_csv: Path,
    mode: str,
    *,
    radio_layout: bool = False,
    campaign_tags: Sequence[str] = (),
) -> None:
    with slices_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        exported = {row["slice_id"]: row for row in csv.DictReader(handle)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COPY_FIELDS)
        writer.writeheader()
        for item in items:
            row = exported.get(item["clip_id"])
            if not row:
                raise WorkflowError(f"导出清单缺少 {item['clip_id']}")
            item_mode = str(item.get("content_type") or mode).strip().lower()
            if item_mode not in {"narrative", "song"}:
                item_mode = "narrative"
            tags = derive_tags(item, item_mode)
            item_campaign_tags = merge_campaign_tags(
                campaign_tags, item.get("title", "")
            )
            writer.writerow(
                {
                    "clip_id": item["clip_id"],
                    "video": row["file"],
                    "title": item["title"],
                    "content_type": item_mode,
                    "cover_mode": "song" if item_mode == "song" else "quote-impact",
                    "cover_source_region": "radio-left" if radio_layout else "auto",
                    "cover_time_seconds": "",
                    "cover_text_primary": item["cover_text_primary"],
                    "cover_text_secondary": item["cover_text_secondary"],
                    "cover_emotion": "自动判断",
                    "cover_emote": "",
                    "description": collaboration_description(item),
                    "tags": ",".join(tags),
                    "campaign_tags": ",".join(
                        str(value).strip() for value in item_campaign_tags
                        if str(value).strip()
                    ),
                    "tag_evidence": str(item.get("tag_evidence") or "").strip() or (
                        f"来自已审选片标题、副标题和时间范围；{item['clip_id']} "
                        + ", ".join(
                            f"{format_clock(value['start_seconds'])}-{format_clock(value['end_seconds'])}"
                            for value in item["timestamps"]
                        )
                    ),
                    "vr_topic": str(item.get("vr_topic") or ""),
                    "tid": "",
                    "reference_image": "",
                    "evidence_image": "",
                    "collaboration_type": item.get("collaboration_type", "single"),
                    "participants": ",".join(item.get("participants") or []),
                    "participant_profile_keys": ",".join(
                        item.get("participant_profile_keys") or []
                    ),
                    "speaker_evidence": str(item.get("speaker_evidence") or ""),
                    "guest_dialogue_verified": "0",
                    "guest_character_images": "",
                }
            )

def subtitle_axis_root(run_root: Path) -> Path:
    """Prefer the Flow1-derived subtitle axis while reading older runs compatibly."""
    authoritative = run_root / "authoritative-subtitles"
    legacy = run_root / "clip-local-asr"
    if authoritative.exists() or not legacy.exists():
        return authoritative
    return legacy

def subtitle_template(creator: str) -> Path:
    specific = SKILL_ROOT / "assets" / "subtitles" / f"narrative-{creator}-v2.ass"
    return specific if specific.is_file() else SKILL_ROOT / "assets" / "subtitles" / "narrative-v2.ass"


def subtitle_style(creator: str) -> str:
    profile = load_profiles().get(creator, {})
    configured = str(profile.get("subtitle_style", "")).strip()
    if configured:
        return configured
    return {
        "kioi": "Kioi",
        "sumire": "Sumire",
        "viridis": "Viridis",
        "yuchu": "Yuchu",
    }.get(creator, "Regular")


def create_lyric_review(asr_root: Path, clips: Sequence[Path], output: Path) -> None:
    rows: list[dict[str, str]] = []
    for clip in clips:
        transcript = asr_root / clip.stem / "transcript.csv"
        if not transcript.is_file():
            raise WorkflowError(f"歌切缺少 Flow1 权威字幕映射：{transcript}")
        with transcript.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                text = (row.get("text") or "").strip()
                if text:
                    rows.append(
                        {
                            "clip": clip.stem,
                            "start_seconds": row["start_seconds"],
                            "end_seconds": row["end_seconds"],
                            "recognized_text": text,
                            "corrected_text": "",
                            "reviewed": "no",
                            "timed_units": "",
                        }
                    )
    if not rows:
        raise WorkflowError("没有识别到可供人工校对的歌词行")
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def media_dimensions(path: Path) -> tuple[int, int] | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = run_checked_with_transient_retries(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        streams = json.loads(completed.stdout).get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        rotation = stream.get("tags", {}).get("rotate", 0)
        for item in stream.get("side_data_list", []):
            if item.get("rotation") is not None:
                rotation = item["rotation"]
                break
        try:
            if abs(int(float(rotation))) % 180 == 90:
                width, height = height, width
        except (TypeError, ValueError):
            pass
        return (width, height) if width > 0 and height > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return None


def should_use_radio_layout(path: Path) -> bool:
    if re.search(r"电台|radio", path.stem, re.IGNORECASE):
        return True
    dimensions = media_dimensions(path)
    return bool(dimensions and dimensions[1] >= dimensions[0])


def bundled_ffmpeg() -> Path:
    for folder in ("ffmpeg", "ffmpeg-bin"):
        candidate = WORKSPACE_ROOT / "tools" / folder / (
            "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        )
        if candidate.is_file():
            return candidate
    located = shutil.which("ffmpeg")
    if not located:
        raise WorkflowError("找不到 FFmpeg")
    return Path(located)


def write_radio_song_review_placeholder(path: Path, style: str, speaker: str) -> None:
    """Never expose raw song ASR as if it were reviewed lyrics."""
    source = path.read_text(encoding="utf-8-sig")
    retained = [line for line in source.splitlines() if not line.startswith("Dialogue:")]
    retained.append(
        f"Dialogue: 0,0:00:00.00,9:59:59.00,{style},{speaker},0,0,0,,"
        r"歌词待校对，流程4生成正式字幕"
    )
    path.write_text("\n".join(retained) + "\n", encoding="utf-8-sig")

def step1_transcribe(state_path: Path, state: dict[str, Any]) -> list[Path]:
    project_root = state_path.parent
    config = state["config"]
    transcript = project_root / "analysis" / "transcript"
    transcript.mkdir(parents=True, exist_ok=True)
    profile = load_profiles()[config["creator"]]
    hotwords = profile.get("hotwords", []) + profile.get("stream_hotwords", [])
    asr = config["asr"]
    speaker_args: tuple[object, ...] = ()
    if asr.get("speaker_count") is not None:
        speaker_args = ("--speaker-count", int(asr["speaker_count"]))
    multilingual = config["mode"] in {"song", "mixed"}
    if multilingual:
        command = script_command(
            "content_slicer.py", "transcribe-multilingual",
            "--input", config["source"], "--output", transcript,
            "--model", asr["model"], "--languages", "zh,ja,en",
            "--chunk-seconds", 60, "--device", asr["device"],
            "--compute-type", asr["compute_type"],
            "--initial-prompt", ",".join(hotwords),
            *speaker_args,
        )
        transcript_source = transcript / "transcript_multilingual.csv"
        outputs = [
            transcript_source,
            transcript / "transcript_multilingual.json",
            transcript / "transcript-completeness.json",
            transcript / "speech-activity.json",
            transcript / "transcript-confidence-review.csv",
        ]
    else:
        command = script_command(
            "content_slicer.py", "transcribe",
            "--input", config["source"], "--output", transcript,
            "--model", asr["model"], "--language", "zh",
            "--device", asr["device"], "--compute-type", asr["compute_type"],
            "--initial-prompt", ",".join(hotwords),
            *speaker_args,
        )
        transcript_source = transcript / "transcript.csv"
        outputs = [
            transcript_source,
            transcript / "transcript.json",
            transcript / "transcript-completeness.json",
            transcript / "speech-activity.json",
            transcript / "transcript-confidence-review.csv",
        ]
    run_external(project_root, "flow1-transcription", command)
    provenance = transcript / "qwen-local-asr-provenance.json"
    state["transcription_no_speech"] = (
        str(asr["model"]).startswith("local:qwen3-asr-")
        and provenance.is_file()
        and json.loads(provenance.read_text(encoding="utf-8-sig")).get("no_speech") is True
    )

    if config["mode"] in {"narrative", "mixed"}:
        run_external(
            project_root,
            "flow1-gift-filter",
            script_command(
                "content_slicer.py", "filter-gifts",
                "--transcript", transcript_source,
                "--output", transcript / "transcript.filtered.csv",
                "--removed-output", transcript / "gift-acknowledgements.csv",
            ),
        )
        outputs.extend(
            [transcript / "transcript.filtered.csv", transcript / "gift-acknowledgements.csv"]
        )
    xml = Path(config["xml"]) if config.get("xml") else None
    engagement = project_root / "analysis" / "engagement"
    command = script_command(
        "analyze_engagement.py", "--output", engagement,
        "--windows", "10,30,60", "--reaction-lag-max", 30,
        "--sc-response-window", 180,
    )
    if xml:
        command.extend(["--input", str(xml)])
    for keyword in DEFAULT_KEYWORDS:
        command.extend(["--keyword", keyword])
    # Refresh empty evidence too, so reruns cannot reuse stale chat peaks.
    run_external(project_root, "flow1-engagement", command)
    outputs.extend([
        engagement / "engagement-hotspots.csv",
        engagement / "engagement-summary.json",
        engagement / "superchats.csv",
    ])
    if xml and xml.is_file() and xml.stat().st_size > 0:
        annotated_transcript = transcript / "transcript.filtered.csv"
        if config["mode"] in {"narrative", "mixed"} and annotated_transcript.is_file():
            read_audit = transcript / "read-message-annotations.csv"
            run_external(
                project_root,
                "flow1-read-message-annotation",
                script_command(
                    "annotate_chat_reads.py",
                    "--transcript", annotated_transcript,
                    "--xml", xml,
                    "--output", annotated_transcript,
                    "--audit", read_audit,
                    "--window-seconds", 20,
                ),
            )
            outputs.append(read_audit)
    save_project(state_path, state)
    return outputs

def step3_prepare(state_path: Path, state: dict[str, Any]) -> tuple[Path, list[Path]]:
    project_root = state_path.parent
    config = state["config"]
    items = load_canonical_selection(state)
    canonical_csv, canonical_json, completeness_report, speech_json = (
        authoritative_transcript_files(state_path, state)
    )
    run_root = project_root / "runs" / f"prepare-{run_stamp()}"
    run_root.mkdir(parents=True, exist_ok=True)
    export_root = run_root / "export"
    clips_dir = export_root / "clips"
    asr_root = run_root / "authoritative-subtitles"
    plan = run_root / "edit-plan.csv"
    gift = project_root / "analysis" / "transcript" / "gift-acknowledgements.csv"
    radio_layout = should_use_radio_layout(Path(config["source"]))

    prepared = prepare_executable_plan(state_path, state, items)
    plan_items = prepared["items"]
    canonical_json = Path(prepared["transcript_json"])
    boundary_audit_path = run_root / "narrative-boundary-refinement.csv"
    write_narrative_refinement_audit(boundary_audit_path, prepared["boundary_audit"])
    trim_audit = prepared["gift_audit"]
    atomic_json(run_root / "subtitle-boundary-alignment.json", {
        "input_digest": prepared["input_digest"],
        "word_alignment": prepared["timing_audit"], "edge_trim": prepared["edge_audit"],
    })
    write_edit_plan(plan, plan_items)
    atomic_json(run_root / "narrative-structures.json", {
        item["clip_id"]: {**item.get("narrative_structure", {}), "source_ranges": item["timestamps"]}
        for item in plan_items
    })
    trim_audit_path = run_root / "gift-trim-audit.csv"
    write_gift_trim_audit(trim_audit_path, trim_audit)
    validate = script_command(
        "content_slicer.py", "validate-plan", "--plan", plan,
        "--audio", config["source"],
        "--tail-padding", FLOW3_TAIL_PADDING_SECONDS,
    )
    if gift.is_file():
        validate.extend(["--gift-acks", str(gift)])
    run_external(project_root, "flow3-plan-validation", validate)
    export = script_command(
        "content_slicer.py", "export", "--audio", config["source"],
        "--video", config["source"], "--plan", plan, "--output", export_root,
        "--tail-padding", FLOW3_TAIL_PADDING_SECONDS,
    )
    if gift.is_file():
        export.extend(["--gift-acks", str(gift)])
    run_external(project_root, "flow3-export", export)

    copy_csv = clips_dir / "titles-and-covers.csv"
    write_copy_csv(
        copy_csv,
        items,
        export_root / "slices.csv",
        config["mode"],
        radio_layout=radio_layout,
        campaign_tags=config.get("campaign_tags", []),
    )
    if config["mode"] != "song":
        run_external(
            project_root,
            "flow3-title-validation",
            script_command("validate_narrative_titles.py", "--warn-only", copy_csv),
        )
    run_external(
        project_root,
        "flow3-tag-validation",
        script_command(
            "validate_selection_tags.py", copy_csv,
            "--creator", config["creator"], "--profiles", PROFILES_PATH,
        ),
    )

    profile = load_profiles()[config["creator"]]
    canonical_command = script_command(
        "content_slicer.py", "slice-authoritative",
        "--transcript", canonical_csv,
        "--transcript-json", canonical_json,
        "--completeness-report", completeness_report,
        "--speech-json", speech_json,
        "--plan", plan,
        "--manifest", export_root / "slices.csv",
        "--output", asr_root,
        "--ass-output", clips_dir,
        "--ass-template", subtitle_template(config["creator"]),
        "--ass-style", subtitle_style(config["creator"]),
        "--ass-name", profile["display_name"],
        "--ass-font", profile.get("dialogue_font", "Microsoft YaHei"),
        "--ass-font-size", profile.get("dialogue_font_size", 85),
        "--ass-primary-color", profile.get(
            "subtitle_fill_color", profile.get("role_color", "#FFFFFF")
        ),
        "--ass-outline-color", profile.get("subtitle_outline_color", "#111318"),
        "--ass-outline-width", profile.get("subtitle_outline_width", 5.0),
        "--content-types-csv", copy_csv,
        "--tail-padding", FLOW3_TAIL_PADDING_SECONDS,
    )
    run_external(
        project_root, "flow3-authoritative-subtitles", canonical_command
    )

    clips = sorted(clips_dir.glob("*.mp4"))
    rows = copy_rows(copy_csv)
    content_types = {
        Path(row["video"]).name: (row.get("content_type") or "narrative").strip().lower()
        for row in rows
    }
    song_clips = [
        clip for clip in clips if content_types.get(clip.name, "narrative") == "song"
    ]
    if song_clips:
        create_lyric_review(asr_root, song_clips, run_root / "lyric-corrections.csv")

    # Review media must show the active audited glossary, not only apply it
    # later during Flow 4. This is especially important for radio reviews,
    # where Flow 3 burns the editable ASS into the right-hand information panel.
    for clip in clips:
        if content_types.get(clip.name, "narrative") == "song":
            continue
        review_ass = clips_dir / f"{clip.stem}.ass"
        if review_ass.is_file():
            review_workspace.normalize_paid_messages_ass(review_ass)

    burn_source_dir = clips_dir
    cover_render_dir = clips_dir
    cover_copy_csv = copy_csv
    if radio_layout:
        # The review surface for a vertical/radio clip must show the subtitles in
        # the right-hand panel. Keep a separate unburned layout master for Flow
        # 4 so an approved ASS can still be burned exactly once.
        layout_master_dir = export_root / "radio-layout-master"
        layout_dir = export_root / "radio-layout"
        run_external(
            project_root,
            "flow3-radio-layout",
            script_command(
                "render_vertical_radio_layout.py",
                "--input-dir", clips_dir,
                "--output-dir", layout_master_dir,
                "--ffmpeg", bundled_ffmpeg(),
                "--color", profile["role_color"],
                "--subtitle-fill-color", profile.get(
                    "subtitle_fill_color", profile.get("role_color", "#FFFFFF")
                ),
                "--subtitle-outline-color", profile.get(
                    "subtitle_outline_color", "#111318"
                ),
                "--speaker", profile["display_name"],
                "--style-name", f"{config['creator'].title()}Radio",
                "--font", profile.get("dialogue_font", "Microsoft YaHei"),
                "--font-size", profile.get("radio_subtitle_size", 80),
            ),
        )
        for song_clip in song_clips:
            write_radio_song_review_placeholder(
                layout_master_dir / f"{song_clip.stem}.ass",
                f"{config['creator'].title()}Radio",
                profile["display_name"],
            )
        run_external(
            project_root,
            "flow3-radio-review-burn",
            script_command(
                "burn_ass_subtitles.py",
                "--clips", layout_master_dir,
                "--ass", layout_master_dir,
                "--output", layout_dir,
                "--ffmpeg", bundled_ffmpeg(),
                "--fontsdir", SKILL_ROOT / "assets" / "fonts",
            ),
        )
        for master_ass in layout_master_dir.glob("*.ass"):
            shutil.copy2(master_ass, layout_dir / master_ass.name)
        shutil.copy2(copy_csv, layout_dir / "titles-and-covers.csv")
        burn_source_dir = layout_master_dir
        clips_dir = layout_dir
        copy_csv = layout_dir / "titles-and-covers.csv"
        clips = sorted(clips_dir.glob("*.mp4"))
        cover_render_dir = layout_dir
        cover_copy_csv = layout_dir / "titles-and-covers.csv"

    run_external(
        project_root,
        "flow3-cover-render",
        script_command(
            "local_publish.py", "render-local", "--copy", cover_copy_csv,
            "--clips-dir", cover_render_dir, "--creator", config["creator"], "--force",
        ),
    )
    if radio_layout and cover_render_dir.resolve() != clips_dir.resolve():
        for cover in cover_render_dir.glob("*-cover.jpg"):
            shutil.copy2(cover, clips_dir / cover.name)
    if not radio_layout:
        run_external(
            project_root,
            "flow3-review-timeline",
            script_command(
                "prepare_review_proxies.py",
                "--review-dir", clips_dir,
                "--ffmpeg", bundled_ffmpeg(),
            ),
        )
    review_workspace.load_decisions(clips_dir)
    checklist = run_root / "review-checklist.txt"
    checklist.write_text(
        "请逐条检查：\n"
        "1. 片段起因、升级、结果和最后一句完整；\n"
        "2. ASS 开头、片中、结尾及每个剪点与声音同步；\n"
        "3. 人名、数字、同音词、说话人和简繁体正确；\n"
        "4. 封面标题清楚且未遮脸；\n"
        "5. 在工作台审核台逐条标记“通过／不通过／人工重做”；不通过不会删除源文件，但不会进入烧录投稿；\n"
        + (
            "5. 电台素材必须是 1920x1080 左侧完整原画、右侧字幕信息区，字号 80；\n"
            if radio_layout else ""
        )
        + (
            "6. lyric-corrections.csv 每行填写 corrected_text 并设置 reviewed=yes；"
            "无可靠逐字轴时保持 timed_units 为空，程序使用整行平滑淡入。\n"
            if song_clips else ""
        ),
        encoding="utf-8",
    )
    state["active_run_dir"] = str(run_root)
    state["active_review_dir"] = str(clips_dir)
    state["active_burn_source_dir"] = str(burn_source_dir)
    state["radio_layout"] = radio_layout
    state.pop("delivery_dir", None)
    state.pop("publish_preview_digest", None)
    save_project(state_path, state)
    outputs = [clips_dir, copy_csv, checklist, review_workspace.decisions_path(clips_dir)]
    if boundary_audit_path.is_file():
        outputs.append(boundary_audit_path)
    if trim_audit_path.is_file():
        outputs.append(trim_audit_path)
    if song_clips:
        outputs.append(run_root / "lyric-corrections.csv")
    return run_root, outputs

def assert_reviewed_lyrics(
    path: Path, allowed_clips: set[str] | None = None
) -> None:
    problems: list[int] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if allowed_clips is not None:
        rows = [row for row in rows if (row.get("clip") or "") in allowed_clips]
    if not rows:
        raise WorkflowError("歌词校对表中没有本次通过审核的歌切")
    for row_number, row in enumerate(rows, 2):
        if (
            (row.get("reviewed") or "").strip().casefold() not in REVIEWED_VALUES
            or not (row.get("corrected_text") or "").strip()
        ):
            problems.append(row_number)
    if problems:
        shown = ", ".join(str(value) for value in problems[:20])
        raise WorkflowError(f"歌词仍有未审核或空白行：{shown}")


def machine_publish_lyric_rows(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Use reviewed corrections when present, otherwise fall back to machine text."""
    output: list[dict[str, str]] = []
    missing: list[str] = []
    for index, source in enumerate(rows, 2):
        row = dict(source)
        corrected = (
            (row.get("corrected_text") or "").strip()
            or (row.get("recognized_text") or "").strip()
            or (row.get("text") or "").strip()
        )
        if not corrected:
            missing.append(str(index))
        else:
            row["corrected_text"] = corrected
            row["reviewed"] = "yes"
        output.append(row)
    if missing:
        raise WorkflowError(
            "无人审核直传仍要求每行有机器歌词文本；缺失行："
            + "、".join(missing[:20])
        )
    return output
def write_csv_subset(
    source: Path,
    destination: Path,
    rows: Sequence[dict[str, str]],
) -> None:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        raise WorkflowError(f"CSV 缺少表头：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fieldnames} for row in rows)


def copy_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def step4_burn(
    state_path: Path,
    state: dict[str, Any],
    confirm_reviewed: bool,
    approval_source: str = "human",
) -> tuple[Path, list[Path]]:
    if approval_source == "human" and not confirm_reviewed:
        raise WorkflowError("人工审核模式必须先确认已检查片段、字幕/歌词和封面")
    project_root = state_path.parent
    config = state["config"]
    run_root = Path(state.get("active_run_dir", ""))
    clips_dir = Path(state.get("active_review_dir", ""))
    burn_source_value = str(state.get("active_burn_source_dir", "")).strip()
    source_clips_dir = Path(burn_source_value) if burn_source_value else clips_dir
    if not run_root.is_dir() or not clips_dir.is_dir():
        raise WorkflowError("没有可烧录的 Flow 3 审核批次")
    if not source_clips_dir.is_dir():
        source_clips_dir = clips_dir
    copy_csv = clips_dir / "titles-and-covers.csv"
    rows = copy_rows(copy_csv)
    try:
        selected_names, decision_state = review_workspace.selected_video_names(
            clips_dir,
            require_human_decisions=approval_source == "human",
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    revision_names = {
        Path(str(name)).name
        for name in (state.get("revision_clip_names") or [])
        if str(name).strip()
    }
    if revision_names:
        unavailable = sorted(revision_names - set(selected_names))
        if unavailable:
            raise WorkflowError(
                "要替换的历史切片尚未全部重新通过审核："
                + "、".join(unavailable)
            )
        selected_names = sorted(revision_names)
    selected_name_set = set(selected_names)
    rows = [row for row in rows if Path(row["video"]).name in selected_name_set]
    if not rows:
        raise WorkflowError("审核决定与 titles-and-covers.csv 没有可交付交集")
    content_types = {
        Path(row["video"]).name: (row.get("content_type") or "narrative").strip().lower()
        for row in rows
    }
    selected_clips = [
        clip for clip in sorted(source_clips_dir.glob("*.mp4"))
        if clip.name in selected_name_set
    ]
    if len(selected_clips) != len(selected_name_set):
        missing = sorted(selected_name_set - {clip.name for clip in selected_clips})
        raise WorkflowError("通过审核的切片缺少烧录母版：" + "、".join(missing))
    has_song = any(value == "song" for value in content_types.values())
    packaging = effective_media_packaging(state, project_root)
    transitions = delivery_narrative_transitions(run_root, clips_dir, rows)
    for clip in selected_clips:
        if content_types.get(clip.name, "narrative") == "song":
            continue
        reviewed_ass = clips_dir / f"{clip.stem}.ass"
        if reviewed_ass.is_file():
            review_workspace.normalize_paid_messages_ass(reviewed_ass)

    # Rebuild from the latest saved copy before recording approval or burning.
    # Only selected clips participate; rejected clips may have incomplete copy.
    for name in selected_names:
        run_external(
            project_root,
            "flow4-cover-render",
            script_command(
                "local_publish.py", "render-local", "--copy", copy_csv,
                "--clips-dir", clips_dir, "--creator", config["creator"],
                "--force", "--only-video", name,
            ),
        )
        cover = clips_dir / f"{Path(name).stem}-cover.jpg"
        if not cover.is_file() or not cover.stat().st_size:
            raise WorkflowError(f"烧录前重生成封面失败：{cover}")

    review_files = [
        copy_csv,
        review_workspace.decisions_path(clips_dir),
        *(clips_dir / name for name in selected_names),
        *(clips_dir / f"{Path(name).stem}.ass" for name in selected_names),
        *(clips_dir / f"{Path(name).stem}-cover.jpg" for name in selected_names),
    ]
    if has_song:
        review_files.append(run_root / "lyric-corrections.csv")
    if packaging["enabled"]:
        review_files.extend(Path(packaging[key]) for key in ("intro_path", "outro_path") if packaging[key])
    review_digest = digest_paths(review_files, extra={"config": config, "media_packaging": packaging, "transitions": transitions})
    state.setdefault("approvals", {})["delivery"] = {
        "digest": review_digest,
        "approved_at": now_iso(),
        "source": approval_source,
        "approved_videos": selected_names,
        "skipped_videos": sorted(
            name for name, row in decision_state.get("clips", {}).items()
            if row.get("status") == "skipped"
        ),
    }
    save_project(state_path, state)

    delivery_root = project_root / "deliveries" / f"delivery-{run_stamp()}"
    package_ass = delivery_root / "package-ass"
    delivery = delivery_root / "files"
    package_ass.mkdir(parents=True, exist_ok=True)
    delivery.mkdir(parents=True, exist_ok=True)
    packaging_path = delivery_root / "media-packaging.json"
    transition_path = delivery_root / "narrative-transitions.json"
    atomic_json(packaging_path, packaging)
    atomic_json(transition_path, transitions)
    asr_root = subtitle_axis_root(run_root)
    timing_report: Path | None = None

    if has_song:
        lyrics = run_root / "lyric-corrections.csv"
        selected_song_stems = {
            Path(name).stem for name, value in content_types.items() if value == "song"
        }
        if approval_source == "human":
            assert_reviewed_lyrics(lyrics, selected_song_stems)
        selected_lyrics = delivery_root / "selected-lyric-corrections.csv"
        lyric_rows = [
            row for row in copy_rows(lyrics)
            if (row.get("clip") or "") in selected_song_stems
        ]
        if approval_source != "human":
            lyric_rows = machine_publish_lyric_rows(lyric_rows)
        write_csv_subset(lyrics, selected_lyrics, lyric_rows)
        retimed = delivery_root / "retimed-lyrics.csv"
        timing_report = delivery_root / "live-timing-report.csv"
        retime_command = script_command(
            "retime_reviewed_lyrics_from_asr.py", "--lyrics", selected_lyrics,
            "--asr-root", asr_root, "--output", retimed, "--report", timing_report,
        )
        manual = run_root / "manual-song-anchors.csv"
        if manual.is_file():
            retime_command.extend(["--manual-anchors", str(manual)])
        run_external(project_root, "flow4-song-retime", retime_command)
        profile = load_profiles()[config["creator"]]
        run_external(
            project_root,
            "flow4-song-ass",
            script_command(
                "make_word_fade_ass.py", "--lyrics", retimed,
                "--output-dir", package_ass,
                "--font", profile.get("song_font", "AR WeiBeiGBStd BD"),
                "--japanese-font", "Yuji Syuku", "--primary", profile.get(
                    "subtitle_fill_color", profile["role_color"]
                ),
                "--outline", profile.get("subtitle_outline_color", "#111318"),
                "--shadow", "#000000",
                "--style-name", f"{config['creator'].title()}Lyric",
                "--speaker-name", profile["display_name"],
            ),
        )

    for clip in selected_clips:
        if content_types.get(clip.name, "narrative") == "song":
            continue
        reviewed_ass = clips_dir / f"{clip.stem}.ass"
        if not reviewed_ass.is_file():
            raise WorkflowError(f"缺少已审核 ASS：{reviewed_ass}")
        shutil.copy2(reviewed_ass, package_ass / reviewed_ass.name)

    for clip in selected_clips:
        ass = package_ass / f"{clip.stem}.ass"
        if not ass.is_file():
            raise WorkflowError(f"打包字幕不存在：{ass}")
        run_external(
            project_root,
            f"flow4-clamp-{clip.stem}",
            script_command("clamp_ass_to_media.py", "--media", clip, "--ass", ass),
        )
        audit = script_command(
            "audit_media_subtitles.py", "--media", clip, "--ass", ass,
            "--asr-json", asr_root / clip.stem / "transcript.json",
        )
        if content_types.get(clip.name, "narrative") != "song":
            audit.extend(["--speech-json", str(asr_root / clip.stem / "speech-activity.json")])
        reviewed_clip = clips_dir / clip.name
        if review_workspace.timeline_backup_video_path(reviewed_clip).is_file():
            audit.append("--timeline-edited")
        if approval_source == "human":
            audit.append("--human-reviewed")
        run_external(project_root, f"flow4-audit-{clip.stem}", audit)

    run_external(
        project_root,
        "flow4-burn",
        script_command(
            "burn_ass_subtitles.py", "--clips", source_clips_dir, "--ass", package_ass,
            "--media-packaging", packaging_path, "--transitions", transition_path,
            "--output", delivery, "--ffmpeg", bundled_ffmpeg(),
            "--fontsdir", SKILL_ROOT / "assets" / "fonts",
            *sum((["--prefix", Path(name).stem] for name in selected_names), []),
        ),
    )
    for video in sorted(delivery.glob("*.mp4")):
        packaging_report = video.with_suffix(".packaging.json")
        intro_seconds = float(json.loads(packaging_report.read_text(encoding="utf-8"))["intro_seconds"]) if packaging_report.is_file() else 0.0
        if intro_seconds:
            media_packaging.shift_ass_file(package_ass / f"{video.stem}.ass", video.with_suffix(".ass"), intro_seconds)
        else:
            shutil.copy2(package_ass / f"{video.stem}.ass", video.with_suffix(".ass"))
    write_csv_subset(copy_csv, delivery / "titles-and-covers.csv", rows)
    for video in sorted(delivery.glob("*.mp4")):
        reviewed_cover = clips_dir / f"{video.stem}-cover.jpg"
        shutil.copy2(reviewed_cover, video.with_name(f"{video.stem}-cover.jpg"))
    if timing_report:
        for video in sorted(delivery.glob("*.mp4")):
            if content_types.get(video.name, "narrative") != "song":
                continue
            gate = video.with_name(f"{video.stem}.song-timing-gate.json")
            run_external(
                project_root,
                f"flow4-song-gate-{video.stem}",
                script_command(
                    "audit_song_publish_gate.py", "--video", video,
                    "--ass", video.with_suffix(".ass"), "--report", timing_report,
                    "--clip", video.stem, "--output", gate,
                    "--expected-ass-delay", 0.1 + (
                        float(json.loads(video.with_suffix(".packaging.json").read_text(encoding="utf-8"))["intro_seconds"])
                        if video.with_suffix(".packaging.json").is_file() else 0.0
                    ),
                ),
            )
    report = delivery_root / "final-delivery-audit.csv"
    run_external(
        project_root,
        "flow4-final-audit",
        script_command("audit_final_delivery.py", delivery, "--report", report),
    )
    state["delivery_dir"] = str(delivery)
    state["delivery_digest"] = digest_paths([delivery], extra=config["publish"])
    state.pop("publish_preview_digest", None)
    save_project(state_path, state)
    return delivery, [delivery, report]

def publish_command(state: dict[str, Any], execute: bool, confirmation: str = "") -> list[str]:
    config = state["config"]
    publish = config["publish"]
    delivery = Path(state.get("delivery_dir", ""))
    if not delivery.is_dir():
        raise WorkflowError("没有通过 Flow 4 的交付目录")
    command = script_command(
        "biliup_publish.py", "upload", "--delivery", delivery,
        "--creator", config["creator"], "--tid", publish["tid"],
        "--song-tid", publish["song_tid"], "--copyright", publish["copyright"],
        "--source", publish.get("source", ""),
        "--no-reprint", int(bool(publish.get("no_reprint", False))),
        "--limit", publish.get("limit", 3),
        "--cooldown", publish.get("cooldown", 0),
        "--submit", "web",
    )
    if execute:
        command.extend(["--execute", "--confirm", confirmation])
    return command


def previous_publish_receipts(state: dict[str, Any]) -> dict[str, Any]:
    for revision in reversed(state.get("publication_history") or []):
        if not isinstance(revision, dict):
            continue
        receipts = revision.get("publish_receipts")
        if (
            isinstance(receipts, dict)
            and isinstance(receipts.get("items"), dict)
            and receipts["items"]
        ):
            return copy.deepcopy(receipts)
    raise WorkflowError("没有找到重做前的投稿回执，无法确定要替换的 BV 稿件")


def replacement_targets_path(state: dict[str, Any]) -> Path:
    delivery = Path(state.get("delivery_dir", ""))
    if not delivery.is_dir():
        raise WorkflowError("没有通过 Flow 4 的交付目录")
    return delivery / "replacement-targets.json"


def write_replacement_targets(state: dict[str, Any]) -> Path:
    target = replacement_targets_path(state)
    atomic_json(target, previous_publish_receipts(state))
    return target


def replacement_publish_command(
    state: dict[str, Any],
    execute: bool,
    confirmation: str = "",
) -> list[str]:
    config = state["config"]
    publish = config["publish"]
    delivery = Path(state.get("delivery_dir", ""))
    targets = replacement_targets_path(state)
    if not targets.is_file():
        raise WorkflowError("缺少换源目标映射，请先运行换源预览")
    command = script_command(
        "biliup_publish.py", "replace",
        "--delivery", delivery,
        "--previous-receipts", targets,
        "--creator", config["creator"],
        "--tid", publish["tid"],
        "--song-tid", publish["song_tid"],
        "--copyright", publish["copyright"],
        "--source", publish.get("source", ""),
        "--no-reprint", int(bool(publish.get("no_reprint", False))),
        "--limit", publish.get("limit", 3),
        "--submit", "web",
    )
    if execute:
        command.extend(["--execute", "--confirm", confirmation])
    return command


def expected_replacement_confirmation(state: dict[str, Any]) -> str:
    delivery = Path(state.get("delivery_dir", ""))
    copy_csv = delivery / "titles-and-covers.csv"
    if not copy_csv.is_file():
        raise WorkflowError("交付目录缺少 titles-and-covers.csv")
    with copy_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        count = sum(1 for _ in csv.DictReader(handle))
    if count < 1:
        raise WorkflowError("换源目录没有任何条目")
    return f"替换 {count} 条"


def expected_confirmation(state: dict[str, Any]) -> str:
    delivery = Path(state.get("delivery_dir", ""))
    copy_csv = delivery / "titles-and-covers.csv"
    if not copy_csv.is_file():
        raise WorkflowError("交付目录缺少 titles-and-covers.csv")
    with copy_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        count = sum(1 for _ in csv.DictReader(handle))
    if count < 1:
        raise WorkflowError("投稿目录没有任何条目")
    return f"发布 {count} 条"


def ensure_profile_publish_enabled(state: dict[str, Any]) -> None:
    creator = state["config"]["creator"]
    profile = load_profiles().get(creator, {})
    if profile.get("archived"):
        raise WorkflowError(
            f"{profile.get('display_name', creator)} 已从人物列表移除，不能继续投稿"
        )
    if profile.get("upload", {}).get("enabled") is False:
        raise WorkflowError(
            f"{profile.get('display_name', creator)} 是新建人物草稿；请先在“人物与字幕模板”"
            "补全投稿简介、固定 Tag 和合集，并勾选“允许这个人物公开投稿”后保存"
        )


def repair_delivery_covers(state_path: Path, state: dict[str, Any]) -> list[Path]:
    """Refresh old delivery covers without rebuilding or uploading the videos."""
    # A failed repair must also invalidate an earlier successful preview.
    for field in (
        "publish_preview_digest", "publish_preview_at",
        "replacement_preview_digest", "replacement_preview_at",
    ):
        state.pop(field, None)
    save_project(state_path, state)
    delivery_value = str(state.get("delivery_dir") or "").strip()
    delivery = Path(delivery_value)
    copy_csv = delivery / "titles-and-covers.csv"
    if not delivery_value or not delivery.is_dir() or not copy_csv.is_file():
        raise WorkflowError("没有可修复封面的交付目录或 titles-and-covers.csv")
    rows = copy_rows(copy_csv)
    review_value = str(state.get("active_review_dir") or "").strip()
    review = Path(review_value) if review_value else None
    review_copy = review / "titles-and-covers.csv" if review else None
    review_rows = {
        Path(str(row.get("video") or "")).name: row
        for row in copy_rows(review_copy)
    } if review_copy and review_copy.is_file() else {}
    decisions = (
        review_workspace.load_decisions(review, create=False).get("clips", {})
        if review and review_workspace.decisions_path(review).is_file() else {}
    )
    human_review = state.get("approvals", {}).get("delivery", {}).get("source", "human") == "human"
    selected: list[tuple[Path, dict[str, str], Path]] = []
    seen: set[str] = set()
    for row in rows:
        name = Path(str(row.get("video") or "")).name
        video = delivery / name
        if not name or video.suffix.casefold() != ".mp4" or not video.is_file():
            continue
        if name.casefold() in seen:
            raise WorkflowError(f"交付文案中存在重复切片：{name}")
        seen.add(name.casefold())
        decision = decisions.get(name, {})
        status = decision.get("status")
        if status in {"revise", "skipped"} or (status == "pending" and human_review):
            raise WorkflowError(f"切片需要重新通过审核，不能仅修复封面后投稿：{name}")
        if review:
            edit_path = review_workspace.timeline_edit_path(review / name)
            if edit_path.is_file() and json.loads(edit_path.read_text(encoding="utf-8-sig")).get("status") == "pending":
                raise WorkflowError(f"仍有未应用的时间轴剪辑：{name}")
        reviewed_row = review_rows.get(name)
        current_row = dict(reviewed_row if reviewed_row is not None else row)
        base = review_copy.parent if reviewed_row is not None else copy_csv.parent
        source = review / name if review and (review / name).is_file() else video
        selected.append((source, current_row, base))
    if not selected:
        raise WorkflowError("交付目录没有可修复封面的切片")

    # Generate every replacement before touching the delivery. Hard links keep
    # normal same-volume repairs from copying or changing the source videos.
    from PIL import Image

    covers: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=".cover-repair-", dir=delivery.parent) as temporary:
        staging = Path(temporary)
        staged_rows: list[dict[str, str]] = []
        for source, row, base in selected:
            staged_video = staging / source.name
            try:
                os.link(source, staged_video)
            except OSError:
                shutil.copy2(source, staged_video)
            row["video"] = source.name
            for field in ("reference_image", "evidence_image"):
                value = str(row.get(field) or "").strip()
                if value:
                    path = Path(value)
                    row[field] = str((path if path.is_absolute() else base / path).resolve())
            guest_paths = []
            for value in re.split(r"[;；\n]+", str(row.get("guest_character_images") or "")):
                if value.strip():
                    path = Path(value.strip())
                    guest_paths.append(str((path if path.is_absolute() else base / path).resolve()))
            if guest_paths:
                row["guest_character_images"] = ";".join(guest_paths)
            staged_rows.append(row)
        staging_csv = staging / "titles-and-covers.csv"
        fields = list(dict.fromkeys(key for row in staged_rows for key in row))
        with staging_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(staged_rows)
        for row in staged_rows:
            name = row["video"]
            run_external(
                state_path.parent,
                "flow5-cover-repair",
                script_command(
                    "local_publish.py", "render-local", "--copy", staging_csv,
                    "--clips-dir", staging, "--creator", state["config"]["creator"],
                    "--force", "--only-video", name,
                ),
            )
            cover = staging / f"{Path(name).stem}-cover.jpg"
            try:
                with Image.open(cover) as rendered:
                    if rendered.format != "JPEG":
                        raise ValueError("封面不是 JPEG 图片")
                    if rendered.size != (1920, 1080):
                        raise ValueError("封面尺寸必须为 1920×1080")
                    rendered.load()
            except (OSError, ValueError) as exc:
                raise WorkflowError(f"投稿前自动修复封面失败：{name}（{exc}）") from exc
            covers.append(delivery / cover.name)
        for cover in covers:
            os.replace(staging / cover.name, cover)
    state["delivery_digest"] = digest_paths([delivery], extra=state["config"]["publish"])
    save_project(state_path, state)
    return covers


def step5_preview(state_path: Path, state: dict[str, Any]) -> str:
    ensure_profile_publish_enabled(state)
    repair_delivery_covers(state_path, state)
    project_root = state_path.parent
    current = state["delivery_digest"]
    run_external(project_root, "flow5-publish-preview", publish_command(state, False))
    state["publish_preview_digest"] = current
    state["publish_preview_at"] = now_iso()
    save_project(state_path, state)
    return expected_confirmation(state)


def _record_publish_progress(state_path: Path, state: dict[str, Any], line: str) -> None:
    event = parse_progress_line(line)
    if event is None:
        return
    step = state.setdefault("steps", {}).setdefault("flow5", {})
    step.update(event)
    step.update(status="running", updated_at=now_iso())
    save_project(state_path, state)


def step5_upload(state_path: Path, state: dict[str, Any], confirmation: str) -> None:
    ensure_profile_publish_enabled(state)
    expected = expected_confirmation(state)
    if confirmation != expected:
        raise WorkflowError(f"确认语不匹配；必须输入：{expected}")
    delivery = Path(state.get("delivery_dir", ""))
    current = digest_paths([delivery], extra=state["config"]["publish"])
    if current != state.get("publish_preview_digest"):
        raise WorkflowError("预览后文件或投稿字段发生变化，请重新运行 Flow 5 预览")
    state.setdefault("approvals", {})["publish"] = {
        "digest": current,
        "approved_at": now_iso(),
    }
    save_project(state_path, state)
    attempts = state.setdefault("publish_attempts", [])
    attempt = {"number": len(attempts) + 1, "started_at": now_iso(), "digest": current, "status": "running"}
    attempts.append(attempt)
    mark_step(state_path, state, "flow5", "running", "正在投递成片并等待平台回执", [delivery])
    try:
        run_external(
            state_path.parent, "flow5-publish-execute", publish_command(state, True, confirmation),
            on_output=lambda line: _record_publish_progress(state_path, state, line),
        )
    except Exception as exc:
        attempt.update(status="failed", finished_at=now_iso(), error=redact(str(exc)))
        mark_step(state_path, state, "flow5", "failed", "投稿失败，可再次投递：" + redact(str(exc)), [delivery])
        raise
    attempt.update(status="published", finished_at=now_iso())
    state["published_at"] = now_iso()
    mark_step(state_path, state, "flow5", "published", "投稿及合集回读完成", [delivery])


def step5_replace_preview(state_path: Path, state: dict[str, Any]) -> str:
    ensure_profile_publish_enabled(state)
    repair_delivery_covers(state_path, state)
    project_root = state_path.parent
    delivery = Path(state.get("delivery_dir", ""))
    targets = write_replacement_targets(state)
    current = digest_paths(
        [delivery, targets],
        extra={"publish": state["config"]["publish"], "operation": "replace"},
    )
    run_external(
        project_root,
        "flow5-replace-preview",
        replacement_publish_command(state, False),
    )
    state["replacement_preview_digest"] = current
    state["replacement_preview_at"] = now_iso()
    save_project(state_path, state)
    return expected_replacement_confirmation(state)


def step5_replace_upload(
    state_path: Path,
    state: dict[str, Any],
    confirmation: str,
) -> None:
    ensure_profile_publish_enabled(state)
    expected = expected_replacement_confirmation(state)
    if confirmation != expected:
        raise WorkflowError(f"换源确认语不匹配；必须输入：{expected}")
    delivery = Path(state.get("delivery_dir", ""))
    targets = replacement_targets_path(state)
    current = digest_paths(
        [delivery, targets],
        extra={"publish": state["config"]["publish"], "operation": "replace"},
    )
    if current != state.get("replacement_preview_digest"):
        raise WorkflowError("换源预览后文件或投稿字段发生变化，请重新预览")
    state.setdefault("approvals", {})["replace_publish"] = {
        "digest": current,
        "approved_at": now_iso(),
    }
    save_project(state_path, state)
    attempts = state.setdefault("replacement_attempts", [])
    attempt = {
        "number": len(attempts) + 1,
        "started_at": now_iso(),
        "digest": current,
        "status": "running",
    }
    attempts.append(attempt)
    mark_step(state_path, state, "flow5", "running", "正在上传新版素材并保留原 BV 号", [delivery])
    try:
        run_external(
            state_path.parent,
            "flow5-replace-execute",
            replacement_publish_command(state, True, confirmation),
            on_output=lambda line: _record_publish_progress(state_path, state, line),
        )
    except Exception as exc:
        attempt.update(status="failed", finished_at=now_iso(), error=redact(str(exc)))
        mark_step(
            state_path, state, "flow5", "failed",
            "换源失败，可再次替换原稿：" + redact(str(exc)), [delivery],
        )
        raise
    attempt.update(status="published", finished_at=now_iso())
    state["published_at"] = now_iso()
    mark_step(
        state_path,
        state,
        "flow5",
        "published",
        "已保留原 BV 号并提交新版素材，等待 B 站重新审核",
        [delivery, delivery / "replacement-receipts.json"],
    )


def run_stage(project: Path, name: str, action) -> Any:
    state_path, state = load_project(project)
    mark_step(state_path, state, name, "running")
    try:
        result = action(state_path, state)
    except Exception as exc:
        _, latest = load_project(state_path)
        mark_step(state_path, latest, name, "failed", redact(str(exc)))
        raise
    _, latest = load_project(state_path)
    outputs: Sequence[Path | str] = ()
    detail = "完成"
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], list):
        outputs = result[1]
    elif isinstance(result, list):
        outputs = result
    elif isinstance(result, Path):
        outputs = [result]
    elif isinstance(result, str):
        detail = result
    mark_step(state_path, latest, name, "completed", detail, outputs)
    return result


def show_status(project: Path) -> None:
    state_path, state = load_project(project)
    console_print(f"project={state_path.parent}")
    console_print(f"creator={state['config']['creator']} mode={state['config']['mode']}")
    for name in ("flow0", "flow1", "flow2", "flow3", "flow4", "flow5"):
        step = state.get("steps", {}).get(name, {})
        console_print(f"{name}={step.get('status', 'pending')} {step.get('detail', '')}")
    if state.get("active_review_dir"):
        console_print(f"review={state['active_review_dir']}")
    if state.get("delivery_dir"):
        console_print(f"delivery={state['delivery_dir']}")


def run_doctor(project: Path | None) -> None:
    root = project.resolve() if project else WORKSPACE_ROOT
    run_external(root, "doctor-content", script_command("content_slicer.py", "doctor"))
    run_external(root, "doctor-profiles", script_command("local_publish.py", "validate-profiles"))
    run_external(root, "doctor-publish", script_command("biliup_publish.py", "doctor"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create or update a local workflow project")
    init.add_argument("--project", type=Path, required=True)
    init.add_argument("--source", type=Path, required=True)
    init.add_argument("--xml", type=Path)
    init.add_argument("--creator", required=True)
    init.add_argument("--mode", choices=("narrative", "song", "mixed"), default="narrative")
    init.add_argument("--model", default="local:qwen3-asr-auto")
    init.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    init.add_argument("--compute-type", default="float16")
    init.add_argument("--speaker-count", type=int)

    for name in ("step1", "prompt", "step3", "preview", "status"):
        command = sub.add_parser(name)
        command.add_argument("--project", type=Path, required=True)
    import_parser = sub.add_parser("import-selection")
    import_parser.add_argument("--project", type=Path, required=True)
    import_parser.add_argument("--json", type=Path, required=True)
    burn = sub.add_parser("step4")
    burn.add_argument("--project", type=Path, required=True)
    review_mode = burn.add_mutually_exclusive_group()
    review_mode.add_argument("--confirm-reviewed", action="store_true")
    review_mode.add_argument("--skip-human-review", action="store_true")
    upload = sub.add_parser("upload")
    upload.add_argument("--project", type=Path, required=True)
    upload.add_argument("--confirm", required=True)
    expected = sub.add_parser("expected-confirmation")
    expected.add_argument("--project", type=Path, required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--project", type=Path)
    credentials = sub.add_parser("credentials")
    group = credentials.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", action="store_true")
    group.add_argument("--create-template", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            path = init_project(
                args.project, args.source, args.xml,
                args.creator, args.mode, args.model, args.device, args.compute_type,
                args.speaker_count,
            )
            console_print(f"项目已保存：{path}")
        elif args.command == "step1":
            run_stage(args.project, "flow1", step1_transcribe)
        elif args.command == "prompt":
            result = run_stage(
                args.project,
                "flow2",
                lambda state_path, state: list(build_selection_prompt(state_path, state)),
            )
            console_print(f"Prompt 文件：{result[0]}")
        elif args.command == "import-selection":
            result = run_stage(
                args.project,
                "flow2",
                lambda state_path, state: import_selection(state_path, state, args.json),
            )
            console_print(f"选片 JSON 已导入：{result}")
        elif args.command == "step3":
            result = run_stage(args.project, "flow3", step3_prepare)
            _, latest = load_project(args.project)
            console_print(f"审核文件夹：{latest.get('active_review_dir', result[0])}")
        elif args.command == "step4":
            result = run_stage(
                args.project,
                "flow4",
                lambda state_path, state: step4_burn(
                    state_path,
                    state,
                    args.confirm_reviewed,
                    approval_source=(
                        "user-authorized-unreviewed"
                        if args.skip_human_review
                        else "human"
                    ),
                ),
            )
            console_print(f"交付文件夹：{result[0]}")
        elif args.command == "preview":
            confirmation = run_stage(args.project, "flow5", step5_preview)
            console_print(f"预览完成。执行上传时请输入：{confirmation}")
        elif args.command == "upload":
            state_path, state = load_project(args.project)
            step5_upload(state_path, state, args.confirm)
        elif args.command == "expected-confirmation":
            _, state = load_project(args.project)
            console_print(expected_confirmation(state))
        elif args.command == "status":
            show_status(args.project)
        elif args.command == "doctor":
            run_doctor(args.project)
        elif args.command == "credentials":
            flag = "--login" if args.login else "--create-template"
            run_external(
                WORKSPACE_ROOT,
                "credentials",
                script_command("biliup_publish.py", "credentials", flag),
            )
        return 0
    except (WorkflowError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        console_print(f"错误：{redact(str(exc))}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
