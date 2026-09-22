#!/usr/bin/env python3
"""Render three representative video frames into one compact JPEG QA strip."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fractions", default="0.22,0.55,0.84")
    parser.add_argument("--height", type=int, default=300)
    args = parser.parse_args()

    fractions = [float(value) for value in args.fractions.split(",")]
    if len(fractions) != 3 or any(value <= 0 or value >= 1 for value in fractions):
        raise SystemExit("fractions must contain three values between zero and one")
    video_duration = duration(args.input)
    moments = [video_duration * value for value in fractions]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    for moment in moments:
        command.extend(["-ss", f"{moment:.3f}", "-i", str(args.input)])
    command.extend(
        [
            "-filter_complex",
            ";".join(
                [
                    f"[{index}:v]scale=-2:{args.height}[v{index}]" for index in range(3)
                ]
                + ["[v0][v1][v2]hstack=inputs=3[out]"]
            ),
            "-map",
            "[out]",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-y",
            str(args.output),
        ]
    )
    subprocess.run(command, check=True)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
