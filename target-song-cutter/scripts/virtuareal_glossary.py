#!/usr/bin/env python3
"""Load, validate, export, and apply the shared VirtuaReal ASR glossary."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


DEFAULT_GLOSSARY = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "virtuareal-glossary.json"
)
DEFAULT_CORRECTION_DICTIONARY = (
    Path(__file__).resolve().parents[2]
    / "workflow-projects"
    / "字幕纠错词典.json"
)
SEED_CORRECTIONS = {
    "小鹿": "小路",
    "四小路": "四时小路",
    "四小鹿": "四时小路",
    "四时小鹿": "四时小路",
    "刚镚": "钢镚",
    "杠镚": "钢镚",
    "缸镚": "钢镚",
    "钢绷": "钢镚",
    "钢本": "钢镚",
    "刚蹦": "钢镚",
    "杠蹦": "钢镚",
    "阴道劲": "阴道镜",
    "引道劲": "阴道镜",
    "贡颈癌": "宫颈癌",
    "波罗芬": "布洛芬",
    "储姬": "筑基",
    "储机": "筑基",
    "助吉": "筑基",
    "储成": "筑基",
    "小粉元": "小粉螈",
    "小粉员": "小粉螈",
    "小粉圆": "小粉螈",
    "欧米嘎": "Omega",
    "欧米干": "Omega",
    "KPR": "KPL",
    "SPSHAT": "Super Chat",
    "Supacchartel": "Super Chat",
    "知景": "枝堇",
    "肢颈": "枝堇",
    "织景": "枝堇",
    "志敬": "枝堇",
}

MIN_ACTIVE_CORRECTION_CHARS = 2


def _correction_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _correction_id(wrong: str, replacement: str) -> str:
    return f"{wrong}\u241f{replacement}"


def _write_correction_dictionary(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _remove_shadow_candidates(
    entries: dict[str, Any], wrong: str, replacement: str
) -> bool:
    """Drop minimal-diff candidates already covered by an active full phrase."""
    changed = False
    for key, candidate in list(entries.items()):
        if candidate.get("status") != "candidate":
            continue
        candidate_wrong = str(candidate.get("wrong", ""))
        candidate_replacement = str(candidate.get("replacement", ""))
        if (
            candidate_wrong
            and candidate_replacement
            and (candidate_wrong, candidate_replacement) != (wrong, replacement)
            and candidate_wrong in wrong
            and wrong.replace(candidate_wrong, candidate_replacement) == replacement
        ):
            del entries[key]
            changed = True
    return changed


def load_correction_dictionary(
    path: Path = DEFAULT_CORRECTION_DICTIONARY, *, create: bool = False
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    entries = value.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    changed = False
    for wrong, replacement in SEED_CORRECTIONS.items():
        key = _correction_id(wrong, replacement)
        if key in entries:
            continue
        entries[key] = {
            "wrong": wrong,
            "replacement": replacement,
            "status": "active",
            "source": "内置请求",
            "count": 0,
            "updated_at": _correction_now(),
            "examples": [],
        }
        changed = True
    # A one-character active rule would be a global character substitution (for
    # example 上→山), which is too broad for subtitle correction. Preserve the
    # row for auditability but make sure it cannot affect transcripts.
    for row in entries.values():
        wrong = str(row.get("wrong", "")).strip()
        if row.get("status") == "active" and len(wrong) < MIN_ACTIVE_CORRECTION_CHARS:
            row["status"] = "disabled"
            row["source"] = "单字全局替换（安全停用）"
            row["updated_at"] = _correction_now()
            changed = True
        row.setdefault("match_mode", "exact_phrase")

    for row in list(entries.values()):
        if row.get("status") == "active":
            changed = _remove_shadow_candidates(
                entries,
                str(row.get("wrong", "")),
                str(row.get("replacement", "")),
            ) or changed

    value.update({"schema_version": 2, "entries": entries})
    if changed:
        value["updated_at"] = _correction_now()
    if create and (changed or not path.is_file()):
        _write_correction_dictionary(path, value)
    return value


def active_user_corrections(
    path: Path = DEFAULT_CORRECTION_DICTIONARY,
) -> dict[str, str]:
    value = load_correction_dictionary(path)
    return {
        str(row.get("wrong", "")): str(row.get("replacement", ""))
        for row in value.get("entries", {}).values()
        if row.get("status") == "active"
        and len(str(row.get("wrong", "")).strip()) >= MIN_ACTIVE_CORRECTION_CHARS
        and str(row.get("wrong", ""))
        and str(row.get("replacement", ""))
    }


def upsert_correction(
    wrong: str,
    replacement: str,
    *,
    status: str = "active",
    path: Path = DEFAULT_CORRECTION_DICTIONARY,
) -> dict[str, Any]:
    wrong = " ".join(str(wrong).split())
    replacement = " ".join(str(replacement).split())
    if not wrong or not replacement or wrong == replacement:
        raise ValueError("纠错词与正确词不能为空或相同")
    if status not in {"active", "candidate", "disabled"}:
        raise ValueError(f"未知词典状态：{status}")
    if status == "active" and len(wrong) < MIN_ACTIVE_CORRECTION_CHARS:
        raise ValueError("为避免误改所有同字，启用纠错时错词至少填写 2 个字的完整词组")
    value = load_correction_dictionary(path, create=True)
    key = _correction_id(wrong, replacement)
    row = dict(value["entries"].get(key, {}))
    row.update(
        {
            "wrong": wrong,
            "replacement": replacement,
            "status": status,
            "source": row.get("source", "主页人工新增"),
            "match_mode": "exact_phrase",
            "count": int(row.get("count", 0) or 0),
            "updated_at": _correction_now(),
            "examples": list(row.get("examples", [])),
        }
    )
    value["entries"][key] = row
    if status == "active":
        _remove_shadow_candidates(value["entries"], wrong, replacement)
    value["updated_at"] = _correction_now()
    _write_correction_dictionary(path, value)
    return {"id": key, **row}


def set_correction_status(
    correction_id: str,
    status: str,
    *,
    path: Path = DEFAULT_CORRECTION_DICTIONARY,
) -> dict[str, Any]:
    if status not in {"active", "candidate", "disabled"}:
        raise ValueError(f"未知词典状态：{status}")
    value = load_correction_dictionary(path, create=True)
    if correction_id not in value["entries"]:
        raise KeyError("纠错词条不存在")
    if (
        status == "active"
        and len(str(value["entries"][correction_id].get("wrong", "")).strip())
        < MIN_ACTIVE_CORRECTION_CHARS
    ):
        raise ValueError("单字纠错不能全局启用；请填写包含该字的完整词组")
    value["entries"][correction_id]["status"] = status
    value["entries"][correction_id]["updated_at"] = _correction_now()
    value["updated_at"] = _correction_now()
    _write_correction_dictionary(path, value)
    return {"id": correction_id, **value["entries"][correction_id]}


def correction_dictionary_rows(
    query: str = "", *, path: Path = DEFAULT_CORRECTION_DICTIONARY
) -> list[dict[str, Any]]:
    needle = str(query).strip().casefold()
    value = load_correction_dictionary(path, create=True)
    rows = [
        {"id": key, **dict(row)} for key, row in value.get("entries", {}).items()
    ]
    user_pairs = {(row.get("wrong"), row.get("replacement")) for row in rows}
    for wrong, replacement in load_glossary().get("asr_replacements", {}).items():
        if (wrong, replacement) in user_pairs:
            continue
        rows.append(
            {
                "id": "",
                "wrong": wrong,
                "replacement": replacement,
                "status": "builtin",
                "source": "共享术语表",
                "count": 0,
                "updated_at": "",
                "examples": [],
            }
        )
    if needle:
        rows = [
            row for row in rows
            if needle in str(row.get("wrong", "")).casefold()
            or needle in str(row.get("replacement", "")).casefold()
            or needle in str(row.get("source", "")).casefold()
        ]
    order = {"candidate": 0, "active": 1, "disabled": 2, "builtin": 3}
    return sorted(
        rows,
        key=lambda row: (
            order.get(str(row.get("status", "")), 9),
            -int(row.get("count", 0) or 0),
            str(row.get("wrong", "")),
        ),
    )


def load_glossary(path: Path = DEFAULT_GLOSSARY) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    validate_glossary(data)
    return data


def validate_glossary(data: dict[str, Any]) -> None:
    required = {"schema_version", "organization", "members", "asr_replacements"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError(f"Glossary missing keys: {', '.join(missing)}")
    members = data["members"]
    source_ids = set(data.get("sources", {}))
    if not isinstance(members, list) or not members:
        raise ValueError("Glossary members must be a non-empty array")
    canonical_names: set[str] = set()
    for index, member in enumerate(members, 1):
        for key in ("generation", "canonical", "name_zh", "romanized"):
            if not member.get(key):
                raise ValueError(f"Member {index} missing {key}")
        canonical = str(member["canonical"])
        if canonical in canonical_names:
            raise ValueError(f"Duplicate member canonical name: {canonical}")
        canonical_names.add(canonical)
        for fan_term in member.get("fan_terms", []):
            if not fan_term.get("term") or not fan_term.get("kind"):
                raise ValueError(f"Invalid fan term under {canonical}")
            unknown_sources = set(fan_term.get("source_ids", [])) - source_ids
            if unknown_sources:
                raise ValueError(
                    f"Unknown source IDs under {canonical}: "
                    f"{', '.join(sorted(unknown_sources))}"
                )
    if len(members) != 49:
        raise ValueError(
            f"Expected 49 current VirtuaReal Project members, found {len(members)}"
        )


def _strings(values: Iterable[Any]) -> Iterable[str]:
    for value in values:
        text = str(value).strip()
        if text and not any(char.isspace() for char in text):
            yield text


def iter_hotwords(data: dict[str, Any]) -> Iterable[str]:
    organization = data["organization"]
    yield from _strings([organization["canonical"], *organization.get("aliases", [])])
    for member in data["members"]:
        yield from _strings(
            [
                member["canonical"],
                member["name_zh"],
                member["romanized"],
                *member.get("aliases", []),
            ]
        )
        yield from _strings(term["term"] for term in member.get("fan_terms", []))
    for item in data.get("additional_names", []):
        yield from _strings([item["canonical"], *item.get("aliases", [])])
    yield from _strings(data.get("general_hotwords", []))


def hotwords(data: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for term in iter_hotwords(data):
        folded = term.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(term)
    return result


def load_hotword_string(path: Path = DEFAULT_GLOSSARY) -> str:
    return " ".join(hotwords(load_glossary(path)))


def load_replacements(path: Path = DEFAULT_GLOSSARY) -> dict[str, str]:
    data = load_glossary(path)
    replacements = {
        str(wrong): str(canonical)
        for wrong, canonical in data.get("asr_replacements", {}).items()
    }
    replacements.update(active_user_corrections())
    return replacements


@lru_cache(maxsize=1)
def _simplifier():
    try:
        from opencc import OpenCC
        return OpenCC("t2s")
    except Exception:
        return None


def normalize_text(text: str, replacements: dict[str, str]) -> str:
    simplifier = _simplifier()
    if simplifier is not None:
        text = simplifier.convert(text)
    for wrong in sorted(replacements, key=len, reverse=True):
        text = text.replace(wrong, replacements[wrong])
    # Apply paid-message cleanup in every ASR/transcript path.
    return compact_paid_thanks(text)


PAID_MESSAGE_TOKEN = (
    r"(?:(?<![A-Za-z])(?:Super\s*(?:Chat|Cat|Chad)|S\s*[.·_-]?\s*C)"
    r"(?![A-Za-z])|醒目留言|醒目留音|"
    r"(?:钢|刚|杠|缸|冈|罡)[镚蹦崩绷蚌棒镑本奔宝](?:儿)?)"
)
PAID_THANKS_RE = re.compile(
    rf"(?:谢谢|感谢|多谢|感恩|thank\s*(?:you|s)?)\s*"
    rf"[^，。！？；：:\n]{{0,40}}?(?:的\s*)?{PAID_MESSAGE_TOKEN}"
    rf"(?:\s*(?:和|、|以及|还有|,)\s*[^，。！？；：:\n]{{0,24}}?"
    rf"(?:的\s*)?{PAID_MESSAGE_TOKEN})*",
    re.IGNORECASE,
)
PAID_SENDER_RE = re.compile(
    rf"(?<![A-Za-z0-9])[^，。！？；：:\n]{{1,28}}?"
    rf"(?:发的|送的|投喂的)\s*{PAID_MESSAGE_TOKEN}",
    re.IGNORECASE,
)
PAID_TOKEN_RE = re.compile(PAID_MESSAGE_TOKEN, re.IGNORECASE)
PAID_TOKEN_THANKS_RE = re.compile(
    rf"{PAID_MESSAGE_TOKEN}[^，。！？；：:\n]{{0,16}}?"
    rf"(?:谢谢|感谢|多谢|感恩|thank\s*(?:you|s)?)",
    re.IGNORECASE,
)
PAID_RECEIPT_RE = re.compile(
    rf"(?:(?:收到|收到了|收下|看到|来了|送来|发来|投喂了)"
    rf"[^，。！？；：:\n]{{0,12}}?{PAID_MESSAGE_TOKEN}(?:了|啦|呀|啊)?|"
    rf"{PAID_MESSAGE_TOKEN}\s*(?:来了|收到|收到了|送到了|到账了))",
    re.IGNORECASE,
)
THANKS_AROUND_MARKER_RE = re.compile(
    r"(?:谢谢|感谢|多谢|感恩|thank\s*(?:you|s)?)\s*（谢谢SC）|"
    r"（谢谢SC）\s*(?:谢谢|感谢|多谢|感恩|thank\s*(?:you|s)?)",
    re.IGNORECASE,
)


def contains_paid_message(text: str) -> bool:
    """Use the same fuzzy SC recognizer in transcription and gift filtering."""
    return bool(PAID_TOKEN_RE.search(str(text)))


def compact_paid_thanks(text: str) -> str:
    """Canonicalize paid-message mentions and remove sender/thanks copy."""
    marker = "（谢谢SC）"
    sentinel = "\uFFF0PAID_MESSAGE\uFFF1"
    value = str(text).replace(marker, sentinel)
    value = PAID_THANKS_RE.sub(sentinel, value)
    value = PAID_SENDER_RE.sub(sentinel, value)
    value = PAID_TOKEN_THANKS_RE.sub(sentinel, value)
    value = PAID_RECEIPT_RE.sub(sentinel, value).replace(sentinel, marker)
    previous = None
    while previous != value:
        previous = value
        value = THANKS_AROUND_MARKER_RE.sub(marker, value)
        value = re.sub(r"(?:（谢谢SC）\s*){2,}", marker, value)
    return value


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    hotword_parser = subparsers.add_parser("hotwords")
    hotword_parser.add_argument("--output", type=Path)
    normalize_parser = subparsers.add_parser("normalize")
    normalize_parser.add_argument("text")
    args = parser.parse_args()

    data = load_glossary(args.glossary)
    if args.command == "validate":
        fan_terms = sum(len(member.get("fan_terms", [])) for member in data["members"])
        print(
            f"OK: {len(data['members'])} current members, "
            f"{fan_terms} fan terms, {len(hotwords(data))} unique hotwords"
        )
        return 0
    if args.command == "hotwords":
        value = " ".join(hotwords(data))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(value + "\n", encoding="utf-8")
            print(args.output.resolve())
        else:
            print(value)
        return 0
    if args.command == "normalize":
        print(normalize_text(args.text, load_replacements(args.glossary)))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise
