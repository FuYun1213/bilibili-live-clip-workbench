#!/usr/bin/env python3
"""Restore YUC-R05 subtitles from the verified full-session timestamps."""

from __future__ import annotations

import argparse
from pathlib import Path


DIALOGUES = [
    (0.10, 2.00, "我问你"),
    (2.00, 2.90, "我问你们"),
    (2.90, 4.25, "我说宇宙猫们"),
    (4.25, 5.70, "你们喜不喜欢我呀"),
    (5.70, 7.35, "宇宙猫们来了一句"),
    (7.35, 11.95, "这个赛季的卡莎是给"),
    (11.95, 15.40, "物理装还是法强装啊"),
    (15.40, 16.90, "就这种感觉一样啊"),
    (17.75, 19.35, "感觉牛头不对马嘴的"),
]


def ass_time(seconds: float) -> str:
    centiseconds = int(round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ass", type=Path)
    args = parser.parse_args()
    content = args.ass.read_text(encoding="utf-8-sig")
    prefix = content.split("[Events]", 1)[0].rstrip()
    lines = [
        prefix,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in DIALOGUES:
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Yuchu,羽啾chu2u,0,0,0,,{text}"
        )
    args.ass.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
