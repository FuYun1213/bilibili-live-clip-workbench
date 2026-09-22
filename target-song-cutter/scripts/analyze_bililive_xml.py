#!/usr/bin/env python3
"""Turn BililiveRecorder XML into auditable danmaku/SC discovery evidence."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


SPACE = re.compile(r"\s+")


def clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def normalized(text: str) -> str:
    return SPACE.sub(" ", text.strip())


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(buffer.getvalue(), encoding="utf-8-sig")
    temporary.replace(path)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_windows(value: str) -> list[float]:
    windows: list[float] = []
    for item in value.split(","):
        try:
            number = float(item.strip())
        except ValueError as exc:
            raise ValueError(f"invalid window size: {item!r}") from exc
        if number <= 0:
            raise ValueError("window sizes must be positive")
        if number not in windows:
            windows.append(number)
    if not windows:
        raise ValueError("at least one window size is required")
    return windows


def parse_xml(path: Path) -> tuple[list[dict], list[dict]]:
    danmaku: list[dict] = []
    superchats: list[dict] = []
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag == "d":
            parts = element.attrib.get("p", "").split(",")
            try:
                timestamp = float(parts[0])
            except (ValueError, IndexError):
                element.clear()
                continue
            raw_text = element.text or ""
            danmaku.append(
                {
                    "source_time": timestamp,
                    "time": timestamp,
                    "clock": clock(timestamp),
                    # Standard Bilibili XML stores the sender hash in p[6].
                    # Some recorders additionally expose a friendlier user attribute.
                    "user": element.attrib.get("user", "") or (parts[6] if len(parts) > 6 else ""),
                    "raw_text": raw_text,
                    "text": normalized(raw_text),
                }
            )
        elif element.tag == "sc":
            try:
                timestamp = float(element.attrib.get("ts", ""))
            except ValueError:
                element.clear()
                continue
            raw_text = element.text or ""
            superchats.append(
                {
                    "source_time": timestamp,
                    "time": timestamp,
                    "clock": clock(timestamp),
                    "user": element.attrib.get("user", ""),
                    "price": element.attrib.get("price", ""),
                    "display_seconds": element.attrib.get("time", ""),
                    "raw_text": raw_text,
                    "text": normalized(raw_text),
                }
            )
        element.clear()
    return danmaku, superchats



def parse_optional_xml(path: Path | None) -> tuple[list[dict], list[dict], str]:
    """Allow absent engagement data without hiding malformed XML or I/O errors."""
    if path is None:
        return [], [], "missing_xml"
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], [], "missing_xml"
    if size == 0:
        return [], [], "empty_xml"
    danmaku, superchats = parse_xml(path)
    return danmaku, superchats, "available"


def apply_alignment(items: list[dict], offset_seconds: float) -> list[dict]:
    aligned: list[dict] = []
    for source in items:
        item = source.copy()
        item["time"] = max(0.0, item["source_time"] + offset_seconds)
        item["clock"] = clock(item["time"])
        aligned.append(item)
    return aligned

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xml", type=Path, help="Optional danmaku/SC XML")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--window", type=float, default=10.0)
    parser.add_argument("--burst-top", type=int, default=60)
    parser.add_argument("--context", type=float, default=60.0)
    parser.add_argument("--min-repeat", type=int, default=3)
    parser.add_argument("--keyword", action="append", default=[])
    args = parser.parse_args(argv)
    if args.window <= 0 or args.context <= 0 or args.min_repeat < 2:
        raise ValueError("window/context must be positive and min-repeat must be at least 2")

    danmaku, superchats, input_status = parse_optional_xml(args.xml)
    if not danmaku:
        print(f"未发现有效弹幕记录（{input_status}）；继续使用录播音频和转写，保留可用 SC。")

    windows: dict[int, list[dict]] = defaultdict(list)
    for item in danmaku:
        windows[math.floor(item["time"] / args.window)].append(item)
    counts = [len(items) for items in windows.values()]
    mean = statistics.fmean(counts) if counts else 0.0
    deviation = (statistics.pstdev(counts) or 1.0) if counts else 1.0
    median = statistics.median(counts) if counts else 0.0

    window_rows: list[dict] = []
    for index, items in sorted(windows.items()):
        start = index * args.window
        messages = Counter(item["text"] for item in items if item["text"])
        top = " | ".join(f"{text}×{count}" for text, count in messages.most_common(5))
        nearby_sc = [
            sc for sc in superchats if start - args.context <= sc["time"] < start + args.window + args.context
        ]
        window_rows.append(
            {
                "start_seconds": f"{start:.3f}",
                "end_seconds": f"{start + args.window:.3f}",
                "clock": clock(start),
                "count": len(items),
                "unique_users": len({item["user"] for item in items if item["user"]}),
                "z_score": f"{(len(items) - mean) / deviation:.3f}",
                "relative_to_median": f"{len(items) / max(1, median):.3f}",
                "top_messages": top,
                "nearby_sc": " | ".join(f"{sc['clock']} ￥{sc['price']} {sc['text']}" for sc in nearby_sc[:5]),
            }
        )

    burst_rows = [
        row.copy()
        for row in sorted(
            window_rows,
            key=lambda row: (float(row["z_score"]), int(row["count"])),
            reverse=True,
        )[: args.burst_top]
    ]
    for rank, row in enumerate(burst_rows, 1):
        row["rank"] = rank
        row["inspect_start"] = f"{max(0, float(row['start_seconds']) - args.context):.3f}"
        row["inspect_end"] = f"{float(row['end_seconds']) + args.context:.3f}"

    repeated = Counter(item["text"] for item in danmaku if item["text"])
    repeated_rows: list[dict] = []
    by_message: dict[str, list[dict]] = defaultdict(list)
    for item in danmaku:
        by_message[item["text"]].append(item)
    for text, count in repeated.most_common():
        if count < args.min_repeat:
            break
        items = by_message[text]
        repeated_rows.append(
            {
                "text": text,
                "count": count,
                "first_seconds": f"{items[0]['time']:.3f}",
                "first_clock": items[0]["clock"],
                "last_seconds": f"{items[-1]['time']:.3f}",
                "last_clock": items[-1]["clock"],
                "unique_users": len({item["user"] for item in items if item["user"]}),
            }
        )

    keyword_rows: list[dict] = []
    for keyword in args.keyword:
        for item in danmaku:
            if keyword.casefold() in item["text"].casefold():
                keyword_rows.append(
                    {
                        "keyword": keyword,
                        "time_seconds": f"{item['time']:.3f}",
                        "clock": item["clock"],
                        "user": item["user"],
                        "text": item["text"],
                        "inspect_start": f"{max(0, item['time'] - args.context):.3f}",
                        "inspect_end": f"{item['time'] + args.context:.3f}",
                    }
                )

    write_csv(
        args.output / "danmaku-windows.csv",
        ["start_seconds", "end_seconds", "clock", "count", "unique_users", "z_score", "relative_to_median", "top_messages", "nearby_sc"],
        window_rows,
    )
    write_csv(
        args.output / "danmaku-bursts.csv",
        ["rank", "start_seconds", "end_seconds", "clock", "count", "unique_users", "z_score", "relative_to_median", "top_messages", "nearby_sc", "inspect_start", "inspect_end"],
        burst_rows,
    )
    write_csv(
        args.output / "repeated-messages.csv",
        ["text", "count", "first_seconds", "first_clock", "last_seconds", "last_clock", "unique_users"],
        repeated_rows,
    )
    write_csv(
        args.output / "superchats.csv",
        ["time", "clock", "user", "price", "display_seconds", "text"],
        superchats,
    )
    write_csv(
        args.output / "keyword-context.csv",
        ["keyword", "time_seconds", "clock", "user", "text", "inspect_start", "inspect_end"],
        keyword_rows,
    )
    print(
        f"danmaku={len(danmaku)} sc={len(superchats)} windows={len(window_rows)} "
        f"bursts={len(burst_rows)} repeats={len(repeated_rows)} keywords={len(keyword_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
