#!/usr/bin/env python3
"""Add progressive Gaussian-blur peaks at edit joins without blurring full clips."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--boundaries", required=True)
    parser.add_argument("--radius", type=float, default=0.32)
    parser.add_argument("--sigma", type=float, default=12.0)
    parser.add_argument("--crf", type=int, default=20)
    args = parser.parse_args()

    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    boundaries = sorted(float(value.strip()) for value in args.boundaries.split(",") if value.strip())
    if not source.is_file() or not boundaries or args.radius <= 0 or args.sigma <= 0:
        raise SystemExit("invalid input, boundary, radius, or sigma")

    cuts: list[tuple[float, float | None, bool]] = []
    cursor = 0.0
    for point in boundaries:
        left, right = point - args.radius, point + args.radius
        if left <= cursor:
            raise SystemExit("transition windows overlap or begin before zero")
        cuts.extend(((cursor, left, False), (left, right, True)))
        cursor = right
    cuts.append((cursor, None, False))

    graph: list[str] = [f"[0:v]split={len(cuts)}" + "".join(f"[s{i}]" for i in range(len(cuts)))]
    labels: list[str] = []
    for index, (start, end, transition) in enumerate(cuts):
        end_arg = f":end={end:.6f}" if end is not None else ""
        label = f"v{index}"
        if not transition:
            graph.append(f"[s{index}]trim=start={start:.6f}{end_arg},setpts=PTS-STARTPTS[{label}]")
        else:
            graph.append(
                f"[s{index}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
                f"split=2[base{index}][blurbase{index}]"
            )
            graph.append(f"[blurbase{index}]gblur=sigma={args.sigma:.3f}:steps=3[blur{index}]")
            weight = f"max(0,1-abs(T-{args.radius:.6f})/{args.radius:.6f})"
            graph.append(
                f"[base{index}][blur{index}]blend=all_expr='A*(1-({weight}))+B*({weight})'[{label}]"
            )
        labels.append(f"[{label}]")
    graph.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[v]")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is not available on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source),
        "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ], check=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
