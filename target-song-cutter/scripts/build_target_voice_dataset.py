#!/usr/bin/env python3
"""Build fixed-length target-speaker training files from several recordings.

The input pools are expected to have already passed broad content selection.
Each six-second window is separated into two speech sources with SepFormer.
ECAPA speaker embeddings then choose the source closest to the references.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio
from speechbrain.inference.separation import SepformerSeparation
from speechbrain.inference.speaker import EncoderClassifier


RATE = 16_000
CHUNK_SECONDS = 6
CHUNK_FRAMES = RATE * CHUNK_SECONDS


def load_mono(path: Path, target_rate: int = RATE) -> np.ndarray:
    samples, rate = sf.read(path, dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    tensor = torch.from_numpy(samples)
    if rate != target_rate:
        tensor = torchaudio.functional.resample(tensor, rate, target_rate)
    return tensor.numpy()


def normalized_embeddings(classifier, waves: torch.Tensor) -> torch.Tensor:
    with torch.inference_mode():
        embeddings = classifier.encode_batch(waves).squeeze(1).float()
    return embeddings / embeddings.norm(p=2, dim=1, keepdim=True).clamp_min(1e-8)


def build_target_embedding(classifier, references: list[Path], device: str) -> torch.Tensor:
    chunks: list[torch.Tensor] = []
    for path in references:
        samples = load_mono(path)
        for start in range(0, len(samples) - CHUNK_FRAMES + 1, CHUNK_FRAMES):
            chunks.append(torch.from_numpy(samples[start : start + CHUNK_FRAMES]))
    if not chunks:
        raise ValueError("No six-second reference chunks were available")
    embeddings = []
    for start in range(0, len(chunks), 32):
        batch = torch.stack(chunks[start : start + 32]).to(device)
        embeddings.append(normalized_embeddings(classifier, batch).cpu())
    target = torch.cat(embeddings).mean(dim=0)
    return (target / target.norm(p=2).clamp_min(1e-8)).to(device)


def scan_collaboration(
    classifier,
    target: torch.Tensor,
    audio: Path,
    cache: Path,
    device: str,
) -> list[dict[str, float]]:
    if cache.is_file():
        with cache.open(encoding="utf-8-sig", newline="") as handle:
            return [
                {
                    "start_seconds": float(row["start_seconds"]),
                    "similarity": float(row["similarity"]),
                    "rms_db": float(row["rms_db"]),
                }
                for row in csv.DictReader(handle)
            ]

    rows: list[dict[str, float]] = []
    pending: list[torch.Tensor] = []
    starts: list[float] = []
    rms_values: list[float] = []

    def flush() -> None:
        if not pending:
            return
        batch = torch.stack(pending).to(device)
        embeddings = normalized_embeddings(classifier, batch)
        similarities = (embeddings @ target).detach().cpu().numpy()
        for start, similarity, rms_db in zip(starts, similarities, rms_values):
            rows.append(
                {
                    "start_seconds": start,
                    "similarity": float(similarity),
                    "rms_db": rms_db,
                }
            )
        pending.clear()
        starts.clear()
        rms_values.clear()

    with sf.SoundFile(audio) as source:
        if source.samplerate != RATE or source.channels != 1:
            raise ValueError(f"Collaboration scan input must be 16 kHz mono: {audio}")
        index = 0
        while True:
            data = source.read(CHUNK_FRAMES, dtype="float32", always_2d=False)
            if len(data) < CHUNK_FRAMES:
                break
            rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64)) + 1e-12))
            pending.append(torch.from_numpy(data))
            starts.append(index * CHUNK_SECONDS)
            rms_values.append(20.0 * math.log10(max(rms, 1e-8)))
            index += 1
            if len(pending) == 32:
                flush()
                if index % 320 == 0:
                    print(f"Collaboration scan: {index * CHUNK_SECONDS / 60:.1f} min", flush=True)
        flush()

    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["start_seconds", "similarity", "rms_db"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def chunks_from_files(files: list[Path], required_seconds: int) -> list[dict]:
    required_chunks = required_seconds // CHUNK_SECONDS
    chunks: list[dict] = []
    for path in files:
        samples = load_mono(path)
        for start in range(0, len(samples) - CHUNK_FRAMES + 1, CHUNK_FRAMES):
            chunks.append(
                {
                    "source": str(path),
                    "start_seconds": start / RATE,
                    "samples": samples[start : start + CHUNK_FRAMES],
                    "mix_similarity": "",
                }
            )
            if len(chunks) == required_chunks:
                return chunks
    raise ValueError(f"Only {len(chunks) * CHUNK_SECONDS}s available; need {required_seconds}s")


def chunks_from_collaboration(
    audio: Path,
    scores: list[dict[str, float]],
    required_seconds: int,
) -> list[dict]:
    required_chunks = required_seconds // CHUNK_SECONDS
    ranked = sorted(
        (row for row in scores if row["rms_db"] >= -45.0),
        key=lambda row: row["similarity"],
        reverse=True,
    )
    if len(ranked) < required_chunks:
        raise ValueError("Not enough active collaboration windows")
    selected = ranked[:required_chunks]
    chunks = []
    with sf.SoundFile(audio) as source:
        for row in selected:
            source.seek(round(row["start_seconds"] * RATE))
            samples = source.read(CHUNK_FRAMES, dtype="float32", always_2d=False)
            if len(samples) != CHUNK_FRAMES:
                continue
            chunks.append(
                {
                    "source": str(audio),
                    "start_seconds": row["start_seconds"],
                    "samples": samples,
                    "mix_similarity": row["similarity"],
                }
            )
    if len(chunks) != required_chunks:
        raise ValueError("Could not read every selected collaboration window")
    return chunks


def normalize_window(samples: torch.Tensor, target_db: float = -24.0) -> torch.Tensor:
    rms = samples.square().mean().sqrt().clamp_min(1e-6)
    target = 10.0 ** (target_db / 20.0)
    samples = samples * min(float(target / rms), 8.0)
    peak = samples.abs().max().clamp_min(1e-6)
    if peak > 0.95:
        samples = samples * (0.95 / peak)
    fade = min(160, samples.numel() // 4)
    if fade:
        ramp = torch.linspace(0.0, 1.0, fade, device=samples.device)
        samples[:fade] *= ramp
        samples[-fade:] *= ramp.flip(0)
    return samples


def process_pool(
    name: str,
    chunks: list[dict],
    separator,
    classifier,
    target: torch.Tensor,
    output: Path,
    audit: Path,
    device: str,
) -> None:
    expected_frames = len(chunks) * CHUNK_FRAMES
    if output.is_file():
        with sf.SoundFile(output) as existing:
            if (
                existing.samplerate == RATE
                and existing.channels == 1
                and len(existing) == expected_frames
            ):
                print(f"{name}: using cached cleaned pool", flush=True)
                return

    output.parent.mkdir(parents=True, exist_ok=True)
    audit_rows = []
    with sf.SoundFile(
        output,
        mode="w",
        samplerate=RATE,
        channels=1,
        subtype="PCM_16",
        format="WAV",
    ) as destination:
        for batch_start in range(0, len(chunks), 4):
            batch_rows = chunks[batch_start : batch_start + 4]
            batch = torch.stack(
                [torch.from_numpy(row["samples"]) for row in batch_rows]
            ).to(device)
            with torch.inference_mode():
                separated = separator.separate_batch(batch)
            source_a = separated[:, :, 0]
            source_b = separated[:, :, 1]
            embeddings = normalized_embeddings(
                classifier, torch.cat([source_a, source_b], dim=0)
            )
            sim_a = embeddings[: len(batch_rows)] @ target
            sim_b = embeddings[len(batch_rows) :] @ target
            for index, row in enumerate(batch_rows):
                choose_b = bool(sim_b[index] > sim_a[index])
                chosen = source_b[index] if choose_b else source_a[index]
                chosen = normalize_window(chosen).detach().cpu()
                destination.write(chosen.numpy())
                audit_rows.append(
                    {
                        "pool": name,
                        "source": row["source"],
                        "start_seconds": f"{row['start_seconds']:.3f}",
                        "mix_similarity": (
                            f"{row['mix_similarity']:.4f}"
                            if isinstance(row["mix_similarity"], float)
                            else ""
                        ),
                        "separated_a_similarity": f"{float(sim_a[index]):.4f}",
                        "separated_b_similarity": f"{float(sim_b[index]):.4f}",
                        "chosen_source": "B" if choose_b else "A",
                    }
                )
            completed = min(batch_start + len(batch_rows), len(chunks))
            if completed % 40 == 0 or completed == len(chunks):
                print(
                    f"{name}: {completed}/{len(chunks)} windows "
                    f"({completed * CHUNK_SECONDS / 60:.1f} min)",
                    flush=True,
                )

    with audit.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pool",
                "source",
                "start_seconds",
                "mix_similarity",
                "separated_a_similarity",
                "separated_b_similarity",
                "chosen_source",
            ],
        )
        writer.writeheader()
        writer.writerows(audit_rows)


def read_exact(source: sf.SoundFile, seconds: int) -> np.ndarray:
    frames = seconds * RATE
    samples = source.read(frames, dtype="float32", always_2d=False)
    if len(samples) != frames:
        raise ValueError(f"Pool ended early: wanted {frames}, got {len(samples)}")
    return samples


def filter_cleaned_pool(
    source: Path,
    audit: Path,
    destination: Path,
    required_seconds: int,
    threshold: float = 0.55,
) -> None:
    required_chunks = required_seconds // CHUNK_SECONDS
    with audit.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected = 0
    with (
        sf.SoundFile(source) as cleaned,
        sf.SoundFile(
            destination,
            mode="w",
            samplerate=RATE,
            channels=1,
            subtype="PCM_16",
            format="WAV",
        ) as output,
    ):
        for row in rows:
            samples = cleaned.read(CHUNK_FRAMES, dtype="float32", always_2d=False)
            if len(samples) != CHUNK_FRAMES:
                raise ValueError(f"Cleaned pool ended early: {source}")
            chosen_similarity = float(
                row[
                    "separated_b_similarity"
                    if row["chosen_source"] == "B"
                    else "separated_a_similarity"
                ]
            )
            if chosen_similarity < threshold:
                continue
            output.write(samples)
            selected += 1
            if selected == required_chunks:
                break
    if selected != required_chunks:
        raise ValueError(
            f"{source.name}: only {selected * CHUNK_SECONDS}s passed "
            f"similarity {threshold}; need {required_seconds}s"
        )


def assemble(output: Path, old_pool: Path, recent_pool: Path, collab_pool: Path) -> None:
    clips = output / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    with (
        sf.SoundFile(old_pool) as old,
        sf.SoundFile(recent_pool) as recent,
        sf.SoundFile(collab_pool) as collab,
    ):
        for index in range(1, 25):
            parts = [
                read_exact(old, 180),
                read_exact(recent, 15),
                read_exact(collab, 105),
            ]
            combined = np.concatenate(parts)
            destination = clips / f"kioi_multisource_{index:03d}.wav"
            sf.write(destination, combined, RATE, subtype="PCM_16")
            manifest_rows.append(
                {
                    "file": destination.name,
                    "duration_seconds": 300,
                    "source_2026_07_22_seconds": 180,
                    "source_2026_07_23_seconds": 15,
                    "source_2026_07_24_seconds": 105,
                }
            )
    with (output / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest_rows[0]))
        writer.writeheader()
        writer.writerows(manifest_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--old-dir", type=Path, required=True)
    parser.add_argument("--recent-dir", type=Path, required=True)
    parser.add_argument("--collaboration-audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()

    log_handle = None
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_handle = args.log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = log_handle
        sys.stderr = log_handle

    output = args.output.resolve()
    work = output / "work"
    work.mkdir(parents=True, exist_ok=True)
    references = sorted(args.reference_dir.resolve().glob("*.wav"))
    old_files = sorted(args.old_dir.resolve().glob("*.wav"))
    recent_files = sorted(args.recent_dir.resolve().glob("*.wav"))
    if not references or not old_files or not recent_files:
        raise ValueError("Reference, old, and recent WAV inputs are required")

    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str((args.model_root / "spkrec-ecapa").resolve()),
        run_opts={"device": args.device},
    )
    target = build_target_embedding(classifier, references, args.device)
    separator = SepformerSeparation.from_hparams(
        source="speechbrain/sepformer-whamr16k",
        savedir=str((args.model_root / "sepformer-whamr16k").resolve()),
        run_opts={"device": args.device},
    )

    scores = scan_collaboration(
        classifier,
        target,
        args.collaboration_audio.resolve(),
        work / "collaboration_scores.csv",
        args.device,
    )
    old_chunks = chunks_from_files(old_files, 5_880)
    recent_chunks = chunks_from_files(recent_files, 600)
    collaboration_chunks = chunks_from_collaboration(
        args.collaboration_audio.resolve(), scores, 2_526
    )

    old_pool = work / "cleaned_2026_07_22.wav"
    recent_pool = work / "cleaned_2026_07_23.wav"
    collab_pool = work / "cleaned_2026_07_24.wav"
    process_pool(
        "2026-07-22",
        old_chunks,
        separator,
        classifier,
        target,
        old_pool,
        work / "audit_2026_07_22.csv",
        args.device,
    )
    process_pool(
        "2026-07-23",
        recent_chunks,
        separator,
        classifier,
        target,
        recent_pool,
        work / "audit_2026_07_23.csv",
        args.device,
    )
    process_pool(
        "2026-07-24",
        collaboration_chunks,
        separator,
        classifier,
        target,
        collab_pool,
        work / "audit_2026_07_24.csv",
        args.device,
    )
    filtered_old = work / "filtered_2026_07_22.wav"
    filtered_recent = work / "filtered_2026_07_23.wav"
    filtered_collab = work / "filtered_2026_07_24.wav"
    filter_cleaned_pool(
        old_pool, work / "audit_2026_07_22.csv", filtered_old, 4_320
    )
    filter_cleaned_pool(
        recent_pool, work / "audit_2026_07_23.csv", filtered_recent, 360
    )
    filter_cleaned_pool(
        collab_pool, work / "audit_2026_07_24.csv", filtered_collab, 2_520
    )
    assemble(output, filtered_old, filtered_recent, filtered_collab)
    print(f"Done: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
