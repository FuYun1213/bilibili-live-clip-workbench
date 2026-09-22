#!/usr/bin/env python3
"""Validate evidence-backed clip tags authored during candidate selection."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence


PROFILE_PATH = Path(__file__).resolve().parents[1] / "assets" / "creator-profiles.json"
GENERIC_TAGS = {
    "virtuareal", "虚拟主播", "直播切片", "vup", "虚拟singer", "歌切", "音乐",
    "主播", "直播", "切片", "杂谈", "娱乐", "直播日常", "精彩切片",
    "太搞笑了", "当场破防了",
}
TAG_SENTENCE_PUNCTUATION = re.compile(r"[，。！？：；|｜]")
REJECTED_DECISIONS = {"reject", "rejected", "drop", "dropped", "淘汰", "拒绝"}


def split_tags(raw: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in re.split(r"[,，;；|]", raw):
        tag = value.strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def merge_tags(base: Sequence[str], dynamic: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in [*base, *dynamic]:
        tag = str(value).strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result[:10]


def validate_tag_row(
    row: Mapping[str, str],
    creator: str,
    profile_tags: Sequence[str],
    *,
    strict_quality: bool = False,
) -> list[str]:
    """Validate upload-safe manual tags; optionally enforce model-output quality."""
    errors: list[str] = []
    raw = str(row.get("tags") or row.get("publish_tags") or "").strip()
    dynamic = split_tags(raw)
    minimum = 5 if strict_quality else 1
    if len(dynamic) < minimum:
        errors.append(
            "requires exactly 5 model-authored clip-specific tags"
            if strict_quality
            else "requires at least 1 clip-specific tag"
        )
    if len(dynamic) > 5:
        errors.append("allows at most 5 clip-specific tags")

    profile_keys = {str(tag).strip().casefold() for tag in profile_tags}
    duplicates = [tag for tag in dynamic if tag.casefold() in profile_keys]
    if duplicates:
        errors.append("clip-specific tags duplicate profile tags: " + ", ".join(duplicates))

    sentence_like = [
        tag for tag in dynamic
        if TAG_SENTENCE_PUNCTUATION.search(tag) or len(tag) > 20
    ]
    if strict_quality and sentence_like:
        errors.append("tags must be concise nouns, not title fragments: " + ", ".join(sentence_like))

    specific = [
        tag for tag in dynamic
        if tag.casefold() not in GENERIC_TAGS and tag.casefold() not in profile_keys
    ]
    required_specific = 3 if strict_quality else 1
    if len(specific) < required_specific:
        errors.append(
            "requires 3 non-generic content tags"
            if strict_quality
            else "requires at least 1 non-generic content tag"
        )

    evidence = str(row.get("tag_evidence") or "").strip()
    if not evidence:
        errors.append("tag_evidence is required")

    if creator == "chilly" and any(
        "vr" in tag.casefold() or "虚拟现实" in tag for tag in dynamic
    ):
        confirmed = str(row.get("vr_topic") or "").strip().casefold()
        if confirmed not in {"yes", "true", "1", "是", "有"}:
            errors.append("Chilly VR tags require vr_topic=yes because VR is a topic, not identity")
    return errors


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--creator", required=True)
    parser.add_argument("--profiles", type=Path, default=PROFILE_PATH)
    args = parser.parse_args()

    profiles = json.loads(args.profiles.read_text(encoding="utf-8-sig"))["profiles"]
    if args.creator not in profiles:
        parser.error(f"unknown creator: {args.creator}")
    profile_tags = profiles[args.creator].get("upload", {}).get("tags", [])
    with args.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("CSV has no rows")

    failures: list[str] = []
    checked = 0
    for index, row in enumerate(rows, 1):
        decision = str(row.get("decision") or "").strip().casefold()
        if decision in REJECTED_DECISIONS:
            continue
        checked += 1
        clip_id = str(row.get("clip_id") or row.get("candidate_id") or index).strip()
        failures.extend(f"{clip_id}: {error}" for error in validate_tag_row(row, args.creator, profile_tags))
    if failures:
        raise ValueError("\n".join(failures))
    print(f"OK: {checked} rows have evidence-backed selection tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

