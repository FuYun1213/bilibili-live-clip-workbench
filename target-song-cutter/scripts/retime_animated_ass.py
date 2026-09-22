#!/usr/bin/env python3
"""Move animated ASS dialogue events onto a corrected reviewed-line axis."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


def parse_clock(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def clock(seconds: float) -> str:
    ticks = max(0, round(seconds * 100))
    hours, ticks = divmod(ticks, 360000)
    minutes, ticks = divmod(ticks, 6000)
    secs, ticks = divmod(ticks, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{ticks:02d}"


def load(path: Path) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped[row["clip"].strip()].append(row)
    return grouped


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-lyrics", type=Path, required=True)
    parser.add_argument("--new-lyrics", type=Path, required=True)
    parser.add_argument("--ass-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    old = load(args.old_lyrics)
    new = load(args.new_lyrics)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for clip, old_rows in old.items():
        if clip not in new or len(new[clip]) != len(old_rows):
            raise ValueError(f"Lyric row mismatch for {clip}")
        exact = args.ass_dir / f"{clip}.ass"
        candidates = [exact] if exact.is_file() else sorted(
            args.ass_dir.glob(f"*{clip.split('-', 2)[-1]}.ass")
        )
        if len(candidates) != 1:
            raise ValueError(f"Expected one ASS for {clip}, found {candidates}")
        source = candidates[0]
        lines = source.read_text(encoding="utf-8-sig").splitlines()
        dialogue_indices = [index for index, line in enumerate(lines) if line.startswith("Dialogue:")]
        if len(dialogue_indices) != len(old_rows):
            raise ValueError(f"ASS/lyric count mismatch for {clip}: {len(dialogue_indices)} vs {len(old_rows)}")
        for event_index, (old_row, new_row) in enumerate(zip(old_rows, new[clip])):
            line_index = dialogue_indices[event_index]
            fields = lines[line_index].split(",", 9)
            old_event_start = parse_clock(fields[1])
            old_event_end = parse_clock(fields[2])
            start_shift = float(new_row["start_seconds"]) - float(old_row["start_seconds"])
            end_shift = float(new_row["end_seconds"]) - float(old_row["end_seconds"])
            fields[1] = clock(old_event_start + start_shift)
            fields[2] = clock(old_event_end + end_shift)
            lines[line_index] = ",".join(fields)
        destination = args.output_dir / source.name
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        print(f"{clip}: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
