#!/usr/bin/env python3
"""Apply final live-performance anchors to the last lyric lines."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


FIELDS = (
    "clip",
    "start_seconds",
    "end_seconds",
    "recognized_text",
    "corrected_text",
    "reviewed",
)


def load(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def save(path: Path, rows: list[dict[str, str]]) -> None:
    rows.sort(key=lambda row: (row["clip"], float(row["start_seconds"])))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path}: {len(rows)} reviewed lines")


def set_last_end(rows, prefix: str, end: float) -> None:
    selected = [row for row in rows if row["clip"].startswith(prefix)]
    row = max(selected, key=lambda item: float(item["start_seconds"]))
    row["end_seconds"] = f"{end:.3f}"
    row["recognized_text"] = "[performance tail reviewed]"


def repair_sumire(path: Path) -> None:
    rows = load(path)
    clip_rows = [row for row in rows if row["clip"].startswith("SUM-S01_")]
    tail = sorted(clip_rows, key=lambda row: float(row["start_seconds"]))[-4:]
    anchors = (
        (56.60, 63.52),
        (63.60, 69.42),
        (70.80, 77.72),
        (77.80, 83.50),
    )
    for row, (start, end) in zip(tail, anchors):
        row["start_seconds"] = f"{start:.3f}"
        row["end_seconds"] = f"{end:.3f}"
        row["recognized_text"] = "[performance chorus reviewed]"
    save(path, rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kioi", type=Path, required=True)
    parser.add_argument("--sumire", type=Path, required=True)
    parser.add_argument("--yuchu", type=Path, required=True)
    args = parser.parse_args()

    kioi = load(args.kioi)
    set_last_end(kioi, "KIO-S01_", 174.20)
    save(args.kioi, kioi)

    repair_sumire(args.sumire)

    yuchu = load(args.yuchu)
    set_last_end(yuchu, "YUC-S01_", 179.72)
    save(args.yuchu, yuchu)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
