#!/usr/bin/env python3
"""Run FunASR with a configurable CAM++ clustering threshold."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def clean(text: str) -> str:
    return re.sub(r"<\|[^|]+\|>", "", text or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--vad-model", type=Path, required=True)
    parser.add_argument("--punc-model", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--mode", choices=("punc_segment", "vad_segment"), default="vad_segment")
    parser.add_argument("--merge-threshold", type=float, default=0.95)
    parser.add_argument("--hotword", default="")
    args = parser.parse_args()

    from funasr import AutoModel

    model = AutoModel(
        model=str(args.model.resolve()),
        vad_model=str(args.vad_model.resolve()),
        punc_model=str(args.punc_model.resolve()),
        spk_model="cam++",
        spk_mode=args.mode,
        spk_kwargs={"cb_kwargs": {"merge_thr": args.merge_threshold}},
        device=args.device,
        disable_update=True,
    )
    result = model.generate(
        input=str(args.input.resolve()),
        batch_size_s=300,
        hotword=args.hotword,
        sentence_timestamp=True,
        return_raw_text=True,
    )
    raw = result[0] if isinstance(result, list) else result
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = []
    for item in raw.get("sentence_info") or []:
        text = clean(str(item.get("text", "")))
        if text:
            rows.append(
                {
                    "start_ms": int(item.get("start", 0)),
                    "end_ms": int(item.get("end", item.get("start", 0))),
                    "speaker": item.get("spk", ""),
                    "text": text,
                }
            )
    with (args.output / "diarized.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["start_ms", "end_ms", "speaker", "text"])
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row["speaker"])
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"rows": len(rows), "speaker_counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
