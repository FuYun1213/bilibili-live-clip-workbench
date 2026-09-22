#!/usr/bin/env python3
"""Build reviewed lyric correction CSV files for the 2026-08-13/14 song cuts.

Public synchronized lyric clocks are mapped onto the locally reviewed
performance arrangements. Only sections actually sung in each master are
retained, so shortened covers never inherit unused studio-track verses.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path


LRC_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")


@dataclass(frozen=True)
class TimedLine:
    start: float
    text: str


TRACKS = {
    "kioi": {
        "From The Start": "KIO-S01_《From The Start》翻唱【柚雨Kioi】",
        "Time Machine": "KIO-S02_《Time Machine》翻唱【柚雨Kioi】",
        "Stay With Me": "KIO-S03_《真夜中のドア〜Stay With Me》翻唱【柚雨Kioi】",
    },
    "sumire": {"外婆桥": "SUM-S01_《外婆桥》翻唱【枝堇SUMIRE】"},
    "yuchu": {
        "SOS": "YUC-S01_《SOS》翻唱【羽啾chu2u】",
        "砂のこども": "YUC-S02_《砂のこども》翻唱【羽啾chu2u】",
        "Bad Apple!!": "YUC-S03_《Bad Apple!!》翻唱【羽啾chu2u】",
        "カタオモイ": "YUC-S04_《カタオモイ》翻唱【羽啾chu2u】",
    },
}


DURATIONS = {
    TRACKS["kioi"]["From The Start"]: 182.12,
    TRACKS["kioi"]["Time Machine"]: 203.22,
    TRACKS["kioi"]["Stay With Me"]: 190.24,
    TRACKS["sumire"]["外婆桥"]: 85.60,
    TRACKS["yuchu"]["SOS"]: 204.60,
    TRACKS["yuchu"]["砂のこども"]: 264.00,
    TRACKS["yuchu"]["Bad Apple!!"]: 338.80,
    TRACKS["yuchu"]["カタオモイ"]: 202.00,
}


def fetch_lrc(record_id: int) -> list[TimedLine]:
    request = urllib.request.Request(
        f"https://lrclib.net/api/get/{record_id}",
        headers={"User-Agent": "CodexSongCutter/1.0 (local subtitle workflow)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    synced = payload.get("syncedLyrics") or ""
    output: list[TimedLine] = []
    for raw in synced.splitlines():
        match = LRC_RE.match(raw.strip())
        if not match:
            continue
        text = match.group(3).strip()
        if not text:
            continue
        output.append(TimedLine(int(match.group(1)) * 60 + float(match.group(2)), text))
    if not output:
        raise ValueError(f"LRCLIB record {record_id} has no synchronized lyrics")
    return output


def mapped(rows, transform, include=lambda _seconds: True) -> list[TimedLine]:
    output = []
    for row in rows:
        start = transform(row.start)
        if include(row.start) and start >= 0:
            output.append(TimedLine(start, row.text))
    return output


def append_rows(destination, clip: str, lines: list[TimedLine], max_hold: float = 8.0) -> None:
    lines = sorted(lines, key=lambda row: row.start)
    duration = DURATIONS[clip]
    for index, line in enumerate(lines):
        if line.start >= duration - 0.20:
            continue
        next_start = lines[index + 1].start if index + 1 < len(lines) else duration
        end = min(next_start - 0.08, line.start + max_hold, duration - 0.12)
        if end <= line.start + 0.40:
            end = min(line.start + 1.20, duration - 0.12)
        destination.append(
            {
                "clip": clip,
                "start_seconds": f"{line.start:.3f}",
                "end_seconds": f"{end:.3f}",
                "recognized_text": "[performance arrangement reviewed]",
                "corrected_text": line.text,
                "reviewed": "yes",
            }
        )


def manual_time_machine() -> list[TimedLine]:
    offset = 2.99
    source = [
        (30.00, "I wish to go back to the times that I loved"),
        (36.00, "Why do the stars shine so bright in the sky"),
        (42.00, "If most of the people are sleeping at night"),
        (48.00, "Why do we only have one chance at life"),
        (54.00, "I wish I could go back in time"),
        (90.00, "So I try to forget all the times that I loved"),
        (96.50, "Why do we remember beautiful lies"),
        (102.00, "We end up regretting them most of our life"),
        (108.50, "Why do we only have one chance to try"),
        (114.00, "I wish I could go back in time"),
        (120.00, "I might fall asleep"),
        (125.00, "I always see you there in my dreams"),
        (130.00, "It's like going back in a time machine"),
        (135.00, "I know when I wake up you're there with me"),
        (141.00, "So don't let me fall asleep"),
        (146.00, "I don't wanna meet you there in my dreams"),
        (152.72, "I know that we'll never build a time machine"),
        (158.64, "It's time for me"),
        (161.80, "To try and wake up again"),
    ]
    return [TimedLine(start - offset, text) for start, text in source]


def manual_sand_children() -> list[TimedLine]:
    source = [
        (4.30, "栽培時期に合った品種を選ぶことが大切で"),
        (9.48, "芯やヘタは取っておいて"),
        (12.56, "苗が十分育ったら"),
        (16.35, "砂のこどもが乾いたら"),
        (29.32, "目一杯栄養をとるまでの辛抱"),
        (38.64, "手に取ると売るときの感動"),
        (47.90, "上にある枕　気候の変動に　耳か目から"),
        (57.39, "手に出す　目に出す　貧民の歓声は　まだ無関係で"),
        (65.76, "いつか背の高い花を生みたい"),
        (75.91, "まだ青い園児みたい"),
        (85.21, "背の高い花が咲いたら"),
        (94.60, "まだ青い子供たちが"),
        (113.32, "最後の月を最高にしたい場合"),
        (122.50, "湧いてくるのは鳥の蝶の変貌"),
        (131.90, "芽がで実がでるまで猛勉強　まずは開墾から"),
        (141.33, "絵になるような風景をつくる　目を見張ると"),
        (149.75, "いつか背の高い花を生みたい"),
        (159.86, "まだ青い園児みたい"),
        (169.22, "背の高い花が咲いたら"),
        (178.70, "まだ青い子供たちが"),
        (209.15, "いつか背の高い花を生みたい"),
        (218.41, "まだ青い園児みたい"),
        (227.69, "背の高い花が咲いたら"),
        (237.22, "まだ青い子供たちが"),
    ]
    return [TimedLine(start, text) for start, text in source]


def manual_grandma_bridge() -> list[TimedLine]:
    original = [
        (31.22, "乌篷点纱灯岩上青石悄着新纹"),
        (36.98, "喃喃细雨时归来燕子它不等人"),
        (43.18, "五指方扣桨蓑衣翁正系桥下绳"),
        (49.33, "春雨轻敛去绣花鞋落起唢呐声"),
        (54.72, "爆竹燃暗淡月弯弯"),
        (60.75, "锣鼓转踏醒路长长"),
        (66.99, "烛火晃斑驳儿时廊旁谁家白墙"),
        (73.90, "照湿谁家闺女脸庞"),
        (79.30, "摇啊摇十五摇过春分就是外婆桥"),
        (85.49, "盼啊盼阿嬷阿嬷地甜甜叫"),
        (91.60, "吵啊吵米花糖挂嘴角总是吃不饱"),
        (97.78, "美啊美小脚桥上翘啊翘"),
    ]
    return [TimedLine(start - 23.43, text) for start, text in original]


def copy_kataomoi(path: Path) -> list[TimedLine]:
    output = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["clip"] != "012_カタオモイ":
                continue
            if row.get("reviewed", "").strip().casefold() not in {"yes", "true", "1"}:
                continue
            output.append(TimedLine(float(row["start_seconds"]), row["corrected_text"]))
    if not output:
        raise ValueError("No reviewed 012_カタオモイ rows found")
    return output


def write_creator(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "clip",
        "start_seconds",
        "end_seconds",
        "recognized_text",
        "corrected_text",
        "reviewed",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path}: {len(rows)} reviewed lines")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kataomoi-csv", type=Path, required=True)
    args = parser.parse_args()
    creator_rows = {name: [] for name in TRACKS}

    append_rows(
        creator_rows["kioi"],
        TRACKS["kioi"]["From The Start"],
        mapped(fetch_lrc(33240434), lambda value: 3.00 + 1.037 * value),
    )
    append_rows(creator_rows["kioi"], TRACKS["kioi"]["Time Machine"], manual_time_machine())
    stay = fetch_lrc(24356771)
    append_rows(
        creator_rows["kioi"],
        TRACKS["kioi"]["Stay With Me"],
        mapped(stay, lambda value: value - 23.10, lambda value: 23.10 <= value <= 132.08)
        + mapped(stay, lambda value: value - 77.25, lambda value: 201.75 <= value <= 257.18),
    )
    append_rows(creator_rows["sumire"], TRACKS["sumire"]["外婆桥"], manual_grandma_bridge())
    append_rows(
        creator_rows["yuchu"],
        TRACKS["yuchu"]["SOS"],
        mapped(fetch_lrc(23557722), lambda value: value - 40.00, lambda value: value >= 40.00),
    )
    append_rows(creator_rows["yuchu"], TRACKS["yuchu"]["砂のこども"], manual_sand_children())
    append_rows(
        creator_rows["yuchu"],
        TRACKS["yuchu"]["Bad Apple!!"],
        mapped(fetch_lrc(9174610), lambda value: value + 7.70),
    )
    append_rows(
        creator_rows["yuchu"],
        TRACKS["yuchu"]["カタオモイ"],
        copy_kataomoi(args.kataomoi_csv),
    )
    for creator, rows in creator_rows.items():
        write_creator(args.output_dir / creator / "lyric-corrections.csv", rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
