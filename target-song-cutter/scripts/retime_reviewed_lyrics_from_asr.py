#!/usr/bin/env python3
"""Retime reviewed lyric lines against word-timestamped clip-local ASR.

The reviewed lyric sheet remains authoritative for text.  ASR is used only as
an acoustic clock: matching characters provide local timing offsets, and
unmatched lines inherit an interpolated offset from reliable neighbouring
lines.  This avoids treating a reference recording's LRC axis as if it were
the live performance axis.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


IGNORED = set(" \t\r\n　（）()【】[]、。！？!?・…‥『』「」\"'：:；;，,.-—~～_/\\")
REVIEWED = {"1", "true", "yes", "y", "reviewed", "ok"}


def norm_char(char: str) -> str:
    return unicodedata.normalize("NFKC", char).casefold()


def visible(text: str) -> list[str]:
    return [norm_char(char) for char in text if char not in IGNORED]


def flatten_asr(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: list[dict] = []
    for segment in payload.get("segments", []):
        for word in segment.get("words", []) or []:
            chars = visible(str(word.get("word", "")))
            if not chars:
                continue
            start = float(word["start_seconds"])
            end = max(start + 0.04, float(word["end_seconds"]))
            for index, char in enumerate(chars):
                result.append(
                    {
                        "char": char,
                        "start": start + (end - start) * index / len(chars),
                        "end": start + (end - start) * (index + 1) / len(chars),
                    }
                )
    return result


def median_absolute_deviation(values: list[float]) -> float:
    if not values:
        return math.inf
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def best_line_anchor(target: list[str], nominal_start: float, nominal_end: float, raw: list[dict], window: float) -> dict:
    candidates = [item for item in raw if nominal_start - window <= item["start"] <= nominal_end + window]
    if not target or not candidates:
        return {"coverage": 0.0, "offset": None, "mad": None, "matches": 0, "score": -math.inf}

    # Estimate the real line onset from matched acoustic character times. A
    # fixed-duration median shift is not sufficient for live tempo changes.
    best: dict | None = None
    raw_text = "".join(item["char"] for item in candidates)
    target_text = "".join(target)
    minimum = max(1, int(len(target) * 0.55))
    maximum = min(len(candidates), max(minimum, int(len(target) * 1.75) + 4))
    step_lengths = sorted(set([minimum, len(target), maximum] + list(range(minimum, maximum + 1, max(1, len(target) // 4)))))
    for length in step_lengths:
        if length <= 0 or length > len(candidates):
            continue
        for begin in range(0, len(candidates) - length + 1):
            end_index = begin + length
            sample = raw_text[begin:end_index]
            matcher = SequenceMatcher(None, target_text, sample, autojunk=False)
            pairs: list[tuple[int, int]] = []
            for block in matcher.get_matching_blocks():
                pairs.extend((block.a + offset, begin + block.b + offset) for offset in range(block.size))
            if not pairs:
                continue
            coverage = len({left for left, _ in pairs}) / len(target)
            matched_target_indices = {left for left, _ in pairs}
            onset_matched = 0 in matched_target_indices
            ending_matched = (len(target) - 1) in matched_target_indices
            points = sorted((left, float(candidates[right]["start"])) for left, right in pairs)
            denominator = max(1, len(target) - 1)
            first_times = [time for target_index, time in points if target_index == 0]
            slopes: list[float] = []
            for left_index, left_time in points:
                left_position = left_index / denominator
                for right_index, right_time in points:
                    right_position = right_index / denominator
                    if right_position - left_position < 0.18:
                        continue
                    slope_value = (right_time - left_time) / (right_position - left_position)
                    if 0.20 <= slope_value <= 30.0:
                        slopes.append(slope_value)
            nominal_duration = max(0.25, nominal_end - nominal_start)
            slope_value = statistics.median(slopes) if slopes else nominal_duration
            if first_times:
                onset = statistics.median(first_times)
            else:
                onset = statistics.median(
                    time - slope_value * (target_index / denominator)
                    for target_index, time in points
                )
            residuals = [
                time - (onset + slope_value * (target_index / denominator))
                for target_index, time in points
            ]
            mad = median_absolute_deviation(residuals)
            offset = onset - nominal_start
            sample_midpoint = (candidates[begin]["start"] + candidates[end_index - 1]["end"]) / 2
            fitted_midpoint = onset + slope_value / 2
            proximity = abs(sample_midpoint - fitted_midpoint)
            score = (
                coverage * 3.0
                - min(1.5, mad) * 0.80
                - min(window, abs(offset)) / max(window, 0.1) * 0.35
                - min(2.0, proximity) * 0.05
                + (0.65 if onset_matched else -0.80)
                + (0.15 if ending_matched else 0.0)
            )
            item = {
                "coverage": coverage,
                "offset": offset,
                "mad": mad,
                "matches": len(pairs),
                "score": score,
                "onset_matched": onset_matched,
            }
            if best is None or item["score"] > best["score"]:
                best = item
    return best or {"coverage": 0.0, "offset": None, "mad": None, "matches": 0, "score": -math.inf}


def select_monotonic_reliable(rows: list[dict], anchors: list[dict], minimum_coverage: float, maximum_mad: float) -> list[int]:
    """Choose the highest-scoring increasing anchor chain across the song."""
    candidates = [
        index for index, anchor in enumerate(anchors)
        if anchor.get("offset") is not None
        and float(anchor["coverage"]) >= minimum_coverage
        and float(anchor["mad"]) <= maximum_mad
        and bool(anchor.get("onset_matched"))
    ]
    if not candidates:
        return []
    acoustic_starts = {
        index: float(rows[index]["start_seconds"]) + float(anchors[index]["offset"])
        for index in candidates
    }
    scores = {
        index: max(0.05, float(anchors[index].get("score", 0.0)))
        for index in candidates
    }
    totals: dict[int, float] = {}
    previous: dict[int, int | None] = {}
    for position, index in enumerate(candidates):
        totals[index] = scores[index]
        previous[index] = None
        for earlier in candidates[:position]:
            if acoustic_starts[earlier] + 0.08 > acoustic_starts[index]:
                continue
            value = totals[earlier] + scores[index]
            if value > totals[index]:
                totals[index] = value
                previous[index] = earlier
    cursor: int | None = max(candidates, key=lambda index: totals[index])
    selected: list[int] = []
    while cursor is not None:
        selected.append(cursor)
        cursor = previous[cursor]
    selected.reverse()
    return selected

def interpolate_offsets(
    rows: list[dict],
    anchors: list[dict],
    minimum_coverage: float,
    maximum_mad: float,
    reliable_indices: list[int] | None = None,
) -> list[float]:
    if reliable_indices is None:
        reliable_indices = select_monotonic_reliable(
            rows, anchors, minimum_coverage, maximum_mad
        )
    reliable = [
        (index, float(anchors[index]["offset"]))
        for index in reliable_indices
    ]
    if not reliable:
        raise ValueError("No reliable lyric-to-ASR timing anchors")
    # Remove gross outliers relative to the song's median shift, then use a
    # piecewise-linear offset curve so modest live-tempo drift is preserved.
    median_shift = statistics.median(value for _, value in reliable)
    filtered = [(index, value) for index, value in reliable if abs(value - median_shift) <= 8.0]
    reliable = filtered or reliable
    offsets: list[float] = []
    for index in range(len(rows)):
        exact = next((value for anchor_index, value in reliable if anchor_index == index), None)
        if exact is not None:
            offsets.append(exact)
            continue
        left = max(((anchor_index, value) for anchor_index, value in reliable if anchor_index < index), default=None)
        right = min(((anchor_index, value) for anchor_index, value in reliable if anchor_index > index), default=None)
        if left and right:
            ratio = (index - left[0]) / (right[0] - left[0])
            offsets.append(left[1] + (right[1] - left[1]) * ratio)
        elif left:
            offsets.append(left[1])
        elif right:
            offsets.append(right[1])
        else:
            offsets.append(median_shift)
    # Smooth only interpolated rows. Reliable ASR anchors stay exact; smoothing
    # them can reintroduce a visible local offset even when the match was good.
    reliable_indices = {index for index, _ in reliable}
    smoothed = offsets[:]
    for index in range(1, len(offsets) - 1):
        if index not in reliable_indices:
            smoothed[index] = statistics.median(offsets[index - 1:index + 2])
    return smoothed


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--lyrics", type=Path, required=True)
    parser.add_argument("--asr-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--window", type=float, default=10.0)
    parser.add_argument("--minimum-coverage", type=float, default=0.30)
    parser.add_argument("--maximum-mad", type=float, default=1.20)
    parser.add_argument("--minimum-anchor-fraction", type=float, default=0.20)
    parser.add_argument("--maximum-residual", type=float, default=0.45)
    parser.add_argument("--display-lead", type=float, default=0.12)
    parser.add_argument("--manual-anchors", type=Path)
    args = parser.parse_args()
    manual_anchors: dict[tuple[str, int], dict] = {}
    if args.manual_anchors:
        with args.manual_anchors.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("reviewed", "").strip().casefold() not in REVIEWED:
                    raise ValueError(f"Unreviewed manual anchor: {row}")
                key = (row["clip"].strip(), int(row["line"]))
                if key in manual_anchors:
                    raise ValueError(f"Duplicate manual anchor: {key}")
                manual_anchors[key] = row

    with args.lyrics.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in source_rows:
        if row.get("reviewed", "").strip().casefold() not in REVIEWED:
            raise ValueError(f"Unreviewed lyric row: {row}")
        groups[row["clip"].strip()].append(row)

    output_rows: list[dict] = []
    report_rows: list[dict] = []
    for clip, rows in groups.items():
        transcript = args.asr_root / clip / "transcript.json"
        if not transcript.is_file():
            raise FileNotFoundError(transcript)
        raw = flatten_asr(transcript)
        anchors = [
            best_line_anchor(
                visible(row["corrected_text"]),
                float(row["start_seconds"]),
                float(row["end_seconds"]),
                raw,
                args.window,
            )
            for row in rows
        ]
        for index, row in enumerate(rows):
            manual = manual_anchors.get((clip, index + 1))
            if manual is None:
                continue
            manual_start = float(manual["start_seconds"])
            anchors[index] = {
                "coverage": 1.0,
                "offset": manual_start - float(row["start_seconds"]),
                "mad": 0.0,
                "matches": len(visible(row["corrected_text"])),
                "score": 100.0,
                "onset_matched": True,
                "manual": True,
            }
        reliable_indices = select_monotonic_reliable(
            rows, anchors, args.minimum_coverage, args.maximum_mad
        )
        required = max(3, math.ceil(len(rows) * args.minimum_anchor_fraction))
        if len(reliable_indices) < required:
            raise ValueError(
                f"{clip}: only {len(reliable_indices)} reliable anchors; need {required}"
            )
        missing_thirds = []
        for third in range(3):
            low = math.floor(len(rows) * third / 3)
            high = math.ceil(len(rows) * (third + 1) / 3)
            if not any(low <= index < high for index in reliable_indices):
                missing_thirds.append(third + 1)
        if missing_thirds:
            raise ValueError(f"{clip}: no reliable ASR anchor in thirds {missing_thirds}")
        offsets = interpolate_offsets(
            rows,
            anchors,
            args.minimum_coverage,
            args.maximum_mad,
            reliable_indices,
        )
        proposed: list[tuple[float, float]] = []
        for row, offset in zip(rows, offsets):
            old_start = float(row["start_seconds"])
            old_end = float(row["end_seconds"])
            start = max(0.0, old_start + offset - args.display_lead)
            end = max(start + 0.25, old_end + offset - args.display_lead)
            proposed.append((start, end))
        # Preserve every measured vocal onset. If adjacent live lines overlap,
        # shorten the earlier display instead of delaying the later lyric.
        for index in range(len(proposed) - 1):
            start, end = proposed[index]
            next_start = proposed[index + 1][0]
            if end >= next_start:
                proposed[index] = (start, max(start + 0.25, next_start - 0.02))

        for index, (row, anchor, offset, timing) in enumerate(zip(rows, anchors, offsets, proposed), 1):
            old_start = float(row["start_seconds"])
            old_end = float(row["end_seconds"])
            new_start, new_end = timing
            anchor_offset = float(anchor["offset"]) if index - 1 in reliable_indices else None
            residual = (
                new_start - (old_start + anchor_offset - args.display_lead)
                if anchor_offset is not None else None
            )
            if residual is not None and abs(residual) > args.maximum_residual:
                raise ValueError(
                    f"{clip} line {index}: post-retime residual {residual:+.3f}s "
                    f"exceeds {args.maximum_residual:.3f}s"
                )
            revised = dict(row)
            revised["start_seconds"] = f"{new_start:.3f}"
            revised["end_seconds"] = f"{new_end:.3f}"
            revised["evidence"] = "clip-local ASR timing anchors + reviewed lyric text"
            output_rows.append(revised)
            report_rows.append(
                {
                    "clip": clip,
                    "line": index,
                    "old_start": f"{old_start:.3f}",
                    "new_start": f"{new_start:.3f}",
                    "applied_offset": f"{offset - args.display_lead:.3f}",
                    "anchor_coverage": f"{float(anchor['coverage']):.3f}",
                    "anchor_mad": "" if anchor.get("mad") is None else f"{float(anchor['mad']):.3f}",
                    "anchor_offset": "" if anchor_offset is None else f"{anchor_offset:.3f}",
                    "residual_seconds": "" if residual is None else f"{residual:.3f}",
                    "anchor_status": (
                        "manual" if anchor.get("manual") else
                        "reliable" if anchor_offset is not None else "interpolated"
                    ),
                    "text": row["corrected_text"],
                }
            )
        reliable_count = len(reliable_indices)
        print(f"{clip}: {len(rows)} lines, {reliable_count} reliable anchors, shift {min(offsets):+.2f}..{max(offsets):+.2f}s")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()))
        writer.writeheader()
        writer.writerows(output_rows)
    with args.report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report_rows[0].keys()))
        writer.writeheader()
        writer.writerows(report_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
