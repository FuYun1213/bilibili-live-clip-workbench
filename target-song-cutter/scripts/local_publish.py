#!/usr/bin/env python3
"""Render local covers beside videos; keep manifest tooling opt-in."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cover_emotes
import cover_layout
from campaign_rules import merge_campaign_tags
from windows_process import hidden_subprocess_kwargs, run_checked_with_transient_retries

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = SKILL_ROOT / "assets" / "creator-profiles.json"
SAFE_LEFT, SAFE_RIGHT = 240, 1680
CANVAS = (1920, 1080)
VALID_MODES = {"quote-impact", "evidence-reaction", "clash-challenge", "song", "viridis-song"}


def is_song_cover_mode(mode: str) -> bool:
    """Use the creator profile for song covers; keep viridis-song as a legacy alias."""
    return mode in {"song", "viridis-song"}
VALID_SOURCE_REGIONS = {"auto", "full", "radio-left"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_profiles() -> dict[str, dict[str, Any]]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"]


def validate_profiles(profiles: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    colors: dict[str, str] = {}
    subtitle_fills: dict[str, str] = {}
    for key, profile in profiles.items():
        if profile.get("archived"):
            continue
        for field in ("display_name", "title_tag", "room_id", "role_color", "hotwords", "upload"):
            if field not in profile:
                errors.append(f"{key}: missing {field}")
        color = str(profile.get("role_color", ""))
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            errors.append(f"{key}: invalid role_color {color!r}")
        elif color.upper() in colors:
            errors.append(f"{key}: duplicate role_color with {colors[color.upper()]}")
        else:
            colors[color.upper()] = key
        fill = str(profile.get("subtitle_fill_color", ""))
        outline = str(profile.get("subtitle_outline_color", ""))
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", fill):
            errors.append(f"{key}: invalid subtitle_fill_color {fill!r}")
        elif fill.upper() in subtitle_fills:
            errors.append(
                f"{key}: duplicate subtitle_fill_color with "
                f"{subtitle_fills[fill.upper()]}"
            )
        else:
            channels = tuple(int(fill[index:index + 2], 16) for index in (1, 3, 5))
            for other_fill, other_key in subtitle_fills.items():
                other_channels = tuple(
                    int(other_fill[index:index + 2], 16) for index in (1, 3, 5)
                )
                distance = math.sqrt(
                    sum((left - right) ** 2 for left, right in zip(channels, other_channels))
                )
                if distance < 55:
                    errors.append(
                        f"{key}: subtitle_fill_color is too close to {other_key} "
                        f"({distance:.1f} < 55)"
                    )
            subtitle_fills[fill.upper()] = key
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", outline):
            errors.append(f"{key}: invalid subtitle_outline_color {outline!r}")
        outline_width = profile.get("subtitle_outline_width")
        if not isinstance(outline_width, (int, float)) or not 3 <= float(outline_width) <= 8:
            errors.append(f"{key}: subtitle_outline_width must be from 3 to 8")
        if any("\ufffd" in str(value) or re.search(r"\?{2,}", str(value)) for value in profile.values()):
            errors.append(f"{key}: probable text-encoding damage")
        font = str(profile.get("dialogue_font", "")).strip()
        if not font:
            errors.append(f"{key}: dialogue_font must not be empty")
        for size_field, fallback in (("dialogue_font_size", 85), ("radio_subtitle_size", 80)):
            size = profile.get(size_field, fallback)
            if not isinstance(size, int) or not 24 <= size <= 160:
                errors.append(f"{key}: {size_field} must be an integer from 24 to 160")
        character = str(profile.get("cover_character_image", "")).strip()
        if character:
            character_path = Path(character)
            if not character_path.is_absolute():
                character_path = PROFILE_PATH.parent / character_path
            if not character_path.is_file():
                errors.append(f"{key}: cover_character_image is missing: {character}")
        tid = profile.get("upload", {}).get("tid")
        if tid is not None and (not isinstance(tid, int) or tid <= 0):
            errors.append(f"{key}: upload.tid must be null or a positive integer")
    viridis = profiles.get("viridis", {})
    if not viridis.get("archived") and viridis.get("role_color", "").upper() != "#8EB056":
        errors.append("viridis: role_color must be #8EB056")
    return errors


def resolve_input(value: str, base: Path) -> Path | None:
    if not value.strip():
        return None
    path = Path(value.strip())
    return (path if path.is_absolute() else base / path).resolve()


def truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "是", "已核实"}


def split_names(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,，、;；|\n]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item).strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def resolve_guest_character_images(
    row: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    creator: str,
    base: Path,
) -> tuple[list[Path], list[str]]:
    """Resolve only human-verified audible guests; visual/name-only guests stay out."""
    collaboration_type = str(row.get("collaboration_type") or "single").strip().lower()
    if collaboration_type == "single" or not truthy(row.get("guest_dialogue_verified")):
        return [], []
    keys = split_names(row.get("participant_profile_keys"))
    if not keys:
        aliases: dict[str, str] = {}
        for key, profile in profiles.items():
            for alias in (
                key,
                profile.get("display_name", ""),
                profile.get("title_tag", ""),
                *profile.get("hotwords", []),
            ):
                normalized = re.sub(r"\s+", "", str(alias)).casefold()
                if normalized:
                    aliases.setdefault(normalized, key)
        keys = [
            aliases.get(re.sub(r"\s+", "", name).casefold(), "")
            for name in split_names(row.get("participants"))
        ]
    paths: list[Path] = []
    missing: list[str] = []
    for key in keys:
        if not key or key == creator:
            continue
        profile = profiles.get(key)
        if not profile:
            missing.append(key)
            continue
        raw = str(profile.get("cover_character_image") or "").strip()
        if not raw:
            missing.append(str(profile.get("display_name") or key))
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = PROFILE_PATH.parent / path
        path = path.resolve()
        if path.is_file() and path not in paths:
            paths.append(path)
        else:
            missing.append(str(profile.get("display_name") or key))
    explicit_value = row.get("guest_character_images") or []
    explicit_values = (
        explicit_value
        if isinstance(explicit_value, list)
        else re.split(r"[;；\n]+", str(explicit_value))
    )
    for raw in explicit_values:
        if not str(raw).strip():
            continue
        raw_text = str(raw).strip()
        path = Path(raw_text)
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if path.is_file() and path not in paths:
            paths.append(path)
        elif raw_text not in missing:
            missing.append(raw_text)
    return paths[:3], missing

def split_tags(raw: str, fallback: list[str]) -> list[str]:
    values = [str(value).strip() for value in fallback if str(value).strip()]
    values.extend(
        part.strip() for part in re.split(r"[,\uFF0C;\uFF1B]", raw) if part.strip()
    )
    return list(dict.fromkeys(values))[:10]


def optional_nonnegative_float(raw: str, field: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return parsed


def find_video(clips_dir: Path, clip_id: str, explicit: str, base: Path) -> Path | None:
    if explicit.strip():
        resolved = resolve_input(explicit, base)
        if resolved and resolved.is_file():
            return resolved
        explicit_path = Path(explicit.strip())
        if not explicit_path.is_absolute():
            clip_relative = (clips_dir / explicit_path).resolve()
            if clip_relative.is_file():
                return clip_relative
        return resolved
    matches = sorted(clips_dir.glob(f"{clip_id}*.mp4"))
    if not matches:
        matches = sorted(clips_dir.glob(f"*{clip_id}*.mp4"))
    return matches[0].resolve() if matches else None


def versioned_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}-v{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def prepare(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    errors = validate_profiles(profiles)
    if errors:
        raise SystemExit("\n".join(errors))
    if args.creator not in profiles:
        raise SystemExit(f"unknown creator profile: {args.creator}")
    profile = profiles[args.creator]
    copy_path = Path(args.copy).resolve()
    clips_dir = Path(args.clips_dir).resolve()
    output = Path(args.output).resolve()
    if not args.force:
        output = versioned_path(output)

    with copy_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("publishing copy CSV has no rows")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        clip_id = (row.get("clip_id") or f"{index:03d}").strip()
        title = (row.get("title") or row.get("title_a") or "").strip()
        primary = (row.get("cover_text_primary") or row.get("cover_text_1") or "").strip()
        secondary = (row.get("cover_text_secondary") or row.get("cover_text_2") or "").strip()
        mode = (row.get("cover_mode") or "quote-impact").strip()
        source_region = (row.get("cover_source_region") or "auto").strip()
        video = find_video(clips_dir, clip_id, row.get("video", ""), copy_path.parent)
        reference = resolve_input(row.get("reference_image", ""), copy_path.parent)
        evidence = resolve_input(row.get("evidence_image", ""), copy_path.parent)
        raw_tid = (row.get("tid") or "").strip()
        tid = int(raw_tid) if raw_tid.isdigit() else profile["upload"].get("tid")
        campaign_tags = merge_campaign_tags(
            split_tags(row.get("campaign_tags", ""), []), title
        )
        if clip_id in seen:
            raise SystemExit(f"duplicate clip_id: {clip_id}")
        seen.add(clip_id)
        items.append({
            "clip_id": clip_id,
            "video": str(video) if video else "",
            "title": title,
            "description": (row.get("description") or "").strip(),
            "tags": split_tags(
                row.get("tags", ""),
                [
                    *profile["upload"].get("tags", []),
                    *campaign_tags,
                ],
            ),
            "tid": tid,
            "copyright": int(profile["upload"].get("copyright", 1)),
            "no_reprint": bool(profile["upload"].get("no_reprint", False)),
            "cover_mode": mode,
            "cover_source_region": source_region,
            "cover_time_seconds": optional_nonnegative_float(
                row.get("cover_time_seconds", ""), "cover_time_seconds"
            ),
            "cover_text_primary": primary,
            "cover_text_secondary": secondary,
            "cover_emotion": (row.get("cover_emotion") or cover_emotes.AUTO_EMOTION).strip(),
            "cover_emote": (row.get("cover_emote") or "").strip(),
            "reference_image": str(reference) if reference else "",
            "evidence_image": str(evidence) if evidence else "",
            "collaboration_type": (row.get("collaboration_type") or "single").strip(),
            "participants": split_names(row.get("participants")),
            "participant_profile_keys": split_names(
                row.get("participant_profile_keys")
            ),
            "speaker_evidence": (row.get("speaker_evidence") or "").strip(),
            "guest_dialogue_verified": truthy(row.get("guest_dialogue_verified")),
            "guest_character_images": split_names(
                row.get("guest_character_images")
            ),
            "cover": "",
            "state": "prepared",
            "attempts": 0,
            "last_error": "",
            "bvid": "",
            "aid": "",
            "updated_at": now_iso(),
        })

    manifest = {
        "schema_version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "creator_profile": args.creator,
        "creator": profile["display_name"],
        "role_color": profile["role_color"],
        "copy_source": str(copy_path),
        "items": items,
    }
    atomic_json(output, manifest)
    print(output)
    return 0


def executable(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    for folder in ("ffmpeg", "ffmpeg-bin"):
        bundled = SKILL_ROOT.parent / "tools" / folder / f"{name}{suffix}"
        if bundled.is_file():
            return str(bundled)
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"{name} is not available on PATH")
    return value


WINDOWS_DLL_INIT_FAILURES = {0xC0000142, -1073741502}


def run_frame_extraction(command: list[str], retries: int = 3) -> None:
    """Retry only the transient Windows process-start failure seen from ffmpeg."""
    for attempt in range(1, retries + 1):
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as exc:
            if exc.returncode not in WINDOWS_DLL_INIT_FAILURES or attempt >= retries:
                raise
            time.sleep(0.4 * attempt)


def extract_reference(
    video: Path, destination: Path, moment_seconds: float | None = None
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    probe = run_checked_with_transient_retries(
        [executable("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, **hidden_subprocess_kwargs(),
    )
    duration = max(float(probe.stdout.strip() or 1), 1)
    if moment_seconds is None:
        moment = min(max(duration * 0.28, 1.0), max(duration - 0.5, 0.1))
    else:
        moment = float(moment_seconds)
        if moment >= duration:
            raise ValueError(
                f"cover_time_seconds {moment:.3f} is outside video duration {duration:.3f}"
            )
    run_frame_extraction(
        [executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", f"{moment:.3f}", "-i", str(video), "-frames:v", "1", "-y", str(destination)]
    )


def font_candidates() -> list[Path]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [
        SKILL_ROOT / "assets" / "fonts" / "autoclip" / "MaoKenShiJinHei-2.ttf",
        windows / "msyhbd.ttc",
        windows / "msyh.ttc",
        windows / "simhei.ttf",
        windows / "simsun.ttc",
    ]


def load_font(size: int):
    from PIL import ImageFont
    for path in font_candidates():
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def fit_font(draw, text: str, max_width: int, start: int, minimum: int):
    size = start
    while size >= minimum:
        font = load_font(size)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=8)
        if box[2] - box[0] <= max_width:
            return font
        size -= 4
    return load_font(minimum)


def crop_to_16_9(image):
    """AutoClip-compatible centered 16:9 crop."""
    width, height = image.size
    target_ratio = 16 / 9
    current_ratio = width / max(height, 1)
    if abs(current_ratio - target_ratio) < 0.005:
        return image
    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))
    new_height = int(width / target_ratio)
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def autoclip_canvas(source):
    from PIL import Image

    canvas = crop_to_16_9(source.convert("RGBA"))
    if canvas.size != CANVAS:
        canvas = canvas.resize(CANVAS, Image.Resampling.LANCZOS)
    return canvas


def fit_autoclip_font(draw, text: str, max_width: int, start_size: int = 150):
    size = start_size
    font = load_font(size)
    width = draw.textlength(text, font=font)
    if width > max_width:
        size = max(18, int(size * max_width / width))
        font = load_font(size)
    return font


def draw_autoclip_text(image, position, text, font, fill_color, stroke_width=12, anchor="mm") -> None:
    """Copy AutoClip's MaxFilter multilayer black-stroke renderer."""
    from PIL import Image, ImageDraw, ImageFilter

    text_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text(position, text, font=font, fill=fill_color, anchor=anchor)
    stroke_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(stroke_layer).text(position, text, font=font, fill=(0, 0, 0, 255), anchor=anchor)
    for _ in range(stroke_width):
        stroke_layer = stroke_layer.filter(ImageFilter.MaxFilter(3))
    image.alpha_composite(stroke_layer)
    image.alpha_composite(text_layer)


def narrative_cover_layout(source_region: str) -> tuple[float, int, float, float]:
    """Return text center, safe width, and two y positions for the selected layout."""
    left, right = cover_layout.cover_text_bounds(source_region)
    return (left + right) / 2, right - left, 230 / CANVAS[1], 830 / CANVAS[1]

def composite_guest_characters(
    canvas, images: list[Path], *, source_region: str = "auto"
) -> list[str]:
    """Place transparent guest emotes over the source frame and below cover text."""
    from PIL import Image

    selected = images[:3]
    if not selected:
        return []
    centers_by_count = (
        {
            1: [580],
            2: [330, 780],
            3: [220, 550, 880],
        }
        if source_region == "radio-left"
        else {
            1: [1510],
            2: [430, 1490],
            3: [300, 960, 1620],
        }
    )
    centers = centers_by_count[len(selected)]
    used: list[str] = []
    for path, center_x in zip(selected, centers):
        with Image.open(path) as source:
            image = source.convert("RGBA")
        alpha = image.getchannel("A")
        if alpha.getextrema() == (255, 255):
            raise ValueError(f"guest character image has no transparent background: {path}")
        bounds = alpha.getbbox()
        if not bounds:
            raise ValueError(f"guest character image is fully transparent: {path}")
        image = image.crop(bounds)
        max_width = 520 if len(selected) == 1 else 450
        max_height = 600 if len(selected) == 1 else 530
        scale = min(max_width / image.width, max_height / image.height, 1.8)
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        x = max(0, min(CANVAS[0] - image.width, round(center_x - image.width / 2)))
        y = CANVAS[1] - image.height - 12
        canvas.alpha_composite(image, (x, y))
        used.append(str(path.resolve()))
    return used

def render_cover(
    item: dict[str, Any], profile: dict[str, Any], output: Path, *,
    creator: str = "",
) -> None:
    """Render the reviewed AutoClip cover styles without custom grading or panels."""
    from PIL import Image, ImageDraw, ImageFilter

    mode = item["cover_mode"]
    if mode not in VALID_MODES:
        raise ValueError(f"invalid cover_mode: {mode}")
    video = Path(item["video"])
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")

    if is_song_cover_mode(mode):
        song_cover = profile.get("song_cover")
        if not isinstance(song_cover, dict):
            raise ValueError("creator profile has no song_cover configuration")
        reference = SKILL_ROOT / "assets" / str(song_cover.get("asset", ""))
        if not reference.is_file():
            raise FileNotFoundError(f"song background missing: {reference}")
        item["reference_image"] = str(reference.resolve())
    else:
        reference = Path(item["reference_image"]) if item["reference_image"] else output.parent.parent / "cover-references" / f"{item['clip_id']}.png"
        if not reference.is_file():
            extract_reference(video, reference, item.get("cover_time_seconds"))
            item["reference_image"] = str(reference.resolve())

    canvas = autoclip_canvas(Image.open(reference))
    safe_width = int(CANVAS[0] * 0.75) - 40
    probe = ImageDraw.Draw(canvas)
    if not is_song_cover_mode(mode):
        source_region = item.get("cover_source_region", "auto")
        item["cover_guest_images_used"] = composite_guest_characters(
            canvas,
            [Path(value) for value in item.get("guest_character_images_resolved", [])],
            source_region=source_region,
        )
        phrases = cover_emotes.split_cover_phrases(
            item.get("cover_text_primary") or item.get("title", ""),
            item.get("cover_text_secondary", ""),
        )
        emote = None
        if not item["cover_guest_images_used"]:
            emote = cover_emotes.select_emote(
                "｜".join([item.get("title", ""), *phrases]),
                emotion=item.get("cover_emotion", cover_emotes.AUTO_EMOTION),
                label=item.get("cover_emote", ""),
                creator=creator,
            )
        item["cover_emotion_used"] = (
            str(emote.get("category") or "") if emote else ""
        )
        item["cover_emote_used"] = ""

    if is_song_cover_mode(mode):
        song_cover = profile["song_cover"]
        text = f"{song_cover['prefix']}{item['cover_text_primary']}"
        text_y = float(song_cover.get("text_y", 0.78))
        font = fit_autoclip_font(probe, text, safe_width, 150)
        draw_autoclip_text(
            canvas,
            (CANVAS[0] / 2, CANVAS[1] * text_y),
            text,
            font,
            (255, 225, 0, 255),
            12,
        )
    else:
        phrase_layout = cover_layout.render_cover_regions(
            canvas,
            item.get("cover_text_primary", ""),
            item.get("cover_text_secondary", ""),
            profile,
            source_region=source_region,
            has_emote=bool(emote),
            fit_font=fit_autoclip_font,
            draw_text=draw_autoclip_text,
        )
        if emote:
            sentence_end = None
            if phrase_layout:
                sentence_end = (
                    float(phrase_layout["suffix_x"]),
                    float(phrase_layout["suffix_y"]),
                )
            item["cover_emote_used"] = cover_layout.composite_cover_emote(
                canvas,
                emote,
                source_region=source_region,
                sentence_end=sentence_end,
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    final_image = canvas.convert("RGB").filter(ImageFilter.SHARPEN)
    final_image.save(output, format="JPEG", quality=95)



def render_local(args: argparse.Namespace) -> int:
    """Render one same-folder cover per video without a publishing manifest."""
    profiles = load_profiles()
    errors = validate_profiles(profiles)
    if errors:
        raise SystemExit("\n".join(errors))
    if args.creator not in profiles:
        raise SystemExit(f"unknown creator profile: {args.creator}")
    profile = profiles[args.creator]
    copy_path = Path(args.copy).resolve()
    clips_dir = Path(args.clips_dir).resolve()
    with copy_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("title and cover CSV has no rows")

    failures = 0
    processed = 0
    target_video = Path(str(getattr(args, "only_video", "") or "")).name
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        clip_id = (row.get("clip_id") or f"{index:03d}").strip()
        row_video = Path(str(row.get("video") or "")).name
        if target_video and target_video not in {row_video, clip_id}:
            continue
        processed += 1
        if clip_id in seen:
            print(f"blocked {clip_id}: duplicate clip_id")
            failures += 1
            continue
        seen.add(clip_id)
        video = find_video(clips_dir, clip_id, row.get("video", ""), copy_path.parent)
        try:
            if video is None or not video.is_file():
                raise FileNotFoundError(f"video not found for clip_id {clip_id}")
            if video.parent.resolve() != clips_dir:
                raise ValueError(f"video must be inside clips-dir: {video}")
            output = video.with_name(f"{video.stem}-cover.jpg")
            existed = output.exists()
            item = {
                "clip_id": clip_id,
                "video": str(video),
                "title": (row.get("title") or row.get("title_a") or "").strip(),
                "cover_mode": (row.get("cover_mode") or "quote-impact").strip(),
                "cover_source_region": (row.get("cover_source_region") or "auto").strip(),
                "cover_time_seconds": optional_nonnegative_float(
                    row.get("cover_time_seconds", ""), "cover_time_seconds"
                ),
                "cover_text_primary": (
                    row.get("cover_text_primary") or row.get("cover_text_1") or ""
                ).strip(),
                "cover_text_secondary": (
                    row.get("cover_text_secondary") or row.get("cover_text_2") or ""
                ).strip(),
                "cover_emotion": (
                    row.get("cover_emotion") or cover_emotes.AUTO_EMOTION
                ).strip(),
                "cover_emote": (row.get("cover_emote") or "").strip(),
                "reference_image": str(
                    resolve_input(row.get("reference_image", ""), copy_path.parent) or ""
                ),
                "evidence_image": str(
                    resolve_input(row.get("evidence_image", ""), copy_path.parent) or ""
                ),
                "collaboration_type": (row.get("collaboration_type") or "single").strip(),
                "participants": split_names(row.get("participants")),
                "participant_profile_keys": split_names(
                    row.get("participant_profile_keys")
                ),
                "speaker_evidence": (row.get("speaker_evidence") or "").strip(),
                "guest_dialogue_verified": truthy(row.get("guest_dialogue_verified")),
                "cover": str(output),
            }
            guest_images, missing_guests = resolve_guest_character_images(
                row, profiles, args.creator, copy_path.parent
            )
            item["guest_character_images_resolved"] = [
                str(path) for path in guest_images
            ]
            item["guest_character_images_missing"] = missing_guests
            if not existed or args.force:
                if not is_song_cover_mode(item["cover_mode"]) and not item["reference_image"]:
                    with tempfile.TemporaryDirectory(prefix=f"{clip_id}-cover-") as temp_dir:
                        reference = Path(temp_dir) / f"{clip_id}.png"
                        extract_reference(video, reference, item.get("cover_time_seconds"))
                        item["reference_image"] = str(reference)
                        render_cover(item, profile, output, creator=args.creator)
                else:
                    render_cover(item, profile, output, creator=args.creator)
            item["cover"] = str(output)
            item_problems = item_errors(item, upload_ready=False)
            if item_problems:
                raise ValueError("; ".join(item_problems))
            action = "reused" if existed and not args.force else "rendered"
            guest_count = len(item.get("cover_guest_images_used", []))
            guest_note = f"\tguests={guest_count}"
            if item.get("guest_character_images_missing"):
                guest_note += "\tmissing=" + ",".join(
                    item["guest_character_images_missing"]
                )
            print(f"{action}\t{video.name}\t{output.name}{guest_note}")
        except Exception as exc:
            print(f"blocked {clip_id}: {exc}")
            failures += 1
    if target_video and not processed:
        print(f"blocked: copy CSV has no row for {target_video}")
        return 1
    print(f"local_covers={processed - failures} blocked={failures} folder={clips_dir}")
    return 1 if failures else 0

def render_covers(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    profiles = load_profiles()
    creator = manifest["creator_profile"]
    profile = profiles[creator]
    cover_dir = manifest_path.parent / "covers"
    failures = 0
    for item in manifest["items"]:
        if item.get("state") == "success" and item.get("bvid"):
            continue
        output = cover_dir / f"{item['clip_id']}.jpg"
        try:
            guest_images, missing_guests = resolve_guest_character_images(
                item,
                profiles,
                creator,
                Path(str(manifest.get("copy_source") or manifest_path)).parent,
            )
            item["guest_character_images_resolved"] = [
                str(path) for path in guest_images
            ]
            item["guest_character_images_missing"] = missing_guests
            render_cover(item, profile, output, creator=creator)
            item["cover"] = str(output.resolve())
            item["state"] = "cover_ready"
            item["last_error"] = ""
        except Exception as exc:
            item["state"] = "blocked"
            item["last_error"] = str(exc)
            failures += 1
        item["updated_at"] = now_iso()
        manifest["updated_at"] = now_iso()
        atomic_json(manifest_path, manifest)
    print(f"rendered={len(manifest['items']) - failures} blocked={failures}")
    return 1 if failures else 0


def probe_video(path: Path) -> str | None:
    try:
        result = run_checked_with_transient_retries(
            [executable("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path)],
            capture_output=True, text=True, **hidden_subprocess_kwargs(),
        )
        return None if json.loads(result.stdout).get("streams", []) else "video has no decodable video stream"
    except Exception as exc:
        return f"ffprobe failed: {exc}"


def item_errors(item: dict[str, Any], upload_ready: bool) -> list[str]:
    errors: list[str] = []
    title = item.get("title", "")
    if not title:
        errors.append("missing title")
    if len(title) > 80:
        errors.append("title exceeds 80 characters")
    for field in ("title", "cover_text_primary", "cover_text_secondary"):
        if "'" in item.get(field, ""):
            errors.append(f"{field} contains an English single quote")
        if "\ufffd" in item.get(field, "") or re.search(r"\?{2,}", item.get(field, "")):
            errors.append(f"{field} has probable text-encoding damage")
    if item.get("cover_mode") not in VALID_MODES:
        errors.append("invalid cover_mode")
    if item.get("cover_source_region", "auto") not in VALID_SOURCE_REGIONS:
        errors.append("invalid cover_source_region")
    if is_song_cover_mode(item.get("cover_mode", "")):
        song_title = item.get("cover_text_primary", "")
        if not 1 <= len(song_title) <= 24:
            errors.append("song title must be 1-24 characters")
        if item.get("cover_text_secondary", ""):
            errors.append("song cover must use one line only")
    else:
        try:
            cover_emotes.validate_cover_lines(
                item.get("cover_text_primary", ""),
                item.get("cover_text_secondary", ""),
            )
        except ValueError as exc:
            errors.append(str(exc))
        phrases = cover_emotes.split_cover_phrases(
            item.get("cover_text_primary", ""), item.get("cover_text_secondary", "")
        )
        cover_copy = "｜".join(phrases)
        emoji_count = sum(
            cover_copy.count(emoji)
            for emoji in cover_emotes.ALLOWED_COVER_EMOJIS
        )
        if emoji_count > 1:
            errors.append("narrative cover allows at most one approved emoji")
        if any(
            0x1F300 <= ord(char) <= 0x1FAFF
            and char not in cover_emotes.ALLOWED_COVER_EMOJIS
            for char in cover_copy
        ):
            errors.append("narrative cover contains an unsupported emoji")
        emotion = str(item.get("cover_emotion") or cover_emotes.AUTO_EMOTION)
        if emotion not in {
            "", cover_emotes.AUTO_EMOTION, cover_emotes.NO_EMOTE,
            *cover_emotes.EMOTION_CATEGORIES,
        }:
            errors.append("invalid cover emotion")
    video = Path(item.get("video", ""))
    if not video.is_file():
        errors.append("video missing")
    else:
        error = probe_video(video)
        if error:
            errors.append(error)
    cover = Path(item.get("cover", ""))
    if not cover.is_file():
        errors.append("cover missing")
    else:
        from PIL import Image
        try:
            with Image.open(cover) as image:
                if image.size != CANVAS:
                    errors.append(f"cover size is {image.size}, expected {CANVAS}")
        except Exception as exc:
            errors.append(f"cover unreadable: {exc}")
    if item.get("cover_mode") == "evidence-reaction":
        evidence = item.get("evidence_image", "")
        if not evidence or not Path(evidence).is_file():
            errors.append("evidence image missing")
    if upload_ready and (not isinstance(item.get("tid"), int) or item["tid"] <= 0):
        errors.append("upload tid is not approved")
    return errors


def check(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    titles: dict[str, str] = {}
    failures = 0
    report: list[dict[str, Any]] = []
    for item in manifest["items"]:
        errors = item_errors(item, args.upload_ready)
        title = item.get("title", "")
        if title in titles:
            errors.append(f"duplicate title with {titles[title]}")
        else:
            titles[title] = item["clip_id"]
        if errors:
            item["state"] = "blocked"
            item["last_error"] = "; ".join(errors)
            failures += 1
        elif item.get("state") not in {"success", "uploading"}:
            item["state"] = "validated"
            item["last_error"] = ""
        item["updated_at"] = now_iso()
        report.append({"clip_id": item["clip_id"], "state": item["state"], "errors": errors})
    manifest["updated_at"] = now_iso()
    manifest["validation"] = {"upload_ready": args.upload_ready, "checked_at": now_iso(), "failures": failures, "items": report}
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest["validation"], ensure_ascii=False, indent=2))
    return 1 if failures else 0


def validate_profiles_command(_args: argparse.Namespace) -> int:
    errors = validate_profiles(load_profiles())
    if errors:
        print("\n".join(errors))
        return 1
    print("profiles valid")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-profiles")
    validate.set_defaults(func=validate_profiles_command)
    prep = sub.add_parser("prepare")
    prep.add_argument("--copy", required=True)
    prep.add_argument("--clips-dir", required=True)
    prep.add_argument("--creator", required=True)
    prep.add_argument("--output", required=True)
    prep.add_argument("--force", action="store_true")
    prep.set_defaults(func=prepare)
    render = sub.add_parser("render-covers")
    render.add_argument("--manifest", required=True)
    render.set_defaults(func=render_covers)
    verify = sub.add_parser("check")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--upload-ready", action="store_true")
    verify.set_defaults(func=check)
    local = sub.add_parser("render-local")
    local.add_argument("--copy", required=True)
    local.add_argument("--clips-dir", required=True)
    local.add_argument("--creator", required=True)
    local.add_argument("--force", action="store_true")
    local.add_argument("--only-video", default="")
    local.set_defaults(func=render_local)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
