#!/usr/bin/env python3
"""Cluster subtitle-cue voices with a local SpeechBrain ECAPA model."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio
from sklearn.cluster import KMeans
from speechbrain.inference.speaker import EncoderClassifier


SRT_BLOCK = re.compile(
    r"(\d+)\s*\n"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)",
    re.S,
)


def seconds(groups: tuple[str, ...]) -> float:
    hours, minutes, secs, millis = map(int, groups)
    return hours * 3600 + minutes * 60 + secs + millis / 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--srt-dir", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--savedir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clusters", type=int, default=5)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()

    classifier = EncoderClassifier.from_hparams(
        source=str(args.model),
        savedir=str(args.savedir),
        run_opts={"device": args.device},
    )

    records: list[dict[str, object]] = []
    embeddings: list[np.ndarray] = []
    for srt_path in sorted(args.srt_dir.glob("*.srt")):
        wav_path = args.audio_dir / f"{srt_path.stem}.wav"
        samples, sample_rate = sf.read(wav_path, dtype="float32", always_2d=True)
        waveform = torch.from_numpy(samples).mean(dim=1)
        if sample_rate != 16000:
            waveform = torchaudio.functional.resample(waveform, sample_rate, 16000)
            sample_rate = 16000
        for match in SRT_BLOCK.finditer(srt_path.read_text(encoding="utf-8-sig")):
            start = seconds(match.groups()[1:5])
            end = seconds(match.groups()[5:9])
            # Avoid neighbouring turns while retaining enough speech for ECAPA.
            pad = min(0.08, max(0.0, (end - start - 0.4) / 4))
            lo = max(0, int((start + pad) * sample_rate))
            hi = min(waveform.numel(), int((end - pad) * sample_rate))
            segment = waveform[lo:hi]
            if segment.numel() < int(0.75 * sample_rate):
                needed = int(0.75 * sample_rate) - segment.numel()
                segment = torch.nn.functional.pad(segment, (needed // 2, needed - needed // 2))
            with torch.inference_mode():
                embedding = classifier.encode_batch(segment.unsqueeze(0)).squeeze().cpu().numpy()
            embedding /= np.linalg.norm(embedding) + 1e-12
            records.append(
                {
                    "file": srt_path.name,
                    "cue": int(match.group(1)),
                    "start": f"{start:.3f}",
                    "end": f"{end:.3f}",
                    "text": " ".join(match.group(10).split()),
                }
            )
            embeddings.append(embedding)

    matrix = np.vstack(embeddings)
    labels = KMeans(n_clusters=args.clusters, random_state=20260802, n_init=50).fit_predict(matrix)

    # Clips 005 and 006 contain only Kioi; their centroid is a stable reference.
    kioi_mask = np.array(
        [str(record["file"]).startswith(("005_", "006_")) for record in records]
    )
    kioi_centroid = matrix[kioi_mask].mean(axis=0)
    kioi_centroid /= np.linalg.norm(kioi_centroid) + 1e-12
    similarities = matrix @ kioi_centroid

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["file", "cue", "start", "end", "cluster", "kioi_similarity", "text"],
        )
        writer.writeheader()
        for record, label, similarity in zip(records, labels, similarities, strict=True):
            writer.writerow({**record, "cluster": int(label), "kioi_similarity": f"{similarity:.4f}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
