#!/usr/bin/env python3
"""Repair two reviewed lyric timelines after candidate-tail verification.

Stay With Me used a shortened live arrangement, while 砂のこども ran faster
than the public reference and omitted its earliest lines.  The anchors below
come from the no-VAD performance ASR, not from blindly applying studio clocks.
"""

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


SAND_STARTS = (
    0.20,
    12.90,
    22.00,
    29.00,
    38.00,
    46.00,
    52.00,
    61.50,
    74.68,
    93.68,
    98.68,
    112.00,
    122.00,
    130.00,
    140.00,
    149.00,
    159.00,
    189.00,
    198.00,
    207.00,
    216.50,
)


def read(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path}: {len(rows)} reviewed lines")


def repair_kioi(path: Path) -> None:
    rows = read(path)
    output = [
        row
        for row in rows
        if not (
            row["clip"].startswith("KIO-S03_")
            and float(row["start_seconds"]) >= 159.93
        )
    ]
    clip = next(row["clip"] for row in rows if row["clip"].startswith("KIO-S03_"))
    output.append(
        {
            "clip": clip,
            "start_seconds": "162.400",
            "end_seconds": "170.400",
            "recognized_text": "[performance tail reviewed]",
            "corrected_text": "まだ忘れず 大事にしていた",
            "reviewed": "yes",
        }
    )
    output.sort(key=lambda row: (row["clip"], float(row["start_seconds"])))
    write(path, output)


def repair_yuchu(path: Path) -> None:
    rows = read(path)
    sand_rows = [row for row in rows if row["clip"].startswith("YUC-S02_")]
    if len(sand_rows) != 24:
        raise ValueError(f"Expected 24 砂のこども rows, got {len(sand_rows)}")

    # The first three studio-reference lines are not present in the live cut.
    selected = sand_rows[3:]
    if len(selected) != len(SAND_STARTS):
        raise AssertionError("Sand anchor count mismatch")
    repaired = []
    for index, (row, start) in enumerate(zip(selected, SAND_STARTS)):
        next_start = SAND_STARTS[index + 1] if index + 1 < len(SAND_STARTS) else 224.50
        end = min(next_start - 0.08, start + 8.0)
        repaired.append(
            {
                "clip": row["clip"],
                "start_seconds": f"{start:.3f}",
                "end_seconds": f"{end:.3f}",
                "recognized_text": "[performance anchors reviewed]",
                "corrected_text": row["corrected_text"],
                "reviewed": "yes",
            }
        )

    output = [row for row in rows if not row["clip"].startswith("YUC-S02_")]
    output.extend(repaired)
    output.sort(key=lambda row: (row["clip"], float(row["start_seconds"])))
    write(path, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kioi", type=Path, required=True)
    parser.add_argument("--yuchu", type=Path, required=True)
    args = parser.parse_args()
    repair_kioi(args.kioi)
    repair_yuchu(args.yuchu)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
