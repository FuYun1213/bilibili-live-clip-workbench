# Setup and tuning

## Requirements

- Python 3.10–3.12 is recommended. Python 3.14 is not yet supported by every audio/ML dependency.
- FFmpeg and FFprobe must be available on `PATH`.
- An NVIDIA GPU is optional but greatly speeds up Demucs and embedding inference.

Create an isolated environment and install dependencies:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The first real run downloads pretrained Demucs and SpeechBrain models. Internet access is therefore required once; later runs use their local caches.

## Reference recordings

Use 30–120 seconds of the target person singing with little background music. Multiple `--reference` arguments improve robustness across registers and recording conditions. Avoid including other singers.

## Tuning order

1. Start with `--threshold 0.65`.
2. Inspect `segments.csv` and listen to every candidate.
3. Raise the threshold for false positives; lower it for missed songs.
4. Change `--merge-gap` only after the identity threshold is reasonable.
5. Use a singing-specialized embedding model if speech-trained ECAPA remains unreliable for the material.

The default ECAPA model was trained for speaker recognition on speech. Singing is a domain shift, so scores are ranking signals rather than calibrated probabilities.



## New-recording automation

Use the workspace launcher so the existing Python 3.12 runtime is preferred:

~~~powershell
recording-automation.cmd doctor
recording-automation.cmd init
recording-automation.cmd scan
recording-automation.cmd run
~~~

The checked-in `recording-automation.json` contains no credentials. It watches the configured
recording roots, writes resumable state under `automation-runs`, and points to the private
Cookie path through `%LOCALAPPDATA%`. Keep `initial_scan` set to `ignore-existing` for the
first initialization unless a deliberate historical backfill was requested.

For continuous local operation, run `recording-automation.cmd watch` in a dedicated terminal.
The loop stops at review gates; it does not approve or publish content by itself. Read
`recording-automation.md` before changing stages, one-time publishing metadata, or recovery
behavior.

## Fully local Qwen3-ASR with speaker diarization (default)

Flow 1 now defaults to `local:qwen3-asr-auto`. It performs Chinese speech recognition
with the open-source Qwen3-ASR model, obtains sentence boundaries from the local
FSMN-VAD model, and assigns anonymous speakers with the local CAM++ model. Audio stays
on this computer; this path needs no API key and has no upload gate.

On this 8 GB NVIDIA GPU, `auto` selects the verified Qwen3-ASR-0.6B profile so Qwen,
VAD, and speaker clustering fit together reliably. You can force either model from the UI:

- `local:qwen3-asr-1.7b` is an optional accuracy upgrade and falls back to 0.6B on CUDA OOM.
- `local:qwen3-asr-0.6b` is the installed stable profile and starts faster.
- `large-v3-turbo` returns to the previous local Faster-Whisper path.

Install or repair the isolated runtime and model cache with:

~~~powershell
powershell -ExecutionPolicy Bypass -File target-song-cutter\scripts\setup_qwen_local.ps1
~~~


The default setup downloads only the verified 0.6B profile, VAD, and CAM++. To add the
optional 1.7B weights later, run:

~~~powershell
powershell -ExecutionPolicy Bypass -File target-song-cutter\scripts\setup_qwen_local.ps1 -Include17B
~~~
The isolated Python environment is stored under
`%LOCALAPPDATA%\target-song-cutter\qwen-asr-runtime`, deliberately outside the Chinese
workspace path because a Windows dependency used by speaker diarization cannot load its
bundled data through some non-ASCII paths. The model weights are addressed through
`models\qwen3-asr`; in this workspace, `models` is an existing junction to
`%LOCALAPPDATA%\target-song-cutter\models-cache`.

This mode intentionally produces sentence/segment timestamps rather than word-level
timestamps. Speaker labels such as `说话人 1` are anonymous session-local clusters, not
verified identities. The final ASS stores the speaker label in each event's `Name` field.

Useful optional variables:

- `QWEN_LOCAL_MODEL_ROOT` changes the local model cache directory.
- `QWEN_LOCAL_PYTHON` changes the isolated worker Python path.
- `QWEN_LOCAL_SPEAKER_COUNT=2` supplies a known speaker-count hint.
- `QWEN_LOCAL_DIARIZATION=false` disables speaker clustering.
- `QWEN_LOCAL_DISABLE_06B_FALLBACK=true` disables the CUDA OOM retry.

The first setup needs internet access only to download open-source packages and model
weights. Normal transcription is then local and offline. The cloud Qwen adapter below
remains available as an explicit fallback but is never selected by the local model names.

## Local subtitle timing for cuts inside a sentence

Flow 2 now checks the final edit ranges before recording editorial approval. If
an edit crosses a sentence-only timestamp, the workflow aligns the existing
canonical text to that source audio using the official
[Qwen3 ForcedAligner](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B).
Install the local Qwen runtime above, then run from the workspace root:

~~~powershell
.python312\python.exe target-song-cutter/scripts/setup_subtitle_aligner.py
~~~

The pinned model is stored in `assets/models/Qwen3-ForcedAligner-0.6B` (about
1.84 GB). Normal alignment is offline, runs on CPU, and handles only rows crossed
by edits. It preserves Flow 1 text, speaker metadata, and read-SC labels. A
subsecond, single Han-character utterance can reuse its existing measured VAD
interval directly; longer text is never proportionally assigned word times.

Flow 2 and Flow 3 reuse a verified plan under `selection/executable-plans/`.
Changes to source identity, canonical subtitles, selection, or gift filtering
invalidate that plan. Unresolved timing reports `字幕时间待修复`, keeps the
selection output, and does not repeatedly retry the same export at 50%.

## Optional cloud Qwen Audio transcription with speaker diarization

For ordinary livestream subtitles, Flow 1 can use Alibaba Cloud Model Studio's
`qwen-audio-3.0-asr-flash-filetrans` instead of Faster-Whisper. Qwen Audio performs the
speech recognition and anonymous speaker diarization; `qwen3.8-max` is an optional,
conservative text-correction pass and is not used as the speech recognizer.

The cloud path is opt-in because it uploads extracted audio to Alibaba Cloud temporary
storage. Confirm that the current recording may be processed in the cloud, then set both
the credential and the explicit upload gate in the environment that launches the workflow:

~~~powershell
$env:DASHSCOPE_API_KEY = "<your-api-key>"
$env:QWEN_ASR_ALLOW_UPLOAD = "true"
$env:QWEN_TRANSCRIPT_REFINE = "true" # optional qwen3.8-max cleanup
~~~

Then set the Flow 1 ASR model in the applicable automation/project configuration to the
Qwen file-transcription model:

~~~json
{ "asr": { "model": "qwen-audio-3.0-asr-flash-filetrans" } }
~~~

The adapter extracts 16 kHz mono FLAC chunks locally, uploads one chunk at a time, polls
the asynchronous transcription task, and writes speaker columns into the authoritative
Flow 1 CSV/JSON. Raw SRT visibly prefixes anonymous labels such as `[说话人 1]`; final ASS
stores the same label in each event's `Name` field without changing the visible subtitle.
For recordings longer than one upload chunk, later labels are prefixed with `分段 N`.
Diarization identities are not assumed to remain stable across independent cloud tasks,
so the workflow never falsely merges two anonymous speakers from different chunks.

Useful optional variables:

- `QWEN_ASR_SPEAKER_COUNT=2` supplies a speaker-count hint when the count is known.
- `QWEN_ASR_DIARIZATION=false` disables speaker diarization.
- `QWEN_ASR_CHUNK_SECONDS=6900` controls local upload chunking.
- `QWEN_TRANSCRIPT_REFINE_MODEL=qwen3.8-max` selects the correction model.

To return to local-only transcription, set the model back to `large-v3-turbo` or remove
the Qwen model override. Leaving `QWEN_ASR_ALLOW_UPLOAD` unset or false makes the Qwen path
fail closed before FFmpeg extraction or any network request.

No API key is stored in checked-in JSON. Speaker labels are anonymous diarization results,
not verified identities; use the creator profile and editorial review when a real person's
name matters.
