#!/usr/bin/env python3
"""Audit final MP4/ASS delivery pairs and optional song-master durations."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
from pathlib import Path

from windows_process import hidden_subprocess_kwargs, run_checked_with_transient_retries


ASS_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})[.](\d{2})$")


def ffprobe_media(path: Path) -> dict:
    result = run_checked_with_transient_retries(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,start_time,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        **hidden_subprocess_kwargs(),
    )
    return json.loads(result.stdout)


def ffprobe_duration(path: Path) -> float:
    return float(ffprobe_media(path)["format"]["duration"])


def av_integrity_issues(metadata: dict, *, tolerance: float = 0.25) -> list[str]:
    """Container length alone can hide a frozen/missing video tail under a longer audio track."""
    ends = {}
    issues = []
    for kind in ("video", "audio"):
        stream = next((row for row in metadata.get("streams", []) if row.get("codec_type") == kind), None)
        if stream is None:
            issues.append(f"missing {kind} stream")
            continue
        try:
            end = float(stream.get("start_time", 0)) + float(stream["duration"])
            if not math.isfinite(end) or float(stream["duration"]) <= 0:
                raise ValueError("invalid duration")
            ends[kind] = end
        except (KeyError, TypeError, ValueError):
            issues.append(f"unverifiable {kind} stream duration")
    if len(ends) == 2 and abs(ends["video"] - ends["audio"]) > tolerance:
        issues.append(f"audio/video ending mismatch {ends['video'] - ends['audio']:+.3f}s")
    return issues


def ass_seconds(value: str) -> float:
    match = ASS_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported ASS timestamp: {value!r}")
    hour, minute, second, centisecond = (int(item) for item in match.groups())
    return hour * 3600 + minute * 60 + second + centisecond / 100


def ass_span(path: Path) -> tuple[float | None, float | None, int]:
    first: float | None = None
    last: float | None = None
    count = 0
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.startswith("Dialogue:"):
            continue
        fields = raw_line.split(",", 9)
        if len(fields) < 10:
            continue
        start = ass_seconds(fields[1])
        end = ass_seconds(fields[2])
        first = start if first is None else min(first, start)
        last = end if last is None else max(last, end)
        count += 1
    return first, last, count


def index_by_name(root: Path | None) -> dict[str, Path]:
    if root is None:
        return {}
    return {path.name: path for path in root.rglob("*.mp4")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("delivery", type=Path)
    parser.add_argument("--song-masters", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--duration-tolerance", type=float, default=0.08)
    args = parser.parse_args()

    delivery = args.delivery.resolve()
    videos = sorted(delivery.rglob("*.mp4"))
    masters = index_by_name(args.song_masters.resolve() if args.song_masters else None)
    rows: list[dict[str, object]] = []
    failures: list[str] = []

    for video in videos:
        ass = video.with_suffix(".ass")
        media = ffprobe_media(video)
        duration = float(media["format"]["duration"])
        failures.extend(f"{issue}: {video.name}" for issue in av_integrity_issues(media))
        if not ass.exists():
            failures.append(f"missing ASS: {video}")
            first_ass = last_ass = None
            dialogue_count = 0
        else:
            from asr_hotword_guard import audit_artifact
            if audit_artifact(ass)["findings"]:
                failures.append(f"hotword-list echo: {video.name}")
            first_ass, last_ass, dialogue_count = ass_span(ass)
            if dialogue_count == 0:
                failures.append(f"empty ASS: {ass}")
            if last_ass is not None and last_ass > duration + 0.05:
                failures.append(
                    f"ASS exceeds video by {last_ass - duration:.3f}s: {video.name}"
                )

        master_duration: float | None = None
        master_delta: float | None = None
        if "song" in video.relative_to(delivery).parts:
            master = masters.get(video.name)
            if master is None:
                failures.append(f"missing song master: {video.name}")
            else:
                master_duration = ffprobe_duration(master)
                master_delta = duration - master_duration
                if abs(master_delta) > args.duration_tolerance:
                    failures.append(
                        f"song duration mismatch {master_delta:+.3f}s: {video.name}"
                    )

        rows.append(
            {
                "relative_video": str(video.relative_to(delivery)),
                "video_seconds": f"{duration:.3f}",
                "ass_first_seconds": "" if first_ass is None else f"{first_ass:.3f}",
                "ass_last_seconds": "" if last_ass is None else f"{last_ass:.3f}",
                "ass_dialogue_count": dialogue_count,
                "song_master_seconds": "" if master_duration is None else f"{master_duration:.3f}",
                "song_master_delta_seconds": "" if master_delta is None else f"{master_delta:+.3f}",
                "status": "PASS" if not any(video.name in item for item in failures) else "FAIL",
            }
        )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)

    print(f"videos={len(videos)} paired_ass={sum(video.with_suffix('.ass').exists() for video in videos)}")
    print(f"failures={len(failures)}")
    for failure in failures:
        print(f"FAIL\t{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
