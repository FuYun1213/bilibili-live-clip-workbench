#!/usr/bin/env python3
"""Create native Aegisub ASS lyrics with a smooth per-character reveal.

Every glyph starts fully transparent, fades to opaque at its assigned time,
and remains visible until the lyric line ends.  The animation uses only ASS
override tags, so the result stays editable in Aegisub and can be burned by
FFmpeg/libass without PowerPoint or PNG frames.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path


REVIEWED = {"1", "true", "yes", "y", "ok", "reviewed"}
JAPANESE_SCRIPT = re.compile(r"[\u3040-\u30ff]")


@dataclass(frozen=True)
class TimedUnit:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Lyric:
    clip: str
    start: float
    end: float
    text: str
    timed_units: tuple[TimedUnit, ...] = ()


def ass_clock(seconds: float) -> str:
    value = max(0, round(seconds * 100))
    hours, value = divmod(value, 360_000)
    minutes, value = divmod(value, 6_000)
    secs, centis = divmod(value, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def ass_colour(value: str, alpha: int = 0) -> str:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB colour, got {value!r}")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}"


def escape_text(value: str) -> str:
    return value.replace("\\", "／").replace("{", "｛").replace("}", "｝").replace("\n", " ").strip()


def load_lyrics(path: Path) -> dict[str, list[Lyric]]:
    grouped: dict[str, list[Lyric]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), 2):
            text = (row.get("corrected_text") or row.get("text") or "").strip()
            reviewed = (row.get("reviewed") or "yes").strip().casefold()
            if reviewed not in REVIEWED or not text:
                raise ValueError(f"Unreviewed or empty lyric on CSV row {row_number}")
            raw_units = (row.get("timed_units") or "").strip()
            units: tuple[TimedUnit, ...] = ()
            if raw_units:
                parsed = json.loads(raw_units)
                units = tuple(
                    TimedUnit(
                        start=float(unit["start_seconds"]),
                        end=float(unit["end_seconds"]),
                        text=escape_text(str(unit["text"])),
                    )
                    for unit in parsed
                    if str(unit.get("text") or "").strip()
                )
            item = Lyric(
                clip=row["clip"].strip(),
                start=float(row["start_seconds"]),
                end=float(row["end_seconds"]),
                text=escape_text(text),
                timed_units=units,
            )
            if item.end <= item.start:
                raise ValueError(f"Invalid lyric interval on CSV row {row_number}")
            if item.timed_units:
                if any(unit.end <= unit.start for unit in item.timed_units):
                    raise ValueError(f"Invalid timed lyric unit on CSV row {row_number}")
                if any(unit.start < item.start - 0.05 or unit.end > item.end + 0.25 for unit in item.timed_units):
                    raise ValueError(f"Timed lyric unit outside its line on CSV row {row_number}")
            grouped.setdefault(item.clip, []).append(item)
    if not grouped:
        raise ValueError("No reviewed lyrics found")
    for rows in grouped.values():
        rows.sort(key=lambda item: (item.start, item.end))
    return grouped


def reveal_timed_units(row: Lyric, fade_ms: int) -> str:
    """Reveal only at measured unit times; never invent evenly-spaced timing."""
    if not row.timed_units:
        return rf"{{\fad({max(80, fade_ms)},0)\blur0.4}}{row.text}"
    output: list[str] = []
    for unit in row.timed_units:
        begin = max(0, round((unit.start - row.start) * 1000))
        measured_duration = max(1, round((unit.end - unit.start) * 1000))
        end = begin + max(40, min(fade_ms, measured_duration))
        output.append(
            rf"{{\alpha&HFF&\blur0.6\t({begin},{end},\alpha&H00&)}}{unit.text}"
        )
    return "".join(output)


def header(args, clip: str) -> str:
    primary = ass_colour(args.primary)
    secondary = ass_colour(args.primary, 255)
    outline = ass_colour(args.outline)
    back = ass_colour(args.shadow, 96)
    media = f"{clip}.mp4"
    return f"""[Script Info]
; Script generated as native Aegisub/ASS per-character fade animation
Title: {clip} - native word fade lyrics
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {args.width}
PlayResY: {args.height}

[Aegisub Project Garbage]
Audio File: {media}
Video File: {media}
Video AR Mode: 4
Video AR Value: 1.777778
Video Zoom Percent: 0.625000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {args.style_name},{args.font},{args.font_size},{primary},{secondary},{outline},{back},-1,0,0,0,100,100,{args.spacing},0,1,{args.outline_width},{args.shadow_depth},2,{args.margin_lr},{args.margin_lr},{args.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(path: Path, clip: str, rows: list[Lyric], args) -> None:
    events: list[str] = []
    for row in rows:
        start = row.start + args.delay
        end = row.end + args.delay + args.hang
        font_override = rf"{{\fn{args.japanese_font}}}" if JAPANESE_SCRIPT.search(row.text) else ""
        animated = font_override + reveal_timed_units(row, args.fade_ms)
        events.append(
            f"Dialogue: 0,{ass_clock(start)},{ass_clock(end)},{args.style_name},{args.speaker_name},0,0,0,,{animated}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header(args, clip) + "\n".join(events) + "\n", encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lyrics", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--font", default="AR WeiBeiGBStd BD")
    parser.add_argument("--style-name", default="SumireLyric")
    parser.add_argument("--speaker-name", default="枝堇")
    parser.add_argument(
        "--japanese-font",
        default="Yuji Syuku",
        help="Font used for lyric lines containing hiragana or katakana.",
    )
    parser.add_argument("--font-size", type=int, default=46)
    parser.add_argument("--primary", default="#FFF9FC")
    parser.add_argument("--outline", default="#B4A6BF")
    parser.add_argument("--shadow", default="#51495A")
    parser.add_argument("--outline-width", type=float, default=3.2)
    parser.add_argument("--shadow-depth", type=float, default=1.5)
    parser.add_argument("--spacing", type=float, default=1.5)
    parser.add_argument("--margin-lr", type=int, default=100)
    parser.add_argument("--margin-v", type=int, default=52)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fade-ms", type=int, default=240)
    parser.add_argument("--delay", type=float, default=0.10)
    parser.add_argument("--hang", type=float, default=0.16)
    args = parser.parse_args()

    grouped = load_lyrics(args.lyrics.resolve())
    for clip, rows in grouped.items():
        destination = args.output_dir.resolve() / f"{clip}.ass"
        write_ass(destination, clip, rows, args)
        print(f"{destination.name}: {len(rows)} reviewed line(s)")
    print(f"Done: wrote {len(grouped)} native Aegisub ASS file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
