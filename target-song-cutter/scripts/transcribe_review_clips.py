#!/usr/bin/env python3
"""Re-transcribe final review clips with word timestamps for subtitle QA."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    try:
        import jieba as _jieba
    except ModuleNotFoundError:
        _jieba = None

if _jieba is not None:
    _jieba.setLogLevel(40)
    for _subtitle_term in (
        "超跑", "美团外卖", "直播间", "虚拟主播", "钢镚", "管人", "歌切", "切片",
        "我妈", "我爸", "他们", "她们", "晚上再说", "到时候再说",
    ):
        _jieba.add_word(_subtitle_term)

from general_hotwords import load_general_hotwords, merge_terms
from virtuareal_glossary import (
    compact_paid_thanks,
    hotwords,
    load_glossary,
    load_replacements,
    normalize_text,
)


DEFAULT_ASS_TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "subtitles" / "narrative-v2.ass"
DEFAULT_CREATOR_PROFILES = Path(__file__).resolve().parents[1] / "assets" / "creator-profiles.json"
LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[ ._'’+\-][A-Za-z0-9]+)*")
FORCE_SPLIT_WORD_GAP_SECONDS = 2.0


def load_creator_hotwords(creator: str, path: Path = DEFAULT_CREATOR_PROFILES) -> list[str]:
    if not creator.strip():
        return []
    profiles = json.loads(path.read_text(encoding="utf-8-sig"))["profiles"]
    if creator not in profiles:
        raise ValueError(f"unknown creator profile: {creator}")
    profile = profiles[creator]
    values = [
        *profile.get("hotwords", []),
        *profile.get("fan_badges", []),
        *profile.get("stream_hotwords", []),
    ]
    return merge_terms(values)


def media_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for subtitle boundary validation")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def load_content_types(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    with path.resolve().open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        Path(row.get("video", "")).name: (row.get("content_type") or "narrative").strip().lower()
        for row in rows
        if Path(row.get("video", "")).name
    }


def normalize_segments(segments: list[dict], duration: float) -> list[dict]:
    """Sort ASR output, remove covered duplicates, and reject tail hallucinations."""
    bounded: list[dict] = []
    for segment in segments:
        start = float(segment["start_seconds"])
        end = min(float(segment["end_seconds"]), duration)
        if start >= duration or end <= start:
            continue
        if start >= duration - 1.0 and float(segment["avg_logprob"]) < -1.0:
            continue
        segment["end_seconds"] = round(end, 3)
        bounded.append(segment)

    kept: list[dict] = []
    for segment in bounded:
        start = float(segment["start_seconds"])
        end = float(segment["end_seconds"])
        span = max(end - start, 0.001)
        contained = [
            other
            for other in bounded
            if other is not segment
            and float(other["start_seconds"]) >= start + 0.2
            and float(other["end_seconds"]) <= end + 0.2
        ]
        if len(contained) >= 2:
            ordered = sorted(contained, key=lambda item: float(item["start_seconds"]))
            covered = 0.0
            cursor = start
            for other in ordered:
                other_start = max(start, float(other["start_seconds"]))
                other_end = min(end, float(other["end_seconds"]))
                if other_end > max(cursor, other_start):
                    covered += other_end - max(cursor, other_start)
                    cursor = max(cursor, other_end)
            best_logprob = max(float(other["avg_logprob"]) for other in contained)
            if covered / span >= 0.60 and float(segment["avg_logprob"]) <= best_logprob - 0.15:
                continue
        kept.append(segment)
    return sorted(
        kept,
        key=lambda item: (float(item["start_seconds"]), float(item["end_seconds"])),
    )


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def wrap_ass_text(text: str, width: int = 18) -> str:
    """Return one ASS line; cue splitting happens on the word-timestamp axis."""
    del width
    text = " ".join(text.replace("\n", " ").split())
    text = text.replace("{", "｛").replace("}", "｝")
    return compact_paid_thanks(text.replace(r"\N", " "))


def bound_wordless_cue(cue: dict) -> dict:
    """Repair sparse fallback segments that span a long leading silence."""
    result = dict(cue)
    start = float(result["start_seconds"])
    end = float(result["end_seconds"])
    text = str(result.get("text", ""))
    glyph_count = len(re.sub(r"\s+", "", text))
    # Detect sparse text using its uncapped expected duration. Capping the
    # estimate at 12s misclassified every normal sentence over 24s as silence
    # and shifted all of its subtitles to the last 12s of the sentence.
    expected = max(1.2, glyph_count / 4.5 + 0.8)
    if end - start > max(12.0, expected * 2.0):
        result["start_seconds"] = max(start, end - min(12.0, expected))
    return result


def cue_glyph_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def synthetic_words_for_cue(cue: dict) -> list[dict]:
    """Create proportional fallback timing while keeping complete Latin tokens atomic."""
    text = " ".join(str(cue.get("text", "")).replace("\n", " ").split())
    tokens = re.findall(
        r"\s*[A-Za-z0-9]+(?:[._'’+\-][A-Za-z0-9]+)*|\s*[^\s]",
        text,
    )
    if not tokens:
        return []
    weights = [max(1, cue_glyph_count(token)) for token in tokens]
    total_weight = sum(weights)
    start = float(cue["start_seconds"])
    duration = max(0.2, float(cue["end_seconds"]) - start)
    words: list[dict] = []
    elapsed_weight = 0
    for token, weight in zip(tokens, weights):
        token_start = start + duration * elapsed_weight / total_weight
        elapsed_weight += weight
        token_end = start + duration * elapsed_weight / total_weight
        words.append(
            {
                "start_seconds": token_start,
                "end_seconds": token_end,
                "word": token,
            }
        )
    return words


CLAUSE_STARTERS = (
    "换句话说", "与此同时", "没想到", "到时候", "后来", "结果", "然后",
    "但是", "不过", "可是", "所以", "因为", "如果", "而且", "其实",
    "反正", "另外", "接着", "于是", "原来", "毕竟", "比如", "再说",
    "就是说", "就是", "我觉得", "你知道", "不知道", "对了", "还有",
)
SUBJECT_STARTERS = (
    "我妈妈", "我爸爸", "我妈", "我爸", "她妈妈", "她爸爸", "她妈", "她爸",
    "他妈妈", "他爸爸", "他妈", "他爸", "主播", "弹幕", "大家", "人家",
)
INCOMPLETE_ENDINGS = (
    "因为", "所以", "但是", "然后", "如果", "而且", "不过", "可是",
    "的", "地", "得", "把", "被", "给", "跟", "和", "与", "或", "在",
    "从", "向", "让", "就", "又", "还", "再", "才", "只", "很", "太",
    "更", "最", "想", "要", "能", "会", "有", "是", "说", "问", "想说",
    "觉得", "认为", "告诉", "这是", "那是", "这个", "那个",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们",
)
HARD_INCOMPLETE_ENDINGS = (
    "因为", "所以", "但是", "然后", "如果", "可是", "不过", "而且",
    "只要", "虽然", "不仅", "无论", "不管", "让", "把", "被", "给",
    "跟", "和", "与", "或", "在", "从", "向",
)
CONTINUATION_STARTS = (
    "的", "地", "得", "着", "过", "了", "吗", "呢", "吧", "啊", "呀",
    "嘛", "啦", "呗", "而", "但", "却",
)
COMPLETE_ENDINGS = (
    "知道吗", "明白吗", "可以吗", "怎么办", "为什么", "没关系", "不知道",
    "同意了", "说服了", "完成了", "结束了", "算了", "好了", "行了", "我靠",
    "了", "呢", "吧", "吗", "啊", "呀", "嘛", "啦", "哦", "呗", "喽",
)


def _joined_words(items: list[dict]) -> str:
    return " ".join("".join(str(item["word"]) for item in items).split())


_LEXICAL_BOUNDARY_CACHE: dict[str, frozenset[int]] = {}
_PROTECTED_LEXICAL_TERMS = (
    "女儿", "说服", "他们", "她们", "我们", "你们", "即日", "突然",
    "有时候", "时候", "超跑", "直播间", "虚拟主播", "美团外卖", "钢镚",
    "管人", "歌切", "切片", "我妈", "我爸", "晚上再说", "到时候再说",
)


def _lexical_interior_offsets(text: str) -> frozenset[int]:
    compact = re.sub(r"\s+", "", text)
    if compact in _LEXICAL_BOUNDARY_CACHE:
        return _LEXICAL_BOUNDARY_CACHE[compact]
    offsets: set[int] = set()
    if _jieba is not None and compact:
        for _, start, end in _jieba.tokenize(compact, mode="default", HMM=False):
            offsets.update(range(start + 1, end))
    for term in _PROTECTED_LEXICAL_TERMS:
        search_from = 0
        while compact:
            start = compact.find(term, search_from)
            if start < 0:
                break
            offsets.update(range(start + 1, start + len(term)))
            search_from = start + 1
    result = frozenset(offsets)
    _LEXICAL_BOUNDARY_CACHE[compact] = result
    return result


def _boundary_splits_lexical_word(words: list[dict], boundary: int) -> bool:
    if boundary <= 0 or boundary >= len(words):
        return False
    full_text = _joined_words(words)
    offset = cue_glyph_count(_joined_words(words[:boundary]))
    return offset in _lexical_interior_offsets(full_text)


def _merge_latin_fragments(words: list[dict]) -> list[dict]:
    """Keep Whisper's Latin subword pieces atomic before semantic segmentation."""
    merged: list[dict] = []
    for source in words:
        word = dict(source)
        raw = str(word.get("word", ""))
        if merged:
            previous = str(merged[-1].get("word", ""))
            continues_latin = bool(
                previous.rstrip()
                and raw
                and not raw[0].isspace()
                and re.search(r"[A-Za-z0-9]$", previous.rstrip())
                and re.match(r"[A-Za-z0-9]", raw)
            )
            if continues_latin:
                merged[-1]["word"] = previous + raw
                merged[-1]["end_seconds"] = word["end_seconds"]
                continue
        merged.append(word)
    return merged


def _semantic_boundary_score(
    words: list[dict], boundary: int, start: int = 0
) -> int:
    """Score a possible cut; meaning and spoken pauses outrank visual length."""
    if boundary <= 0 or boundary >= len(words):
        return 100
    left = _joined_words(words[start:boundary])
    previous = str(words[boundary - 1].get("word", ""))
    following_raw = str(words[boundary].get("word", ""))
    following = following_raw.strip()
    following_context = _joined_words(words[boundary : boundary + 8]).replace(" ", "")
    gap = max(
        0.0,
        float(words[boundary]["start_seconds"])
        - float(words[boundary - 1]["end_seconds"]),
    )
    score = 0
    if left.endswith(tuple("，,、：:")):
        score = max(score, 95)
    if gap >= 0.80:
        score = max(score, 95)
    elif gap >= 0.45:
        score = max(score, 78)
    elif gap >= 0.25:
        score = max(score, 55)
    if following_raw[:1].isspace():
        score = max(score, 62)
    if following_context.startswith(CLAUSE_STARTERS):
        score = max(score, 92)
    if cue_glyph_count(left) >= 8 and following_context.startswith(SUBJECT_STARTERS):
        score = max(score, 76)
    if left.endswith(COMPLETE_ENDINGS):
        score = max(score, 72)
    explicit_phrase_boundary = bool(
        following_raw[:1].isspace()
        or following_context.startswith(CLAUSE_STARTERS)
    )
    if left.endswith(HARD_INCOMPLETE_ENDINGS):
        score -= 100
    elif left.endswith(INCOMPLETE_ENDINGS) and not explicit_phrase_boundary:
        score -= 100
    if (
        following.startswith(CONTINUATION_STARTS)
        and not following_context.startswith(CLAUSE_STARTERS)
    ):
        score -= 100
    if re.search(r"[A-Za-z0-9]$", previous.rstrip()) and re.match(
        r"[A-Za-z0-9]", following
    ):
        score -= 200
    if _boundary_splits_lexical_word(words, boundary):
        score -= 300
    return score


def _split_semantic_run(
    words: list[dict],
    *,
    max_chars: int,
    max_duration: float,
    target_chars: int,
) -> list[dict]:
    """Split one uninterrupted speech run at the best complete-clause boundary."""
    cues: list[dict] = []
    position = 0
    while position < len(words):
        remainder = words[position:]
        remainder_text = _joined_words(remainder)
        remainder_duration = (
            float(remainder[-1]["end_seconds"])
            - float(remainder[0]["start_seconds"])
        )
        if (
            cue_glyph_count(remainder_text) <= max_chars
            and remainder_duration <= max_duration
        ):
            cut = len(words)
        else:
            allowed: list[tuple[int, int, int, float]] = []
            for boundary in range(position + 1, len(words) + 1):
                chunk = words[position:boundary]
                text = _joined_words(chunk)
                glyphs = cue_glyph_count(text)
                duration = (
                    float(chunk[-1]["end_seconds"])
                    - float(chunk[0]["start_seconds"])
                )
                single_atomic_word = boundary == position + 1
                if (
                    (glyphs > max_chars or duration > max_duration)
                    and not single_atomic_word
                ):
                    break
                score = (
                    100 if boundary == len(words)
                    else _semantic_boundary_score(words, boundary, position)
                )
                allowed.append((boundary, score, glyphs, duration))
            if not allowed:
                cut = position + 1
            else:
                semantic = [item for item in allowed if item[1] >= 50]
                pool = semantic or allowed

                def utility(item: tuple[int, int, int, float]) -> tuple[float, int]:
                    boundary, score, glyphs, _ = item
                    short_penalty = max(0, 8 - glyphs) * 80
                    length_penalty = abs(target_chars - glyphs) * 2
                    dangling_penalty = 300 if score < 0 else 0
                    return (
                        score * 10 - short_penalty - length_penalty - dangling_penalty,
                        boundary,
                    )

                cut = max(pool, key=utility)[0]
        chunk = words[position:cut]
        text = _joined_words(chunk)
        if text:
            cues.append(
                {
                    "start_seconds": chunk[0]["start_seconds"],
                    "end_seconds": chunk[-1]["end_seconds"],
                    "text": text,
                }
            )
        position = cut
    return cues


def _move_trailing_connectors_to_next_run(
    runs: list[list[dict]],
) -> list[list[dict]]:
    """Attach a connector spoken before a pause to the clause it introduces."""
    for index in range(len(runs) - 1):
        run = runs[index]
        if not run:
            continue
        max_length = max(map(len, CLAUSE_STARTERS))
        suffix_start: int | None = None
        for candidate in range(len(run) - 1, -1, -1):
            suffix = _joined_words(run[candidate:]).replace(" ", "")
            if len(suffix) > max_length:
                break
            if suffix in CLAUSE_STARTERS:
                suffix_start = candidate
        if suffix_start is None or suffix_start == 0:
            continue
        connector = run[suffix_start:]
        runs[index] = run[:suffix_start]
        runs[index + 1] = [*connector, *runs[index + 1]]
    return [run for run in runs if run]


def ass_cues(
    segment: dict,
    max_chars: int = 24,
    max_duration: float = 7.0,
    target_chars: int = 18,
) -> list[dict]:
    """Build single-line cues using complete clauses first and length only as fallback."""
    words = [word for word in segment.get("words", []) if word.get("word", "").strip()]
    if not words:
        bounded = bound_wordless_cue(
            {
                "start_seconds": segment["start_seconds"],
                "end_seconds": segment["end_seconds"],
                "text": segment["text"],
            }
        )
        words = synthetic_words_for_cue(bounded)
        if not words:
            return []
    words = _merge_latin_fragments(words)
    runs: list[list[dict]] = []
    current: list[dict] = []
    strong_end = tuple("。！？!?；;")
    for word_index, word in enumerate(words):
        if current:
            gap = (
                float(word["start_seconds"])
                - float(current[-1]["end_seconds"])
            )
            if gap >= 1.15 and (
                gap >= FORCE_SPLIT_WORD_GAP_SECONDS
                or not _boundary_splits_lexical_word(words, word_index)
            ):
                runs.append(current)
                current = []
        current.append(word)
        text = _joined_words(current)
        duration = (
            float(current[-1]["end_seconds"])
            - float(current[0]["start_seconds"])
        )
        latin_period = text.endswith(".") and bool(re.search(r"[A-Za-z0-9][.]$", text))
        if (text.endswith(strong_end) or latin_period) and (
            cue_glyph_count(text) >= 3 or duration >= 0.8
        ):
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    runs = _move_trailing_connectors_to_next_run(runs)

    cues: list[dict] = []
    for run in runs:
        cues.extend(
            _split_semantic_run(
                run,
                max_chars=max_chars,
                max_duration=max_duration,
                target_chars=min(target_chars, max_chars),
            )
        )
    return cues

def merge_intervals(
    intervals: list[tuple[float, float]], max_gap: float = 0.08
) -> list[tuple[float, float]]:
    """Merge overlapping speech regions and tiny detector gaps."""
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + max_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def interval_overlap(
    start: float, end: float, intervals: list[tuple[float, float]]
) -> float:
    return sum(max(0.0, min(end, right) - max(start, left)) for left, right in intervals)


def asr_coverage_intervals(
    segments: list[dict],
    word_merge_gap: float = 0.30,
    word_padding: float = 0.12,
) -> list[tuple[float, float]]:
    """Return the timeline actually backed by ASR words, not broad segment spans."""
    intervals: list[tuple[float, float]] = []
    for segment in segments:
        word_intervals = []
        for word in segment.get("words") or []:
            if not str(word.get("word") or "").strip():
                continue
            start = float(word.get("start_seconds") or 0.0)
            end = float(word.get("end_seconds") or 0.0)
            if end > start:
                word_intervals.append(
                    (max(0.0, start - word_padding), end + word_padding)
                )
        if word_intervals:
            intervals.extend(word_intervals)
            continue
        start = float(segment.get("start_seconds") or 0.0)
        end = float(segment.get("end_seconds") or 0.0)
        if end > start:
            intervals.append((start, end))
    return merge_intervals(intervals, max_gap=word_merge_gap)


def find_uncovered_speech_windows(
    speech_intervals: list[tuple[float, float]],
    segments: list[dict],
    duration: float,
    *,
    min_region: float = 0.35,
    merge_gap: float = 1.20,
    padding: float = 0.60,
    coverage_merge_gap: float = 0.30,
) -> list[dict[str, float]]:
    """Subtract word-backed time from VAD speech and return substantial uncovered gaps."""
    coverage = asr_coverage_intervals(
        segments, word_merge_gap=max(0.0, coverage_merge_gap)
    )
    missing: list[tuple[float, float]] = []
    for start, end in speech_intervals:
        if end <= start:
            continue
        cursor = start
        for covered_start, covered_end in coverage:
            if covered_end <= cursor:
                continue
            if covered_start >= end:
                break
            left = max(start, covered_start)
            right = min(end, covered_end)
            if left - cursor >= min_region:
                missing.append((cursor, left))
            cursor = max(cursor, right)
            if cursor >= end:
                break
        if end - cursor >= min_region:
            missing.append((cursor, end))
    cores = merge_intervals(missing, max_gap=merge_gap)
    return [
        {
            "core_start": start,
            "core_end": end,
            "start": round(max(0.0, start - padding), 3),
            "end": round(min(duration, end + padding), 3),
        }
        for start, end in cores
        if end > start
    ]


def whisper_segment_record(
    item,
    replacements: dict[str, str],
    *,
    offset: float = 0.0,
    core_start: float | None = None,
    core_end: float | None = None,
) -> dict | None:
    """Normalize one Faster-Whisper segment and optionally keep only a rescue core."""
    words: list[dict] = []
    raw_words = [
        word
        for word in (item.words or [])
        if word.start is not None and word.end is not None
    ]
    for word in raw_words:
        start = offset + float(word.start)
        end = offset + float(word.end)
        if end < start:
            continue
        if core_start is not None and core_end is not None:
            midpoint = (start + end) / 2.0
            if not (core_start - 0.08 <= midpoint <= core_end + 0.08):
                continue
        words.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "word": normalize_text(word.word, replacements),
                "probability": round(float(word.probability), 5),
            }
        )
    if raw_words and core_start is not None and not words:
        return None
    if core_start is None or core_end is None:
        start = offset + float(item.start)
        end = offset + float(item.end)
        text_source = item.text
    elif words:
        text_source = "".join(str(word["word"]) for word in words)
        start = float(words[0]["start_seconds"])
        end = float(words[-1]["end_seconds"])
    else:
        start = offset + float(item.start)
        end = offset + float(item.end)
        midpoint = (start + end) / 2.0
        if midpoint < core_start - 0.08 or midpoint > core_end + 0.08:
            return None
        text_source = item.text
    text = compact_paid_thanks(normalize_text(str(text_source).strip(), replacements))
    if not text or end <= start:
        return None
    return {
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "text": text,
        "avg_logprob": round(float(item.avg_logprob), 5),
        "no_speech_prob": round(float(item.no_speech_prob), 5),
        "words": words,
    }


def recover_transcript_gaps(
    model,
    clip: Path,
    segments: list[dict],
    speech_intervals: list[tuple[float, float]],
    duration: float,
    *,
    language: str | None,
    beam_size: int,
    prompt: str,
    replacements: dict[str, str],
    hallucination_silence_threshold: float,
    padding: float,
    minimum_gap: float = 0.35,
    coverage_merge_gap: float = 0.30,
) -> tuple[list[dict], dict]:
    """Re-run only VAD-positive/ASR-empty windows without VAD to recover omissions."""
    windows = find_uncovered_speech_windows(
        speech_intervals,
        segments,
        duration,
        min_region=minimum_gap,
        padding=padding,
        coverage_merge_gap=coverage_merge_gap,
    )
    report = {
        "enabled": True,
        "strategy": (
            "low-threshold Silero VAD coverage audit + targeted no-VAD Whisper retry "
            "+ exact-core auto-language fallback"
        ),
        "windows": windows,
        "recovered_segment_count": 0,
        "exact_auto_language_attempts": [],
        "exact_auto_language_recovered_segment_count": 0,
    }
    if not windows:
        return [], report

    from faster_whisper.audio import decode_audio

    sample_rate = 16_000
    audio = decode_audio(str(clip), sampling_rate=sample_rate)
    recovered: list[dict] = []
    for window in windows:
        sample_start = max(0, round(window["start"] * sample_rate))
        sample_end = min(len(audio), round(window["end"] * sample_rate))
        if sample_end <= sample_start:
            continue
        kwargs = {
            "language": language,
            "beam_size": beam_size,
            "vad_filter": False,
            "condition_on_previous_text": False,
            "word_timestamps": True,
            "hallucination_silence_threshold": hallucination_silence_threshold,
        }
        def decode_window(options: dict) -> tuple[list, object]:
            try:
                raw, detected = model.transcribe(
                    audio[sample_start:sample_end], **options
                )
                return list(raw), detected
            except IndexError:
                fallback = {**options, "word_timestamps": False}
                raw, detected = model.transcribe(
                    audio[sample_start:sample_end], **fallback
                )
                return list(raw), detected

        raw_items, detected = decode_window(kwargs)
        if language is None:
            detected_language = str(getattr(detected, "language", "") or "").strip()
            if detected_language:
                fixed_language = {**kwargs, "language": detected_language}
                if detected_language != "zh":
                    # Chinese creator hotwords can destabilize English/Japanese
                    # decoding after language detection. They are irrelevant here.
                    fixed_language.pop("hotwords", None)
                raw_items, _ = decode_window(fixed_language)
        offset = sample_start / sample_rate
        for item in raw_items:
            record = whisper_segment_record(
                item,
                replacements,
                offset=offset,
                core_start=float(window["core_start"]),
                core_end=float(window["core_end"]),
            )
            if record is None:
                continue
            duplicate = any(
                old.get("text") == record["text"]
                and abs(
                    (float(old["start_seconds"]) + float(old["end_seconds"])) / 2.0
                    - (float(record["start_seconds"]) + float(record["end_seconds"])) / 2.0
                ) < 1.0
                for old in [*segments, *recovered]
            )
            if not duplicate:
                record["gap_recovered"] = True
                recovered.append(record)

    # A padded retry can still miss a song or quoted foreign-language line when
    # the surrounding Chinese speech wins the one-language decision. Retry only
    # the still-uncovered VAD cores without hotwords, so language detection sees
    # the omitted audio itself instead of its neighbourhood.
    exact_windows = find_uncovered_speech_windows(
        speech_intervals,
        [*segments, *recovered],
        duration,
        min_region=minimum_gap,
        padding=0.0,
        coverage_merge_gap=coverage_merge_gap,
    )
    exact_recovered_count = 0
    for window in exact_windows:
        sample_start = max(0, round(window["core_start"] * sample_rate))
        sample_end = min(len(audio), round(window["core_end"] * sample_rate))
        if sample_end <= sample_start:
            continue
        exact_options = {
            "language": None,
            "beam_size": beam_size,
            "vad_filter": False,
            "condition_on_previous_text": False,
            "word_timestamps": True,
            "hallucination_silence_threshold": hallucination_silence_threshold,
        }
        try:
            raw, detected = model.transcribe(
                audio[sample_start:sample_end], **exact_options
            )
            raw_items = list(raw)
        except IndexError:
            raw, detected = model.transcribe(
                audio[sample_start:sample_end],
                **{**exact_options, "word_timestamps": False},
            )
            raw_items = list(raw)
        accepted = 0
        rejected = 0
        offset = sample_start / sample_rate
        for item in raw_items:
            record = whisper_segment_record(
                item,
                replacements,
                offset=offset,
                core_start=float(window["core_start"]),
                core_end=float(window["core_end"]),
            )
            if record is None:
                rejected += 1
                continue
            probabilities = [
                float(word.get("probability", 0.0) or 0.0)
                for word in record.get("words", [])
                if str(word.get("word", "")).strip()
            ]
            confident_ratio = (
                sum(value >= 0.35 for value in probabilities) / len(probabilities)
                if probabilities else 1.0
            )
            glyphs = len(re.sub(r"\s+", "", str(record.get("text", ""))))
            minimum_logprob = -1.10 if probabilities else -0.80
            if (
                float(record.get("avg_logprob", -99.0)) < minimum_logprob
                or float(record.get("no_speech_prob", 1.0)) > 0.80
                or glyphs < 2
                or confident_ratio < 0.35
            ):
                rejected += 1
                continue
            duplicate = any(
                old.get("text") == record["text"]
                and abs(
                    (float(old["start_seconds"]) + float(old["end_seconds"])) / 2.0
                    - (float(record["start_seconds"]) + float(record["end_seconds"])) / 2.0
                ) < 1.0
                for old in [*segments, *recovered]
            )
            if duplicate:
                continue
            record["gap_recovered"] = True
            record["gap_recovery_pass"] = "exact-auto-language"
            recovered.append(record)
            accepted += 1
            exact_recovered_count += 1
        report["exact_auto_language_attempts"].append(
            {
                "core_start": window["core_start"],
                "core_end": window["core_end"],
                "detected_language": str(getattr(detected, "language", "") or ""),
                "decoded_segment_count": len(raw_items),
                "accepted_segment_count": accepted,
                "rejected_segment_count": rejected,
            }
        )
    report["exact_auto_language_recovered_segment_count"] = exact_recovered_count
    report["recovered_segment_count"] = len(recovered)
    return recovered, report


def tighten_cue_to_speech(
    cue: dict,
    speech_intervals: list[tuple[float, float]],
    lead: float = 0.06,
    tail: float = 0.18,
    min_overlap: float = 0.08,
    delay: float = 0.0,
) -> tuple[dict | None, dict]:
    """Clamp a cue to independent VAD speech, or reject a silence-only cue."""
    original_start = float(cue["start_seconds"])
    original_end = float(cue["end_seconds"])
    overlapping = [
        (start, end)
        for start, end in speech_intervals
        if end > original_start and start < original_end
    ]
    overlap = interval_overlap(original_start, original_end, overlapping)
    base = {
        "text": cue.get("text", ""),
        "original_start": round(original_start, 3),
        "original_end": round(original_end, 3),
        "speech_overlap": round(overlap, 3),
    }
    if overlap < min_overlap or not overlapping:
        return None, {
            **base,
            "adjusted_start": "",
            "adjusted_end": "",
            "status": "rejected_no_speech",
            "needs_review": "yes",
            "reviewed": "no",
            "decision": "",
            "notes": "",
        }

    intersections = [
        (max(original_start, start), min(original_end, end))
        for start, end in overlapping
    ]
    significant = [
        (start, end) for start, end in intersections if end - start >= min_overlap
    ]
    effective = significant or intersections
    first_speech = min(start for start, _ in effective)
    last_speech = max(end for _, end in effective)
    adjusted_start = max(original_start, first_speech - max(0.0, lead))
    adjusted_end = min(original_end, last_speech + max(0.0, tail))
    if adjusted_end <= adjusted_start:
        return None, {
            **base,
            "adjusted_start": "",
            "adjusted_end": "",
            "status": "rejected_invalid_after_clamp",
            "needs_review": "yes",
            "reviewed": "no",
            "decision": "",
            "notes": "",
        }

    # A small overlap with the previous speech region can become silence after
    # the configured subtitle delay. Re-evaluate significant intersections on
    # the displayed timeline, then clamp once more to the speech that remains.
    shifted_start = adjusted_start + delay
    shifted_end = adjusted_end + delay
    shifted_candidates = [
        (start, end, max(shifted_start, start), min(shifted_end, end))
        for start, end in speech_intervals
        if end > shifted_start and start < shifted_end
    ]
    shifted_significant = [
        (start, end)
        for start, end, intersection_start, intersection_end in shifted_candidates
        if intersection_end - intersection_start >= min_overlap
    ]
    shifted_effective = shifted_significant or [
        (start, end) for start, end, _, _ in shifted_candidates
    ]
    if shifted_effective:
        first_shifted_speech = min(start for start, _ in shifted_effective)
        last_shifted_speech = max(end for _, end in shifted_effective)
        adjusted_start = max(
            adjusted_start,
            first_shifted_speech - max(0.0, lead),
        )
        adjusted_end = min(
            adjusted_end,
            last_shifted_speech + max(0.0, tail),
        )

    shifted_overlap = interval_overlap(
        adjusted_start + delay,
        adjusted_end + delay,
        speech_intervals,
    )
    if shifted_overlap < min_overlap:
        return None, {
            **base,
            "adjusted_start": round(adjusted_start, 3),
            "adjusted_end": round(adjusted_end, 3),
            "status": "rejected_after_delay_no_speech",
            "needs_review": "yes",
            "reviewed": "no",
            "decision": "",
            "notes": "",
        }

    start_trim = adjusted_start - original_start
    end_trim = original_end - adjusted_end
    status = "clamped" if start_trim > 0.001 or end_trim > 0.001 else "kept"
    needs_review = start_trim > 0.25 or end_trim > 0.35
    adjusted = {**cue, "start_seconds": adjusted_start, "end_seconds": adjusted_end}
    return adjusted, {
        **base,
        "adjusted_start": round(adjusted_start, 3),
        "adjusted_end": round(adjusted_end, 3),
        "status": status,
        "needs_review": "yes" if needs_review else "no",
        "reviewed": "no" if needs_review else "not-required",
        "decision": "",
        "notes": "",
    }


def detect_speech_intervals(
    clip: Path,
    threshold: float,
    min_speech_ms: int,
    min_silence_ms: int,
) -> list[tuple[float, float]]:
    """Build an independent, zero-padding Silero VAD axis for subtitle QA."""
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    sample_rate = 16000
    audio = decode_audio(str(clip), sampling_rate=sample_rate)
    chunks = get_speech_timestamps(
        audio,
        VadOptions(
            threshold=threshold,
            min_speech_duration_ms=min_speech_ms,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=0,
        ),
    )
    return merge_intervals(
        [
            (float(chunk["start"]) / sample_rate, float(chunk["end"]) / sample_rate)
            for chunk in chunks
        ]
    )


def ass_bgr_color(value: str) -> str:
    rgb = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", rgb):
        raise ValueError("ASS profile color must be #RRGGBB")
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def customize_ass_template(
    template: str,
    style: str,
    *,
    font: str = "",
    font_size: int | None = None,
    color: str = "",
    primary_color: str = "",
    outline_color: str = "",
    outline_width: float | None = None,
) -> str:
    """Apply one creator profile to the active dialogue style."""
    if font_size is not None and not 24 <= int(font_size) <= 160:
        raise ValueError("ASS profile font size must be between 24 and 160")
    targets = {style, f"{style}Overlap"}
    converted: list[str] = []
    matched = False
    for line in template.splitlines():
        if line.startswith("Style:") and "," in line:
            prefix, body = line.split(":", 1)
            fields = body.lstrip().split(",")
            if fields and fields[0].strip() in targets:
                matched = True
                if font:
                    fields[1] = font.strip()
                if font_size is not None:
                    fields[2] = str(int(font_size))
                if primary_color:
                    fields[3] = ass_bgr_color(primary_color)
                    fields[4] = ass_bgr_color(primary_color)
                effective_outline = outline_color or color
                if effective_outline:
                    fields[5] = ass_bgr_color(effective_outline)
                if outline_width is not None and len(fields) >= 18:
                    width = float(outline_width)
                    if not 3 <= width <= 8:
                        raise ValueError("ASS outline width must be between 3 and 8")
                    fields[16] = f"{width:g}"
                    fields[17] = f"{max(2.0, min(3.0, width / 2)):g}"
                line = f"{prefix}: " + ",".join(fields)
        converted.append(line)
    if not matched:
        raise ValueError(f"ASS template does not define requested style {style!r}")
    return "\n".join(converted) + "\n"


def write_ass(
    destination: Path,
    template: str,
    segments: list[dict],
    style: str,
    name: str,
    delay: float,
    speech_intervals: list[tuple[float, float]] | None = None,
    vad_lead: float = 0.06,
    vad_tail: float = 0.18,
    vad_min_overlap: float = 0.08,
) -> list[dict]:
    defined_styles = {
        line.split(",", 1)[0].split(":", 1)[1].strip()
        for line in template.splitlines()
        if line.startswith("Style:") and "," in line
    }
    if style not in defined_styles:
        raise ValueError(
            f"ASS template does not define requested style {style!r}; "
            f"available styles: {', '.join(sorted(defined_styles)) or '(none)'}"
        )
    header = template.split("[Events]", 1)[0].rstrip()
    header = re.sub(r"(?m)^WrapStyle:\s*\d+\s*$", "WrapStyle: 2", header)
    events_header = (
        "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    qa_rows: list[dict] = []
    for segment in segments:
        event_name = str(
            segment.get("speaker_label") or segment.get("speaker_key") or name
        ).strip()
        for cue in ass_cues(segment):
            if speech_intervals is not None:
                adjusted, qa = tighten_cue_to_speech(
                    cue,
                    speech_intervals,
                    lead=vad_lead,
                    tail=vad_tail,
                    min_overlap=vad_min_overlap,
                    delay=delay,
                )
                qa_rows.append(qa)
                if adjusted is None:
                    continue
                cue = adjusted
            start = float(cue["start_seconds"]) + delay
            end = max(start + 0.20, float(cue["end_seconds"]) + delay)
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},{event_name},0,0,0,,"
                f"{wrap_ass_text(cue['text'])}"
            )
    destination.write_text(
        header + events_header + "\n".join(events) + "\n", encoding="utf-8-sig"
    )
    return qa_rows


def configure_cuda_dlls(device: str):
    if device != "cuda" or os.name != "nt":
        return None

    # Faster-Whisper/CTranslate2 can use a system CUDA runtime directly.  Torch
    # is only an optional source of bundled DLLs, so do not make it a hard
    # dependency on Windows installations that already have working CUDA.
    try:
        import torch
    except ModuleNotFoundError:
        return None

    torch_lib = Path(torch.__file__).resolve().parent / "lib"
    if not torch_lib.is_dir():
        return None
    os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
    return os.add_dll_directory(str(torch_lib))


def anchor_rows(segments: list[dict]) -> list[dict]:
    if not segments:
        return []
    indices = sorted({0, len(segments) // 2, len(segments) - 1})
    labels = {0: "early", len(segments) // 2: "middle", len(segments) - 1: "late"}
    return [
        {
            "anchor": labels[index],
            "start_seconds": segments[index]["start_seconds"],
            "end_seconds": segments[index]["end_seconds"],
            "text": segments[index]["text"],
        }
        for index in indices
    ]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=(
            "Run clip-local Faster-Whisper after editing so ASS timing follows the "
            "actual delivered MP4 rather than stale full-session timestamps."
        )
    )
    parser.add_argument("--clips-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pattern", default="*.mp4")
    parser.add_argument(
        "--content-types-csv",
        type=Path,
        help="Optional video/content_type table; rows marked song bypass VAD clamping.",
    )
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true", help="Disable speech VAD so quiet singing and musical tails are not discarded.")
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--vad-min-speech-ms", type=int, default=120)
    parser.add_argument("--vad-min-silence-ms", type=int, default=350)
    parser.add_argument("--vad-speech-pad-ms", type=int, default=120)
    parser.add_argument("--hallucination-silence-threshold", type=float, default=1.0)
    parser.add_argument(
        "--gap-recovery-vad-threshold",
        type=float,
        default=0.30,
        help="Lower-threshold VAD used only to find speech that the primary pass omitted.",
    )
    parser.add_argument("--gap-recovery-min-silence-ms", type=int, default=650)
    parser.add_argument("--gap-recovery-padding", type=float, default=0.60)
    parser.add_argument(
        "--no-gap-recovery",
        action="store_true",
        help="Disable targeted no-VAD retries for uncovered speech regions.",
    )
    parser.add_argument(
        "--write-ass",
        action="store_true",
        help="Write one editable ASS beside each final MP4 using Whisper's clip-local axis.",
    )
    parser.add_argument(
        "--ass-output",
        type=Path,
        help="ASS destination. Defaults to --clips-dir so MP4 and ASS stay together.",
    )
    parser.add_argument("--ass-template", type=Path, default=DEFAULT_ASS_TEMPLATE)
    parser.add_argument("--ass-style", default="Kioi")
    parser.add_argument("--ass-name", default="柚雨Kioi")
    parser.add_argument("--ass-font", default="", help="Override active ASS style font")
    parser.add_argument("--ass-font-size", type=int, help="Override active ASS style size")
    parser.add_argument("--ass-color", default="", help="Override active ASS outline/theme color")
    parser.add_argument("--subtitle-delay", type=float, default=0.10)
    parser.add_argument("--subtitle-vad-lead", type=float, default=0.06)
    parser.add_argument("--subtitle-vad-tail", type=float, default=0.18)
    parser.add_argument("--subtitle-vad-min-overlap", type=float, default=0.08)
    parser.add_argument(
        "--overwrite-ass",
        action="store_true",
        help="Allow replacement of an existing reviewed ASS file.",
    )
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--creator", default="", help="Creator profile key for scoped hotwords")
    parser.add_argument("--profiles", type=Path, default=DEFAULT_CREATOR_PROFILES)
    parser.add_argument(
        "--extra-hotwords",
        default="",
        help="Comma-separated recording-specific names or terms.",
    )
    args = parser.parse_args()

    clips_dir = args.clips_dir.resolve()
    output = args.output.resolve()
    ass_output = (args.ass_output or clips_dir).resolve()
    clips = sorted(path for path in clips_dir.glob(args.pattern) if path.is_file())
    if not clips:
        raise FileNotFoundError(f"No clips matched {args.pattern!r} in {clips_dir}")
    output.mkdir(parents=True, exist_ok=True)
    ass_template = ""
    if args.write_ass:
        ass_output.mkdir(parents=True, exist_ok=True)
        ass_template = args.ass_template.resolve().read_text(encoding="utf-8-sig")
        ass_template = customize_ass_template(
            ass_template,
            args.ass_style,
            font=args.ass_font,
            font_size=args.ass_font_size,
            color=args.ass_color,
        )

    glossary = load_glossary(args.glossary) if args.glossary else load_glossary()
    profile_terms = load_creator_hotwords(args.creator, args.profiles)
    prompt_terms = merge_terms(
        load_general_hotwords(),
        hotwords(glossary),
        profile_terms,
        (term.strip() for term in args.extra_hotwords.split(",") if term.strip()),
    )
    prompt = "，".join(prompt_terms)
    replacements = load_replacements(args.glossary) if args.glossary else load_replacements()
    content_types = load_content_types(args.content_types_csv)

    dll_handle = configure_cuda_dlls(args.device)
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(args.model_cache.resolve()) if args.model_cache else None,
    )
    qa_rows: list[dict] = []
    subtitle_vad_rows: list[dict] = []
    for clip in clips:
        use_vad = not args.no_vad and content_types.get(clip.name, "narrative") != "song"

        def transcribe_clip(word_timestamps: bool):
            kwargs = {
                "language": None if args.language.strip().lower() in {"", "auto"} else args.language,
                "beam_size": args.beam_size,
                "vad_filter": use_vad,
                "condition_on_previous_text": False,
                "word_timestamps": word_timestamps,
                "hallucination_silence_threshold": args.hallucination_silence_threshold,
            }
            if use_vad:
                kwargs["vad_parameters"] = {
                    "threshold": args.vad_threshold,
                    "min_speech_duration_ms": args.vad_min_speech_ms,
                    "min_silence_duration_ms": args.vad_min_silence_ms,
                    "speech_pad_ms": args.vad_speech_pad_ms,
                }
            raw, detected = model.transcribe(str(clip), **kwargs)
            return list(raw), detected

        try:
            raw_segments, info = transcribe_clip(True)
        except IndexError as error:
            if use_vad:
                raise
            print(f"{clip.name}: word alignment failed ({error}); retrying full audio with segment timestamps", file=sys.stderr)
            raw_segments, info = transcribe_clip(False)

        segments: list[dict] = []
        for item in raw_segments:
            record = whisper_segment_record(item, replacements)
            if record is not None:
                segments.append(record)

        duration = media_duration(clip)
        segments = normalize_segments(segments, duration)

        clip_output = output / clip.stem
        clip_output.mkdir(parents=True, exist_ok=True)
        speech_intervals = None
        effective_vad_threshold = args.vad_threshold
        effective_min_silence_ms = args.vad_min_silence_ms
        gap_report = {
            "enabled": False,
            "strategy": "disabled for songs/no-VAD mode",
            "windows": [],
            "recovered_segment_count": 0,
        }
        if use_vad:
            effective_vad_threshold = min(
                args.vad_threshold, args.gap_recovery_vad_threshold
            )
            effective_min_silence_ms = max(
                args.vad_min_silence_ms, args.gap_recovery_min_silence_ms
            )
            speech_intervals = detect_speech_intervals(
                clip,
                threshold=effective_vad_threshold,
                min_speech_ms=args.vad_min_speech_ms,
                min_silence_ms=effective_min_silence_ms,
            )
            if not args.no_gap_recovery:
                recovered, gap_report = recover_transcript_gaps(
                    model,
                    clip,
                    segments,
                    speech_intervals,
                    duration,
                    language=(
                        None
                        if args.language.strip().lower() in {"", "auto"}
                        else args.language
                    ),
                    beam_size=args.beam_size,
                    prompt=prompt,
                    replacements=replacements,
                    hallucination_silence_threshold=(
                        args.hallucination_silence_threshold
                    ),
                    padding=args.gap_recovery_padding,
                )
                segments = normalize_segments([*segments, *recovered], duration)
        (clip_output / "gap-recovery.json").write_text(
            json.dumps(gap_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        speech_payload = {
            "source": str(clip),
            "enabled": speech_intervals is not None,
            "detector": "faster-whisper Silero VAD",
            "settings": {
                "threshold": effective_vad_threshold,
                "min_speech_duration_ms": args.vad_min_speech_ms,
                "min_silence_duration_ms": effective_min_silence_ms,
                "speech_pad_ms": 0,
            },
            "regions": [
                {"start_seconds": start, "end_seconds": end}
                for start, end in (speech_intervals or [])
            ],
        }
        (clip_output / "speech-activity.json").write_text(
            json.dumps(speech_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        payload = {
            "source": str(clip),
            "language": info.language,
            "language_probability": info.language_probability,
            "segments": segments,
        }
        (clip_output / "transcript.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (clip_output / "transcript.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=("start_seconds", "end_seconds", "text", "avg_logprob")
            )
            writer.writeheader()
            for segment in segments:
                writer.writerow({key: segment[key] for key in writer.fieldnames})
        if args.write_ass:
            ass_path = ass_output / f"{clip.stem}.ass"
            if ass_path.exists() and not args.overwrite_ass:
                raise FileExistsError(
                    f"Refusing to replace reviewed subtitle: {ass_path}. "
                    "Use --overwrite-ass only when replacement is intentional."
                )
            rows = write_ass(
                ass_path,
                ass_template,
                segments,
                args.ass_style,
                args.ass_name,
                args.subtitle_delay,
                speech_intervals=speech_intervals,
                vad_lead=args.subtitle_vad_lead,
                vad_tail=args.subtitle_vad_tail,
                vad_min_overlap=args.subtitle_vad_min_overlap,
            )
            subtitle_vad_rows.extend({"clip": clip.name, **row} for row in rows)
        for row in anchor_rows(segments):
            qa_rows.append({"clip": clip.name, **row, "verified": "no", "notes": ""})
        print(
            f"{clip.name}: {len(segments)} segments, "
            f"gap-recovered={gap_report['recovered_segment_count']}"
        )

    with (output / "anchor-qa.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ("clip", "anchor", "start_seconds", "end_seconds", "text", "verified", "notes")
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(qa_rows)
    with (output / "subtitle-vad-qa.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "clip", "text", "original_start", "original_end", "adjusted_start",
            "adjusted_end", "speech_overlap", "status", "needs_review",
            "reviewed", "decision", "notes",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(subtitle_vad_rows)
    if dll_handle is not None:
        dll_handle.close()
    print(
        f"Done: {len(clips)} clips; QA sheets: {output / 'anchor-qa.csv'}, "
        f"{output / 'subtitle-vad-qa.csv'}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise

