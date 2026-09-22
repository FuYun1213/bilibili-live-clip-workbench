#!/usr/bin/env python3
"""Build a human-readable manifest for a completed local media delivery."""

from __future__ import annotations

import argparse
import csv
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
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    metadata: dict[str, dict[str, str]] = {}
    for path in args.metadata:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                metadata[row["clip_id"]] = row

    rows: list[dict[str, object]] = []
    for video in sorted(args.delivery.rglob("*.mp4")):
        clip_id = video.name.split("_", 1)[0]
        relative = video.relative_to(args.delivery)
        parts = relative.parts
        if parts[0] == "song":
            category = "song"
            subtitle_mode = "burned+editable-ass"
            creator = parts[1]
        else:
            category = f"narrative-{parts[1]}"
            subtitle_mode = "burned+editable-ass" if parts[1] == "radio" else "review-mp4+editable-ass"
            creator = parts[2]
        item = metadata.get(clip_id, {})
        rows.append(
            {
                "clip_id": clip_id,
                "creator": creator,
                "category": category,
                "title": item.get("title", video.stem.split("_", 1)[-1]),
                "score": item.get("score", "song"),
                "duration_seconds": f"{duration(video):.3f}",
                "size_mib": f"{video.stat().st_size / 1024**2:.1f}",
                "subtitle_mode": subtitle_mode,
                "video": str(video),
                "ass": str(video.with_suffix('.ass')),
                "cover": str(video.with_name(video.stem + '-cover.jpg')),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"rows={len(rows)} output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
