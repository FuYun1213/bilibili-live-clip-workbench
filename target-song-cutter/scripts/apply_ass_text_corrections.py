#!/usr/bin/env python3
"""Apply audited text-only ASS corrections while preserving every cue time."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def split_dialogue(line: str) -> tuple[str, str] | None:
    if not line.startswith("Dialogue:"):
        return None
    parts = line.split(",", 9)
    if len(parts) != 10:
        raise ValueError(f"malformed ASS dialogue: {line}")
    return ",".join(parts[:9]) + ",", parts[9]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ass-dir", required=True, type=Path)
    parser.add_argument("--corrections", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    config = json.loads(args.corrections.read_text(encoding="utf-8-sig"))
    global_replacements = list(config.get("global_replacements", []))
    files = dict(config.get("files", {}))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for name, rules in files.items():
        source = args.ass_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        exact = dict(rules.get("exact_replacements", {}))
        deletions = set(rules.get("delete_exact", []))
        seen_exact = {key: 0 for key in exact}
        seen_delete = {key: 0 for key in deletions}
        output: list[str] = []

        for line in source.read_text(encoding="utf-8-sig").splitlines():
            parsed = split_dialogue(line)
            if parsed is None:
                output.append(line)
                continue
            prefix, text = parsed
            for item in global_replacements:
                text = text.replace(str(item["old"]), str(item["new"]))
            if text in deletions:
                seen_delete[text] += 1
                continue
            if text in exact:
                seen_exact[text] += 1
                text = str(exact[text])
            output.append(prefix + text)

        missing = [f"replace:{key}" for key, count in seen_exact.items() if count == 0]
        missing += [f"delete:{key}" for key, count in seen_delete.items() if count == 0]
        if missing:
            raise ValueError(f"{name}: correction targets not found: {missing}")
        destination = args.output_dir / name
        destination.write_text("\n".join(output) + "\n", encoding="utf-8-sig")
        print(
            f"CORRECTED {name.encode('ascii', 'backslashreplace').decode('ascii')} "
            f"replacements={sum(seen_exact.values())} deletions={sum(seen_delete.values())}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
