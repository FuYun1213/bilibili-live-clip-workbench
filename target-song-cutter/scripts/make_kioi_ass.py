#!/usr/bin/env python3
"""Convert sliced SRT files into the reusable Kioi ASS subtitle design."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SRT_BLOCK = re.compile(
    r"(\d+)\s*\n"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)",
    re.S,
)


def ass_time(groups: tuple[str, ...]) -> str:
    hours, minutes, seconds, millis = map(int, groups)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def wrap_text(text: str, width: int = 18) -> str:
    text = " ".join(text.replace("\n", " ").split())
    text = text.replace("{", "｛").replace("}", "｝")
    if len(text) <= width:
        return text
    # Latin words, abbreviations and numbers are indivisible tokens. A line may
    # exceed the nominal width rather than splitting `call` into `c` + `all`.
    protected = [
        (match.start(), match.end())
        for match in re.finditer(r"[A-Za-z0-9]+(?:[ ._'’+-][A-Za-z0-9]+)*", text)
    ]
    candidates = [
        index
        for index in range(1, len(text))
        if not any(start < index < end for start, end in protected)
    ]
    if not candidates:
        return text
    midpoint = len(text) / 2
    split = min(
        candidates,
        key=lambda index: (
            0 if text[index - 1] in "，。！？、；： " else 1,
            abs(index - midpoint),
        ),
    )
    return text[:split].rstrip() + r"\N" + text[split:].lstrip()


def timestamp_to_ass(value: str) -> str:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{2,3})", value)
    if not match:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    hours, minutes, seconds, fraction = match.groups()
    centis = int(fraction.ljust(3, "0")[:3]) // 10
    return f"{int(hours)}:{minutes}:{seconds}.{centis:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--srt-dir", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--style", default="Kioi")
    parser.add_argument("--name", default="柚雨")
    parser.add_argument("--emphasis-style")
    parser.add_argument(
        "--speaker-map",
        type=Path,
        help="Optional JSON cue map. Top-level keys are three-digit clip IDs.",
    )
    parser.add_argument(
        "--legacy-006-callback-cards",
        action="store_true",
        help="Add the two callback cards used by the 2026-08-01 slice 006.",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    template = args.template.read_text(encoding="utf-8-sig")
    speaker_map = (
        json.loads(args.speaker_map.read_text(encoding="utf-8-sig"))
        if args.speaker_map
        else {}
    )
    header = template.split("[Events]", 1)[0].rstrip()
    events_header = (
        "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    for source in sorted(args.srt_dir.glob("*.srt")):
        body = source.read_text(encoding="utf-8-sig")
        lines: list[str] = []
        clip_map = speaker_map.get(source.name[:3], {})
        for match in SRT_BLOCK.finditer(body):
            cue = match.group(1)
            source_start = ass_time(match.groups()[1:5])
            source_end = ass_time(match.groups()[5:9])
            source_text = match.group(10).strip()
            cue_events = clip_map.get(cue, [{"style": args.style, "name": args.name}])
            if isinstance(cue_events, dict):
                cue_events = [cue_events]
            for event in cue_events:
                start = timestamp_to_ass(event["start"]) if "start" in event else source_start
                end = timestamp_to_ass(event["end"]) if "end" in event else source_end
                text = wrap_text(event.get("text", source_text))
                emphasized = any(
                    word in text
                    for word in ("討厭", "讨厌", "性感", "亲嘴", "清冷御姐", "我的狗", "剃掉")
                )
                style = event.get("style", args.style)
                if emphasized and args.emphasis_style and style == args.style:
                    style = args.emphasis_style
                name = event.get("name", args.name)
                lines.append(f"Dialogue: 0,{start},{end},{style},{name},0,0,0,,{text}")
        if args.legacy_006_callback_cards and source.name.startswith("006_"):
            lines.extend([
                "Dialogue: 1,0:00:08.56,0:00:10.40,CallbackCard,,0,0,0,,前情回放｜犬绒Mofu发烧之后……",
                "Dialogue: 1,0:00:57.01,0:00:58.80,CallbackCard,,0,0,0,,回到现在",
            ])
        destination = args.output / (source.stem + ".ass")
        destination.write_text(header + events_header + "\n".join(lines) + "\n", encoding="utf-8-sig")
        print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
