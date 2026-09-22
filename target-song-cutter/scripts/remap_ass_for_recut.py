#!/usr/bin/env python3
"""Remap reviewed ASS cues from an old non-contiguous edit to a new edit."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def ass_seconds(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def load_plan(path: Path) -> dict[str, list[tuple[float, float]]]:
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


def with_local_offsets(
    ranges: list[tuple[float, float]],
) -> list[tuple[float, float, float, float]]:
    cursor = 0.0
    result = []
    for source_start, source_end in ranges:
        local_end = cursor + source_end - source_start
        result.append((cursor, local_end, source_start, source_end))
        cursor = local_end
    return result


def apply_text_fixes(text: str, clip_id: str, payload: dict) -> str:
    for wrong, right in payload.get("global_replacements", {}).items():
        text = text.replace(wrong, right)
    for wrong, right in payload.get("clip_replacements", {}).get(clip_id, {}).items():
        text = text.replace(wrong, right)
    return payload.get("exact_clip_replacements", {}).get(clip_id, {}).get(text, text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-ass-dir", type=Path, required=True)
    parser.add_argument("--old-plan", type=Path, required=True)
    parser.add_argument("--new-plan", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--replacements", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    old_plan = load_plan(args.old_plan)
    new_plan = load_plan(args.new_plan)
    fixes = (
        json.loads(args.replacements.read_text(encoding="utf-8-sig"))
        if args.replacements
        else {}
    )
    template = args.template.read_text(encoding="utf-8-sig")
    header = template.split("[Events]", 1)[0].rstrip()
    events_header = (
        "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for old_ass in sorted(args.old_ass_dir.glob("*.ass")):
        clip_id = old_ass.name[:3]
        if clip_id not in old_plan or clip_id not in new_plan:
            continue
        old_parts = with_local_offsets(old_plan[clip_id])
        new_parts = with_local_offsets(new_plan[clip_id])
        mapped: list[tuple[float, float, list[str]]] = []
        for line in old_ass.read_text(encoding="utf-8-sig").splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(",", 9)
            if len(fields) != 10:
                continue
            cue_start, cue_end = ass_seconds(fields[1]), ass_seconds(fields[2])
            for old_local_start, old_local_end, source_start, _ in old_parts:
                overlap_start = max(cue_start, old_local_start)
                overlap_end = min(cue_end, old_local_end)
                if overlap_end - overlap_start < 0.30:
                    continue
                source_cue_start = source_start + overlap_start - old_local_start
                source_cue_end = source_start + overlap_end - old_local_start
                for new_local_start, _, new_source_start, new_source_end in new_parts:
                    source_overlap_start = max(source_cue_start, new_source_start)
                    source_overlap_end = min(source_cue_end, new_source_end)
                    if source_overlap_end - source_overlap_start < 0.30:
                        continue
                    new_start = new_local_start + source_overlap_start - new_source_start
                    new_end = new_local_start + source_overlap_end - new_source_start
                    copied = list(fields)
                    copied[9] = apply_text_fixes(copied[9], clip_id, fixes)
                    mapped.append((new_start, new_end, copied))

        mapped.sort(key=lambda item: (item[0], item[1]))
        lines = []
        for start, end, fields in mapped:
            fields[1], fields[2] = ass_time(start), ass_time(end)
            lines.append(",".join(fields))
        destination = args.output_dir / old_ass.name
        destination.write_text(
            header + events_header + "\n".join(lines) + "\n", encoding="utf-8-sig"
        )
        print(f"{destination.name}: {len(lines)} remapped cue(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
