#!/usr/bin/env python3
"""Consolidate reviewed clip ratings into cautious, auditable preferences."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path


LEDGER_FIELDS = [
    "source_file", "source_id", "clip_id", "title", "user_score", "score_scale",
    "normalized_score", "normalization_status", "user_verdict", "user_notes",
    "editorial_tags", "topic_key", "hook_type", "title_style", "duration_seconds",
    "danmaku_signal", "sc_signal",
]

SPLIT_RE = re.compile(r"[|,;\uFF0C\uFF1B]+")
SCALE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*(?:-|~|\u2013|\u2014|\uFF5E|\u5230)\s*(-?\d+(?:\.\d+)?)\s*$"
)


def split_values(value: str) -> list[str]:
    return [part.strip() for part in SPLIT_RE.split(value or "") if part.strip()]


def normalized_score(score_text: str, scale_text: str) -> tuple[str, float | None]:
    try:
        score = float(score_text)
    except (TypeError, ValueError):
        return "missing-or-invalid-score", None
    match = SCALE_RE.match(scale_text or "")
    if not match:
        return "scale-not-explicit", None
    low, high = (float(match.group(1)), float(match.group(2)))
    if high <= low or not low <= score <= high:
        return "score-outside-scale", None
    return "normalized", (score - low) / (high - low)


def read_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for source_row in csv.DictReader(handle):
                if not (source_row.get("user_score") or "").strip():
                    continue
                status, normalized = normalized_score(
                    source_row.get("user_score", ""), source_row.get("score_scale", "")
                )
                row = {field: (source_row.get(field) or "").strip() for field in LEDGER_FIELDS}
                row["source_file"] = str(path.resolve())
                row["source_id"] = row["source_id"] or path.parent.name
                row["normalization_status"] = status
                row["normalized_score"] = f"{normalized:.4f}" if normalized is not None else ""
                rows.append(row)
    return rows


def preference_groups(rows: list[dict]) -> dict[str, dict[str, list[dict]]]:
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row["normalization_status"] != "normalized":
            continue
        for tag in split_values(row["editorial_tags"]):
            groups["editorial_tag"][tag].append(row)
        for dimension in ("hook_type", "title_style", "danmaku_signal"):
            value = row[dimension]
            if value:
                groups[dimension][value].append(row)
        sc_role = row["sc_signal"].split(":", 1)[0].strip()
        if sc_role:
            groups["sc_role"][sc_role].append(row)
    return groups


def build_profile(rows: list[dict], min_evidence: int) -> dict:
    dimensions: dict[str, list[dict]] = {}
    for dimension, values in preference_groups(rows).items():
        summaries = []
        for value, examples in values.items():
            scores = [float(row["normalized_score"]) for row in examples]
            mean = statistics.fmean(scores)
            adjustment = 0
            if len(scores) >= min_evidence and mean >= 0.75:
                adjustment = 1
            elif len(scores) >= min_evidence and mean <= 0.35:
                adjustment = -1
            summaries.append(
                {
                    "value": value,
                    "rated_count": len(scores),
                    "mean_normalized_score": round(mean, 4),
                    "preference_adjustment": adjustment,
                    "stable": len(scores) >= min_evidence,
                    "example_clip_ids": [row["clip_id"] for row in examples[:5]],
                }
            )
        dimensions[dimension] = sorted(
            summaries,
            key=lambda item: (item["preference_adjustment"], item["mean_normalized_score"], item["rated_count"]),
            reverse=True,
        )
    return {
        "rated_clip_count": len(rows),
        "normalized_clip_count": sum(row["normalization_status"] == "normalized" for row in rows),
        "min_evidence_for_adjustment": min_evidence,
        "rule": "Apply only -1/0/+1 after repeated evidence; never override completeness, fairness, or source truth.",
        "dimensions": dimensions,
    }


def write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize clip-evaluation CSVs without overfitting one rating."
    )
    parser.add_argument("--input", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-evidence", type=int, default=3)
    args = parser.parse_args(argv)
    if args.min_evidence < 2:
        raise ValueError("min-evidence must be at least 2")
    missing = [str(path) for path in args.input if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing evaluation CSVs: " + ", ".join(missing))

    rows = read_rows(args.input)
    args.output.mkdir(parents=True, exist_ok=True)
    write_ledger(args.output / "feedback-ledger.csv", rows)
    profile = build_profile(rows, args.min_evidence)
    (args.output / "feedback-profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"rated={profile['rated_clip_count']} normalized={profile['normalized_clip_count']} "
        f"dimensions={len(profile['dimensions'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
