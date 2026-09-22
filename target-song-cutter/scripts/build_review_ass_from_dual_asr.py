#!/usr/bin/env python3
"""Build review-ready ASS files from clip-local Whisper timing and dual-ASR QA.

Whisper supplies the stable cue boundaries.  Paraformer is kept as an
independent comparison source and is written to a report so corrections can be
made without moving the final subtitles away from the delivered video.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import difflib
import json
import re
from pathlib import Path


LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[ ._'’+\-][A-Za-z0-9]+)*")


def simplify(text: str) -> str:
    if not hasattr(ctypes, "windll"):
        return text
    kernel32 = ctypes.windll.kernel32
    flag = 0x02000000  # LCMAP_SIMPLIFIED_CHINESE
    needed = kernel32.LCMapStringEx(
        "zh-CN", flag, text, len(text), None, 0, None, None, 0
    )
    if needed <= 0:
        return text
    buffer = ctypes.create_unicode_buffer(needed)
    kernel32.LCMapStringEx(
        "zh-CN", flag, text, len(text), buffer, needed, None, None, 0
    )
    return buffer.value


def normalize(text: str, replacements: dict[str, str]) -> str:
    text = simplify(text).replace("<unk>", "").replace("�", "")
    text = re.sub(r"\s+", " ", text).strip()
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"\b(?:oh my god|omg)\b", "Oh my god", text, flags=re.I)
    text = text.replace("SuperChat", "Super Chat")
    return text.strip(" ，。")


def load_paraformer_rows(path: Path) -> list[tuple[float, float, str]]:
    if not path.is_file():
        return []
    rows: list[tuple[float, float, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                (
                    float(row["start_ms"]) / 1000,
                    float(row["end_ms"]) / 1000,
                    row["text"],
                )
            )
    return rows


def overlapping_text(
    rows: list[tuple[float, float, str]], start: float, end: float
) -> str:
    parts = [
        text
        for left, right, text in rows
        if start <= (left + right) / 2 <= end
    ]
    return "".join(parts)


def split_units(text: str, max_chars: int = 24) -> list[str]:
    """Split an ASR segment without breaking Latin words."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    clauses = [part for part in re.split(r"(?<=[，。！？!?；;])", text) if part]
    # Do not force a character-count split inside an unpunctuated Chinese
    # phrase: it can create false sentence endings such as `还。/有刚刚…`.
    # Long clauses remain one event and are visually wrapped onto two lines.
    return [clause.strip() for clause in clauses if clause.strip()] or [text]


def wrap_text(text: str, width: int = 18) -> str:
    if len(text) <= width:
        return text
    protected = [(m.start(), m.end()) for m in LATIN_TOKEN.finditer(text)]
    candidates = [
        index
        for index in range(1, len(text))
        if not any(left < index < right for left, right in protected)
    ]
    if not candidates:
        return text
    midpoint = len(text) / 2
    split = min(
        candidates,
        key=lambda index: (
            0 if text[index - 1] in "，。！？、；： ”’" else 1,
            abs(index - midpoint),
        ),
    )
    return text[:split].rstrip() + r"\N" + text[split:].lstrip()


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def punctuate(text: str) -> str:
    if not text or text[-1] in "，。！？…?!~：、":
        return text
    question = any(
        marker in text
        for marker in ("吗", "呢", "为什么", "怎么", "什么", "哪", "谁", "多少", "几岁")
    )
    exclaim = any(
        marker in text.lower()
        for marker in ("oh my god", "好恐怖", "好可爱", "我的妈", "救命")
    )
    return text + ("？" if question else "！" if exclaim else "。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips-dir", type=Path, required=True)
    parser.add_argument("--whisper-root", type=Path, required=True)
    parser.add_argument("--funasr-root", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--style", default="Kioi")
    parser.add_argument("--name", default="Kioi")
    parser.add_argument("--delay", type=float, default=0.10)
    parser.add_argument(
        "--ignore-cue-overrides",
        action="store_true",
        help="Keep phrase replacements but ignore timestamp-specific overrides from an older edit.",
    )
    args = parser.parse_args()

    payload = json.loads(args.corrections.read_text(encoding="utf-8-sig"))
    replacements: dict[str, str] = payload.get("replacements", {})
    clip_replacements: dict[str, dict[str, str]] = payload.get(
        "clip_replacements", {}
    )
    overrides: dict[str, dict[str, str]] = payload.get("cue_overrides", {})
    if args.ignore_cue_overrides:
        overrides = {}
    template = args.template.read_text(encoding="utf-8-sig")
    header = template.split("[Events]", 1)[0].rstrip()
    events_header = (
        "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_lines = ["clip_id,start,end,similarity,whisper,paraformer"]

    for video in sorted(args.clips_dir.glob("*.mp4")):
        clip_id = video.name[:3]
        whisper_csv = args.whisper_root / video.stem / "transcript.csv"
        paraformer_csv = args.funasr_root / video.stem / "funasr-transcript.csv"
        paraformer_rows = load_paraformer_rows(paraformer_csv)
        local_replacements = dict(replacements)
        local_replacements.update(clip_replacements.get(clip_id, {}))
        events: list[str] = []
        previous_end = 0.0
        with whisper_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                start = float(row["start_seconds"])
                end = float(row["end_seconds"])
                whisper = normalize(row["text"], local_replacements)
                paraformer = normalize(
                    overlapping_text(paraformer_rows, start, end), local_replacements
                )
                similarity = difflib.SequenceMatcher(None, whisper, paraformer).ratio()
                report_lines.append(
                    ",".join(
                        [
                            clip_id,
                            f"{start:.2f}",
                            f"{end:.2f}",
                            f"{similarity:.3f}",
                            json.dumps(whisper, ensure_ascii=False),
                            json.dumps(paraformer, ensure_ascii=False),
                        ]
                    )
                )
                key = f"{start:.2f}"
                text = normalize(overrides.get(clip_id, {}).get(key, whisper), local_replacements)
                if not text:
                    continue
                units = split_units(text)
                weights = [max(1, len(re.sub(r"\s+", "", unit))) for unit in units]
                total_weight = sum(weights)
                cursor = start + args.delay
                display_end = max(cursor + 0.45, end + args.delay)
                for index, (unit, weight) in enumerate(zip(units, weights)):
                    if index == len(units) - 1:
                        unit_end = display_end
                    else:
                        unit_end = cursor + (display_end - (start + args.delay)) * weight / total_weight
                    unit_start = max(cursor, previous_end)
                    unit_end = max(unit_start + 0.45, unit_end)
                    events.append(
                        f"Dialogue: 0,{ass_time(unit_start)},{ass_time(unit_end)},"
                        f"{args.style},{args.name},0,0,0,,{wrap_text(punctuate(unit))}"
                    )
                    previous_end = unit_end
                    cursor = unit_end
        destination = args.output_dir / f"{video.stem}.ass"
        destination.write_text(
            header + events_header + "\n".join(events) + "\n", encoding="utf-8-sig"
        )
        print(f"{destination.name}: {len(events)} cues")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(report_lines) + "\n", encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
