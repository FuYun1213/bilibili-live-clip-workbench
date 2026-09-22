# Media packaging and callback rendering

The workbench supports optional intro/outro media at Flow 4 and explicit narrative playback structure from Prompt through JSON import, boundary repair, rendering and subtitle remapping. Neither feature reverses the audio or video inside a retained segment.

## Intro / outro settings

The shared file is `<workspace>/media-packaging.json`:

```json
{"enabled": false, "intro_path": "", "outro_path": ""}
```

The workbench **片头片尾** button saves this configuration. Paths are normalized to absolute paths. When enabled, configured files must exist and must contain a video stream. A bumper without audio receives silence; a bumper with audio keeps its audio. Bumpers are fitted into the main clip dimensions without cropping. Their frame rates and audio formats are normalized for concatenation.

Python interface in `scripts/media_packaging.py`:

```python
load_media_packaging(path, check_files=False)
save_media_packaging(path, value)
normalize_media_packaging(value, base_dir=None, check_files=False)
```

An absent shared file is disabled. `enabled=true` with both paths empty is also a no-op. An invalid configured path fails explicitly; a rejected save leaves the previous settings intact.

An explicit project `state["config"]["media_packaging"]` overrides the shared settings, including `enabled=false`. Projects without that key inherit the latest shared configuration at Flow 4. Existing completed deliveries are not changed automatically: rerun Flow 4 to apply changed bumper settings, then review the new result before publishing. Reburning uses the unwrapped review master, so bumpers do not accumulate.

The review video and editable review ASS stay on the original clip-local timeline. The final delivery ASS is shifted by the measured, whole-frame intro length; the outro adds no subtitle offset. Song timing validation receives the same offset. A sibling `.packaging.json` records actual intro/outro lengths and blur boundaries. With bumpers disabled, the existing output duration and audio-copy path are retained.

## Callback business JSON

See `callback-selection-example.json` for an importable, illustrative selection. The timestamps are examples and must be adapted to an actual recording before rendering. Do not submit it as a real selection without matching source evidence.

Each candidate from the Prompt now includes:

```json
"叙事结构": {
  "类型": "倒叙",
  "段落角色": ["冷开场", "前情", "回归"],
  "转场秒数": 0.5,
  "理由": "先展示完整反应形成疑问，较早的同一事件解释原因，回归保留结果"
}
```

`时间戳` is always in **final playback order**, not numeric order. Roles correspond one-to-one with ranges. Supported roles are 冷开场 / 前情 / 回归 / 结果 / 正文. Chronological candidates use 类型=顺叙 and explain why chronological order works better. Callback candidates must contain a later cold open followed by earlier source material; retained source ranges cannot overlap. Songs must remain chronological.

Older JSON without `叙事结构` remains accepted. A nonchronological array is inferred as a callback; ordinary ascending multi-range edits remain chronological. No placeholder range, duplicate speech, reverse filter or extra transition duration is needed.

At each cold-open-to-setup and setup-to-return jump, the final composed frame (including burned subtitles) blends into Gaussian blur (sigma 10) and back out over a total of approximately 0.5 seconds: 0.25 seconds before the cut and 0.25 seconds after. This is a visual-only effect with no overlapped footage, added frames or audio fade. Ordinary chronological deletions retain hard cuts. Actual applied review source mapping takes precedence over old planned boundaries.

The old automatic boundary repair previously sorted all ranges and rejected hook-first ordering, preventing Prompt-authored callbacks from surviving. It now preserves valid callback order and synchronizes structure roles when chronological ranges merge. Clip export trims and concatenates in order while encoding audio once, avoiding per-fragment AAC encoder-delay accumulation.

## Independent JSON clip renderer

`json-highlight-renderer/render_highlights.py` reads both legacy `segment/timestamp/title/cover_text_1` rows and workbench business JSON (including mixed 普通切片 / 歌切 groups). For business JSON without `segment`, supply the source recording explicitly:

```powershell
python json-highlight-renderer/render_highlights.py `
  --highlights target-song-cutter/references/callback-selection-example.json `
  --source recording.mp4 --transcript transcript.filtered.csv `
  --output callback-review --ffmpeg tools/ffmpeg/ffmpeg.exe `
  --tail-padding 0 --overwrite
```

The transcript may use `start_ms/end_ms` or `start_seconds/end_seconds` (also `start/end`). The renderer maps existing source transcript cues through the exact playback order; it does not retranscribe the clip. `--media-packaging <file>` overrides the shared configuration file for this command. Use `--overwrite` when changing settings for a previously rendered output folder.

The low-level burn script also accepts `--media-packaging <json>` and `--transitions <json>`, where transitions is a mapping of video filename to local `at_seconds/duration_seconds/kind/sigma` rows. Flow 4 generates and supplies these files automatically.

## Verification

`test_media_packaging.py` uses synthetic sources only. It verifies inferred and explicit JSON, source/lyric order, source audio frequency order, fixed output duration, two visual blur windows, bit-identical audio for pure visual transitions, silent and audible bumpers, delivery-only subtitle offset, and the complete independent renderer CLI. Demonstration media and blur comparison frames are retained under `artifacts/workbench-followup-20260907/media-demo`.
