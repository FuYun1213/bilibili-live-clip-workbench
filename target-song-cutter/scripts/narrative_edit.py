#!/usr/bin/env python3
"""Deterministic narrative-boundary cleanup for Flow3 edit plans."""

from __future__ import annotations

import copy
import csv
import re
import media_packaging
from pathlib import Path
from typing import Any, Sequence


def align_authoritative_edit_ranges(
    items: Sequence[dict[str, Any]],
    segments: list[dict],
    *,
    max_edge_trim_seconds: float = 0.35,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Trim tiny unaligned edge fragments, then preflight every subtitle.

    Never expand an approved range, invent word times, or discard a substantial
    part of a sentence. The renderer mapper remains the final authority,
    including for song ranges and corrected CSV text.
    """
    from content_slicer import EditRange, _canonical_bounds, _map_segments_to_parts

    result = copy.deepcopy(list(items))
    audit: list[dict[str, Any]] = []
    unaligned = [
        segment for segment in segments
        if not segment.get("words")
        and segment.get("render_policy") != "native-captions"
        and re.search(r"\w", str(segment.get("text") or ""))
    ]
    for item in result:
        original = copy.deepcopy(item["timestamps"])
        for order, span in enumerate(item["timestamps"], 1):
            left, right = float(span["start_seconds"]), float(span["end_seconds"])
            original_left, original_right = left, right
            if item.get("content_type", "narrative") == "narrative":
                # Overlapping rows must not turn small individual snaps into
                # an unbounded trim. Limit the total change at each edge.
                for _ in range(len(unaligned) + 1):
                    previous = (left, right)
                    for segment in unaligned:
                        start, end = _canonical_bounds(segment)
                        overlap = min(end, right) - max(start, left)
                        if overlap <= 0.020001 or max(left - start, end - right) <= 0.060001:
                            continue
                        if overlap > min(max_edge_trim_seconds, (end - start) / 2) + 0.000001:
                            continue
                        if left <= start < right < end and original_right - start <= max_edge_trim_seconds + 0.000001:
                            right = start
                        elif start < left < end <= right and end - original_left <= max_edge_trim_seconds + 0.000001:
                            left = end
                    if (left, right) == previous:
                        break
            if (left, right) != (original_left, original_right):
                if right - left < 0.5:
                    raise ValueError(f"{item['clip_id']} 第 {order} 段字幕边界对齐后不足 0.5 秒，请调整切点")
                span.update(start_seconds=round(left, 3), end_seconds=round(right, 3))
                audit.append({
                    "clip_id": item["clip_id"], "order": order,
                    "before": [original_left, original_right],
                    "after": [left, right],
                    "reason": "trim_unaligned_subtitle_edge",
                })
        parts = [
            EditRange(item["clip_id"], order, float(span["start_seconds"]),
                      float(span["end_seconds"]), "", "", "", "")
            for order, span in enumerate(item["timestamps"], 1)
        ]
        try:
            mapped = _map_segments_to_parts(segments, parts)
        except ValueError as exc:
            raise ValueError(f"字幕切点预检失败（尚未导出视频）：{exc}") from exc
        if not mapped:
            raise ValueError(f"{item['clip_id']} 字幕切点预检失败：片段内没有可映射的权威字幕")
        if item.get("narrative_structure") and original != item["timestamps"]:
            item["narrative_structure"] = media_packaging.remap_narrative_structure(
                item["narrative_structure"], original, item["timestamps"]
            )
    return result, audit


def _number(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = str(row.get(name, "")).strip()
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    return None


def refine_narrative_edit_ranges(
    items: Sequence[dict[str, Any]],
    transcript_path: Path,
    *,
    leading_margin_seconds: float = 0.12,
    trailing_margin_seconds: float = 0.28,
    long_silence_seconds: float = 2.5,
    silence_edge_margin_seconds: float = 0.18,
    minimum_fragment_seconds: float = 0.5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tighten narrative edges to speech and remove only long internal dead air."""
    with transcript_path.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    transcript_rows: list[tuple[float, float]] = []
    for row in source_rows:
        start = _number(row, "start_seconds", "start")
        end = _number(row, "end_seconds", "end")
        if start is None or end is None or end <= start or not str(row.get("text", "")).strip():
            continue
        transcript_rows.append((start, end))
    transcript_rows.sort()

    refined = copy.deepcopy(list(items))
    audit: list[dict[str, Any]] = []
    for item in refined:
        if item.get("content_type", "narrative") != "narrative":
            continue
        original_ranges = list(item.get("timestamps", []))
        cleaned: list[dict[str, float]] = []
        leading_removed = 0.0
        trailing_removed = 0.0
        silence_removed = 0.0
        for span in original_ranges:
            original_start = float(span["start_seconds"])
            original_end = float(span["end_seconds"])
            overlapping = [
                (start, end)
                for start, end in transcript_rows
                if end > original_start and start < original_end
            ]
            if not overlapping:
                cleaned.append({"start_seconds": original_start, "end_seconds": original_end})
                continue

            tightened_start = max(
                original_start, overlapping[0][0] - max(0.0, leading_margin_seconds)
            )
            tightened_end = min(
                original_end, overlapping[-1][1] + max(0.0, trailing_margin_seconds)
            )
            if tightened_end - tightened_start < minimum_fragment_seconds:
                cleaned.append({"start_seconds": original_start, "end_seconds": original_end})
                continue
            leading_removed += tightened_start - original_start
            trailing_removed += original_end - tightened_end

            cursor = tightened_start
            previous_end = overlapping[0][1]
            for next_start, next_end in overlapping[1:]:
                cut_start = max(cursor, previous_end + silence_edge_margin_seconds)
                cut_end = min(tightened_end, next_start - silence_edge_margin_seconds)
                can_keep_both_sides = (
                    cut_start - cursor >= minimum_fragment_seconds
                    and tightened_end - cut_end >= minimum_fragment_seconds
                )
                if (
                    next_start - previous_end >= long_silence_seconds
                    and cut_end > cut_start
                    and can_keep_both_sides
                ):
                    cleaned.append({
                        "start_seconds": round(cursor, 3),
                        "end_seconds": round(cut_start, 3),
                    })
                    cursor = cut_end
                    silence_removed += cut_end - cut_start
                previous_end = max(previous_end, next_end)
            if tightened_end - cursor >= minimum_fragment_seconds:
                cleaned.append({
                    "start_seconds": round(cursor, 3),
                    "end_seconds": round(tightened_end, 3),
                })

        if not cleaned:
            raise ValueError(
                f"{item.get('clip_id', '')} 精准边界处理后没有可保留的语音片段"
            )
        if item.get("narrative_structure"):
            item["narrative_structure"] = media_packaging.remap_narrative_structure(
                item["narrative_structure"], original_ranges, cleaned,
            )
        item["timestamps"] = cleaned
        original_seconds = sum(
            float(value["end_seconds"]) - float(value["start_seconds"])
            for value in original_ranges
        )
        refined_seconds = sum(
            value["end_seconds"] - value["start_seconds"] for value in cleaned
        )
        if original_seconds - refined_seconds > 0.001:
            audit.append({
                "clip_id": item.get("clip_id", ""),
                "original_seconds": round(original_seconds, 3),
                "refined_seconds": round(refined_seconds, 3),
                "leading_removed_seconds": round(leading_removed, 3),
                "trailing_removed_seconds": round(trailing_removed, 3),
                "long_silence_removed_seconds": round(silence_removed, 3),
                "parts_before": len(original_ranges),
                "parts_after": len(cleaned),
            })
    return refined, audit


def write_narrative_refinement_audit(
    path: Path, rows: Sequence[dict[str, Any]]
) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "clip_id", "original_seconds", "refined_seconds",
                "leading_removed_seconds", "trailing_removed_seconds",
                "long_silence_removed_seconds", "parts_before", "parts_after",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
