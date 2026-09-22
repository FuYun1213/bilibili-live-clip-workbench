#!/usr/bin/env python3
"""Categorized cover-emote catalog and deterministic emotion matching."""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = SKILL_ROOT / "assets" / "cover-emotes"
AUTO_EMOTION = "自动判断"
NO_EMOTE = "不使用表情包"
EMOTION_CATEGORIES = (
    "开心赞同",
    "吐槽得意",
    "震惊崩溃",
    "无语嫌弃",
    "生气红温",
    "委屈哭泣",
    "害羞亲昵",
    "困倦晚安",
    "吃喝日常",
    "动作支援",
)

ALLOWED_COVER_EMOJIS = ("😋", "😅", "😁", "🤣", "🥵", "🤓")
MAX_COVER_REGION_CHARS = 22
MAX_COVER_REGION_LINES = 2
EMOJI_MEANINGS = {
    "😋": "可爱或美味",
    "😅": "无语或尴尬",
    "😁": "大笑",
    "🤣": "搞笑",
    "🥵": "暧昧或性暗示",
    "🤓": "犯蠢或唐诗",
}


EMOTION_KEYWORDS = {
    "生气红温": ("生气", "红温", "气死", "愤怒", "骂", "坏", "邦邦", "拳", "破防"),
    "委屈哭泣": ("委屈", "哭", "泪", "伤心", "难过", "可怜", "毕业", "呜呜"),
    "震惊崩溃": ("震惊", "崩溃", "救命", "离谱", "事故", "吓", "傻眼", "没声音", "消失"),
    "无语嫌弃": ("无语", "嫌弃", "恶心", "变态", "拒绝", "尴尬", "疑惑", "问号", "搞不懂"),
    "害羞亲昵": ("害羞", "喜欢", "爱", "亲", "抱", "啵", "老婆", "老公", "宝宝", "约会", "迷人"),
    "困倦晚安": ("晚安", "睡", "困", "哈欠", "熬夜", "做梦"),
    "吃喝日常": ("吃", "喝", "饭", "奶", "肠粉", "可乐", "蛋糕", "外卖", "牛肉", "茶"),
    "动作支援": ("打call", "支持", "上舰", "加油", "冲", "指着", "操作", "动手"),
    "吐槽得意": ("吐槽", "得意", "坏笑", "拆穿", "反转", "自爆", "骗", "果然", "没想到"),
    "开心赞同": ("开心", "好耶", "哈哈", "笑", "有趣", "感动", "谢谢", "答应", "好的"),
}


def split_cover_phrases(primary: str, secondary: str = "") -> list[str]:
    """Return one to five cover phrases while accepting legacy two-line rows."""
    value = "｜".join(part for part in (primary.strip(), secondary.strip()) if part)
    return [part.strip() for part in re.split(r"[｜|\r\n]+", value) if part.strip()]


def cover_region_lines(value: str) -> list[str]:
    """Return the user-authored lines in one independent cover region."""
    normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def visible_cover_length(value: str) -> int:
    """Count readable copy while ignoring spacing, punctuation, and approved emoji."""
    visible = str(value or "")
    for emoji in ALLOWED_COVER_EMOJIS:
        visible = visible.replace(emoji, "")
    visible = re.sub(r"[\s。！？!?、，,；;：:]", "", visible)
    return len(visible)


def validate_cover_lines(
    primary: str, secondary: str, *, require_secondary: bool = True
) -> tuple[str, str]:
    """Validate 2–3 phrases: one above, one or two below, preserving manual wraps."""
    primary = str(primary or "").strip()
    secondary = str(secondary or "").strip()
    if not primary:
        raise ValueError("普通切片封面上区不能为空")
    if require_secondary and not secondary:
        raise ValueError("普通切片封面下区不能为空；请分别填写上下两个文案区")
    if re.search(r"[｜|]", primary):
        raise ValueError("封面上区只放第一句；其余 1–2 句请填写在下区")
    lower_phrases = [part.strip() for part in re.split(r"[｜|]", secondary)]
    if secondary and (len(lower_phrases) > 2 or not all(lower_phrases)):
        raise ValueError("副标题共 2–3 句；下区可用｜分隔第二句和第三句")
    normalized: list[str] = []
    regions = [("上区", primary)] + [
        ("下区" if len(lower_phrases) == 1 else f"下区第{index + 2}句", value)
        for index, value in enumerate(lower_phrases)
    ]
    for label, value in regions:
        if not value:
            normalized.append("")
            continue
        lines = cover_region_lines(value)
        if len(lines) > MAX_COVER_REGION_LINES:
            raise ValueError(f"普通切片封面{label}最多手动换成 2 行")
        visible = visible_cover_length(value)
        if not 3 <= visible <= MAX_COVER_REGION_CHARS:
            raise ValueError(
                f"普通切片封面{label}应为 3–{MAX_COVER_REGION_CHARS} 个有效文字"
            )
        normalized.append("\n".join(lines))
    combined = primary + secondary
    emoji_count = sum(combined.count(emoji) for emoji in ALLOWED_COVER_EMOJIS)
    if emoji_count > 1:
        raise ValueError("普通切片封面最多使用 1 个指定 emoji")
    unsupported = {
        char
        for char in combined
        if 0x1F300 <= ord(char) <= 0x1FAFF
        and char not in ALLOWED_COVER_EMOJIS
    }
    if unsupported:
        raise ValueError(
            "普通切片封面只能使用这些 emoji：" + "".join(ALLOWED_COVER_EMOJIS)
        )
    return normalized[0], "｜".join(normalized[1:])


def infer_emotion(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "")).casefold()
    scored = []
    for order, category in enumerate(EMOTION_CATEGORIES):
        score = sum(
            2 if len(keyword) >= 2 else 1
            for keyword in EMOTION_KEYWORDS.get(category, ())
            if keyword.casefold() in normalized
        )
        scored.append((score, -order, category))
    best = max(scored)
    return best[2] if best[0] else "吐槽得意"


def creator_catalog_path(creator: str) -> Path | None:
    key = re.sub(r"[^0-9A-Za-z_-]+", "", str(creator or "").strip())
    if not key:
        return None
    return CATALOG_ROOT / key / "catalog.csv"


def load_catalog(
    path: Path | None = None, *, creator: str = ""
) -> list[dict[str, Any]]:
    """Load one creator's catalog; never fall back to another creator."""
    if path is None:
        path = creator_catalog_path(creator)
    if path is None or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result: list[dict[str, Any]] = []
    for row in rows:
        relative = Path(str(row.get("local_path") or ""))
        asset = relative if relative.is_absolute() else SKILL_ROOT / relative
        if not asset.is_file():
            continue
        result.append({
            **row,
            "path": asset.resolve(),
            "overlay_style": str(row.get("overlay_style") or "sticker").strip(),
        })
    return result


def emote_labels(
    catalog: list[dict[str, Any]] | None = None, *, emotion: str = "",
    creator: str = "",
) -> list[str]:
    rows = catalog if catalog is not None else load_catalog(creator=creator)
    return [
        str(row.get("label") or "").strip()
        for row in rows
        if str(row.get("label") or "").strip()
        and (not emotion or str(row.get("category") or "") == emotion)
    ]


def select_emote(
    text: str,
    *,
    emotion: str = "",
    label: str = "",
    creator: str = "",
    catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    rows = catalog if catalog is not None else load_catalog(creator=creator)
    if not rows or emotion == NO_EMOTE or label == NO_EMOTE:
        return None
    if label and label not in {"自动挑选", AUTO_EMOTION}:
        return next((row for row in rows if row.get("label") == label), None)
    category = emotion if emotion in EMOTION_CATEGORIES else infer_emotion(text)
    candidates = [row for row in rows if row.get("category") == category]
    if not candidates:
        candidates = rows
    digest = hashlib.sha256(str(text or category).encode("utf-8")).digest()
    return candidates[int.from_bytes(digest[:4], "big") % len(candidates)]

def extract_cover_emoji(text: str) -> tuple[str, str]:
    """Remove at most one allowed cover emoji for separate color rendering."""
    value = str(text or "")
    found = [emoji for emoji in ALLOWED_COVER_EMOJIS if emoji in value]
    emoji = found[0] if found else ""
    for candidate in ALLOWED_COVER_EMOJIS:
        value = value.replace(candidate, "")
    value = value.replace("\ufe0f", "").strip()
    return value, emoji
