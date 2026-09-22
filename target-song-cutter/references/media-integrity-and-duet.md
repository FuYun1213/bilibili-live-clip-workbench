# Final-media integrity and duet workflow

Use this gate for every narrative slice and song cut before burn-in or delivery.

## Final-media subtitle authority

- Treat the audible track in the final exported MP4 as the only subtitle timing authority.
- Never attach source-recording subtitles, an earlier edit's ASS, or a pre-cut ASR axis to a changed render.
- Render first, then transcribe or remap against that exact render. Rebuild the ASS after every boundary, join, speed, or restart change.
- Check the first cue, last cue, three distributed anchors, and both sides of every join. A cue with no matching audible line, or an audible retained line with no cue, blocks delivery.
- Leave 0.2–0.5 seconds after the last spoken or sung syllable and preserve the natural musical tail when present. A cue ending at or beyond the media edge blocks delivery.

Run the structural and independent-speech audit before burn-in:

```powershell
python scripts/audit_media_subtitles.py `
  --media "clip.mp4" `
  --ass "clip.ass" `
  --asr-json "work/clip-local/transcript.json" `
  --speech-json "work/clip-local/speech-activity.json" `
  --min-tail 0.2
```

For narrative clips, both clip-local ASR JSON and independent speech-activity JSON are required. Fix the cut or subtitle axis instead of suppressing a failed cue.

## Narrative silence and early-cue gate

Faster-Whisper includes Silero VAD, but its loose defaults preserve up to 400 ms around speech and only split after about 2 seconds of silence. Narrative clips instead use `min_silence_duration_ms=350`, transcription `speech_pad_ms=120`, and a second zero-padding VAD axis. The second pass is independent of the transcript text, so the transcript cannot validate its own hallucination.

Clamp each generated cue toward the overlapping speech region while preserving only a small readable lead and tail. Do not put a cue into the generated ASS when it has less than 80 ms of detected speech overlap. Record every clamp or rejection in `subtitle-vad-qa.csv`; any row marked `needs_review=yes` blocks delivery until someone listens, corrects the ASS if necessary, sets `reviewed=yes`, and records a decision. This is a safety gate, not permission to delete quiet speech blindly.

Use the upstream projects according to the kind of failure:

- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper): default engine and built-in Silero VAD; tune its VAD parameters and keep `condition_on_previous_text=False` for independent windows.
- [WhisperX](https://github.com/m-bain/whisperX): challenger for repeated local word-timestamp failures; forced alignment needs a compatible language model and overlapping speech remains difficult.
- [Stable-TS](https://github.com/jianfch/stable-ts): optional challenger for silence suppression or `refine()` when starts are early and ends are late. Development is paused, so it is not a core dependency.
- [ffsubsync](https://github.com/smacke/ffsubsync): use only for a uniform whole-file subtitle offset. It cannot repair cue-by-cue local drift or a timeline rebuilt from non-contiguous ranges.

Never use this narrative gate to decide song completeness. Re-transcribe songs with VAD disabled and review quiet singing and musical tails separately.

## Song live-performance lyric timing gate

Reference LRC/KRC/YRC timecodes are never timing authority for a live take. Render the exact final unburned song master first, transcribe that master with multilingual word timestamps and VAD disabled, and align the reviewed lyric text to that acoustic axis with `scripts/retime_reviewed_lyrics_from_asr.py`.

Delivery is blocked unless every song has all of the following:

- reliable match coverage of at least `0.30` and anchor MAD no greater than `1.20 s`;
- every reliable automatic anchor matches the first lyric unit and belongs to the highest-scoring monotonic full-song chain, so repeated choruses cannot reuse one occurrence;
- at least `max(3, ceil(20% of lyric lines))` reliable anchors, including one in each third of the performance;
- no reliable-anchor residual greater than `0.45 s` and no additional convergence adjustment greater than `0.45 s`;
- an ASS whose ordered lyric text and cue starts match the converged report within `0.10 s`;
- a PASS `.song-timing-gate.json` created by `scripts/audit_song_publish_gate.py` and bound to the exact MP4 and ASS hashes.

Keep measured vocal onsets fixed. Resolve adjacent-cue overlap by shortening the earlier cue, never by pushing the following lyric later. When automatic matching cannot meet the gate, correct a manual performance-anchor sheet and rerun; do not lower the thresholds. Rebuild from the unburned master after every correction and never stack a corrected subtitle burn on top of an already subtitled video. Any change to the MP4 or ASS invalidates the gate and blocks upload until the audit is rerun.
## Song restart and completeness gate

Inspect at least 60 seconds before and after every detected song core. Split attempts when any of these occur:

- the opening lyric or title announcement appears again;
- accompaniment stops and restarts from its opening;
- the singer says 重来、再来、错了、等一下 or otherwise abandons the take;
- a long interruption is followed by the first verse again.

Treat the abandoned take and restarted take as separate candidates. Prefer the longest continuous take that contains a valid opening, continuous internal lyric order, final sung phrase, and accompaniment decay. Do not let subtitles continue from the abandoned take into the restart.

Re-transcribe the final song render with speech VAD disabled. Quiet singing and sustained endings are often misclassified as silence; a VAD-truncated transcript must never decide the song boundary.

If no complete take exists, label the asset and publishing copy explicitly as `片段`、`一段` or `短版`. Do not present it as a complete song. Joining attempts is allowed only as an explicit reconstructed edit; remap each joined range to a new final timeline and review both sides of every join.

## Same-song discovery and duet construction

Normalize song titles by removing artist prefixes, creator suffixes, bracket notes, punctuation, and whitespace. Group only different creators with the same reviewed title; repeated takes by one creator are not a duet candidate.

For a two-person duet:

- give each source exactly half of the 1920×1080 frame;
- separate every source into `vocals` and `no_vocals` stems first, then choose exactly one reviewed `no_vocals` file as the accompaniment master;
- keep that one accompaniment master continuous from the true intro through the musical ending. Never concatenate, alternate, or crossfade two original mixed tracks or two accompaniment stems merely because the active singer changes;
- align the guest's isolated vocal to complete reviewed lyric sections and master beat/phrase anchors. Time-stretch each section independently with pitch fixed and formants preserved; never force the whole performance through one global duration ratio;
- gate only the isolated vocal stems at complete phrase or section boundaries. Leave source padding beyond the first and last audible syllables, and place 60–120 ms edge fades inside that padding rather than on a sung syllable;
- keep the master vocal muted only while the guest vocal is active. A true simultaneous duet requires reviewed key, tempo, phase, and leakage compatibility; otherwise use one active vocal at a time;
- derive all lyric events from the active vocal range, remap both cue start and end to the final composite axis, and rebuild subtitles after any boundary or stretch change;
- keep both pictures lyric-aligned, show the active singer with the creator role color, and keep both creator labels visible;
- call a shortened arrangement `双人合唱短版` or `双人接唱版`, and end only after a complete refrain or musical phrase plus its natural accompaniment decay.

Use `scripts/build_continuous_backing_duet.py` with an auditable JSON manifest. The manifest must name one `continuous_instrumental`, every guest source/target section, tempo factor, and gain. `scripts/build_duet_clip.py` is a legacy mixed-track switcher and must not be used for publishable duet audio.

### Direct-mix fallback for audible separation artifacts

Prefer the continuous-accompaniment workflow, but do not deliver watery, granular, phasey, or synthetic-sounding separated vocals. Compare at least the default model and one quality-focused model on the same phrase. If the user rejects the extracted voice or the quality models still have audible artifacts, stop using the vocal stem in the final mix.

Use `scripts/remaster_duet_with_direct_sections.py` as the fallback:

- use each singer's original full mix for one long, complete song section so the voice remains natural;
- minimize source changes: one guest entry and one return per complete section, never line-by-line alternation;
- extend the guest source before the first vocal and after the last vocal, then place 1–2 second equal-power crossfades entirely inside accompaniment-only material;
- align the same lyric/beat position before crossfading, preserve pitch and formants, and loudness-match the two original sections;
- run final-render ASR with VAD disabled. Move a fade earlier if ASR or waveform review finds any vocal onset inside it;
- label the audio map as `direct original mix`, because this fallback no longer claims one literal accompaniment file.

Natural direct audio takes priority over an obviously damaged isolated stem. This exception does not permit frequent backing-track switches or cuts through sung phrases.

Before delivery, fully decode the burned MP4, run `scripts/audit_media_subtitles.py`, and re-transcribe the final render with VAD disabled. Inspect both sides of every vocal gate. Delivery is blocked if a lyric crosses a gate, the last sung phrase is incomplete, or the audio graph contains a second accompaniment/full-mix source. Deliver the remapped ASS, vocal-map CSV, and burned MP4 together.
