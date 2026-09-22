#!/usr/bin/env python3
"""Add short progressive Gaussian-blur peaks at non-chronological edit joins."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--boundaries", required=True,
                        help="Comma-separated join times on the final clip axis")
    parser.add_argument("--radius", type=float, default=0.32,
                        help="Seconds before and after each join used by the blur ramp")
    parser.add_argument("--sigma", type=float, default=12.0)
    parser.add_argument("--crf", type=int, default=20)
    args = parser.parse_args()

    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if not source.is_file():
        raise SystemExit(f"input does not exist: {source}")
    boundaries = [float(value.strip()) for value in args.boundaries.split(",") if value.strip()]
    if not boundaries:
        raise SystemExit("at least one boundary is required")
    if args.radius <= 0 or args.sigma <= 0:
        raise SystemExit("radius and sigma must be positive")

    weights = [f"max(0,1-abs(T-{point:.6f})/{args.radius:.6f})" for point in boundaries]
    weight = weights[0]
    for extra in weights[1:]:
        weight = f"max({weight},{extra})"
    expression = f"A*(1-({weight}))+B*({weight})"
    graph = (
        f"[0:v]split=2[clear][blurbase];"
        f"[blurbase]gblur=sigma={args.sigma:.3f}:steps=3[blurred];"
        f"[clear][blurred]blend=all_expr='{expression}'[v]"
    )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is not available on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source),
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ]
    subprocess.run(command, check=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
