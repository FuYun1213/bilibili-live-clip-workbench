#!/usr/bin/env python3
"""Build multi-scale, auditable danmaku and Super Chat discovery evidence."""

from __future__ import annotations

import argparse
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from analyze_bililive_xml import (
    apply_alignment,
    clock,
    parse_windows,
    parse_optional_xml,
    write_csv,
    write_json,
)


WINDOW_FIELDS = [
    "window_seconds", "start_seconds", "end_seconds", "clock", "count",
    "unique_users", "unique_user_ratio", "user_coverage_ratio",
    "message_diversity_ratio", "same_user_repeat_ratio", "spam_risk",
    "z_score", "global_median",
    "local_mean", "burst_factor", "delta_from_previous", "delta_ratio",
    "repeat_ratio", "signal_labels", "engagement_score", "top_messages", "nearby_sc",
]


def analyze_scale(
    danmaku: list[dict],
    superchats: list[dict],
    window: float,
    context: float,
    local_context: float,
) -> list[dict]:
    # No chat means no chat-derived peaks. SC discovery is written separately.
    if not danmaku:
        return []
    grouped: dict[int, list[dict]] = defaultdict(list)
    for item in danmaku:
        grouped[math.floor(item["time"] / window)].append(item)
    last_time = max(item["time"] for item in danmaku)
    last_index = max(0, math.floor(last_time / window))
    counts = [len(grouped[index]) for index in range(last_index + 1)]
    global_mean = statistics.fmean(counts)
    global_median = statistics.median(counts)
    deviation = statistics.pstdev(counts) or 1.0
    radius = max(1, math.ceil(local_context / window))
    high_threshold = max(3.0, global_median * 2.0, global_mean * 1.5)

    rows: list[dict] = []
    for index, count in enumerate(counts):
        items = grouped[index]
        start = index * window
        baseline = [
            counts[position]
            for position in range(max(0, index - radius), min(last_index + 1, index + radius + 1))
            if position != index
        ]
        local_mean = statistics.fmean(baseline) if baseline else 0.0
        previous = counts[index - 1] if index else 0
        following = counts[index + 1] if index < last_index else 0
        z_score = (count - global_mean) / deviation
        burst_factor = count / max(1.0, local_mean)
        delta_ratio = count / max(1, previous)
        unique_users = len({item["user"] for item in items if item["user"]})
        unique_ratio = unique_users / count if count else 0.0
        messages = Counter(item["text"] for item in items if item["text"])
        repeat_ratio = messages.most_common(1)[0][1] / count if count and messages else 0.0
        known_user_items = [item for item in items if item["user"]]
        user_coverage = len(known_user_items) / count if count else 0.0
        message_diversity = len(messages) / count if count else 0.0
        unique_user_message_pairs = {
            (item["user"], item["text"])
            for item in known_user_items
            if item["text"]
        }
        same_user_repeat = (
            1.0 - len(unique_user_message_pairs) / len(known_user_items)
            if known_user_items else 0.0
        )
        # Repeated wording from many different people is crowd consensus, not
        # one-person spam. Without sender IDs, penalize only extreme repetition.
        spam_risk = (
            same_user_repeat
            if user_coverage >= 0.5
            else max(0.0, repeat_ratio - 0.8) / 0.2
        )

        labels: list[str] = []
        if count >= 3 and (z_score >= 3.0 or burst_factor >= 3.0):
            labels.append("burst")
        if count >= 3 and delta_ratio >= 2.0 and count - previous >= 2:
            labels.append("rise")
        if count >= high_threshold and (previous >= high_threshold or following >= high_threshold):
            labels.append("sustained")
        if repeat_ratio >= 0.6 and count >= 3:
            labels.append("repeat-heavy")
        if repeat_ratio >= 0.6 and unique_ratio >= 0.6:
            labels.append("crowd-repeat")
        if spam_risk >= 0.35 and count >= 3:
            labels.append("spam-risk")
        if count and user_coverage < 0.5:
            labels.append("low-user-coverage")
        score = (
            max(0.0, z_score)
            + math.log2(max(1.0, burst_factor))
            + min(2.0, max(0.0, delta_ratio - 1.0))
            + (unique_ratio if user_coverage >= 0.5 else min(0.5, message_diversity))
            - 2.0 * spam_risk
        )
        nearby_sc = [
            sc for sc in superchats
            if start - context <= sc["time"] < start + window + context
        ]
        rows.append(
            {
                "window_seconds": f"{window:g}",
                "start_seconds": f"{start:.3f}",
                "end_seconds": f"{start + window:.3f}",
                "clock": clock(start),
                "count": count,
                "unique_users": unique_users,
                "unique_user_ratio": f"{unique_ratio:.3f}",
                "user_coverage_ratio": f"{user_coverage:.3f}",
                "message_diversity_ratio": f"{message_diversity:.3f}",
                "same_user_repeat_ratio": f"{same_user_repeat:.3f}",
                "spam_risk": f"{spam_risk:.3f}",
                "z_score": f"{z_score:.3f}",
                "global_median": f"{global_median:.3f}",
                "local_mean": f"{local_mean:.3f}",
                "burst_factor": f"{burst_factor:.3f}",
                "delta_from_previous": count - previous,
                "delta_ratio": f"{delta_ratio:.3f}",
                "repeat_ratio": f"{repeat_ratio:.3f}",
                "signal_labels": "|".join(labels) or "normal",
                "engagement_score": f"{score:.3f}",
                "top_messages": " | ".join(
                    f"{text}\u00d7{amount}" for text, amount in messages.most_common(5)
                ),
                "nearby_sc": " | ".join(
                    f"{sc['clock']} \uffe5{sc['price']} {sc['text']}" for sc in nearby_sc[:5]
                ),
            }
        )
    return rows


def select_bursts(rows: list[dict], limit: int, context: float) -> list[dict]:
    bursts = [row.copy() for row in rows if row["count"] and row["signal_labels"] != "normal"]
    bursts.sort(
        key=lambda row: (float(row["engagement_score"]), int(row["count"])), reverse=True
    )
    bursts = bursts[:limit]
    for rank, row in enumerate(bursts, 1):
        row["rank"] = rank
        row["inspect_start"] = f"{max(0, float(row['start_seconds']) - context):.3f}"
        row["inspect_end"] = f"{float(row['end_seconds']) + context:.3f}"
    return bursts


def merge_hotspots(
    burst_sets: dict[float, list[dict]],
    danmaku: list[dict],
    superchats: list[dict],
    context: float,
    merge_gap: float,
    reaction_lag_max: float,
) -> list[dict]:
    signals = sorted(
        (
            {
                "start": float(row["start_seconds"]),
                "end": float(row["end_seconds"]),
                "score": float(row["engagement_score"]),
                "scale": scale,
                "labels": set(row["signal_labels"].split("|")),
            }
            for scale, rows in burst_sets.items()
            for row in rows
        ),
        key=lambda item: (item["start"], item["end"]),
    )
    merged: list[dict] = []
    for signal in signals:
        if not merged or signal["start"] > merged[-1]["end"] + merge_gap:
            merged.append(
                {
                    **signal,
                    "scales": {signal["scale"]},
                    "source_window_count": 1,
                }
            )
            continue
        current = merged[-1]
        current["end"] = max(current["end"], signal["end"])
        current["score"] = max(current["score"], signal["score"])
        current["scales"].add(signal["scale"])
        current["labels"].update(signal["labels"])
        current["source_window_count"] += 1

    rows: list[dict] = []
    for hotspot in merged:
        events = [item for item in danmaku if hotspot["start"] <= item["time"] < hotspot["end"]]
        messages = Counter(item["text"] for item in events if item["text"])
        nearby_sc = [
            sc for sc in superchats
            if hotspot["start"] - context <= sc["time"] < hotspot["end"] + context
        ]
        rows.append(
            {
                "start_seconds": f"{hotspot['start']:.3f}",
                "end_seconds": f"{hotspot['end']:.3f}",
                "clock": clock(hotspot["start"]),
                "inspect_start": f"{max(0, hotspot['start'] - context):.3f}",
                "inspect_end": f"{hotspot['end'] + context:.3f}",
                "cause_search_start": f"{max(0, hotspot['start'] - reaction_lag_max):.3f}",
                "chat_peak_start": f"{hotspot['start']:.3f}",
                "chat_peak_end": f"{hotspot['end']:.3f}",
                "payoff_search_end": f"{hotspot['end'] + context:.3f}",
                "peak_engagement_score": f"{hotspot['score']:.3f}",
                "window_scales": "|".join(f"{value:g}" for value in sorted(hotspot["scales"])),
                "signal_labels": "|".join(sorted(hotspot["labels"])),
                "source_window_count": hotspot["source_window_count"],
                "danmaku_count": len(events),
                "unique_users": len({item["user"] for item in events if item["user"]}),
                "top_messages": " | ".join(
                    f"{text}\u00d7{amount}" for text, amount in messages.most_common(8)
                ),
                "nearby_sc": " | ".join(
                    f"{sc['clock']} \uffe5{sc['price']} {sc['text']}" for sc in nearby_sc[:8]
                ),
                "editorial_status": "unreviewed",
                "candidate_id": "",
                "notes": "",
            }
        )
    rows.sort(key=lambda row: float(row["peak_engagement_score"]), reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def sc_followup_peak(
    sc_time: float,
    window_sets: dict[float, list[dict]],
    response_window: float,
) -> dict | None:
    """Return the strongest later non-normal chat window after an SC."""
    candidates = [
        row
        for rows in window_sets.values()
        for row in rows
        if sc_time <= float(row["start_seconds"]) <= sc_time + response_window
        and row["signal_labels"] != "normal"
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (float(row["engagement_score"]), int(row["count"])),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xml", "--input", dest="xml", type=Path, help="Optional danmaku/SC XML")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows", default="10,30,60")
    parser.add_argument("--burst-top", type=int, default=60)
    parser.add_argument("--context", type=float, default=60.0)
    parser.add_argument("--local-context", type=float, default=300.0)
    parser.add_argument("--merge-gap", type=float, default=10.0)
    parser.add_argument("--reaction-lag-max", type=float, default=30.0)
    parser.add_argument("--sc-response-window", type=float, default=180.0)
    parser.add_argument("--min-repeat", type=int, default=3)
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--offset-seconds", type=float, default=0.0)
    parser.add_argument("--alignment-anchor", default="")
    args = parser.parse_args(argv)
    if (
        args.context <= 0 or args.local_context <= 0 or args.merge_gap < 0
        or args.reaction_lag_max < 0 or args.sc_response_window <= 0
        or args.min_repeat < 2
    ):
        raise ValueError("invalid context, merge gap, or repetition threshold")
    windows = parse_windows(args.windows)
    raw_danmaku, raw_superchats, input_status = parse_optional_xml(args.xml)
    if not raw_danmaku:
        print(f"未发现有效弹幕记录（{input_status}）；跳过弹幕热点分析，继续使用录播音频和转写，保留可用 SC。")
    danmaku = apply_alignment(raw_danmaku, args.offset_seconds)
    superchats = apply_alignment(raw_superchats, args.offset_seconds)
    discovery_mode = (
        "transcript_audio_engagement" if danmaku
        else "transcript_audio_sc" if superchats
        else "transcript_audio_only"
    )

    window_sets: dict[float, list[dict]] = {}
    burst_sets: dict[float, list[dict]] = {}
    for window in windows:
        rows = analyze_scale(danmaku, superchats, window, args.context, args.local_context)
        bursts = select_bursts(rows, args.burst_top, args.context)
        window_sets[window] = rows
        burst_sets[window] = bursts
        write_csv(args.output / f"danmaku-windows-{window:g}s.csv", WINDOW_FIELDS, rows)
        write_csv(
            args.output / f"danmaku-bursts-{window:g}s.csv",
            ["rank", *WINDOW_FIELDS, "inspect_start", "inspect_end"],
            bursts,
        )
    write_csv(args.output / "danmaku-windows.csv", WINDOW_FIELDS, window_sets[windows[0]])
    write_csv(
        args.output / "danmaku-bursts.csv",
        ["rank", *WINDOW_FIELDS, "inspect_start", "inspect_end"],
        burst_sets[windows[0]],
    )

    hotspots = merge_hotspots(
        burst_sets, danmaku, superchats, args.context, args.merge_gap,
        args.reaction_lag_max,
    )
    write_csv(
        args.output / "engagement-hotspots.csv",
        [
            "rank", "start_seconds", "end_seconds", "clock", "inspect_start", "inspect_end",
            "cause_search_start", "chat_peak_start", "chat_peak_end", "payoff_search_end",
            "peak_engagement_score", "window_scales", "signal_labels", "source_window_count",
            "danmaku_count", "unique_users", "top_messages", "nearby_sc",
            "editorial_status", "candidate_id", "notes",
        ],
        hotspots,
    )

    repeated = Counter(item["text"] for item in danmaku if item["text"])
    by_message: dict[str, list[dict]] = defaultdict(list)
    for item in danmaku:
        by_message[item["text"]].append(item)
    repeated_rows = []
    for text, count in repeated.most_common():
        if count < args.min_repeat:
            break
        items = by_message[text]
        repeated_rows.append(
            {
                "text": text, "example_raw_text": items[0]["raw_text"], "count": count,
                "first_source_seconds": f"{items[0]['source_time']:.3f}",
                "first_aligned_seconds": f"{items[0]['time']:.3f}", "first_clock": items[0]["clock"],
                "last_source_seconds": f"{items[-1]['source_time']:.3f}",
                "last_aligned_seconds": f"{items[-1]['time']:.3f}", "last_clock": items[-1]["clock"],
                "unique_users": len({item["user"] for item in items if item["user"]}),
            }
        )
    write_csv(
        args.output / "repeated-messages.csv",
        [
            "text", "example_raw_text", "count", "first_source_seconds", "first_aligned_seconds",
            "first_clock", "last_source_seconds", "last_aligned_seconds", "last_clock", "unique_users",
        ],
        repeated_rows,
    )

    sc_rows = []
    for item in superchats:
        peak = sc_followup_peak(item["time"], window_sets, args.sc_response_window)
        sc_rows.append({
            "source_time_seconds": f"{item['source_time']:.3f}",
            "aligned_time_seconds": f"{item['time']:.3f}", "clock": item["clock"],
            "user": item["user"], "price": item["price"],
            "display_seconds": item["display_seconds"], "raw_text": item["raw_text"],
            "text": item["text"], "editorial_role": "unreviewed", "keep_body": "",
            "response_search_start_seconds": f"{item['time']:.3f}",
            "response_search_end_seconds": f"{item['time'] + args.sc_response_window:.3f}",
            "post_peak_start_seconds": peak["start_seconds"] if peak else "",
            "post_peak_lag_seconds": (
                f"{float(peak['start_seconds']) - item['time']:.3f}" if peak else ""
            ),
            "post_peak_engagement_score": peak["engagement_score"] if peak else "",
            "post_peak_window_seconds": peak["window_seconds"] if peak else "",
            "response_start_seconds": "", "response_end_seconds": "",
            "candidate_id": "", "notes": "",
        })
    write_csv(
        args.output / "superchats.csv",
        [
            "source_time_seconds", "aligned_time_seconds", "clock", "user", "price",
            "display_seconds", "raw_text", "text", "editorial_role", "keep_body",
            "response_search_start_seconds", "response_search_end_seconds",
            "post_peak_start_seconds", "post_peak_lag_seconds",
            "post_peak_engagement_score", "post_peak_window_seconds",
            "response_start_seconds", "response_end_seconds", "candidate_id", "notes",
        ],
        sc_rows,
    )

    keyword_rows = []
    for keyword in args.keyword:
        for item in danmaku:
            if keyword.casefold() in item["text"].casefold():
                keyword_rows.append(
                    {
                        "keyword": keyword, "source_time_seconds": f"{item['source_time']:.3f}",
                        "aligned_time_seconds": f"{item['time']:.3f}", "clock": item["clock"],
                        "user": item["user"], "raw_text": item["raw_text"], "text": item["text"],
                        "inspect_start": f"{max(0, item['time'] - args.context):.3f}",
                        "inspect_end": f"{item['time'] + args.context:.3f}",
                    }
                )
    write_csv(
        args.output / "keyword-context.csv",
        [
            "keyword", "source_time_seconds", "aligned_time_seconds", "clock", "user",
            "raw_text", "text", "inspect_start", "inspect_end",
        ],
        keyword_rows,
    )

    alignment_status = "offset_applied" if args.offset_seconds else "source_time_unverified"
    write_json(
        args.output / "timeline-audit.json",
        {
            "source_xml": str(args.xml.resolve()) if args.xml else "",
            "input_status": input_status, "alignment_status": alignment_status,
            "offset_seconds": args.offset_seconds, "alignment_anchor": args.alignment_anchor,
            "rule": "aligned_time = max(0, source_time + offset_seconds)",
            "warning": "Do not invent an offset or clamp engagement events to ASR speech segments.",
        },
    )
    write_json(
        args.output / "engagement-summary.json",
        {
            "input_status": input_status, "discovery_mode": discovery_mode,
            "danmaku_count": len(danmaku), "superchat_count": len(superchats),
            "window_seconds": windows, "hotspot_count": len(hotspots),
            "repeated_message_count": len(repeated_rows), "keyword_match_count": len(keyword_rows),
            "alignment_status": alignment_status, "dense_windows": True,
            "reaction_lag_max_seconds": args.reaction_lag_max,
            "sc_response_window_seconds": args.sc_response_window,
            "spam_note": "Use same-user repetition when IDs exist; crowd repetition is not automatically spam.",
            "ranking_note": "Engagement discovers candidates; transcript and audio decide usability.",
        },
    )
    print(
        f"danmaku={len(danmaku)} sc={len(superchats)} windows={args.windows} "
        f"hotspots={len(hotspots)} repeats={len(repeated_rows)} keywords={len(keyword_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
