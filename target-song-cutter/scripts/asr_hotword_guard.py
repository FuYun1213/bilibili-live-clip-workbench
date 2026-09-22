"""Fresh, content-bound checks for ASR vocabulary leakage in subtitle artifacts."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from pathlib import Path
from functools import lru_cache
from typing import Iterable

SKILL_ROOT = Path(__file__).resolve().parents[1]
GUARD_POLICY = "asr-hotword-guard-v2"
AUDIO_ONLY_POLICY = "audio-only-no-hotword-prompt-v2"


def clean_caption_text(value: str) -> str:
    value = re.sub(r"\{[^}]*\}", "", str(value))
    value = value.replace(r"\N", " ").replace(r"\n", " ")
    value = re.sub(r"^\[(?:说话人|分段)\s*[^]]*\]\s*", "", value)
    return re.sub(r"^[（(](?:念弹幕|读弹幕|读SC|念SC)[）)]\s*", "", value)


def compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", clean_caption_text(value)).casefold()
    return "".join(char for char in normalized if char.isalnum())


def vocabulary_groups() -> list[list[str]]:
    # Read live settings; never memoize a mutable vocabulary configuration.
    profiles = json.loads((SKILL_ROOT / "assets/creator-profiles.json").read_text("utf-8-sig"))
    groups = [
        [*profile.get("hotwords", []), *profile.get("stream_hotwords", [])]
        for profile in profiles["profiles"].values()
    ]
    general = json.loads((SKILL_ROOT / "references/general-hotwords.json").read_text("utf-8-sig"))
    groups.append(general.get("terms", []))
    return groups


@lru_cache(maxsize=128)
def _prepare_terms(terms: tuple[str, ...]) -> tuple[frozenset[str], re.Pattern]:
    # Cache only a pure function of the actual terms, never a mutable file path.
    words = frozenset(filter(None, (compact(term) for term in terms)))
    pattern = "|".join(re.escape(word) for word in sorted(words, key=lambda word: (-len(word), word)))
    return words, re.compile(pattern or r"(?!)")


def _matches_echo(normalized: str, pieces: list[str], prepared: tuple) -> bool:
    words, matcher = prepared
    if len(words) < 3 or len(normalized) < 6:
        return False
    listed = [piece for piece in pieces if piece in words]
    if len(set(listed)) >= 3 and sum(map(len, listed)) / len(normalized) >= 0.90:
        return True
    matches = list(matcher.finditer(normalized))
    return (
        len({match.group() for match in matches}) >= 3
        and sum(len(match.group()) for match in matches) / len(normalized) >= 0.90
    )


def _echo_in_groups(text: str, groups: list[tuple]) -> bool:
    normalized = compact(text)
    if len(normalized) < 6:
        return False
    pieces = [compact(part) for part in re.split(r"[、,，;；\n]", clean_caption_text(text))]
    return any(_matches_echo(normalized, pieces, group) for group in groups)


def is_hotword_echo(text: str, terms: Iterable[str]) -> bool:
    return _echo_in_groups(text, [_prepare_terms(tuple(terms))])


def _row_text(row: dict) -> str:
    return str(row.get("sentence") or row.get("text") or "")


def _row_bounds(row: dict) -> tuple[float, float] | None:
    try:
        return (float(row["start_seconds"]), float(row["end_seconds"]))
    except (KeyError, TypeError, ValueError):
        return None


def find_hotword_echoes(rows: Iterable[dict], *, terms: Iterable[str] = ()) -> list[dict]:
    rows = list(rows)
    groups = [_prepare_terms(tuple(group)) for group in [list(terms), *vocabulary_groups()]]
    findings = []
    covered: set[int] = set()
    for index, row in enumerate(rows):
        if _echo_in_groups(_row_text(row), groups):
            findings.append({
                "start_seconds": row.get("start_seconds", row.get("start")),
                "end_seconds": row.get("end_seconds", row.get("end")),
                "text": _row_text(row), "row_indices": [index],
            })
            covered.add(index)
    # An ASS renderer may split one leaked list into several individually
    # innocuous cues. Inspect only time-adjacent windows, not unrelated dialogue.
    for index, row in enumerate(rows):
        bounds = _row_bounds(row)
        if bounds is None or index in covered:
            continue
        start, end = bounds
        text = _row_text(row)
        for last in range(index + 1, min(len(rows), index + 6)):
            following = _row_bounds(rows[last])
            if following is None:
                break
            left, right = following
            if left < end - 0.05 or left - end > 0.35 or right - start > 12:
                break
            text += "、" + _row_text(rows[last])
            end = right
            if _echo_in_groups(text, groups):
                indices = list(range(index, last + 1))
                if not any(value in covered for value in indices):
                    findings.append({"start_seconds": start, "end_seconds": end,
                                     "text": text, "row_indices": indices})
                    covered.update(indices)
                break
    return findings


def require_no_hotword_echoes(rows: Iterable[dict], *, terms: Iterable[str] = ()) -> None:
    findings = find_hotword_echoes(rows, terms=terms)
    if findings:
        raise ValueError(
            f"检测到 {len(findings)} 段疑似 ASR 热词列表复读（首段 {findings[0]['start_seconds']} 秒）。"
            "请从原录播无热词提示重新核对并修复流程1；不得直接作为对白生成选片或字幕。"
        )


def _clock(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def artifact_rows(data: bytes, suffix: str) -> list[dict]:
    text = data.decode("utf-8-sig")
    if suffix.lower() == ".ass":
        rows = []
        for line in text.splitlines():
            if line.startswith("Dialogue:"):
                fields = line.split(",", 9)
                if len(fields) != 10:
                    raise ValueError("Malformed ASS dialogue")
                rows.append({"start_seconds": _clock(fields[1]),
                             "end_seconds": _clock(fields[2]), "text": fields[9]})
        return sorted(rows, key=lambda row: (row["start_seconds"], row["end_seconds"]))
    if suffix.lower() == ".csv":
        return list(csv.DictReader(io.StringIO(text, newline="")))
    if suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        return list(payload.get("segments", []))
    if suffix.lower() == ".srt":
        rows = []
        for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
            lines = block.splitlines()
            timing = next((i for i, line in enumerate(lines) if " --> " in line), None)
            if timing is None:
                continue
            left, right = lines[timing].split(" --> ", 1)
            rows.append({"start_seconds": _clock(left), "end_seconds": _clock(right),
                         "text": " ".join(lines[timing + 1:])})
        return rows
    raise ValueError(f"Unsupported subtitle artifact: {suffix}")


def stored_guard_terms(path: Path) -> list[str]:
    # Retain the terms used by an old run, even after the user edits its glossary.
    candidates = [path] if path.suffix.lower() == ".json" else []
    for directory in path.resolve().parents:
        if (directory / "workflow-project.json").is_file():
            candidates.extend(directory / "analysis/transcript" / name
                              for name in ("transcript.json", "transcript_multilingual.json"))
            break
    terms: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        payload = json.loads(candidate.read_text("utf-8-sig"))
        if not isinstance(payload, dict):
            continue
        terms.extend(payload.get("hotword_guard_terms", []))
        provider = payload.get("provider", {})
        if isinstance(provider, dict):
            terms.extend(provider.get("guard_terms", []))
    return terms


def audit_artifact(path: Path, *, terms: Iterable[str] = ()) -> dict:
    path = Path(path)
    data = path.read_bytes()  # Always fresh: an old PASS never authorizes new text.
    snapshot_terms = [*terms, *stored_guard_terms(path)]
    rows = artifact_rows(data, path.suffix)
    findings = find_hotword_echoes(rows, terms=snapshot_terms)
    return {"policy": GUARD_POLICY, "status": "FAIL" if findings else "PASS",
            "path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
            "checked_row_count": len(rows), "findings": findings}


def require_clean_artifact(path: Path, *, terms: Iterable[str] = ()) -> dict:
    report = audit_artifact(path, terms=terms)
    if report["findings"]:
        raise ValueError(f"{Path(path).name}: 检测到热词列表复读；请核对源音频后修复字幕")
    return report
