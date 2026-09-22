#!/usr/bin/env python3
"""Burn transparent subtitle PNGs described by a manifest into matching clips."""

from __future__ import annotations

import argparse
import csv
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path


def seconds(value: str) -> float:
    hours, minutes, remainder = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(remainder)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--prefix", action="append")
    parser.add_argument("--margin-bottom", type=float, default=72.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped[row["clip_id"]].append(row)

    for clip in sorted(args.clips.glob("*.mp4")):
        clip_id = clip.name[:3]
        if args.prefix and not any(clip.name.startswith(prefix) for prefix in args.prefix):
            continue
        events = grouped.get(clip_id)
        if not events:
            raise ValueError(f"No subtitle images for {clip.name}")

        command = [
            str(args.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(clip),
        ]
        for event in events:
            command.extend(["-i", event["image"]])

        filters = ["[0:v]setpts=PTS-STARTPTS[v0]"]
        previous = "v0"
        for index, event in enumerate(events, start=1):
            subtitle = f"s{index}"
            output = f"v{index}"
            start = seconds(event["start"])
            end = seconds(event["end"])
            filters.append(f"[{index}:v]format=rgba[{subtitle}]")
            filters.append(
                f"[{previous}][{subtitle}]overlay="
                f"x=(W-w)/2:y=H-h-{args.margin_bottom:g}:enable='between(t,{start:.3f},{end:.3f})'"
                f":eof_action=repeat[{output}]"
            )
            previous = output

        destination = args.output / clip.name
        with tempfile.TemporaryDirectory(prefix="subtitle-filter-", dir=args.output) as temporary:
            filter_script = Path(temporary) / "filter.txt"
            filter_script.write_text(";".join(filters), encoding="utf-8")
            command.extend(
                [
                    "-filter_complex_script",
                    str(filter_script),
                    "-map",
                    f"[{previous}]",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "19",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(destination),
                ]
            )
            subprocess.run(command, check=True)
        # Some Windows terminals still use cp1252 and cannot print Chinese
        # filenames.  The rendered file path is already unambiguous on disk.
        print(f"rendered clip {clip_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
