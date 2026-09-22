"""Isolated Qwen forced alignment. Audio and existing text stay on this PC."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    scripts = Path(__file__).resolve().parent
    # The project's qwen_asr.py is a cloud adapter, not the installed model.
    sys.path[:] = [p for p in sys.path if not p or Path(p).resolve() != scripts]
    import numpy as np
    import torch
    from qwen_asr import Qwen3ForcedAligner
    torch.set_num_threads(4)
    model = Qwen3ForcedAligner.from_pretrained(request["model"],
        dtype=torch.float32, device_map="cpu", local_files_only=True, attn_implementation="eager")
    ffmpeg = scripts.parents[1] / "tools/ffmpeg/ffmpeg.exe"
    results = []
    for index, row in enumerate(request["rows"], 1):
        start, end = float(row["start_seconds"]), float(row["end_seconds"])
        audio_start = max(0.0, start - 0.5)
        leading = start - audio_start
        command = [str(ffmpeg), "-v", "error", "-ss", str(audio_start), "-i", request["source"],
            "-t", str(end-audio_start+0.5), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"]
        audio = subprocess.run(command, capture_output=True, check=True, timeout=90,
            creationflags=0x08000000 if os.name == "nt" else 0).stdout
        sys.path.insert(0, str(scripts))
        from authoritative_alignment import spoken_text
        sys.path.pop(0)
        units = model.align(audio=(np.frombuffer(audio, dtype=np.float32),16000),
            text=spoken_text(row["text"]), language="Chinese")[0]
        # Acoustic context is essential for subsecond utterances. Convert the
        # measured timestamps back to the authoritative row, retaining its bounds.
        results.append([dict(text=u.text,
            start_time=round(max(0.0, min(end-start, u.start_time-leading)), 3),
            end_time=round(max(0.0, min(end-start, u.end_time-leading)), 3)) for u in units])
        print(f"字幕字词对齐 {index}/{len(request['rows'])}：{start:.3f}–{end:.3f}s", flush=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False),encoding="utf-8")

if __name__ == "__main__":
    main()
