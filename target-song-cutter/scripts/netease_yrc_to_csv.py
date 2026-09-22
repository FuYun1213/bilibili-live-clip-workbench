#!/usr/bin/env python3
"""Convert a saved NetEase YRC payload to reviewed word-timed lyric CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
UNIT_RE = re.compile(r"\((\d+),(\d+),\d+\)([^()]*)")
METADATA_PREFIXES = ("作词", "作曲", "编曲", "制作", "录音", "混音", "母带", "吉他", "贝斯", "鼓")


def parse_yrc(text: str, offset: float) -> list[dict]:
    rows: list[dict] = []
    for raw_line in text.splitlines():
        line = LINE_RE.match(raw_line.strip())
        if not line:
            continue
        start_ms, duration_ms, body = int(line.group(1)), int(line.group(2)), line.group(3)
        units: list[dict] = []
        plain: list[str] = []
        for unit in UNIT_RE.finditer(body):
            unit_start_ms, unit_duration_ms, unit_text = int(unit.group(1)), int(unit.group(2)), unit.group(3)
            if not unit_text:
                continue
            plain.append(unit_text)
            units.append(
                {
                    "start_seconds": unit_start_ms / 1000.0 + offset,
                    "end_seconds": (unit_start_ms + unit_duration_ms) / 1000.0 + offset,
                    "text": unit_text,
                }
            )
        lyric = "".join(plain).strip().strip("〖〗")
        if not lyric or lyric.startswith(METADATA_PREFIXES):
            continue
        rows.append(
            {
                "start_seconds": start_ms / 1000.0 + offset,
                "end_seconds": (start_ms + duration_ms) / 1000.0 + offset,
                "text": lyric,
                "timed_units": units,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--clip", required=True)
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--min-time", type=float, default=0.0)
    parser.add_argument("--max-time", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    yrc = payload.get("yrc", {}).get("lyric") or ""
    if not yrc:
        raise RuntimeError(f"No YRC lyric in {args.input}")
    rows = [
        row for row in parse_yrc(yrc, args.offset)
        if row["end_seconds"] >= args.min_time
        and (args.max_time is None or row["start_seconds"] <= args.max_time)
    ]
    if not rows:
        raise RuntimeError("No lyric rows remained after filtering")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["clip", "start_seconds", "end_seconds", "text", "corrected_text", "timed_units", "reviewed"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "clip": args.clip,
                    "start_seconds": f"{row['start_seconds']:.3f}",
                    "end_seconds": f"{row['end_seconds']:.3f}",
                    "text": row["text"],
                    "corrected_text": row["text"],
                    "timed_units": json.dumps(row["timed_units"], ensure_ascii=False, separators=(",", ":")),
                    "reviewed": "yes",
                }
            )
    print(json.dumps({"clip": args.clip, "rows": len(rows), "first": rows[0]["start_seconds"], "last": rows[-1]["end_seconds"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
