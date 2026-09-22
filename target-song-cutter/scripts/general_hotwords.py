#!/usr/bin/env python3
"""Load, validate, export, and merge the slicer's general ASR hotwords."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


DEFAULT_GENERAL_HOTWORDS = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "general-hotwords.json"
)


def validate_general_hotwords(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("General hotwords must use schema_version 1")
    if not str(data.get("updated_at", "")).strip():
        raise ValueError("General hotwords must include updated_at")
    terms = data.get("terms")
    if not isinstance(terms, list) or not terms:
        raise ValueError("General hotwords terms must be a non-empty array")
    seen: set[str] = set()
    for index, value in enumerate(terms, 1):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"General hotword {index} must be a non-empty string")
        term = value.strip()
        folded = term.casefold()
        if folded in seen:
            raise ValueError(f"Duplicate general hotword: {term}")
        seen.add(folded)


def load_general_hotwords(path: Path = DEFAULT_GENERAL_HOTWORDS) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    validate_general_hotwords(data)
    return [str(term).strip() for term in data["terms"]]


def merge_terms(*groups: Iterable[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            term = str(value).strip()
            if not term:
                continue
            folded = term.casefold()
            if folded not in seen:
                seen.add(folded)
                merged.append(term)
    return merged


def build_whisper_prompt(extra_prompt: str | None = None) -> None:
    """Compatibility entry point: vocabulary must never become decoder prompt text."""
    del extra_prompt
    load_general_hotwords()  # Keep validating the user's vocabulary configuration.
    return None


def build_funasr_hotwords(extra_hotwords: str | None = None) -> str:
    """Keep vocabulary for review/corrections, without recognizer-side word bias."""
    del extra_hotwords
    load_general_hotwords()
    return ""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--hotwords", type=Path, default=DEFAULT_GENERAL_HOTWORDS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    export = subparsers.add_parser("export")
    export.add_argument("--output", type=Path)
    args = parser.parse_args()

    terms = load_general_hotwords(args.hotwords)
    if args.command == "validate":
        print(f"OK: {len(terms)} unique general hotwords")
        return 0
    value = " ".join(terms)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(value + "\n", encoding="utf-8")
        print(args.output.resolve())
    else:
        print(value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise
