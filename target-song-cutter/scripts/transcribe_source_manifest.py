#!/usr/bin/env python3
"""Resume-friendly Faster-Whisper transcription for a source manifest.

The model is loaded once, source media is decoded directly, and each completed
recording is written immediately so a later run can skip finished entries.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from general_hotwords import load_general_hotwords, merge_terms
from virtuareal_glossary import hotwords, load_glossary, load_replacements, normalize_text


PLAN_COLUMNS = [
    "slice_id", "order", "start_seconds", "end_seconds", "title",
    "outline", "hook", "reason", "keep",
]


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


def srt_clock(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def configure_cuda_dlls(device: str):
    if device != "cuda" or os.name != "nt":
        return None
    import torch

    torch_lib = Path(torch.__file__).resolve().parent / "lib"
    if not torch_lib.is_dir():
        return None
    os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
    return os.add_dll_directory(str(torch_lib))


def load_profiles(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload.get("profiles", {})


def write_transcript(
    output: Path,
    source: Path,
    source_id: str,
    creator: str,
    language: str,
    language_probability: float,
    segments: list[TranscriptSegment],
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "transcript.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start_seconds", "end_seconds", "text"])
        for item in segments:
            writer.writerow([f"{item.start:.3f}", f"{item.end:.3f}", item.text])
    (output / "transcript.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "source_id": source_id,
                "creator": creator,
                "language": language,
                "language_probability": language_probability,
                "segments": [asdict(item) for item in segments],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (output / "transcript.srt").open("w", encoding="utf-8-sig", newline="\n") as handle:
        for index, item in enumerate(segments, 1):
            handle.write(
                f"{index}\n{srt_clock(item.start)} --> {srt_clock(item.end)}\n"
                f"{item.text}\n\n"
            )
    with (output / "edit-plan.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerow(PLAN_COLUMNS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--creators", help="Optional comma-separated creator keys")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    selected = {
        item.strip() for item in (args.creators or "").split(",") if item.strip()
    }
    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if selected:
        rows = [row for row in rows if row.get("creator") in selected]
    if not rows:
        raise ValueError("No manifest rows selected")

    profiles = load_profiles(args.profiles)
    glossary = load_glossary()
    replacements = load_replacements()
    shared_terms = merge_terms(load_general_hotwords(), hotwords(glossary))

    pending: list[dict] = []
    for row in rows:
        source_id = row["source_id"]
        creator = row["creator"]
        source = Path(row["source_video"]).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        output = args.output_root.resolve() / creator / source_id / "transcript"
        completed = output / "transcript.json"
        if completed.is_file() and not args.overwrite:
            print(f"SKIP {source_id}: transcript.json already exists", flush=True)
            continue
        pending.append({"row": row, "source": source, "output": output})

    if not pending:
        print("All selected sources are already transcribed.")
        return 0

    dll_handle = configure_cuda_dlls(args.device)
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(args.model_cache.resolve()) if args.model_cache else None,
    )
    try:
        for index, item in enumerate(pending, 1):
            row = item["row"]
            creator = row["creator"]
            source_id = row["source_id"]
            profile_terms = profiles.get(creator, {}).get("hotwords", [])
            prompt = ", ".join(merge_terms(shared_terms, profile_terms))
            print(
                f"START {index}/{len(pending)} {source_id} "
                f"duration={row.get('duration_seconds', '?')}s",
                flush=True,
            )
            raw_segments, info = model.transcribe(
                str(item["source"]),
                language=args.language,
                beam_size=args.beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=None,
            )
            segments = [
                TranscriptSegment(
                    round(float(segment.start), 3),
                    round(float(segment.end), 3),
                    normalize_text(segment.text.strip(), replacements),
                )
                for segment in raw_segments
                if segment.text.strip()
            ]
            write_transcript(
                item["output"],
                item["source"],
                source_id,
                creator,
                info.language,
                float(info.language_probability),
                segments,
            )
            print(f"DONE {source_id}: {len(segments)} segments", flush=True)
    finally:
        if dll_handle is not None:
            dll_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
