#!/usr/bin/env python3
"""Turn bracketed BililiveRecorder danmaku into lyric-review rows.

The output is timing evidence, not an automatically approved subtitle file.
Review every line against the rendered clip before setting reviewed=yes.
"""

from __future__ import annotations

import argparse
import csv
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_time(element: ET.Element) -> float:
    return float(element.attrib.get("p", "0").split(",", 1)[0])


def lyric_text(value: str) -> str:
    value = value.strip()
    if value.startswith("【"):
        value = value[1:]
    if value.endswith("】"):
        value = value[:-1]
    return " ".join(value.replace("\u3000", " ").split())


def read_hints(xml_path: Path, start: float, end: float, user_contains: str) -> list[tuple[float, str, str]]:
    rows: list[tuple[float, str, str]] = []
    for _, element in ET.iterparse(xml_path, events=("end",)):
        if element.tag == "d":
            timestamp = parse_time(element)
            user = element.attrib.get("user", "")
            raw = (element.text or "").strip()
            if start <= timestamp <= end and raw.startswith("【") and (not user_contains or user_contains in user):
                text = lyric_text(raw)
                if text and set(text) != {"…"}:
                    rows.append((timestamp, user, text))
        element.clear()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clip", required=True)
    parser.add_argument("--source-in", type=float, required=True)
    parser.add_argument("--lyric-start", type=float, required=True)
    parser.add_argument("--lyric-end", type=float, required=True)
    parser.add_argument("--user-contains", default="")
    parser.add_argument("--max-duration", type=float, default=9.5)
    args = parser.parse_args()

    hints = read_hints(args.xml, args.lyric_start, args.lyric_end, args.user_contains)
    if not hints:
        raise ValueError("No bracketed lyric hints matched the requested range")

    output_rows = []
    for index, (timestamp, user, text) in enumerate(hints):
        start = max(0.0, timestamp - args.source_in)
        next_start = (
            max(start + 0.4, hints[index + 1][0] - args.source_in - 0.12)
            if index + 1 < len(hints)
            else start + min(args.max_duration, max(2.4, 1.0 + len(text) / 4.5))
        )
        natural = start + min(args.max_duration, max(2.4, 1.0 + len(text) / 4.5))
        end = min(next_start, natural)
        output_rows.append({
            "clip": args.clip,
            "start_seconds": f"{start:.3f}",
            "end_seconds": f"{end:.3f}",
            "recognized_text": text,
            "corrected_text": text,
            "reviewed": "no",
            "evidence_user": user,
            "evidence_source_time": f"{timestamp:.3f}",
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_rows[0].keys())
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"{args.output}: {len(output_rows)} lyric hint row(s); review remains required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
