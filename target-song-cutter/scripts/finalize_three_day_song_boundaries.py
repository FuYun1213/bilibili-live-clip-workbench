#!/usr/bin/env python3
"""Create exact-boundary song masters from the three-day wide candidates."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


SONGS = [
    ("kioi-20260813-210124-834", "001", 3.53, 185.65, "kioi", "KIO-S01_《From The Start》翻唱【柚雨Kioi】.mp4"),
    ("kioi-20260813-210124-834", "002", 2.99, 206.21, "kioi", "KIO-S02_《タイムマシン》翻唱【柚雨Kioi】.mp4"),
    ("kioi-20260813-210124-834", "003", 3.46, 193.70, "kioi", "KIO-S03_《真夜中のドア〜Stay With Me》翻唱【柚雨Kioi】.mp4"),
    ("sumire-20260814-000106-784", "001", 3.40, 89.00, "sumire", "SUM-S01_《外婆桥》翻唱【枝堇SUMIRE】.mp4"),
    ("yuchu-20260813-140752-863", "001", 0.00, 204.60, "yuchu", "YUC-S01_《SOS》翻唱【羽啾chu2u】.mp4"),
    ("yuchu-20260813-140752-863", "002", 8.00, 272.00, "yuchu", "YUC-S02_《砂の子供》翻唱【羽啾chu2u】.mp4"),
    ("yuchu-20260813-140752-863", "003", 7.80, 346.60, "yuchu", "YUC-S03_《Bad Apple!!》翻唱【羽啾chu2u】.mp4"),
    ("yuchu-20260813-140752-863", "004", 0.00, 202.00, "yuchu", "YUC-S04_《カタオモイ》翻唱【羽啾chu2u】.mp4"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    for source_id, number, start, end, creator, output_name in SONGS:
        hits = list((args.candidates / source_id / "clips").glob(f"{number}_*.mp4"))
        if len(hits) != 1:
            raise RuntimeError(f"Expected one candidate for {source_id}/{number}, found {len(hits)}")
        destination = args.output / creator / output_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(hits[0]),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(destination),
            ],
            check=True,
        )
        print(f"CREATED {destination}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
