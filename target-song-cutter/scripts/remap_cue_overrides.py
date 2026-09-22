#!/usr/bin/env python3
"""Remap clip-local subtitle overrides after a non-contiguous recut."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def load_ranges(path: Path) -> dict[str, list[tuple[float, float]]]:
    grouped: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("keep", "yes").strip().lower() not in {
                "1", "true", "yes", "y", "keep"
            }:
                continue
            grouped[row["slice_id"].strip()].append(
                (int(row["order"]), float(row["start_seconds"]), float(row["end_seconds"]))
            )
    return {
        clip_id: [(start, end) for _, start, end in sorted(parts)]
        for clip_id, parts in grouped.items()
    }


def local_to_source(local: float, ranges: list[tuple[float, float]]) -> float | None:
    cursor = 0.0
    for start, end in ranges:
        duration = end - start
        if cursor - 0.03 <= local <= cursor + duration + 0.03:
            return start + min(duration, max(0.0, local - cursor))
        cursor += duration
    return None


def source_to_local(source: float, ranges: list[tuple[float, float]]) -> float | None:
    cursor = 0.0
    for start, end in ranges:
        if start - 0.03 <= source <= end + 0.03:
            return cursor + min(end - start, max(0.0, source - start))
        cursor += end - start
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--old-plan", type=Path, required=True)
    parser.add_argument("--new-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.corrections.read_text(encoding="utf-8-sig"))
    old_ranges = load_ranges(args.old_plan)
    new_ranges = load_ranges(args.new_plan)
    remapped: dict[str, dict[str, str]] = {}
    kept = dropped = 0
    for clip_id, overrides in payload.get("cue_overrides", {}).items():
        mapped: dict[str, str] = {}
        for local_text, text in overrides.items():
            source = local_to_source(float(local_text), old_ranges.get(clip_id, []))
            new_local = (
                source_to_local(source, new_ranges.get(clip_id, []))
                if source is not None
                else None
            )
            if new_local is None:
                dropped += 1
                continue
            mapped[f"{new_local:.2f}"] = text
            kept += 1
        if mapped:
            remapped[clip_id] = mapped
    payload["cue_overrides"] = remapped
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Remapped {kept} cue override(s); dropped {dropped} outside the new cut.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
