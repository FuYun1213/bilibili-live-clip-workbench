"""Batch assignments from detected subtitle speakers to creator profiles."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any

import review_workspace as review


def speaker_group(row: dict[str, Any]) -> tuple[str, str]:
    name = str(row.get("name") or "").strip()
    return (name, "" if name else str(row.get("style") or "").strip())


def speaker_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = speaker_group(row)
        group = groups.setdefault(key, {
            "key": key, "label": key[0] or f"未标记（{key[1]}）",
            "count": 0, "sample": str(row.get("text") or ""),
        })
        group["count"] += 1
    return list(groups.values())


def apply_speaker_roles(
    path: Path,
    assignments: dict[tuple[str, str], str],
    profiles: dict[str, dict[str, Any]],
) -> int:
    """Change names/styles together, preserving cue times, text and group identity.

    Resolve every assignment against the original rows so swaps do not cascade.
    Commit only once after every target profile and ASS style is valid.
    """
    rows = review.read_dialogues(path)
    groups = {speaker_group(row) for row in rows}
    for group, role in assignments.items():
        if group not in groups:
            raise ValueError("字幕说话人已变化，请重新打开对应窗口")
        if role not in profiles or profiles[role].get("archived"):
            raise ValueError(f"角色不存在或已停用：{role}")
    if not assignments:
        return 0
    with tempfile.TemporaryDirectory(prefix=".speaker-roles-", dir=path.parent) as directory:
        staged = Path(directory) / path.name
        staged.write_bytes(path.read_bytes())
        styles = {}
        for group, role in assignments.items():
            base = next(row["style"] for row in rows if speaker_group(row) == group)
            styles[group] = review.ensure_speaker_style(staged, role, profiles[role], base_style=base)
        lines = staged.read_text(encoding="utf-8-sig").splitlines()
        count = 0
        for index, line in enumerate(lines):
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(",", 9)
            if len(fields) != 10:
                continue
            group = speaker_group({"name": fields[4], "style": fields[3]})
            if group not in assignments:
                continue
            role = assignments[group]
            name = str(profiles[role].get("display_name") or role)
            if (fields[3], fields[4]) != (styles[group], name):
                fields[3], fields[4] = styles[group], name
                lines[index] = ",".join(fields)
                count += 1
        staged.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        os.replace(staged, path)
    return count
