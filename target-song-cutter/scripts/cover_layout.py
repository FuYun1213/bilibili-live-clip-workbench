#!/usr/bin/env python3
"""Comic-style multi-phrase cover layout and emote compositing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable


CANVAS = (1920, 1080)
PRIMARY_FONT_START = 158
SECONDARY_FONT_START = PRIMARY_FONT_START
SAFE_4_3_LEFT = 240
SAFE_4_3_RIGHT = 1680
TEXT_SAFE_INSET = 60
AUTO_WRAP_TARGET = 11
TEXT_STROKE_WIDTH = 11
REGION_CENTERS_Y = {"primary": 230, "secondary": 830}


def hex_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = str(value or "#FFFFFF").strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        color = "FFFFFF"
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha


def composite_cover_emote(
    canvas,
    emote: dict[str, Any],
    *,
    source_region: str,
    sentence_end: tuple[float, float] | None = None,
) -> str:
    """Composite one categorized emote immediately after the cover sentence."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    path = Path(emote["path"])
    with Image.open(path) as source:
        image = source.convert("RGBA")
    style = str(emote.get("overlay_style") or "sticker")
    if style == "sticker":
        bounds = image.getchannel("A").getbbox()
        if bounds:
            image = image.crop(bounds)
        image.thumbnail(
            (330, 330) if sentence_end else (390, 390),
            Image.Resampling.LANCZOS,
        )
    else:
        image = ImageOps.fit(
            image.convert("RGB"), (320, 320), Image.Resampling.LANCZOS
        ).convert("RGBA")
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 319, 319), radius=34, fill=245)
        image.putalpha(mask)
        framed = Image.new("RGBA", (340, 340), (0, 0, 0, 0))
        framed.alpha_composite(image, (10, 10))
        ImageDraw.Draw(framed).rounded_rectangle(
            (6, 6, 333, 333), radius=40, outline=(255, 255, 255, 245), width=8
        )
        image = framed
    safe_left, safe_right = cover_text_bounds(source_region)
    if sentence_end is None:
        x = safe_left if source_region == "radio-left" else safe_right - image.width
        y = CANVAS[1] - image.height - 55
    else:
        end_x, center_y = sentence_end
        x = round(float(end_x) + 24)
        y = round(float(center_y) - image.height / 2)
        x = max(safe_left, min(safe_right - image.width, x))
        y = max(35, min(CANVAS[1] - image.height - 35, y))
    shadow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    alpha = image.getchannel("A").filter(ImageFilter.GaussianBlur(12))
    shadow_image = Image.new("RGBA", image.size, (0, 0, 0, 155))
    shadow_image.putalpha(alpha)
    shadow.alpha_composite(shadow_image, (x + 12, y + 16))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(image, (x, y))
    return str(path.resolve())


def cover_text_bounds(source_region: str, *, suffix_width: int = 0) -> tuple[int, int]:
    """Return a padded text box contained by the centered 4:3 crop."""
    if source_region == "radio-left":
        left, right = 820, SAFE_4_3_RIGHT - TEXT_SAFE_INSET
    else:
        left = SAFE_4_3_LEFT + TEXT_SAFE_INSET
        right = SAFE_4_3_RIGHT - TEXT_SAFE_INSET
    return left, max(left + 420, right - max(0, suffix_width))


def _balanced_line_break(
    value: str, *, measure: Callable = len, suffix_width: float = 0,
) -> tuple[str, str]:
    """Balance rendered widths, gently preferring punctuation and word boundaries."""
    opening = "（([《【“‘"
    closing = "，。！？、；：,.!?;:）)]》】”’"
    candidates = []
    for split_at in range(1, len(value)):
        before, after = value[split_at - 1], value[split_at]
        if before in opening or after in closing:
            continue
        if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
            continue
        first, second = value[:split_at].strip(), value[split_at:].strip()
        if not first or not second:
            continue
        first_width, second_width = measure(first), measure(second) + suffix_width
        # A nearby clause break wins; a far-away comma cannot create a short/long pair.
        boundary_bonus = (first_width + second_width) * 0.16 if (
            before in closing or before.isspace() or after.isspace()
        ) else 0
        score = abs(first_width - second_width) - boundary_bonus
        candidates.append((score, split_at))
    if not candidates:
        return value, ""
    _, split_at = min(candidates)
    return value[:split_at].strip(), value[split_at:].strip()


def wrap_cover_region(
    text: str, *, target_chars: int = AUTO_WRAP_TARGET,
    measure: Callable = len, suffix_width: float = 0,
) -> list[str]:
    """Preserve one manual break, otherwise balance one region over at most two lines."""
    authored = [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    ]
    if len(authored) > 2:
        raise ValueError("封面每区最多手动换成 2 行，不能截断多余文案")
    if len(authored) > 1:
        return authored
    value = authored[0] if authored else ""
    if not value:
        return []
    if len(value) <= target_chars:
        return [value]
    first, second = _balanced_line_break(value, measure=measure, suffix_width=suffix_width)
    return [part for part in (first, second) if part]


def wrap_cover_sentence(text: str, *, target_chars: int = AUTO_WRAP_TARGET) -> list[str]:
    """Compatibility alias for callers that previously supplied one sentence."""
    return wrap_cover_region(text, target_chars=target_chars)


def _join_manual_lines(text: str) -> str:
    """Remove layout-only breaks without joining adjacent English words."""
    text = text.replace("\r", "")
    return re.sub(r"\s*\n\s*", lambda match: (
        " " if match.start() and match.end() < len(text)
        and text[match.start() - 1].isascii() and text[match.start() - 1].isalnum()
        and text[match.end()].isascii() and text[match.end()].isalnum() else ""
    ), text)


def _plan_region(text: str, probe, font, *, lower: bool = False) -> dict[str, Any]:
    import cover_emotes

    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    phrases = [part.strip() for part in re.split(r"[｜|]", text)] if lower else [text]
    if len(phrases) > 2:
        raise ValueError("封面副标题最多 3 句，不能截断多余文案")
    clean, emoji = cover_emotes.extract_cover_emoji(text)
    if lower and len(phrases) == 2:
        # The lower area owns two rows in total, even for legacy three-sentence copy.
        phrases = [
            _join_manual_lines(cover_emotes.extract_cover_emoji(part)[0])
            for part in phrases
        ]
        separator = "" if re.search(r"[，。！？!?；;：:][”’》】]*$", phrases[0]) else "，"
        clean = separator.join(phrases)
    measure = lambda value: probe.textlength(value, font=font)
    suffix_width = round(font.size * 0.95) + 24 if emoji else 0
    if lower and len(phrases) == 2:
        lines = [part for part in _balanced_line_break(
            clean, measure=measure, suffix_width=suffix_width,
        ) if part]
    else:
        lines = wrap_cover_region(clean, measure=measure, suffix_width=suffix_width)
    return {
        "lines": lines,
        "emoji": emoji,
        "single_line": clean if "\n" not in clean and len(phrases) == 1 else "",
    }


def _emoji_metrics(font, emoji: str) -> tuple[int, int]:
    return (round(font.size * 0.95), max(12, round(font.size * 0.15))) if emoji else (0, 0)


def _line_width(probe, value: str, font, emoji: str = "") -> float:
    box = probe.textbbox((0, 0), value, font=font, anchor="mm")
    # Keep bearings as well as the shared outline inside the padded text region.
    text_width = max(float(probe.textlength(value, font=font)), float(box[2] - box[0]))
    emoji_size, gap = _emoji_metrics(font, emoji)
    return text_width + TEXT_STROKE_WIDTH * 2 + emoji_size + gap


def _shared_font(plans: list[dict[str, Any]], probe, width: int, fit_font: Callable):
    """Choose one size for every row, accounting for outline and the final emoji."""
    start_font = fit_font(probe, "国", width, PRIMARY_FONT_START)
    fitted = [
        fit_font(probe, value, width - TEXT_STROKE_WIDTH * 2, start_font.size)
        for plan in plans for value in plan["lines"]
    ]
    font = min(fitted, key=lambda value: value.size) if fitted else start_font
    while font.size > 18:
        if all(
            _line_width(probe, value, font, plan["emoji"] if index == len(plan["lines"]) - 1 else "") <= width
            for plan in plans for index, value in enumerate(plan["lines"])
        ):
            return font
        font = fit_font(probe, "国", width, font.size - 1)
    return font


def draw_cover_emoji(
    canvas, emoji: str, *, center: tuple[int, int], size: int = 176
) -> None:
    """Draw one supported emoji with its color font instead of a tofu square."""
    import os
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    emoji_font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "seguiemj.ttf"
    if not emoji or not emoji_font.is_file():
        return
    font = ImageFont.truetype(str(emoji_font), size=size)
    layer = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        center, emoji, font=font, anchor="mm", embedded_color=True
    )
    alpha = layer.getchannel("A")
    shadow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    blurred = alpha.filter(ImageFilter.GaussianBlur(12))
    shadow.putalpha(blurred.point(lambda value: round(value * 0.62)))
    canvas.alpha_composite(shadow, (10, 12))
    canvas.alpha_composite(layer)


def _draw_region(
    canvas,
    plan: dict[str, Any],
    *,
    center_y: int,
    left: int,
    right: int,
    font,
    color: tuple[int, int, int, int],
    draw_text: Callable,
) -> dict[str, Any] | None:
    from PIL import ImageDraw

    lines = plan["lines"]
    if not lines:
        return None
    emoji = plan["emoji"]
    probe = ImageDraw.Draw(canvas)
    emoji_size, emoji_gap = _emoji_metrics(font, emoji)
    row_height = max(
        probe.textbbox((0, 0), value, font=font, anchor="mm")[3]
        - probe.textbbox((0, 0), value, font=font, anchor="mm")[1]
        for value in lines
    )
    line_step = max(round(font.size * 1.12), row_height + TEXT_STROKE_WIDTH * 2 + 16, emoji_size + 16)
    y_positions = [center_y + (index - (len(lines) - 1) / 2) * line_step for index in range(len(lines))]
    boxes: list[tuple[float, float, float, float]] = []
    emoji_box = None
    suffix_x, suffix_y = float(left), float(center_y)
    for index, (value, y) in enumerate(zip(lines, y_positions)):
        is_emoji_line = bool(emoji and index == len(lines) - 1)
        bounds = probe.textbbox((0, 0), value, font=font, anchor="mm", stroke_width=TEXT_STROKE_WIDTH)
        text_width = bounds[2] - bounds[0]
        group_width = text_width + (emoji_size + emoji_gap if is_emoji_line else 0)
        group_left = left + (right - left - group_width) / 2
        text_center_x = group_left - bounds[0]
        draw_text(
            canvas, (text_center_x, y), value, font, color,
            stroke_width=TEXT_STROKE_WIDTH,
        )
        bbox = probe.textbbox(
            (text_center_x, y), value, font=font, anchor="mm",
            stroke_width=TEXT_STROKE_WIDTH,
        )
        boxes.append(tuple(float(coordinate) for coordinate in bbox))
        suffix_x = float(bbox[2])
        suffix_y = float((bbox[1] + bbox[3]) / 2)
        if is_emoji_line:
            emoji_center_x = suffix_x + emoji_gap + emoji_size / 2
            draw_cover_emoji(
                canvas, emoji,
                center=(round(emoji_center_x), round(suffix_y)), size=emoji_size,
            )
            emoji_box = (
                emoji_center_x - emoji_size / 2, suffix_y - emoji_size / 2,
                emoji_center_x + emoji_size / 2, suffix_y + emoji_size / 2,
            )
            suffix_x = emoji_center_x + emoji_size / 2
    return {
        "suffix_x": suffix_x, "suffix_y": suffix_y, "emoji": emoji,
        "lines": lines, "boxes": boxes, "font_size": font.size,
        "emoji_box": emoji_box,
    }


def render_cover_regions(
    canvas,
    primary: str,
    secondary: str,
    profile: dict[str, Any],
    *,
    source_region: str,
    has_emote: bool,
    fit_font: Callable,
    draw_text: Callable,
) -> dict[str, Any] | None:
    """Render two balanced areas, at most four rows, with one shared font size."""
    from PIL import ImageDraw

    del profile  # Preserve the upper white / lower yellow color scheme.
    left, right = cover_text_bounds(source_region, suffix_width=270 if has_emote else 0)
    probe = ImageDraw.Draw(canvas)
    base_font = fit_font(probe, "国", right - left, PRIMARY_FONT_START)
    plans = [
        _plan_region(primary, probe, base_font),
        _plan_region(secondary, probe, base_font, lower=True),
    ]
    font = _shared_font(plans, probe, right - left, fit_font)
    for plan in plans:
        # Another area's longer copy may reduce the shared size enough to avoid a wrap.
        if plan["single_line"] and _line_width(probe, plan["single_line"], font, plan["emoji"]) <= right - left:
            plan["lines"] = [plan["single_line"]]
    upper, lower = [
        _draw_region(
            canvas, plan, center_y=REGION_CENTERS_Y[name],
            left=left, right=right, font=font, color=color, draw_text=draw_text,
        )
        for plan, name, color in zip(
            plans, ("primary", "secondary"),
            ((255, 255, 255, 255), (255, 225, 0, 255)),
        )
    ]
    result = lower or upper
    if result is not None:
        # Copy so metadata does not contain a reference back to itself.
        result = dict(result)
        result["safe_bounds"] = (left, right)
        result["regions"] = {"primary": upper, "secondary": lower}
    return result


def render_phrase_cluster(
    canvas,
    phrases: list[str],
    profile: dict[str, Any],
    *,
    source_region: str,
    has_emote: bool,
    fit_font: Callable,
    draw_text: Callable,
) -> dict[str, Any] | None:
    """Compatibility wrapper for legacy phrase-list callers."""
    values = [value for value in phrases if value]
    primary = values[0] if values else ""
    if len(values) > 3:
        raise ValueError("封面副标题最多 3 句，不能截断多余文案")
    secondary = "｜".join(values[1:])
    return render_cover_regions(
        canvas,
        primary,
        secondary,
        profile,
        source_region=source_region,
        has_emote=has_emote,
        fit_font=fit_font,
        draw_text=draw_text,
    )
