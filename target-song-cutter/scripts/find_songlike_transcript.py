#!/usr/bin/env python3
"""Find long, lyric-like clusters in a timestamped transcript.

This is a recall-oriented discovery helper.  Its output is a candidate list,
not an authority for song boundaries: every hit still needs the surrounding
audio inspected for false starts, restarts, and a complete ending.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Row:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def read_rows(path: Path) -> list[Row]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            Row(float(item["start_seconds"]), float(item["end_seconds"]), item["text"].strip())
            for item in reader
            if item.get("text", "").strip()
        ]


def stamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def find_clusters(
    rows: list[Row],
    *,
    long_row_seconds: float,
    join_gap_seconds: float,
    context_seconds: float,
    minimum_long_seconds: float,
) -> list[tuple[float, float, float, list[Row]]]:
    long_rows = [row for row in rows if row.duration >= long_row_seconds]
    if not long_rows:
        return []

    groups: list[list[Row]] = [[long_rows[0]]]
    for row in long_rows[1:]:
        if row.start - groups[-1][-1].end <= join_gap_seconds:
            groups[-1].append(row)
        else:
            groups.append([row])

    output: list[tuple[float, float, float, list[Row]]] = []
    for group in groups:
        long_seconds = sum(row.duration for row in group)
        if long_seconds < minimum_long_seconds:
            continue
        start = max(0.0, group[0].start - context_seconds)
        end = group[-1].end + context_seconds
        context = [row for row in rows if row.end >= start and row.start <= end]
        output.append((start, end, long_seconds, context))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", nargs="+", type=Path)
    parser.add_argument("--long-row-seconds", type=float, default=7.5)
    parser.add_argument("--join-gap-seconds", type=float, default=35.0)
    parser.add_argument("--context-seconds", type=float, default=20.0)
    parser.add_argument("--minimum-long-seconds", type=float, default=35.0)
    parser.add_argument("--snippet-chars", type=int, default=260)
    args = parser.parse_args()

    for path in args.transcripts:
        rows = read_rows(path)
        print(f"SOURCE\t{path}")
        clusters = find_clusters(
            rows,
            long_row_seconds=args.long_row_seconds,
            join_gap_seconds=args.join_gap_seconds,
            context_seconds=args.context_seconds,
            minimum_long_seconds=args.minimum_long_seconds,
        )
        for index, (start, end, long_seconds, context) in enumerate(clusters, 1):
            snippet = " / ".join(row.text.replace("\n", " ") for row in context)
            snippet = snippet[: args.snippet_chars]
            print(
                f"{index:03d}\t{stamp(start)}\t{stamp(end)}\t"
                f"long={long_seconds:.1f}s\t{snippet}"
            )
        print(f"TOTAL\t{len(clusters)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
