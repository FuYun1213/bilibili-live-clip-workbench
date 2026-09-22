#!/usr/bin/env python3
"""Speed one continuous segment, remap ASS events, and burn in one encode."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


EVENT_RE = re.compile(
    r"^(?P<kind>Dialogue|Comment): (?P<layer>[^,]*),(?P<start>[^,]*),(?P<end>[^,]*),"
    r"(?P<style>[^,]*),(?P<name>[^,]*),(?P<ml>[^,]*),(?P<mr>[^,]*),"
    r"(?P<mv>[^,]*),(?P<effect>[^,]*),(?P<text>.*)$"
)


@dataclass
class Event:
    raw: str
    kind: str
    layer: str
    start: float
    end: float
    style: str
    name: str
    ml: str
    mr: str
    mv: str
    effect: str
    text: str


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_time(value: float) -> str:
    value = max(0.0, value)
    centiseconds = int(round(value * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cents:02d}"


def parse_event(line: str) -> Event | None:
    match = EVENT_RE.match(line)
    if not match:
        return None
    data = match.groupdict()
    return Event(
        raw=line,
        kind=data["kind"],
        layer=data["layer"],
        start=parse_time(data["start"]),
        end=parse_time(data["end"]),
        style=data["style"],
        name=data["name"],
        ml=data["ml"],
        mr=data["mr"],
        mv=data["mv"],
        effect=data["effect"],
        text=data["text"],
    )


def render_event(event: Event, start: float, end: float) -> str:
    return (
        f"{event.kind}: {event.layer},{format_time(start)},{format_time(end)},"
        f"{event.style},{event.name},{event.ml},{event.mr},{event.mv},"
        f"{event.effect},{event.text}"
    )


def filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return value.replace(":", r"\:").replace("'", r"\'")


def remap_ass(
    source: Path,
    destination: Path,
    start: float,
    end: float,
    factor: float,
    insert_text: str,
    insert_style: str,
    insert_name: str,
) -> None:
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    new_segment_end = start + (end - start) / factor
    removed = (end - start) - (end - start) / factor
    output: list[str] = []
    inserted = False

    for line in lines:
        event = parse_event(line)
        if event is None:
            output.append(line)
            continue

        if not inserted and event.start >= end:
            output.append(
                f"Dialogue: 0,{format_time(start)},{format_time(new_segment_end)},"
                f"{insert_style},{insert_name},0,0,0,,{insert_text}"
            )
            inserted = True

        if event.end <= start:
            output.append(line)
        elif event.start >= end:
            output.append(render_event(event, event.start - removed, event.end - removed))
        elif event.start >= start and event.end <= end:
            # Replace any original cue wholly inside the sped-up montage with one label.
            continue
        else:
            raise ValueError(
                "Subtitle event crosses the speed boundary; move the boundary to a cue edge: "
                f"{line}"
            )

    if not inserted:
        output.append(
            f"Dialogue: 0,{format_time(start)},{format_time(new_segment_end)},"
            f"{insert_style},{insert_name},0,0,0,,{insert_text}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(output) + "\n", encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ass", type=Path, required=True)
    parser.add_argument("--output-video", type=Path, required=True)
    parser.add_argument("--output-ass", type=Path, required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--factor", type=float, default=4.0)
    parser.add_argument("--insert-text", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--fontsdir", type=Path)
    args = parser.parse_args()

    if not (0 <= args.start < args.end) or args.factor <= 1:
        raise ValueError("Expected 0 <= start < end and factor > 1")

    remap_ass(
        args.ass,
        args.output_ass,
        args.start,
        args.end,
        args.factor,
        args.insert_text,
        args.style,
        args.name,
    )
    args.output_video.parent.mkdir(parents=True, exist_ok=True)

    tempo_filters: list[str] = []
    remaining = args.factor
    while remaining > 2.0:
        tempo_filters.append("atempo=2")
        remaining /= 2.0
    tempo_filters.append(f"atempo={remaining:.10f}")
    tempo = ",".join(tempo_filters)

    ass_filter = f"ass='{filter_path(args.output_ass)}'"
    if args.fontsdir:
        ass_filter += f":fontsdir='{filter_path(args.fontsdir)}'"

    graph = ";".join(
        [
            f"[0:v]trim=start=0:end={args.start:.6f},setpts=PTS-STARTPTS[v0]",
            f"[0:a]atrim=start=0:end={args.start:.6f},asetpts=PTS-STARTPTS[a0]",
            f"[0:v]trim=start={args.start:.6f}:end={args.end:.6f},"
            f"setpts=(PTS-STARTPTS)/{args.factor:.10f}[v1]",
            f"[0:a]atrim=start={args.start:.6f}:end={args.end:.6f},"
            f"asetpts=PTS-STARTPTS,{tempo}[a1]",
            f"[0:v]trim=start={args.end:.6f},setpts=PTS-STARTPTS[v2]",
            f"[0:a]atrim=start={args.end:.6f},asetpts=PTS-STARTPTS[a2]",
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[vcat][acat]",
            f"[vcat]{ass_filter}[vout]",
        ]
    )
    command = [
        str(args.ffmpeg),
        "-y",
        "-i",
        str(args.input),
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "[acat]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "19",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(args.output_video),
    ]
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
