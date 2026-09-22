#!/usr/bin/env python3
"""Create a lyric-review sheet, then burn only reviewed corrections into song clips."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path


SRT_BLOCK = re.compile(
    r"(?:\ufeff)?\d+\s*\r?\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})\s*\r?\n"
    r"(.*?)(?=\r?\n\s*\r?\n|\Z)",
    re.S,
)
REVIEWED_VALUES = {"1", "true", "yes", "y", "reviewed", "ok"}


@dataclass(frozen=True)
class LyricLine:
    clip: str
    start: float
    end: float
    recognized: str
    corrected: str
    reviewed: bool


def parse_srt_clock(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def ass_clock(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    rows = []
    for match in SRT_BLOCK.finditer(path.read_text(encoding="utf-8-sig")):
        text = " ".join(match.group(3).split())
        if text:
            rows.append((parse_srt_clock(match.group(1)), parse_srt_clock(match.group(2)), text))
    return rows


def create_template(args) -> int:
    rows = []
    for source in sorted(args.srt_dir.glob("*.srt")):
        for start, end, recognized in parse_srt(source):
            rows.append({
                "clip": source.stem,
                "start_seconds": f"{start:.3f}",
                "end_seconds": f"{end:.3f}",
                "recognized_text": recognized,
                "corrected_text": "",
                "reviewed": "no",
            })
    if not rows:
        raise ValueError(f"No SRT lyric rows found in {args.srt_dir}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Done: wrote {len(rows)} lyric review rows to {args.output.resolve()}")
    return 0


def load_reviewed_lyrics(path: Path) -> dict[str, list[LyricLine]]:
    grouped: dict[str, list[LyricLine]] = {}
    problems = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), 2):
            corrected = row.get("corrected_text", "").strip()
            reviewed = row.get("reviewed", "").strip().casefold() in REVIEWED_VALUES
            if not reviewed or not corrected:
                problems.append(row_number)
                continue
            line = LyricLine(
                clip=row["clip"].strip(),
                start=float(row["start_seconds"]),
                end=float(row["end_seconds"]),
                recognized=row.get("recognized_text", "").strip(),
                corrected=corrected,
                reviewed=True,
            )
            if line.end <= line.start:
                raise ValueError(f"Invalid lyric timing on row {row_number}")
            grouped.setdefault(line.clip, []).append(line)
    if problems:
        preview = ", ".join(str(value) for value in problems[:12])
        raise ValueError(
            "Refusing to burn unreviewed or empty lyrics. Correct the text and set reviewed=yes "
            f"on CSV row(s): {preview}"
        )
    if not grouped:
        raise ValueError("Lyrics CSV has no reviewed lines")
    return grouped


def visual_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WFA" else 1 for char in value)


def wrap_ass(value: str, max_width: int = 38) -> str:
    value = value.replace("{", "｛").replace("}", "｝").replace("\n", " ").strip()
    if visual_width(value) <= max_width:
        return value
    best = min(
        range(1, len(value)),
        key=lambda index: abs(visual_width(value[:index]) - visual_width(value[index:]))
        - (5 if value[index - 1] in "，。！？、；：,.!?;: " else 0),
    )
    return value[:best].rstrip() + r"\N" + value[best:].lstrip()


def ass_header() -> str:
    return """[Script Info]
Title: Kioi Corrected Song Lyrics
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: KioiLyric,Microsoft YaHei,54,&H00FFFBF8,&H00C8D654,&H903D3027,&H48000000,-1,0,0,0,100,100,0,0,1,4,1,2,150,150,78,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(path: Path, lines: list[LyricLine], delay: float = 0.1) -> None:
    if delay < 0:
        raise ValueError("Subtitle delay cannot be negative")
    events = []
    for line in sorted(lines, key=lambda item: (item.start, item.end)):
        events.append(
            f"Dialogue: 0,{ass_clock(line.start + delay)},{ass_clock(line.end + delay)},"
            f"KioiLyric,柚雨,0,0,0,,{wrap_ass(line.corrected)}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ass_header() + "\n".join(events) + "\n", encoding="utf-8-sig")


def filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def locate_clip(clips_dir: Path, name: str) -> Path:
    direct = clips_dir / name
    if direct.is_file():
        return direct
    matches = [path for path in clips_dir.glob(name + ".*") if path.suffix.lower() in {".mp4", ".mkv", ".mov"}]
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected one video for lyric clip {name}; found {matches}")
    return matches[0]


def burn(args) -> int:
    lyrics = load_reviewed_lyrics(args.lyrics)
    ffmpeg = Path(args.ffmpeg).resolve() if args.ffmpeg else Path(shutil.which("ffmpeg") or "")
    if not ffmpeg.is_file():
        raise FileNotFoundError("FFmpeg not found; pass --ffmpeg")
    ass_dir = args.output / "subtitle-boxes"
    clips_out = args.output / "clips-with-corrected-lyrics"
    ass_dir.mkdir(parents=True, exist_ok=True)
    clips_out.mkdir(parents=True, exist_ok=True)
    for name, lines in lyrics.items():
        source = locate_clip(args.clips_dir, name)
        ass = ass_dir / f"{source.stem}.ass"
        destination = clips_out / f"{source.stem}.mp4"
        write_ass(ass, lines, args.subtitle_delay)
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "warning", "-y",
            "-i", str(source.resolve()), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"ass='{filter_path(ass)}'", "-c:v", "libx264",
            "-preset", args.preset, "-crf", str(args.crf), "-c:a", "copy",
            "-movflags", "+faststart", str(destination),
        ]
        print("+", subprocess.list2cmdline(command), flush=True)
        subprocess.run(command, check=True)
    print(f"Done: burned reviewed lyrics into {len(lyrics)} song clip(s)")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    template = sub.add_parser("template", help="Create a mandatory lyric correction review sheet")
    template.add_argument("--srt-dir", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    render = sub.add_parser("burn", help="Burn only fully reviewed corrected lyrics")
    render.add_argument("--clips-dir", type=Path, required=True)
    render.add_argument("--lyrics", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--ffmpeg")
    render.add_argument("--subtitle-delay", type=float, default=0.1)
    render.add_argument("--preset", default="veryfast")
    render.add_argument("--crf", type=int, default=20)
    return root


def main() -> int:
    args = parser().parse_args()
    return create_template(args) if args.command == "template" else burn(args)


if __name__ == "__main__":
    raise SystemExit(main())
