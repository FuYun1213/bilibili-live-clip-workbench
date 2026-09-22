#!/usr/bin/env python3
"""Build chronological multi-part review clips and record every edit join."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


REQUIRED = {
    "clip_id", "creator", "output_name", "part_order", "source",
    "start_seconds", "end_seconds",
}


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError("missing manifest fields: " + ", ".join(sorted(missing)))
        rows = list(reader)
    if not rows:
        raise ValueError("manifest has no rows")
    return rows


def build_clip(ffmpeg: Path, rows: list[dict[str, str]], destination: Path) -> list[float]:
    ordered = sorted(rows, key=lambda row: int(row["part_order"]))
    orders = [int(row["part_order"]) for row in ordered]
    if orders != list(range(1, len(ordered) + 1)):
        raise ValueError(f"{ordered[0]['clip_id']}: part_order must be contiguous from 1")

    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    durations: list[float] = []
    for row in ordered:
        source = Path(row["source"])
        start = float(row["start_seconds"])
        end = float(row["end_seconds"])
        if not source.is_file():
            raise FileNotFoundError(source)
        if end <= start:
            raise ValueError(f"{row['clip_id']} part {row['part_order']}: invalid interval")
        duration = end - start
        durations.append(duration)
        command.extend(("-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)))

    graph: list[str] = []
    concat_inputs: list[str] = []
    for index in range(len(ordered)):
        graph.append(f"[{index}:v:0]setpts=PTS-STARTPTS[v{index}]")
        graph.append(f"[{index}:a:0]asetpts=PTS-STARTPTS[a{index}]")
        concat_inputs.extend((f"[v{index}]", f"[a{index}]"))
    graph.append(
        "".join(concat_inputs)
        + f"concat=n={len(ordered)}:v=1:a=1[outv][outa]"
    )
    command.extend((
        "-filter_complex", ";".join(graph), "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination),
    ))
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)

    joins: list[float] = []
    elapsed = 0.0
    for duration in durations[:-1]:
        elapsed += duration
        joins.append(elapsed)
    return joins


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--joins-output", type=Path)
    args = parser.parse_args()
    if not args.ffmpeg.is_file():
        parser.error(f"FFmpeg not found: {args.ffmpeg}")

    rows = read_manifest(args.manifest.resolve())
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["clip_id"].strip()].append(row)

    joins_path = args.joins_output or args.output / "edit-joins.csv"
    join_rows: list[dict[str, str]] = []
    for clip_id in sorted(grouped):
        parts = grouped[clip_id]
        output_names = {row["output_name"].strip() for row in parts}
        creators = {row["creator"].strip() for row in parts}
        if len(output_names) != 1 or len(creators) != 1:
            raise ValueError(f"{clip_id}: creator and output_name must be stable across parts")
        destination = args.output.resolve() / next(iter(output_names))
        joins = build_clip(args.ffmpeg.resolve(), parts, destination)
        for index, seconds in enumerate(joins, 1):
            join_rows.append({
                "clip_id": clip_id,
                "creator": next(iter(creators)),
                "output_name": destination.name,
                "join_order": str(index),
                "join_seconds": f"{seconds:.3f}",
                "transition_required": "yes",
            })
        safe_name = destination.name.encode("ascii", "backslashreplace").decode("ascii")
        print(f"CREATED {clip_id} {safe_name} joins={len(joins)}", flush=True)

    joins_path.resolve().parent.mkdir(parents=True, exist_ok=True)
    with joins_path.resolve().open("w", encoding="utf-8-sig", newline="") as handle:
        fields = (
            "clip_id", "creator", "output_name", "join_order", "join_seconds",
            "transition_required",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(join_rows)
    print(f"JOINS {joins_path.resolve()} rows={len(join_rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
