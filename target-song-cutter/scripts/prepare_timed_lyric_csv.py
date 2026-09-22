#!/usr/bin/env python3
"""Merge reviewed word-timed lyric CSVs with measured live-performance offsets."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


METADATA_PREFIXES = (
    "词：", "曲：", "编曲：", "作词：", "作曲：", "作詞：", "作曲 :", "作词 :",
    "Guitar：", "Bass：", "Drums：", "Piano：", "Strings：", "唄：", "LRC：",
)


def metadata(text: str, start: float) -> bool:
    value = text.strip()
    if value.startswith(METADATA_PREFIXES):
        return True
    return start < 15.0 and " - " in value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output_rows: list[dict] = []
    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        configs = list(csv.DictReader(handle))
    for config in configs:
        source = Path(config["input"])
        clip = config["clip"].strip()
        offset = float(config.get("offset_seconds") or 0)
        duration = float(config["duration_seconds"])
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        kept = 0
        for row in rows:
            start = float(row["start_seconds"])
            end = float(row["end_seconds"])
            text = (row.get("corrected_text") or row.get("text") or "").strip()
            if not text or metadata(text, start):
                continue
            units = json.loads(row.get("timed_units") or "[]")
            shifted_units = []
            for unit in units:
                unit_start = float(unit["start_seconds"]) + offset
                unit_end = max(unit_start + 0.04, float(unit["end_seconds"]) + offset)
                if unit_start >= duration:
                    continue
                shifted_units.append(
                    {
                        "start_seconds": max(0.0, unit_start),
                        "end_seconds": min(duration, unit_end),
                        "text": str(unit["text"]),
                    }
                )
            if not shifted_units:
                continue
            measured_start = min(unit["start_seconds"] for unit in shifted_units)
            measured_end = max(unit["end_seconds"] for unit in shifted_units)
            shifted_start = max(0.0, min(start + offset, measured_start))
            shifted_end = min(duration, max(end + offset, measured_end))
            # Some community KRC files give the last line an absurd duration.
            # Use its final measured unit plus a short readable hold instead.
            if shifted_end - measured_end > 4.0:
                shifted_end = min(duration, measured_end + 0.80)
            if shifted_end <= shifted_start:
                continue
            output_rows.append(
                {
                    "clip": clip,
                    "start_seconds": f"{shifted_start:.3f}",
                    "end_seconds": f"{shifted_end:.3f}",
                    "text": text,
                    "corrected_text": text,
                    "timed_units": json.dumps(shifted_units, ensure_ascii=False, separators=(",", ":")),
                    "reviewed": "yes",
                }
            )
            kept += 1
        if kept == 0:
            raise RuntimeError(f"No lyric rows retained for {clip}")
        print(f"{clip}: {kept} reviewed line(s), offset={offset:+.3f}s")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["clip", "start_seconds", "end_seconds", "text", "corrected_text", "timed_units", "reviewed"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"Done: {len(output_rows)} lyric rows -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
