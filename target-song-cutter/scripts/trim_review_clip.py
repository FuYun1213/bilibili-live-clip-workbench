#!/usr/bin/env python3
"""Safely replace one review-stage clip with an accurately re-encoded trim."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    args = parser.parse_args()
    temporary = args.input.with_name(args.input.stem + ".trimmed.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{args.start:.3f}",
    ]
    if args.end is not None:
        command += ["-t", f"{args.end - args.start:.3f}"]
    command += [
        "-i", str(args.input), "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(temporary),
    ]
    subprocess.run(command, check=True)
    temporary.replace(args.input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
