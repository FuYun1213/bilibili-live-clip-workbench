#!/usr/bin/env python3
"""Measure a fixed Live2D mouth ROI and summarize activity per ASS cue."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from pathlib import Path

import numpy as np


EVENT_RE = re.compile(r"^Dialogue: [^,]*,([^,]*),([^,]*),[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$")


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--ass", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=80)
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    graph = (
        f"crop={args.width}:{args.height}:{args.x}:{args.y},"
        f"fps={args.fps},format=rgb24"
    )
    command = [
        args.ffmpeg, "-v", "error", "-i", str(args.video.resolve()),
        "-vf", graph, "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    raw = subprocess.run(command, check=True, capture_output=True).stdout
    frame_bytes = args.width * args.height * 3
    usable = len(raw) - len(raw) % frame_bytes
    frames = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(
        -1, args.height, args.width, 3
    )

    red = frames[..., 0].astype(np.int16)
    green = frames[..., 1].astype(np.int16)
    blue = frames[..., 2].astype(np.int16)
    # The open mouth is a saturated red/pink patch; a closed mouth is a thin gray line.
    mouth_red = (
        (red >= 70)
        & (red - green >= 18)
        & (red - blue >= 8)
        & (green <= 155)
    )
    red_counts = mouth_red.sum(axis=(1, 2))

    cues: list[tuple[float, float, str]] = []
    for line in args.ass.read_text(encoding="utf-8-sig").splitlines():
        match = EVENT_RE.match(line)
        if match:
            cues.append((parse_time(match.group(1)), parse_time(match.group(2)), match.group(3)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["cue", "start", "end", "frames", "red_p50", "red_p90", "red_max", "text"],
        )
        writer.writeheader()
        for index, (start, end, text) in enumerate(cues, 1):
            lo = max(0, int(np.floor(start * args.fps)))
            hi = min(len(red_counts), int(np.ceil(end * args.fps)))
            values = red_counts[lo:hi]
            writer.writerow(
                {
                    "cue": index,
                    "start": f"{start:.3f}",
                    "end": f"{end:.3f}",
                    "frames": len(values),
                    "red_p50": f"{np.percentile(values, 50):.1f}" if len(values) else "",
                    "red_p90": f"{np.percentile(values, 90):.1f}" if len(values) else "",
                    "red_max": int(values.max()) if len(values) else "",
                    "text": text.strip(),
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
