#!/usr/bin/env python3
"""Frame-accurately cut source intervals and burn matching ASS subtitles."""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def render(row: dict, args: argparse.Namespace) -> str:
    source = Path(row["source"])
    start = float(row["start_seconds"])
    end = float(row["end_seconds"])
    output_name = row["output_name"]
    subtitle = args.ass / f"{Path(output_name).stem}.ass"
    destination = args.output / output_name
    partial = destination.with_suffix(".partial.mp4")
    if not source.is_file():
        raise FileNotFoundError(source)
    if not subtitle.is_file():
        raise FileNotFoundError(subtitle)
    if end <= start:
        raise ValueError(f"Invalid interval for {output_name}")
    command = [
        str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0", "-vf", f"ass='{filter_path(subtitle)}'",
        "-c:v", args.video_codec,
    ]
    if args.video_codec == "h264_nvenc":
        command.extend(["-preset", "p6", "-rc", "vbr", "-cq", str(args.quality), "-b:v", "0"])
    else:
        command.extend(["-preset", "veryfast", "-crf", str(args.quality)])
    command.extend([
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(partial),
    ])
    subprocess.run(command, check=True)
    os.replace(partial, destination)
    return f"DONE {output_name} ({end - start:.3f}s)"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ass", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--video-codec", choices=["h264_nvenc", "libx264"], default="h264_nvenc")
    parser.add_argument("--quality", type=int, default=18)
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if (row.get("status") or "accept").strip().casefold() == "accept"]
    if args.skip_existing:
        rows = [row for row in rows if not (args.output / row["output_name"]).is_file()]
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(render, row, args): row for row in rows}
        for future in as_completed(futures):
            try:
                print(future.result(), flush=True)
            except Exception as exc:
                name = futures[future].get("output_name", "unknown")
                failures.append(f"{name}: {exc}")
                print(f"FAILED {name}: {exc}", flush=True)
    if failures:
        raise RuntimeError("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
