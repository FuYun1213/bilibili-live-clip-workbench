#!/usr/bin/env python3
"""Remove video/audio ranges marked by ASS dialogue text and retime the ASS."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Interval:
    start: float
    end: float


def parse_time(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})\.(\d{2})", value.strip())
    if not match:
        raise ValueError(f"Invalid ASS timestamp: {value}")
    hours, minutes, seconds, centis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + centis / 100


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def media_duration(ffmpeg: Path, media: Path) -> float:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(media)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(f"Could not read duration: {media}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def plain_text(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text).replace(r"\N", " ").strip()


def merge(intervals: list[Interval], duration: float) -> list[Interval]:
    clipped = sorted(
        (
            Interval(max(0.0, item.start), min(duration, item.end))
            for item in intervals
            if item.end > 0 and item.start < duration
        ),
        key=lambda item: item.start,
    )
    result: list[Interval] = []
    for item in clipped:
        if item.end <= item.start:
            continue
        if result and item.start <= result[-1].end + 0.001:
            result[-1] = Interval(result[-1].start, max(result[-1].end, item.end))
        else:
            result.append(item)
    return result


def keep_intervals(deletions: list[Interval], duration: float) -> list[Interval]:
    keeps: list[Interval] = []
    cursor = 0.0
    for item in deletions:
        if item.start - cursor > 0.01:
            keeps.append(Interval(cursor, item.start))
        cursor = max(cursor, item.end)
    if duration - cursor > 0.01:
        keeps.append(Interval(cursor, duration))
    return keeps


def retime_ass(lines: list[str], marker: str, keeps: list[Interval]) -> list[str]:
    output: list[str] = []
    offsets: list[float] = []
    elapsed = 0.0
    for keep in keeps:
        offsets.append(elapsed)
        elapsed += keep.end - keep.start

    for line in lines:
        if not line.startswith("Dialogue:"):
            output.append(line)
            continue
        prefix, body = line.split(":", 1)
        fields = body.lstrip().split(",", 9)
        if len(fields) != 10:
            raise ValueError(f"Malformed ASS dialogue: {line}")
        start = parse_time(fields[1])
        end = parse_time(fields[2])
        if plain_text(fields[9]) == marker:
            continue

        emitted = False
        for keep, offset in zip(keeps, offsets):
            part_start = max(start, keep.start)
            part_end = min(end, keep.end)
            if part_end - part_start <= 0.01:
                continue
            updated = list(fields)
            updated[1] = ass_time(offset + part_start - keep.start)
            updated[2] = ass_time(offset + part_end - keep.start)
            output.append(f"{prefix}: " + ",".join(updated))
            emitted = True
        if not emitted and end <= 0:
            output.append(line)
    return output


def render_cut(ffmpeg: Path, source: Path, destination: Path, keeps: list[Interval]) -> None:
    if len(keeps) == 1 and keeps[0].start <= 0.001:
        shutil.copy2(source, destination)
        return

    filters: list[str] = []
    inputs: list[str] = []
    for index, keep in enumerate(keeps):
        filters.extend(
            [
                f"[0:v]trim=start={keep.start:.3f}:end={keep.end:.3f},setpts=PTS-STARTPTS[v{index}]",
                f"[0:a]atrim=start={keep.start:.3f}:end={keep.end:.3f},asetpts=PTS-STARTPTS[a{index}]",
            ]
        )
        inputs.append(f"[v{index}][a{index}]")
    filters.append(f"{''.join(inputs)}concat=n={len(keeps)}:v=1:a=1[outv][outa]")
    subprocess.run(
        [
            str(ffmpeg), "-y", "-i", str(source),
            "-filter_complex", ";".join(filters),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(destination),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--ass", type=Path, required=True)
    parser.add_argument("--output-clips", type=Path, required=True)
    parser.add_argument("--output-ass", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--marker", default="/删除")
    args = parser.parse_args()
    args.output_clips.mkdir(parents=True, exist_ok=True)
    args.output_ass.mkdir(parents=True, exist_ok=True)

    for subtitle in sorted(args.ass.glob("*.ass")):
        clip = args.clips / f"{subtitle.stem}.mp4"
        if not clip.is_file():
            raise FileNotFoundError(clip)
        lines = subtitle.read_text(encoding="utf-8-sig").splitlines()
        duration = media_duration(args.ffmpeg, clip)
        marked: list[Interval] = []
        for line in lines:
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(":", 1)[1].lstrip().split(",", 9)
            if len(fields) == 10 and plain_text(fields[9]) == args.marker:
                marked.append(Interval(parse_time(fields[1]), parse_time(fields[2])))
        deletions = merge(marked, duration)
        keeps = keep_intervals(deletions, duration)
        if not keeps:
            raise RuntimeError(f"All video content was marked for deletion: {clip}")

        output_clip = args.output_clips / clip.name
        output_subtitle = args.output_ass / subtitle.name
        render_cut(args.ffmpeg, clip, output_clip, keeps)
        output_subtitle.write_text(
            "\n".join(retime_ass(lines, args.marker, keeps)) + "\n",
            encoding="utf-8-sig",
        )
        removed = sum(item.end - item.start for item in deletions)
        spans = ", ".join(f"{item.start:.2f}-{item.end:.2f}" for item in deletions) or "none"
        print(f"{subtitle.stem[:3]} removed={removed:.2f}s spans={spans}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
