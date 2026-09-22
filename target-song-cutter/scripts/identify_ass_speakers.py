#!/usr/bin/env python3
"""Rank ASS cue speakers against reviewed ECAPA voice anchors."""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from sklearn.cluster import KMeans
from speechbrain.inference.speaker import EncoderClassifier


EVENT_RE = re.compile(r"^Dialogue: [^,]*,([^,]*),([^,]*),([^,]*),([^,]*),[^,]*,[^,]*,[^,]*,[^,]*,(.*)$")


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def load_mono_16k(path: Path) -> np.ndarray:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    if sample_rate != 16000:
        raise ValueError(f"Expected 16 kHz WAV, got {sample_rate}: {path}")
    return mono


def slice_audio(audio: np.ndarray, start: float, end: float) -> torch.Tensor:
    pad = min(0.06, max(0.0, (end - start - 0.4) / 4))
    lo = max(0, int((start + pad) * 16000))
    hi = min(len(audio), int((end - pad) * 16000))
    segment = torch.from_numpy(audio[lo:hi])
    minimum = int(0.75 * 16000)
    if segment.numel() < minimum:
        needed = minimum - segment.numel()
        segment = torch.nn.functional.pad(segment, (needed // 2, needed - needed // 2))
    return segment


def embedding(classifier: EncoderClassifier, segment: torch.Tensor) -> np.ndarray:
    with torch.inference_mode():
        value = classifier.encode_batch(segment.unsqueeze(0)).squeeze().cpu().numpy()
    return value / (np.linalg.norm(value) + 1e-12)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--ass", type=Path, required=True)
    parser.add_argument("--anchors", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--savedir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clusters", type=int, default=7)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()

    classifier = EncoderClassifier.from_hparams(
        source=str(args.model.resolve()),
        savedir=str(args.savedir.resolve()),
        run_opts={"device": args.device},
    )
    cache: dict[Path, np.ndarray] = {}

    def audio_for(path: Path) -> np.ndarray:
        path = path.resolve()
        if path not in cache:
            cache[path] = load_mono_16k(path)
        return cache[path]

    anchors: dict[str, list[np.ndarray]] = defaultdict(list)
    with args.anchors.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            speaker = row["speaker"].strip()
            path = Path(row["audio"])
            if not path.is_absolute():
                path = args.anchors.parent / path
            start, end = float(row["start"]), float(row["end"])
            anchors[speaker].append(
                embedding(classifier, slice_audio(audio_for(path), start, end))
            )
    centroids = {
        speaker: np.mean(values, axis=0) / (np.linalg.norm(np.mean(values, axis=0)) + 1e-12)
        for speaker, values in anchors.items()
    }

    cues: list[dict[str, object]] = []
    for line in args.ass.read_text(encoding="utf-8-sig").splitlines():
        match = EVENT_RE.match(line)
        if not match:
            continue
        start, end = parse_time(match.group(1)), parse_time(match.group(2))
        cues.append(
            {
                "start": start,
                "end": end,
                "style": match.group(3),
                "name": match.group(4),
                "text": match.group(5).strip(),
            }
        )

    target_audio = audio_for(args.audio)
    cue_embeddings = np.vstack(
        [
            embedding(classifier, slice_audio(target_audio, float(cue["start"]), float(cue["end"])))
            for cue in cues
        ]
    )
    labels = KMeans(
        n_clusters=args.clusters,
        random_state=20260812,
        n_init=100,
    ).fit_predict(cue_embeddings)

    speakers = sorted(centroids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "cue", "start", "end", "cluster", "best_speaker", "best_similarity",
            "second_similarity", "margin", "text",
        ] + [f"sim_{speaker}" for speaker in speakers]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, (cue, vector, label) in enumerate(zip(cues, cue_embeddings, labels, strict=True), 1):
            scores = {speaker: float(vector @ centroids[speaker]) for speaker in speakers}
            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
            writer.writerow(
                {
                    "cue": index,
                    "start": f"{float(cue['start']):.3f}",
                    "end": f"{float(cue['end']):.3f}",
                    "cluster": int(label),
                    "best_speaker": ranked[0][0],
                    "best_similarity": f"{ranked[0][1]:.4f}",
                    "second_similarity": f"{ranked[1][1]:.4f}",
                    "margin": f"{ranked[0][1] - ranked[1][1]:.4f}",
                    "text": cue["text"],
                    **{f"sim_{speaker}": f"{scores[speaker]:.4f}" for speaker in speakers},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
