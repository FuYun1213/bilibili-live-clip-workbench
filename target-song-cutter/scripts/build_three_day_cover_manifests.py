#!/usr/bin/env python3
"""Build per-folder AutoClip cover manifests for the three-day delivery."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


SONGS = [
    {
        "creator": "kioi",
        "clip_id": "KIO-S01",
        "title": "柚雨温柔唱起《From The Start》，开口就是复古爵士氛围！【柚雨Kioi】",
        "cover_text_primary": "复古爵士开嗓",
        "cover_text_secondary": "温柔得刚刚好",
    },
    {
        "creator": "kioi",
        "clip_id": "KIO-S02",
        "title": "柚雨翻唱《Time Machine》，把想回到过去的遗憾唱得太戳心！【柚雨Kioi】",
        "cover_text_primary": "想回到过去",
        "cover_text_secondary": "遗憾唱进心里",
    },
    {
        "creator": "kioi",
        "clip_id": "KIO-S03",
        "title": "柚雨翻唱《真夜中のドア》，昭和夜色一下被她唱回来了！【柚雨Kioi】",
        "cover_text_primary": "昭和夜色重现",
        "cover_text_secondary": "一开口就入戏",
    },
    {
        "creator": "sumire",
        "clip_id": "SUM-S01",
        "title": "枝堇轻声唱起《外婆桥》，温柔童谣听着却满是思念！【枝堇SUMIRE】",
        "cover_text_primary": "轻唱外婆桥",
        "cover_text_secondary": "温柔里全是思念",
    },
    {
        "creator": "yuchu",
        "clip_id": "YUC-S01",
        "title": "羽啾甜声翻唱《SOS》，恋爱警报从第一句就拉满！【羽啾chu2u】",
        "cover_text_primary": "恋爱警报拉满",
        "cover_text_secondary": "甜声发出SOS",
    },
    {
        "creator": "yuchu",
        "clip_id": "YUC-S02",
        "title": "羽啾翻唱《砂のこども》，轻柔声线把孤独感唱进夜里！【羽啾chu2u】",
        "cover_text_primary": "砂之子低声吟唱",
        "cover_text_secondary": "孤独落进夜里",
    },
    {
        "creator": "yuchu",
        "clip_id": "YUC-S03",
        "title": "羽啾翻唱《Bad Apple!!》，经典旋律一响东方魂直接回来了！【羽啾chu2u】",
        "cover_text_primary": "东方经典开唱",
        "cover_text_secondary": "前奏一响魂归",
    },
    {
        "creator": "yuchu",
        "clip_id": "YUC-S04",
        "title": "羽啾翻唱《カタオモイ》，直球告白甜得让人反复循环！【羽啾chu2u】",
        "cover_text_primary": "单恋直球告白",
        "cover_text_secondary": "甜到反复循环",
    },
]


FIELDS = [
    "creator",
    "clip_id",
    "title",
    "cover_mode",
    "cover_text_primary",
    "cover_text_secondary",
]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--narrative-copy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    with args.narrative_copy.open("r", encoding="utf-8-sig", newline="") as handle:
        narrative = list(csv.DictReader(handle))
    for row in narrative:
        row["cover_mode"] = "quote-impact"

    creators = sorted({row["creator"] for row in narrative})
    for creator in creators:
        regular = [
            {field: row.get(field, "") for field in FIELDS}
            for row in narrative
            if row["creator"] == creator and "-R" not in row["clip_id"]
        ]
        radio = [
            {field: row.get(field, "") for field in FIELDS}
            for row in narrative
            if row["creator"] == creator and "-R" in row["clip_id"]
        ]
        if regular:
            write_csv(args.output_dir / f"narrative-landscape-{creator}.csv", regular)
        if radio:
            write_csv(args.output_dir / f"narrative-radio-{creator}.csv", radio)

    song_rows = [{**row, "cover_mode": "quote-impact"} for row in SONGS]
    write_csv(args.output_dir.parent / "song-titles-and-covers.csv", song_rows)
    for creator in sorted({row["creator"] for row in song_rows}):
        write_csv(
            args.output_dir / f"song-{creator}.csv",
            [{field: row.get(field, "") for field in FIELDS} for row in song_rows if row["creator"] == creator],
        )

    print(f"narrative={len(narrative)} songs={len(song_rows)} manifests={len(list(args.output_dir.glob('*.csv')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
