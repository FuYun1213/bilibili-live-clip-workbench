#!/usr/bin/env python3
"""Extract candidate songs by a target singer from long media."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from general_hotwords import build_whisper_prompt


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    score: float
    rms_db: float


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    mean_score: float
    max_score: float
    hit_windows: int


def run_command(args: Sequence[str], *, capture: bool = False) -> str:
    args = list(args)
    if args and args[0] == "ffmpeg":
        ffmpeg = ensure_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("FFmpeg is unavailable")
        args[0] = ffmpeg
    print("+", subprocess.list2cmdline([str(x) for x in args]), flush=True)
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [str(x) for x in args], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=child_env,
    )
    return result.stdout if capture else ""


def ensure_ffmpeg() -> str | None:
    existing = shutil.which("ffmpeg")
    if existing:
        return existing
    try:
        import imageio_ffmpeg
        executable = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    except Exception:
        return None
    os.environ["PATH"] = str(executable.parent) + os.pathsep + os.environ.get("PATH", "")
    return str(executable)


def media_duration(path: Path) -> float:
    ffmpeg = ensure_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is unavailable")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(f"Could not determine media duration for {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def merge_windows(
    windows: Sequence[Window], threshold: float, min_rms_db: float,
    merge_gap: float, padding: float, min_duration: float,
    max_duration: float, media_length: float,
) -> list[Segment]:
    hits = [w for w in windows if w.score >= threshold and w.rms_db >= min_rms_db]
    if not hits:
        return []
    groups: list[list[Window]] = [[hits[0]]]
    for window in hits[1:]:
        if window.start - groups[-1][-1].end <= merge_gap:
            groups[-1].append(window)
        else:
            groups.append([window])

    segments: list[Segment] = []
    for group in groups:
        start = max(0.0, group[0].start - padding)
        end = min(media_length, group[-1].end + padding)
        if end - start < min_duration:
            continue
        # Long continuous runs are divided at max_duration boundaries. They remain
        # candidates because true song boundaries in gapless medleys need review.
        cursor = start
        while end - cursor > max_duration:
            part_end = cursor + max_duration
            scores = [w.score for w in group if w.end > cursor and w.start < part_end]
            segments.append(Segment(cursor, part_end, sum(scores) / len(scores), max(scores), len(scores)))
            cursor = part_end
        if end - cursor >= min_duration:
            scores = [w.score for w in group if w.end > cursor and w.start < end]
            segments.append(Segment(cursor, end, sum(scores) / len(scores), max(scores), len(scores)))
    return segments


def accompaniment_levels(mix_path: Path, vocals_path: Path, frame_seconds: float = 1.0) -> list[float]:
    """Measure accompaniment RMS by subtracting Demucs vocals from the mixture."""
    import numpy as np
    import soundfile as sf

    levels: list[float] = []
    with sf.SoundFile(mix_path) as mix, sf.SoundFile(vocals_path) as vocals:
        if mix.samplerate != vocals.samplerate or mix.channels != vocals.channels:
            raise ValueError("Mix and vocal WAV files must have matching sample rates and channels")
        block = int(frame_seconds * mix.samplerate)
        while True:
            mix_data = mix.read(block, dtype="float32", always_2d=True)
            vocal_data = vocals.read(block, dtype="float32", always_2d=True)
            count = min(len(mix_data), len(vocal_data))
            if count == 0:
                break
            residual = mix_data[:count] - vocal_data[:count]
            rms = float(np.sqrt(np.mean(np.square(residual, dtype=np.float64)) + 1e-12))
            levels.append(20.0 * math.log10(max(rms, 1e-8)))
            if count < block:
                break
    return levels


def _quiet_runs(flags: Sequence[bool], minimum_frames: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for index, quiet in enumerate([*flags, False]):
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            if index - start >= minimum_frames:
                runs.append((start, index))
            start = None
    return runs


def _merge_overlapping_segments(segments: Sequence[Segment], gap: float = 2.0) -> list[Segment]:
    if not segments:
        return []
    merged = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        if segment.start <= previous.end + gap:
            total_hits = previous.hit_windows + segment.hit_windows
            weighted_mean = (
                previous.mean_score * previous.hit_windows
                + segment.mean_score * segment.hit_windows
            ) / max(total_hits, 1)
            merged[-1] = Segment(
                previous.start,
                max(previous.end, segment.end),
                weighted_mean,
                max(previous.max_score, segment.max_score),
                total_hits,
            )
        else:
            merged.append(segment)
    return merged


def expand_to_music_boundaries(
    segments: Sequence[Segment], levels_db: Sequence[float], frame_seconds: float,
    boundary_gap: float, boundary_drop_db: float, max_expand: float,
    boundary_margin: float, media_length: float, max_duration: float,
) -> list[Segment]:
    """Expand singer-hit cores to nearby sustained accompaniment dropouts."""
    if not segments or not levels_db:
        return list(segments)
    import numpy as np

    levels = np.asarray(levels_db, dtype=np.float64)
    # A short median filter rejects isolated applause hits and one-second dropouts.
    if len(levels) >= 5:
        padded = np.pad(levels, (2, 2), mode="edge")
        levels = np.asarray([np.median(padded[i:i + 5]) for i in range(len(levels))])
    minimum_frames = max(1, math.ceil(boundary_gap / frame_seconds))
    expanded: list[Segment] = []
    for segment in segments:
        core_start = max(0, int(segment.start / frame_seconds))
        core_end = min(len(levels), max(core_start + 1, math.ceil(segment.end / frame_seconds)))
        core_levels = levels[core_start:core_end]
        core_median = float(np.median(core_levels))
        threshold = max(-52.0, core_median - boundary_drop_db)
        search_start = max(0, core_start - math.ceil(max_expand / frame_seconds))
        search_end = min(len(levels), core_end + math.ceil(max_expand / frame_seconds))
        quiet = levels[search_start:search_end] < threshold
        runs = [(a + search_start, b + search_start) for a, b in _quiet_runs(quiet, minimum_frames)]

        before = [run for run in runs if run[1] <= core_start]
        after = [run for run in runs if run[0] >= core_end]
        start = segment.start
        end = segment.end
        if before:
            start = max(0.0, before[-1][1] * frame_seconds - boundary_margin)
        if after:
            end = min(media_length, after[0][0] * frame_seconds + boundary_margin)

        # Reject implausibly broad expansions rather than cutting an arbitrary long program section.
        if end - start > max_duration:
            start, end = segment.start, segment.end
        expanded.append(Segment(
            start, end, segment.mean_score, segment.max_score, segment.hit_windows,
        ))
    return _merge_overlapping_segments(expanded)


def filter_windows_by_music(
    windows: Sequence[Window], levels_db: Sequence[float], frame_seconds: float,
    percentile: float = 70.0, minimum_db: float | None = None,
) -> tuple[list[Window], float]:
    """Keep identity hits whose time window also contains accompaniment energy."""
    if not levels_db:
        return list(windows), float("-inf")
    import numpy as np

    levels = np.asarray(levels_db, dtype=np.float64)
    threshold = float(minimum_db) if minimum_db is not None else float(np.percentile(levels, percentile))
    kept = []
    for window in windows:
        start = max(0, int(window.start / frame_seconds))
        end = min(len(levels), max(start + 1, math.ceil(window.end / frame_seconds)))
        if float(np.median(levels[start:end])) >= threshold:
            kept.append(window)
    return kept, threshold


def split_long_segments(
    segments: Sequence[Segment], levels_db: Sequence[float], frame_seconds: float,
    split_longer_than: float, minimum_part: float, valley_seconds: float,
) -> list[Segment]:
    """Split implausibly long merged candidates at the quietest internal valley."""
    if not levels_db or split_longer_than <= 0:
        return list(segments)
    import numpy as np

    levels = np.asarray(levels_db, dtype=np.float64)
    window_frames = max(1, round(valley_seconds / frame_seconds))
    kernel = np.ones(window_frames, dtype=np.float64) / window_frames
    smoothed = np.convolve(levels, kernel, mode="same")
    pending = list(segments)
    result: list[Segment] = []
    while pending:
        segment = pending.pop(0)
        if segment.end - segment.start <= split_longer_than:
            result.append(segment)
            continue
        lower = math.ceil((segment.start + minimum_part) / frame_seconds)
        upper = math.floor((segment.end - minimum_part) / frame_seconds)
        lower = max(0, lower)
        upper = min(len(smoothed), upper)
        if lower >= upper:
            result.append(segment)
            continue
        split_index = lower + int(np.argmin(smoothed[lower:upper]))
        split_time = split_index * frame_seconds
        total_duration = segment.end - segment.start
        left_ratio = (split_time - segment.start) / total_duration
        left_hits = max(1, round(segment.hit_windows * left_ratio))
        right_hits = max(1, segment.hit_windows - left_hits)
        pending.insert(0, Segment(
            split_time, segment.end, segment.mean_score,
            segment.max_score, right_hits,
        ))
        pending.insert(0, Segment(
            segment.start, split_time, segment.mean_score,
            segment.max_score, left_hits,
        ))
    return result


def doctor() -> int:
    ffmpeg = ensure_ffmpeg()
    checks = {
        "python_supported (3.10-3.12 recommended)": (3, 10) <= sys.version_info[:2] <= (3, 12),
        "ffmpeg": ffmpeg is not None,
        "demucs": importlib.util.find_spec("demucs") is not None,
        "numpy": importlib.util.find_spec("numpy") is not None,
        "soundfile": importlib.util.find_spec("soundfile") is not None,
        "torch": importlib.util.find_spec("torch") is not None,
        "speechbrain": importlib.util.find_spec("speechbrain") is not None,
    }
    for name, ok in checks.items():
        print(f"[{'OK' if ok else 'MISSING'}] {name}")
    return 0 if all(checks.values()) else 1


def convert_analysis_audio(source: Path, destination: Path) -> None:
    run_command(["ffmpeg", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination)])


def separate_vocals(source: Path, work: Path, device: str) -> Path:
    separated = work / "separated"
    demucs_source = source
    if source.suffix.lower() != ".wav":
        demucs_source = work / "source_audio.wav"
        if not demucs_source.exists() or demucs_source.stat().st_size < 1024:
            run_command([
                "ffmpeg", "-y", "-i", str(source), "-vn", "-ac", "2",
                "-ar", "44100", "-c:a", "pcm_s16le", str(demucs_source),
            ])
    duration = media_duration(demucs_source)
    if duration <= 1800:
        run_command([
            sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
            "--device", device, "-o", str(separated), str(demucs_source),
        ])
        result = separated / "htdemucs" / demucs_source.stem / "vocals.wav"
    else:
        chunk_seconds = 900
        chunks = work / "chunks"
        chunks.mkdir(parents=True, exist_ok=True)
        vocal_chunks: list[Path] = []
        for index, start in enumerate(range(0, math.ceil(duration), chunk_seconds)):
            chunk = chunks / f"chunk_{index:03d}.wav"
            if not chunk.exists() or chunk.stat().st_size < 1024:
                run_command([
                    "ffmpeg", "-y", "-ss", str(start), "-t", str(chunk_seconds),
                    "-i", str(demucs_source), "-c:a", "pcm_s16le", str(chunk),
                ])
            chunk_vocals = separated / "htdemucs" / chunk.stem / "vocals.wav"
            if not chunk_vocals.exists() or chunk_vocals.stat().st_size < 1024:
                run_command([
                    sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
                    "--device", device, "-o", str(separated), str(chunk),
                ])
            vocal_chunks.append(chunk_vocals)
        result = separated / "htdemucs" / demucs_source.stem / "vocals.wav"
        result.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(vocal_chunks[0]), "rb") as first:
            params = first.getparams()
        with wave.open(str(result), "wb") as destination:
            destination.setparams(params)
            for chunk_vocals in vocal_chunks:
                with wave.open(str(chunk_vocals), "rb") as source_wave:
                    chunk_params = source_wave.getparams()
                    if (
                        chunk_params.nchannels != params.nchannels
                        or chunk_params.sampwidth != params.sampwidth
                        or chunk_params.framerate != params.framerate
                        or chunk_params.comptype != params.comptype
                    ):
                        raise RuntimeError(f"Incompatible vocal chunk format: {chunk_vocals}")
                    while frames := source_wave.readframes(1024 * 1024):
                        destination.writeframesraw(frames)
    if not result.exists():
        raise RuntimeError(f"Demucs completed but vocals file was not found: {result}")
    return result


def load_classifier(device: str):
    from speechbrain.inference.speaker import EncoderClassifier
    return EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": device},
    )


def read_chunks(path: Path, seconds: float, hop: float):
    import soundfile as sf
    with sf.SoundFile(path) as audio:
        rate = audio.samplerate
        frames = int(seconds * rate)
        hop_frames = int(hop * rate)
        start = 0
        while start + frames <= len(audio):
            audio.seek(start)
            data = audio.read(frames, dtype="float32", always_2d=False)
            if getattr(data, "ndim", 1) > 1:
                data = data.mean(axis=1)
            yield start / rate, data
            start += hop_frames


def normalized_embedding(classifier, samples, device: str):
    import torch
    wave = torch.as_tensor(samples, dtype=torch.float32, device=device).unsqueeze(0)
    with torch.inference_mode():
        embedding = classifier.encode_batch(wave).squeeze().float()
    return embedding / embedding.norm(p=2).clamp_min(1e-8)


def build_reference_embedding(
    classifier, references: Sequence[Path], work: Path, device: str,
    minimum_rms_db: float = -40.0,
):
    import numpy as np
    import torch
    reference_centroids = []
    for index, reference in enumerate(references):
        converted = work / f"reference_{index:02d}.wav"
        convert_analysis_audio(reference, converted)
        embeddings = []
        for _, samples in read_chunks(converted, 8.0, 6.0):
            rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)) + 1e-12))
            rms_db = 20.0 * math.log10(max(rms, 1e-8))
            if rms_db < minimum_rms_db:
                continue
            embeddings.append(normalized_embedding(classifier, samples, device))
        if embeddings:
            centroid = torch.stack(embeddings).mean(dim=0)
            reference_centroids.append(centroid / centroid.norm(p=2).clamp_min(1e-8))
    if not reference_centroids:
        raise ValueError("Reference audio must contain at least 8 seconds of usable audio")
    average = torch.stack(reference_centroids).mean(dim=0)
    return average / average.norm(p=2).clamp_min(1e-8)


def score_audio(classifier, target, analysis: Path, device: str, window: float, hop: float) -> list[Window]:
    import numpy as np
    results = []
    for index, (start, samples) in enumerate(read_chunks(analysis, window, hop), 1):
        rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)) + 1e-12))
        rms_db = 20.0 * math.log10(max(rms, 1e-8))
        embedding = normalized_embedding(classifier, samples, device)
        score = float((embedding * target).sum().item())
        results.append(Window(start, start + window, score, rms_db))
        if index % 100 == 0:
            print(f"Scored {index} windows ({start / 60:.1f} min)", flush=True)
    return results


def export_clip(
    source: Path,
    destination: Path,
    start: float,
    end: float,
    output_format: str,
    video_source: Path | None = None,
) -> None:
    if output_format == "mp4" and video_source is not None:
        duration = end - start
        run_command([
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video_source),
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
            "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", str(destination),
        ])
        return
    base = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source)]
    if output_format == "mp4":
        args = base + ["-map", "0:v?", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", str(destination)]
    elif output_format == "mp3":
        args = base + ["-vn", "-c:a", "libmp3lame", "-q:a", "2", str(destination)]
    else:
        args = base + ["-vn", "-c:a", "pcm_s16le", str(destination)]
    run_command(args)


def write_report(path: Path, segments: Sequence[Segment], files: Sequence[Path]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "start_seconds", "end_seconds", "duration_seconds", "mean_similarity", "max_similarity", "hit_windows", "review_status"])
        for segment, file in zip(segments, files):
            writer.writerow([file.name, f"{segment.start:.3f}", f"{segment.end:.3f}", f"{segment.end-segment.start:.3f}", f"{segment.mean_score:.4f}", f"{segment.max_score:.4f}", segment.hit_windows, "REVIEW"])


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def generate_subtitles(args) -> int:
    dll_handle = None
    if args.device == "cuda" and os.name == "nt":
        import torch
        torch_lib = Path(torch.__file__).resolve().parent / "lib"
        if torch_lib.is_dir():
            os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
            dll_handle = os.add_dll_directory(str(torch_lib))
    from faster_whisper import WhisperModel

    inputs = [path.resolve() for path in args.input]
    if args.input_dir:
        inputs.extend(sorted(args.input_dir.resolve().glob("*.mp4")))
    inputs = list(dict.fromkeys(inputs))
    if not inputs:
        raise ValueError("No input videos were supplied")
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(args.model_cache.resolve()) if args.model_cache else None,
    )
    for index, media in enumerate(inputs, 1):
        print(f"Transcribing {index}/{len(inputs)}: {media.name}", flush=True)
        segments, _ = model.transcribe(
            str(media),
            language=args.language,
            beam_size=args.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=build_whisper_prompt(args.initial_prompt),
        )
        destination = output / f"{media.stem}.srt"
        with destination.open("w", encoding="utf-8-sig", newline="\n") as handle:
            subtitle_index = 1
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                handle.write(
                    f"{subtitle_index}\n{srt_timestamp(segment.start + args.subtitle_delay)} --> "
                    f"{srt_timestamp(segment.end + args.subtitle_delay)}\n{text}\n\n"
                )
                subtitle_index += 1
    print(f"Done: generated {len(inputs)} subtitle file(s) in {output}")
    if dll_handle is not None:
        dll_handle.close()
    return 0


def execute(args) -> int:
    source = args.input.resolve()
    video_source = args.video_source.resolve() if args.video_source else None
    references = [path.resolve() for path in args.reference]
    for path in [source, *references, *([video_source] if video_source else [])]:
        if not path.is_file():
            raise FileNotFoundError(path)
    if video_source and args.format != "mp4":
        raise ValueError("--video-source is only valid with --format mp4")
    output = args.output.resolve()
    clips = output / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    work_context = (
        tempfile.TemporaryDirectory(prefix="target-song-cutter-", dir=output)
        if not args.keep_work and not args.work_dir else None
    )
    if args.work_dir:
        work = args.work_dir.resolve()
    else:
        work = Path(work_context.name) if work_context else output / "work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        if args.vocals:
            vocals = args.vocals.resolve()
        elif args.skip_separation:
            vocals = source
        else:
            vocals = separate_vocals(source, work, args.device)
        if args.analysis:
            analysis = args.analysis.resolve()
            if not analysis.is_file():
                raise FileNotFoundError(analysis)
        else:
            analysis = work / "analysis_16k.wav"
            convert_analysis_audio(vocals, analysis)
        classifier = load_classifier(args.device)
        target = build_reference_embedding(
            classifier, references, work, args.device, args.reference_min_rms_db,
        )
        windows = score_audio(classifier, target, analysis, args.device, args.window, args.hop)
        duration = media_duration(source)
        if args.mix_audio:
            mix_audio = args.mix_audio.resolve()
        elif (work / "source_audio.wav").is_file():
            mix_audio = work / "source_audio.wav"
        elif source.suffix.lower() == ".wav":
            mix_audio = source
        else:
            mix_audio = None
        levels = None
        if args.music_boundaries or args.singing_gate:
            if mix_audio is None or not mix_audio.is_file() or vocals.suffix.lower() != ".wav":
                print("Warning: accompaniment analysis skipped because matching mix/vocal WAV files are unavailable", file=sys.stderr)
            else:
                print("Analyzing accompaniment energy...", flush=True)
                levels = accompaniment_levels(mix_audio, vocals, args.boundary_frame)
        if args.singing_gate and levels is not None:
            windows, gate = filter_windows_by_music(
                windows, levels, args.boundary_frame,
                args.music_gate_percentile, args.music_gate_db,
            )
            print(f"Singing gate: accompaniment threshold {gate:.2f} dB; {len(windows)} windows retained", flush=True)
        segments = merge_windows(windows, args.threshold, args.min_rms_db, args.merge_gap, args.padding, args.min_duration, args.max_duration, duration)
        if args.music_boundaries and levels is not None:
                segments = expand_to_music_boundaries(
                    segments, levels, args.boundary_frame, args.boundary_gap,
                    args.boundary_drop_db, args.max_boundary_expand,
                    args.boundary_margin, duration, args.max_duration,
                )
        if levels is not None:
            segments = split_long_segments(
                segments, levels, args.boundary_frame, args.split_longer_than,
                args.minimum_split_part, args.split_valley_seconds,
            )
        exported = []
        for index, segment in enumerate(segments, 1):
            destination = clips / f"song_{index:03d}.{args.format}"
            export_clip(source, destination, segment.start, segment.end, args.format, video_source)
            exported.append(destination)
        write_report(output / "segments.csv", segments, exported)
        print(f"Done: exported {len(exported)} candidate clip(s) to {clips}")
        return 0
    finally:
        if work_context:
            work_context.cleanup()


def export_existing(args) -> int:
    source = args.input.resolve()
    video_source = args.video_source.resolve() if args.video_source else None
    report = args.segments.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if video_source and not video_source.is_file():
        raise FileNotFoundError(video_source)
    if video_source and args.format != "mp4":
        raise ValueError("--video-source is only valid with --format mp4")
    if not report.is_file():
        raise FileNotFoundError(report)
    output = args.output.resolve()
    clips = output / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    segments: list[Segment] = []
    with report.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            segments.append(Segment(
                float(row["start_seconds"]),
                float(row["end_seconds"]),
                float(row["mean_similarity"]),
                float(row["max_similarity"]),
                int(row["hit_windows"]),
            ))
    exported = []
    for index, segment in enumerate(segments, 1):
        destination = clips / f"song_{index:03d}.{args.format}"
        export_clip(source, destination, segment.start, segment.end, args.format, video_source)
        exported.append(destination)
    write_report(output / "segments.csv", segments, exported)
    print(f"Done: exported {len(exported)} clip(s) from existing boundaries to {clips}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check external programs and Python packages")
    run = sub.add_parser("run", help="Extract target-singer candidate clips")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--video-source", type=Path, help="Optional matching video-only source; analyze --input audio and mux both into MP4 clips")
    run.add_argument("--reference", type=Path, required=True, action="append", help="Repeat for multiple references")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--work-dir", type=Path, help="Store large reusable intermediates separately from output")
    run.add_argument("--vocals", type=Path, help="Reuse an existing isolated vocal WAV")
    run.add_argument("--analysis", type=Path, help="Reuse an existing mono 16 kHz analysis WAV")
    run.add_argument("--mix-audio", type=Path, help="Original mixture WAV used for music-boundary detection")
    run.add_argument("--skip-separation", action="store_true")
    run.add_argument("--keep-work", action="store_true")
    run.add_argument("--format", choices=["mp4", "mp3", "wav"], default="mp4")
    run.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    run.add_argument("--threshold", type=float, default=0.65)
    run.add_argument("--reference-min-rms-db", type=float, default=-40.0)
    run.add_argument("--min-rms-db", type=float, default=-45.0)
    run.add_argument("--window", type=float, default=8.0)
    run.add_argument("--hop", type=float, default=2.0)
    run.add_argument("--merge-gap", type=float, default=15.0)
    run.add_argument("--padding", type=float, default=0.0)
    run.add_argument("--min-duration", type=float, default=30.0)
    run.add_argument("--max-duration", type=float, default=480.0)
    run.add_argument("--music-boundaries", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--singing-gate", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--music-gate-percentile", type=float, default=70.0)
    run.add_argument("--music-gate-db", type=float)
    run.add_argument("--split-longer-than", type=float, default=420.0)
    run.add_argument("--minimum-split-part", type=float, default=90.0)
    run.add_argument("--split-valley-seconds", type=float, default=5.0)
    run.add_argument("--boundary-frame", type=float, default=1.0)
    run.add_argument("--boundary-gap", type=float, default=3.0)
    run.add_argument("--boundary-drop-db", type=float, default=8.0)
    run.add_argument("--max-boundary-expand", type=float, default=180.0)
    run.add_argument("--boundary-margin", type=float, default=2.0)
    export = sub.add_parser("export", help="Export clips from an existing segments CSV")
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--video-source", type=Path, help="Optional matching video-only source to combine with --input audio")
    export.add_argument("--segments", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--format", choices=["mp4", "mp3", "wav"], default="mp4")
    subtitles = sub.add_parser("subtitles", help="Generate one SRT subtitle file per video")
    subtitles.add_argument("--input", type=Path, action="append", default=[])
    subtitles.add_argument("--input-dir", type=Path)
    subtitles.add_argument("--output", type=Path, required=True)
    subtitles.add_argument("--model", default="large-v3-turbo")
    subtitles.add_argument("--model-cache", type=Path)
    subtitles.add_argument("--language", default="zh")
    subtitles.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    subtitles.add_argument("--compute-type", default="float16")
    subtitles.add_argument("--beam-size", type=int, default=5)
    subtitles.add_argument(
        "--initial-prompt",
        help="Optional extra context appended after the shared general hotwords",
    )
    subtitles.add_argument("--subtitle-delay", type=float, default=0.1)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if ensure_ffmpeg() is None:
            raise RuntimeError("FFmpeg is unavailable; install FFmpeg or imageio-ffmpeg")
        if args.command == "doctor":
            return doctor()
        if args.command == "export":
            return export_existing(args)
        if args.command == "subtitles":
            return generate_subtitles(args)
        return execute(args)
    except subprocess.CalledProcessError as exc:
        print(f"External command failed with exit code {exc.returncode}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
