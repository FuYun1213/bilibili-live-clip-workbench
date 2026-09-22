"""Audio-grounded word timing for only the authoritative rows crossed by edits."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_REVISION = "c7cbfc2048c462b0d63a45797104fc9db3ad62b7"
MODEL_PATH = ROOT / "assets/models/Qwen3-ForcedAligner-0.6B"
ALIGNMENT_VERSION = 3
PREFIX = re.compile(r"^(?:【读(?:SC|弹幕|留言)】)+")

class AlignmentError(ValueError):
    pass


def spoken_text(text):
    return PREFIX.sub("", text)


def kept(char):
    return char == "'" or unicodedata.category(char)[0] in "LN"


def restore_words(segment, units):
    """Match every aligned character to canonical text; retain punctuation/labels.

    This is text matching only: all timings must come from the acoustic model.
    Never interpolate a paragraph or use the aligner's text as a correction.
    """
    text = segment["text"]
    prefix = PREFIX.match(text)
    offset = prefix.end() if prefix else 0
    positions = [i for i, char in enumerate(text) if i >= offset and kept(char)]
    normalized = "".join(text[i] for i in positions)
    cursor = 0
    previous_end = 0.0
    duration = float(segment["end_seconds"]) - float(segment["start_seconds"])
    spans = []
    for unit in units:
        token = "".join(c for c in str(unit["text"]) if kept(c))
        if not token or not normalized.startswith(token, cursor):
            raise AlignmentError("强制对齐文字与权威字幕不匹配")
        start, end = float(unit["start_time"]), float(unit["end_time"])
        if not all(math.isfinite(t) for t in (start, end)):
            raise AlignmentError("强制对齐返回非有限时间")
        if start < -0.001 or end < start or end > duration + 0.08 or start < previous_end - 0.001:
            raise AlignmentError("强制对齐时间越界或逆序")
        spans.append((positions[cursor], min(start, duration), min(end, duration)))
        cursor += len(token)
        previous_end = end
    if cursor != len(normalized) or not spans or not any(b > a for _, a, b in spans):
        raise AlignmentError("强制对齐未完整覆盖权威字幕")
    words = []
    for i, (_, start, end) in enumerate(spans):
        begin = 0 if i == 0 else spans[i][0]
        finish = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        words.append(dict(word=text[begin:finish],
                          start_seconds=round(float(segment["start_seconds"]) + start, 3),
                          end_seconds=round(float(segment["start_seconds"]) + end, 3)))
    assert "".join(w["word"] for w in words) == text
    return words


def crossed_rows(items, segments):
    needed = set()
    for item in items:
        for span in item["timestamps"]:
            left, right = float(span["start_seconds"]), float(span["end_seconds"])
            for i, row in enumerate(segments):
                if row.get("words") or row.get("render_policy") == "native-captions" or not re.search(r"\w", row["text"]):
                    continue
                start, end = float(row["start_seconds"]), float(row["end_seconds"])
                if min(end, right) - max(start, left) > 0.020001 and max(left - start, end - right) > 0.060001:
                    needed.add(i)
    return sorted(needed)


def row_cache_key(source, row):
    stat = source.stat()
    payload = [ALIGNMENT_VERSION, MODEL_REVISION, str(source.resolve()), stat.st_size,
               stat.st_mtime_ns, row["start_seconds"], row["end_seconds"], row["text"]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def run_aligner(source, rows, destination, *, device="cpu"):
    from qwen_local_asr import _worker_python
    from windows_process import hidden_subprocess_kwargs
    from workflow_app_core import atomic_json
    if not (MODEL_PATH / "model.safetensors").is_file():
        raise AlignmentError("缺少本地字幕强制对齐模型，请运行 setup_subtitle_aligner.py")
    with tempfile.TemporaryDirectory(prefix="subtitle-align-", dir=destination) as temp:
        request, output = Path(temp) / "request.json", Path(temp) / "result.json"
        atomic_json(request, dict(source=str(source), rows=rows, model=str(MODEL_PATH), device=device))
        worker = Path(__file__).with_name("authoritative_alignment_worker.py")
        process = subprocess.Popen([str(_worker_python()), "-X", "utf8", str(worker),
            "--request", str(request), "--output", str(output)],
            cwd=str(ROOT), **hidden_subprocess_kwargs())
        try:
            code = process.wait(timeout=max(180, 45 * len(rows)))
        except subprocess.TimeoutExpired:
            # Windows venv Python is a launcher: killing only its PID leaves the
            # actual model process alive. Stop this owned worker's entire tree.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=15, **hidden_subprocess_kwargs())
            else:
                process.kill()
            process.wait(timeout=15)
            raise AlignmentError("本地字幕时间对齐超时，已停止对齐进程；原字幕与选片已保留")
        if code or not output.is_file():
            raise AlignmentError(f"本地字幕时间对齐未完成（退出码 {code}），原字幕与选片已保留")
        result = json.loads(output.read_text(encoding="utf-8-sig"))
        if len(result) != len(rows):
            raise AlignmentError("本地字幕时间对齐返回行数不符")
        return result


def single_unit_vad_words(row):
    """A subsecond, single Han character already has measured unit boundaries.

    Reuse the authoritative VAD interval itself, without subdividing a sentence
    or inventing internal word timing. Acoustic aligners can collapse these very
    short interjections to zero duration. Multi-character rows never use this.
    """
    token = "".join(c for c in spoken_text(row["text"]) if kept(c))
    start, end = float(row["start_seconds"]), float(row["end_seconds"])
    if (re.fullmatch(r"[\u3400-\u9fff]", token)
            and math.isfinite(start) and math.isfinite(end)
            and start >= 0 and 0 < end - start <= 1.0):
        return [dict(word=row["text"], start_seconds=start, end_seconds=end)]
    return None


def align_crossed_rows(source, segments, items, cache_dir, *, aligner=None):
    from workflow_app_core import atomic_json
    result = copy.deepcopy(segments)
    needed = crossed_rows(items, result)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pending, audit = [], []
    for index in needed:
        row = result[index]
        key = row_cache_key(source, row)
        path = cache_dir / (key + ".json")
        measured_unit = single_unit_vad_words(row)
        if measured_unit:
            row["words"] = measured_unit
            row["subtitle_prefix"] = PREFIX.match(row["text"])[0] if PREFIX.match(row["text"]) else ""
            audit.append(dict(row=index, start=row["start_seconds"], end=row["end_seconds"],
                              cache="source", method="authoritative-single-unit-vad", key=key))
            continue
        try:
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            if saved.get("key") != key:
                raise AlignmentError("过期的字词对齐缓存")
            words = restore_words(row, saved["units"])
        except (OSError, ValueError, KeyError, TypeError):
            pending.append((index, key, path))
        else:
            row["words"] = words
            row["subtitle_prefix"] = PREFIX.match(row["text"])[0] if PREFIX.match(row["text"]) else ""
            audit.append(dict(row=index, start=row["start_seconds"], end=row["end_seconds"], cache="hit", key=key))
    if pending:
        print(f"正在对齐 {len(pending)} 行跨切点字幕（本地音频＋现有文字，不重新转写）", flush=True)
        units_by_row = (aligner or run_aligner)(source, [result[i] for i, _, _ in pending], cache_dir)
        if len(units_by_row) != len(pending):
            raise AlignmentError("本地字幕时间对齐返回行数不符")
        for (index, key, path), units in zip(pending, units_by_row):
            row = result[index]
            row["words"] = restore_words(row, units)
            row["subtitle_prefix"] = PREFIX.match(row["text"])[0] if PREFIX.match(row["text"]) else ""
            atomic_json(path, dict(key=key, model_revision=MODEL_REVISION, units=units))
            audit.append(dict(row=index, start=row["start_seconds"], end=row["end_seconds"], cache="new", key=key))
    return result, audit
