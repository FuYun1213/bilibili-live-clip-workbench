#!/usr/bin/env python3
"""Bind a reviewed live-performance lyric timing report to a final MP4/ASS pair."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
IGNORED_RE = re.compile(r"[\s　（）()【】\[\]、。！？!?・…‥『』「』\"'：:；;，,\.\-—~～_/\\]+")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def norm_text(value: str) -> str:
    value = ASS_OVERRIDE_RE.sub("", value).replace(r"\N", " ").replace(r"\n", " ")
    return IGNORED_RE.sub("", value).casefold()


def ass_seconds(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ass_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    in_events = False
    format_fields: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_events = line.casefold() == "[events]"
            continue
        if not in_events:
            continue
        if line.casefold().startswith("format:"):
            format_fields = [part.strip().casefold() for part in line.split(":", 1)[1].split(",")]
            continue
        if not line.casefold().startswith("dialogue:"):
            continue
        if not format_fields:
            raise ValueError(f"ASS Events Format missing: {path}")
        parts = line.split(":", 1)[1].lstrip().split(",", len(format_fields) - 1)
        if len(parts) != len(format_fields):
            raise ValueError(f"Malformed ASS dialogue: {line[:120]}")
        item = dict(zip(format_fields, parts))
        events.append({
            "start": ass_seconds(item["start"]),
            "end": ass_seconds(item["end"]),
            "text": norm_text(item["text"]),
        })
    if not events:
        raise ValueError(f"No dialogue events in ASS: {path}")
    return events


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--ass", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--clip", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-coverage", type=float, default=0.30)
    parser.add_argument("--maximum-mad", type=float, default=1.20)
    parser.add_argument("--minimum-anchor-fraction", type=float, default=0.20)
    parser.add_argument("--maximum-adjustment", type=float, default=0.45)
    parser.add_argument("--maximum-residual", type=float, default=0.45)
    parser.add_argument(
        "--expected-ass-delay",
        type=float,
        default=0.10,
        help="Expected renderer delay added to the verified lyric axis.",
    )
    parser.add_argument("--maximum-ass-start-delta", type=float, default=0.10)
    args = parser.parse_args()

    video = args.video.resolve()
    ass = (args.ass or video.with_suffix(".ass")).resolve()
    report = args.report.resolve()
    output = (args.output or video.with_name(video.stem + ".song-timing-gate.json")).resolve()
    for path in (video, ass, report):
        if not path.is_file():
            raise FileNotFoundError(path)

    with report.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("clip", "").strip() == args.clip]
    if not rows:
        raise ValueError(f"No timing rows for clip {args.clip!r}")
    rows.sort(key=lambda row: int(row["line"]))

    reliable: list[int] = []
    adjustments: list[float] = []
    residuals: list[float] = []
    for index, row in enumerate(rows):
        adjustments.append(abs(float(row["applied_offset"])))
        if row.get("residual_seconds", "").strip():
            residuals.append(abs(float(row["residual_seconds"])))
        coverage = float(row.get("anchor_coverage") or 0.0)
        mad_text = row.get("anchor_mad", "").strip()
        mad = float(mad_text) if mad_text else math.inf
        if row.get("anchor_status", "").strip().casefold() in {"reliable", "manual"} and coverage >= args.minimum_coverage and mad <= args.maximum_mad:
            reliable.append(index)

    required = max(3, math.ceil(len(rows) * args.minimum_anchor_fraction))
    if len(reliable) < required:
        raise ValueError(f"{args.clip}: only {len(reliable)} reliable anchors; need {required}")
    third_hits: list[bool] = []
    for third in range(3):
        low = math.floor(len(rows) * third / 3)
        high = math.ceil(len(rows) * (third + 1) / 3)
        third_hits.append(any(low <= index < high for index in reliable))
    if not all(third_hits):
        raise ValueError(f"{args.clip}: missing reliable anchor in song thirds {third_hits}")
    max_adjustment = max(adjustments, default=0.0)
    max_residual = max(residuals, default=0.0)
    if max_adjustment > args.maximum_adjustment:
        raise ValueError(f"{args.clip}: adjustment {max_adjustment:.3f}s exceeds {args.maximum_adjustment:.3f}s")
    if max_residual > args.maximum_residual:
        raise ValueError(f"{args.clip}: residual {max_residual:.3f}s exceeds {args.maximum_residual:.3f}s")

    events = ass_events(ass)
    if len(events) != len(rows):
        raise ValueError(f"{args.clip}: ASS has {len(events)} lyric events; report has {len(rows)}")
    start_deltas: list[float] = []
    for row, event in zip(rows, events):
        expected_text = norm_text(row.get("text", ""))
        if expected_text != event["text"]:
            raise ValueError(f"{args.clip} line {row['line']}: ASS/report lyric text mismatch")
        expected_start = float(row["new_start"]) + args.expected_ass_delay
        delta = abs(float(event["start"]) - expected_start)
        start_deltas.append(delta)
        if delta > args.maximum_ass_start_delta:
            raise ValueError(
                f"{args.clip} line {row['line']}: ASS start differs from verified axis by {delta:.3f}s"
            )

    payload = {
        "schema": "target-song-cutter.song-timing-gate.v1",
        "status": "PASS",
        "clip": args.clip,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "video": {"name": video.name, "sha256": sha256(video)},
        "ass": {"name": ass.name, "sha256": sha256(ass)},
        "timing_report": {"name": report.name, "sha256": sha256(report)},
        "thresholds": {
            "minimum_coverage": args.minimum_coverage,
            "maximum_mad_seconds": args.maximum_mad,
            "minimum_anchor_fraction": args.minimum_anchor_fraction,
            "minimum_anchor_count": 3,
            "maximum_adjustment_seconds": args.maximum_adjustment,
            "maximum_residual_seconds": args.maximum_residual,
            "expected_ass_delay_seconds": args.expected_ass_delay,
            "maximum_ass_start_delta_seconds": args.maximum_ass_start_delta,
            "requires_anchor_in_each_third": True,
        },
        "metrics": {
            "line_count": len(rows),
            "reliable_anchor_count": len(reliable),
            "required_anchor_count": required,
            "anchor_in_each_third": third_hits,
            "maximum_absolute_adjustment_seconds": round(max_adjustment, 6),
            "maximum_absolute_residual_seconds": round(max_residual, 6),
            "maximum_ass_start_delta_seconds": round(max(start_deltas, default=0.0), 6),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"PASS {args.clip}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
