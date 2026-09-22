#!/usr/bin/env python3
"""Export exact source intervals from a reviewed clip manifest."""

from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise RuntimeError("Clip manifest is empty")
    for row in rows:
        if (row.get("status") or "accept").strip().casefold() != "accept":
            print(f"SKIPPED {row.get('output_name', '')}: status={row.get('status', '')}", flush=True)
            continue
        source = Path(row["source"])
        start = float(row["start_seconds"])
        end = float(row["end_seconds"])
        destination = args.output / row["output_name"]
        if not source.is_file():
            raise FileNotFoundError(source)
        if end <= start:
            raise ValueError(f"Invalid interval for {destination.name}")
        subprocess.run(
            [
                str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(destination),
            ],
            check=True,
        )
        print(f"CREATED {destination} ({end - start:.3f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
