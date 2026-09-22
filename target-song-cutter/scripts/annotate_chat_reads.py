#!/usr/bin/env python3
"""Mark transcript lines that closely repeat nearby danmaku or Super Chats."""

from __future__ import annotations

import argparse
import csv
import re
from bisect import bisect_left, bisect_right
from difflib import SequenceMatcher
from pathlib import Path

from analyze_bililive_xml import parse_optional_xml


READ_TAG_RE = re.compile(r"^【(?:读弹幕|读SC)】")
NOISE_RE = re.compile(r"[\s，。！？、；：,.!?;:~～‘’“”\"'（）()【】\[\]<>《》]+")
SPEECH_PREFIX_RE = re.compile(
    r"^(?:弹幕|有人|他说|她说|这个人|这个SC|这条SC|这个醒目留言)"
    r"(?:问|说|写|发|是|就是|在说)?"
)
GENERIC_CHAT_RE = re.compile(r"^(?:哈+|啊+|草+|6+|好+|对+|确实|可爱|哈哈哈)$", re.I)


def semantic_text(value: str) -> str:
    text = READ_TAG_RE.sub("", str(value).strip()).casefold()
    text = text.replace("（谢谢sc）", "")
    text = SPEECH_PREFIX_RE.sub("", text)
    return NOISE_RE.sub("", text)


def similarity(left: str, right: str) -> float:
    """Return a conservative score for spoken-message semantic repetition."""
    left = semantic_text(left)
    right = semantic_text(right)
    if min(len(left), len(right)) < 4 or GENERIC_CHAT_RE.fullmatch(right):
        return 0.0
    shorter, longer = sorted((left, right), key=len)
    if shorter in longer and len(shorter) / len(longer) >= 0.52:
        return 1.0
    length_ratio = len(shorter) / len(longer)
    if length_ratio < 0.45:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def transcript_times(row: dict[str, str]) -> tuple[float, float]:
    if row.get("start_seconds") not in (None, ""):
        return float(row["start_seconds"]), float(row["end_seconds"])
    return float(row["start_ms"]) / 1000.0, float(row["end_ms"]) / 1000.0


def annotate_rows(
    rows: list[dict[str, str]],
    danmaku: list[dict],
    superchats: list[dict],
    *,
    window_seconds: float = 20.0,
    threshold: float = 0.76,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    events = []
    for label, source in (("读弹幕", danmaku), ("读SC", superchats)):
        for item in source:
            body = str(item.get("text") or "").strip()
            if len(semantic_text(body)) < 4:
                continue
            events.append((float(item.get("time", item.get("source_time", 0.0))), label, body))
    events.sort(key=lambda item: item[0])
    event_times = [item[0] for item in events]

    annotated = [dict(row) for row in rows]
    audit: list[dict[str, str]] = []
    for index, row in enumerate(annotated):
        original = str(row.get("text") or "").strip()
        if not original or READ_TAG_RE.match(original):
            continue
        start, end = transcript_times(row)
        center = (start + end) / 2.0
        left = bisect_left(event_times, center - window_seconds)
        right = bisect_right(event_times, center + window_seconds)
        best: tuple[float, int, float, str, str] | None = None
        for event_time, label, body in events[left:right]:
            score = similarity(original, body)
            if score < threshold:
                continue
            # Prefer SC over danmaku at the same similarity, then the nearest event.
            candidate = (score, 1 if label == "读SC" else 0, -abs(event_time - center), label, body)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
        if best is None:
            continue
        score, _priority, _distance, label, body = best
        row["text"] = f"【{label}】{original}"
        audit.append(
            {
                "row_index": str(index),
                "start_seconds": f"{start:.3f}",
                "end_seconds": f"{end:.3f}",
                "label": label,
                "similarity": f"{score:.3f}",
                "message": body,
                "transcript_text": original,
            }
        )
    return annotated, audit


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--window-seconds", type=float, default=20.0)
    parser.add_argument("--threshold", type=float, default=0.76)
    args = parser.parse_args(argv)
    if args.window_seconds <= 0 or not 0.0 < args.threshold <= 1.0:
        raise ValueError("window-seconds and threshold must be positive")
    with args.transcript.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    danmaku, superchats, _ = parse_optional_xml(args.xml)
    annotated, audit = annotate_rows(
        rows,
        danmaku,
        superchats,
        window_seconds=args.window_seconds,
        threshold=args.threshold,
    )
    write_rows(args.output, annotated, fields)
    write_rows(
        args.audit,
        audit,
        [
            "row_index", "start_seconds", "end_seconds", "label",
            "similarity", "message", "transcript_text",
        ],
    )
    print(f"Done: annotated {len(audit)} nearby danmaku/SC read(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
