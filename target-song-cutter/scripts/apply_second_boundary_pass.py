#!/usr/bin/env python3
"""Apply the manually reviewed second boundary pass to the current review stage."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


TRIMS: dict[str, tuple[float, float | None]] = {
    "KIO-001": (57.50, 107.00),
    "KIO-002": (46.20, 102.70),
    "KIO-003": (10.30, 108.00),
    "KIO-007": (5.10, 81.60),
    "KIO-008": (25.20, 96.50),
    "SUM-004": (0.00, 70.55),
    "SUM-005": (0.00, 116.60),
    "VIR-002": (0.00, 76.90),
    "VIR-004": (0.00, 46.00),
    "YUC-001": (0.00, 66.55),
    "YUC-003": (0.00, 18.27),
}


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def matching(stage: Path, clip_id: str) -> Path:
    hits = list(stage.rglob(f"{clip_id}_*.mp4"))
    if len(hits) != 1:
        raise RuntimeError(f"Expected one {clip_id} MP4, found {len(hits)}")
    return hits[0]


def replace_with_trim(path: Path, start: float, end: float | None) -> None:
    temporary = path.with_name(path.stem + ".pass2.mp4")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}"]
    if end is not None:
        command += ["-t", f"{end - start:.3f}"]
    command += [
        "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(temporary),
    ]
    run(command)
    temporary.replace(path)


def prepend_kio_004(stage: Path, source: Path) -> None:
    path = matching(stage, "KIO-004")
    temporary = path.with_name(path.stem + ".pass2.mp4")
    filter_graph = (
        "[0:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS[v0];"
        "[0:a]aresample=48000,asetpts=PTS-STARTPTS[a0];"
        "[1:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS[v1];"
        "[1:a]aresample=48000,asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", "6136.550", "-t", "21.450", "-i", str(source),
        "-i", str(path), "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(temporary),
    ]
    run(command)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--kio-004-source", type=Path, required=True)
    args = parser.parse_args()

    for clip_id, (start, end) in TRIMS.items():
        path = matching(args.stage, clip_id)
        replace_with_trim(path, start, end)
        print(f"TRIMMED {clip_id}")
    prepend_kio_004(args.stage, args.kio_004_source)
    print("PREPENDED KIO-004")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
