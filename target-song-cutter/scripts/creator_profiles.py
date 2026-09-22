#!/usr/bin/env python3
"""Create and update creator profiles used by the local clipping workbench."""

from __future__ import annotations

import colorsys
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SKILL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = SKILL_ROOT / "assets" / "creator-profiles.json"
CHARACTER_IMAGE_DIRNAME = "creator-images"
DEFAULT_FONT = "Microsoft YaHei"
DEFAULT_DIALOGUE_SIZE = 85
DEFAULT_RADIO_SIZE = 80
DEFAULT_SUBTITLE_OUTLINE_COLOR = "#111318"
DEFAULT_SUBTITLE_OUTLINE_WIDTH = 5.0
GENERIC_PARENT_NAMES = {
    "录播", "录播文件", "录制", "recordings", "recording", "downloads", "视频", "素材"
}


def _read(path: Path = PROFILE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload.get("profiles"), dict):
        raise ValueError("主播配置缺少 profiles 对象")
    return payload


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def validate_key(value: str) -> str:
    key = value.strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]{2,48}", key):
        raise ValueError("人物代号只能使用 2–48 位小写英文、数字或下划线")
    return key


def validate_color(value: str) -> str:
    color = value.strip().upper()
    if not color.startswith("#"):
        color = "#" + color
    if not re.fullmatch(r"#[0-9A-F]{6}", color):
        raise ValueError("字幕主题色必须是 #RRGGBB，例如 #F9A699")
    return color


def subtitle_colors(profile: dict[str, Any]) -> tuple[str, str]:
    """Return collision-resistant subtitle fill/outline colors with legacy fallbacks."""
    fill = validate_color(
        str(profile.get("subtitle_fill_color") or profile.get("role_color") or "#FFFFFF")
    )
    outline = validate_color(
        str(profile.get("subtitle_outline_color") or DEFAULT_SUBTITLE_OUTLINE_COLOR)
    )
    return fill, outline


def validate_size(value: int | str, label: str) -> int:
    try:
        size = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if not 24 <= size <= 160:
        raise ValueError(f"{label}必须在 24–160 之间")
    return size


def normalize_upload_tags(value: str | Iterable[str]) -> list[str]:
    raw_values = (
        re.split(r"[,，、;\n]+", value)
        if isinstance(value, str)
        else [str(item) for item in value]
    )
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        tag = str(raw).strip()
        key = tag.casefold()
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
    return result


def optional_positive_id(value: int | str | None, label: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{label}必须是正整数，或留空让程序自动查找/创建合集")
    return int(text)
def _unique_color(seed: str, profiles: dict[str, Any]) -> str:
    used = {str(item.get("role_color", "")).upper() for item in profiles.values()}
    digest = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8], 16)
    for offset in range(360):
        hue = ((digest % 360) + offset * 37) % 360 / 360.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.34, 0.88)
        color = f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"
        if color not in used:
            return color
    raise ValueError("无法为新人物生成不重复的主题色")


def _default_profile(
    display_name: str,
    room_id: str,
    color: str,
    *,
    auto_created: bool,
) -> dict[str, Any]:
    name = display_name.strip()
    return {
        "display_name": name,
        "title_tag": name,
        "room_id": room_id.strip(),
        "role_color": color,
        "subtitle_fill_color": color,
        "subtitle_outline_color": DEFAULT_SUBTITLE_OUTLINE_COLOR,
        "subtitle_outline_width": DEFAULT_SUBTITLE_OUTLINE_WIDTH,
        "dialogue_font": DEFAULT_FONT,
        "dialogue_font_size": DEFAULT_DIALOGUE_SIZE,
        "radio_subtitle_size": DEFAULT_RADIO_SIZE,
        "cover_character_image": "",
        "song_font": "AR WeiBeiGBStd BD",
        "hotwords": [name] if name else [],
        "stream_hotwords": [],
        "profile_status": "draft",
        "auto_created": bool(auto_created),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "upload": {
            "enabled": False,
            "tid": None,
            "copyright": 1,
            "no_reprint": False,
            "tags": [name, "虚拟主播", "直播切片"] if name else ["直播切片"],
            "description": "",
        },
    }


def upsert_profile(
    *,
    key: str,
    display_name: str,
    room_id: str = "",
    role_color: str,
    subtitle_fill_color: str | None = None,
    subtitle_outline_color: str | None = None,
    subtitle_outline_width: float | str | None = None,
    dialogue_font: str = DEFAULT_FONT,
    dialogue_font_size: int | str = DEFAULT_DIALOGUE_SIZE,
    radio_subtitle_size: int | str = DEFAULT_RADIO_SIZE,
    cover_character_image: str | None = None,
    upload_enabled: bool | None = None,
    upload_description: str | None = None,
    upload_tags: str | Iterable[str] | None = None,
    narrative_collection_title: str | None = None,
    narrative_season_id: int | str | None = None,
    narrative_section_id: int | str | None = None,
    song_collection_title: str | None = None,
    song_season_id: int | str | None = None,
    song_section_id: int | str | None = None,
    profiles_path: Path = PROFILE_PATH,
) -> str:
    """Create or edit the identity and subtitle portion of one profile."""
    normalized_key = validate_key(key)
    name = display_name.strip()
    if not name:
        raise ValueError("人物显示名不能为空")
    room = room_id.strip()
    if room and not room.isdigit():
        raise ValueError("直播间房间号只能填写数字")
    color = validate_color(role_color)
    fill_color = (
        validate_color(subtitle_fill_color)
        if subtitle_fill_color is not None else None
    )
    outline_color = (
        validate_color(subtitle_outline_color)
        if subtitle_outline_color is not None else None
    )
    outline_width: float | None = None
    if subtitle_outline_width is not None:
        try:
            outline_width = float(str(subtitle_outline_width).strip())
        except ValueError as exc:
            raise ValueError("字幕描边宽度必须是数字") from exc
        if not 3 <= outline_width <= 8:
            raise ValueError("字幕描边宽度必须在 3–8 之间")
    font = dialogue_font.strip()
    if not font:
        raise ValueError("字幕字体不能为空")
    dialogue_size = validate_size(dialogue_font_size, "普通字幕字号")
    radio_size = validate_size(radio_subtitle_size, "电台字幕字号")

    managed_character_image: str | None = None
    if cover_character_image is not None:
        raw_image = cover_character_image.strip()
        if raw_image:
            source_image = Path(raw_image)
            if not source_image.is_absolute():
                source_image = profiles_path.parent / source_image
            source_image = source_image.expanduser().resolve()
            if source_image.suffix.lower() not in {".png", ".webp"}:
                raise ValueError("多人封面人物图只支持带透明背景的 PNG 或 WebP")
            if not source_image.is_file():
                raise ValueError(f"多人封面人物图不存在：{source_image}")
            try:
                from PIL import Image

                with Image.open(source_image) as image:
                    alpha = image.convert("RGBA").getchannel("A")
                    if alpha.getextrema() == (255, 255):
                        raise ValueError("多人封面人物图必须带透明背景")
            except ImportError:
                pass
            target_dir = profiles_path.parent / CHARACTER_IMAGE_DIRNAME
            target_dir.mkdir(parents=True, exist_ok=True)
            target_image = target_dir / f"{normalized_key}{source_image.suffix.lower()}"
            if source_image != target_image.resolve():
                shutil.copy2(source_image, target_image)
            managed_character_image = target_image.relative_to(
                profiles_path.parent
            ).as_posix()
        else:
            managed_character_image = ""

    payload = _read(profiles_path)
    profiles = payload["profiles"]
    for other_key, profile in profiles.items():
        if profile.get("archived"):
            continue
        if other_key != normalized_key and room and str(profile.get("room_id", "")) == room:
            raise ValueError(f"房间号 {room} 已属于 {profile.get('display_name', other_key)}")
    profile = profiles.get(normalized_key)
    if profile is None:
        profile = _default_profile(name, room, color, auto_created=False)
        profiles[normalized_key] = profile
    profile.update(
        {
            "display_name": name,
            "title_tag": name,
            "room_id": room,
            "role_color": color,
            "dialogue_font": font,
            "dialogue_font_size": dialogue_size,
            "radio_subtitle_size": radio_size,
        }
    )
    if fill_color is not None:
        profile["subtitle_fill_color"] = fill_color
    else:
        profile.setdefault("subtitle_fill_color", color)
    if outline_color is not None:
        profile["subtitle_outline_color"] = outline_color
    else:
        profile.setdefault("subtitle_outline_color", DEFAULT_SUBTITLE_OUTLINE_COLOR)
    if outline_width is not None:
        profile["subtitle_outline_width"] = outline_width
    else:
        profile.setdefault("subtitle_outline_width", DEFAULT_SUBTITLE_OUTLINE_WIDTH)
    if managed_character_image is not None:
        profile["cover_character_image"] = managed_character_image
    profile.pop("archived", None)
    profile.pop("archived_at", None)
    profile["profile_status"] = "configured"

    upload = profile.setdefault("upload", {})
    if upload_description is not None:
        upload["description"] = upload_description.strip()
    if upload_tags is not None:
        upload["tags"] = normalize_upload_tags(upload_tags)
    collections = upload.setdefault("collections", {})
    for kind, title_value, season_value, section_value in (
        (
            "narrative",
            narrative_collection_title,
            narrative_season_id,
            narrative_section_id,
        ),
        ("song", song_collection_title, song_season_id, song_section_id),
    ):
        if title_value is None and season_value is None and section_value is None:
            continue
        collection = collections.setdefault(kind, {})
        if title_value is not None:
            collection["title"] = title_value.strip()
        season_id = optional_positive_id(season_value, f"{kind} season_id")
        section_id = optional_positive_id(section_value, f"{kind} section_id")
        if (season_id is None) != (section_id is None):
            raise ValueError(f"{kind} 合集的 season_id 和 section_id 必须同时填写或同时留空")
        if season_id is None:
            collection.pop("season_id", None)
            collection.pop("section_id", None)
        else:
            collection["season_id"] = season_id
            collection["section_id"] = section_id
    if upload_enabled is not None:
        if upload_enabled:
            if not str(upload.get("description") or "").strip():
                raise ValueError("启用投稿前必须填写投稿简介")
            if not normalize_upload_tags(upload.get("tags") or []):
                raise ValueError("启用投稿前必须填写至少一个固定 Tag")
            narrative = collections.get("narrative") or {}
            if not str(narrative.get("title") or "").strip():
                raise ValueError("启用投稿前必须填写普通切片合集名称")
        upload["enabled"] = bool(upload_enabled)

    _atomic_write(profiles_path, payload)
    return normalized_key


def archive_profile(
    key: str,
    *,
    profiles_path: Path = PROFILE_PATH,
) -> dict[str, str]:
    """Hide one profile without deleting its settings or any media/project files."""
    normalized_key = validate_key(key)
    payload = _read(profiles_path)
    profiles = payload["profiles"]
    profile = profiles.get(normalized_key)
    if profile is None or profile.get("archived"):
        raise ValueError("人物不存在或已经移除")
    active_count = sum(
        1 for item in profiles.values() if not bool(item.get("archived"))
    )
    if active_count <= 1:
        raise ValueError("至少保留一个可用人物，不能移除最后一个人物")
    profile["archived"] = True
    profile["archived_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    profile["profile_status"] = "archived"
    upload = profile.setdefault("upload", {})
    upload["enabled"] = False
    _atomic_write(profiles_path, payload)
    return {
        "key": normalized_key,
        "display_name": str(profile.get("display_name") or normalized_key),
        "room_id": str(profile.get("room_id") or ""),
    }


def display_name_from_paths(paths: Iterable[Path], room_id: str) -> str:
    """Use a recorder folder such as ``123456-主播名`` without using stream titles."""
    for path in paths:
        parent = path.parent.name.strip()
        candidate = re.sub(
            rf"^{re.escape(room_id)}(?:\s*[-_｜|]\s*|\s+)?", "", parent
        ).strip(" -_｜|")
        if (
            candidate
            and candidate.casefold() not in GENERIC_PARENT_NAMES
            and candidate != room_id
            and len(candidate) <= 48
        ):
            return candidate
    return f"新主播（房间{room_id}）"


def ensure_auto_profile(
    room_id: str,
    paths: Iterable[Path],
    *,
    profiles_path: Path = PROFILE_PATH,
) -> str:
    """Return the room's profile key, creating a non-publishable draft when needed."""
    room = room_id.strip()
    if not room or not room.isdigit():
        return ""
    payload = _read(profiles_path)
    profiles = payload["profiles"]
    matching = [
        key
        for key, profile in profiles.items()
        if not profile.get("archived") and str(profile.get("room_id", "")) == room
    ]
    if len(matching) == 1:
        return matching[0]
    if len(matching) > 1:
        return ""
    if any(
        profile.get("archived") and str(profile.get("room_id", "")) == room
        for profile in profiles.values()
    ):
        return ""
    key = validate_key(f"room_{room}")
    display_name = display_name_from_paths(paths, room)
    color = _unique_color(room, profiles)
    profiles[key] = _default_profile(display_name, room, color, auto_created=True)
    _atomic_write(profiles_path, payload)
    return key
