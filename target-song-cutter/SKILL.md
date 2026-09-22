---
name: target-song-cutter
description: Create audio-first, outsider-friendly narrative clips, reviewed song cuts, or a single-pass mixed batch from long livestream recordings. Transcribe the source recording itself, align danmaku and Super Chat signals, select complete stories and continuous singing attempts, generate final-media subtitles, reviewed lyrics, covers, radio layouts, and optional Bilibili previews or submissions. Use for livestreams, radio streams, interviews, karaoke, concerts, compilations, 切片、爆点、歌切、普通与歌切混合处理, long-form-to-short-form editing, or explicitly approved Bilibili submission.
---

# Audio-first Content Slicer

Choose one workbench mode:

- Narrative: ordinary highlight/story slicing with Chinese clip-local ASR and VAD review.
- Song: multilingual discovery, no-VAD final-clip ASR, mandatory lyric correction, and live timing gates.
- Mixed: one multilingual whole-session transcript and one Codex selection call, then per-item narrative/song branches. Read [references/mixed-workflow-template.md](references/mixed-workflow-template.md).

Do not restore “authoritative audio”, automatic song classification, or target-voice detection as a required stage. Music/speech models may surface candidates only; content evidence and review determine singing attempts, lyrics, and boundaries.
Use the source recording's own embedded audio as the analysis and edit timeline. The local workbench does not accept a separate replacement audio track: the same recording file supplies both picture and sound. Do not inspect or analyze video content when the user requests audio-only processing.

## Current end-to-end batch workflow

For a complete recording batch, a resumed run after restart, multi-creator processing, or a request that includes selection through publishing or archiving, read and follow [references/end-to-end-workflow.md](references/end-to-end-workflow.md) first. It is the current execution order for source stability, creator routing, discovery, 3–5 point selection, narrative and song branches, final-media subtitles, AutoClip covers, burn-in, collection-verified publishing, the mandatory unmade-candidate report, and OneDrive cleanup. Mode-specific references below remain authoritative for their detailed gates.

For the desktop mixed mode, its exact business JSON, one-call selection contract, per-item
Flow 3–5 routing, radio behavior, and song-review rationale live in
[references/mixed-workflow-template.md](references/mixed-workflow-template.md). Do not run
the narrative and song pipelines twice over the same source.
This account uses a channel-specific title convention: put exactly one creator label at the beginning as `【中文名English】`, then write a complete narrative sentence whose body still contains an explicit subject, concrete action or event, and result, reversal, or emotional response. The label contains only the creator identity, never a hook summary. Strip local filename indices such as `001` or `002B` from platform titles. Narrative cover copy uses 2–3 phrases, each with 3–22 effective characters. The first phrase is in the upper region; the remaining one or two phrases are in the lower region, separated by ｜. Each phrase may use one manual line break; otherwise the renderer balances it across at most two lines. Use the concrete-event-plus-viewer-commentary style in references/narrative-cover-prompt.md. Titles and cover text never contain an English single quote.

For every rendered narrative or song asset, read and enforce [references/media-integrity-and-duet.md](references/media-integrity-and-duet.md). Final-render subtitle authority, audible-line coverage, restart detection, complete-ending or explicit-short-version labeling, phrase-aligned duet remapping, and either one uninterrupted accompaniment master or the audited long-section direct-mix fallback are blocking requirements.
## Mandatory narrative review gates

For every narrative livestream or radio slice, read and enforce [references/editorial-review-gates.md](references/editorial-review-gates.md) before candidate selection and again before delivery. When a same-folder Bilibili XML exists, run `scripts/analyze_bililive_xml.py` and use burst windows, repeated messages, keyword context, and original SC text as a candidate library. The first-subtitle integrity gate, complete-story gate, repetition compression rule, 3–5 point selection threshold, mandatory reverse-edit transition flow, explicit-subject complete-sentence title gate, and three-bundle title/cover mode are blocking requirements, not optional polish.
When danmaku or SC data exists, also read [references/engagement-signal-fusion.md](references/engagement-signal-fusion.md). For a same-folder Bilibili XML, prefer `scripts/analyze_engagement.py` and use its multi-scale hotspots, repeated messages, keyword context, original SC text, and timeline audit as the candidate library. Keep `scripts/analyze_bililive_xml.py` only for compatibility with earlier batches.


When rated `clip-evaluation.csv` files exist, read [references/feedback-calibration.md](references/feedback-calibration.md), run `scripts/summarize_clip_feedback.py`, and use only repeated, auditable preference adjustments. Preference evidence may rerank eligible candidates but never override completeness, fairness, source truth, or duplicate rejection.

Before final narrative ranking, create `topic-map.csv` and `candidate-report.csv` using the semantic topic boundaries and independent proposal lanes in `references/editorial-guide.md`. Deduplicate proposals by topic and payoff before authoring `edit-plan.csv`.

Before delivering narrative titles and cover copy, run `python scripts/validate_narrative_titles.py <titles-and-covers.csv>`. Any failure blocks delivery; fix the title instead of bypassing the validator.

## Optional delivery packaging and executable callbacks

For configurable intro/outro files and narrative callback editing, read
[references/media-packaging-and-callback.md](references/media-packaging-and-callback.md).
Shared bumper settings are disabled by default and are applied only when Flow 4
is rerun. Keep review subtitles on the original clip-local clock; shift only the
delivery ASS by the measured intro duration. Preserve the JSON timestamp array
as final playback order and render its visual-only 0.5-second Gaussian blur
jumps without reversing any source audio/video or reordering song performances.

## General persistent hotwords

All bundled ASR entry points decode without vocabulary prompts or text bias,
including local/cloud Qwen, Whisper, FunASR, and retry/fallback paths. Quiet or
short VAD regions can otherwise reproduce the supplied list as dialogue. Keep
the vocabularies for post-transcription checks and reviewed spelling corrections;
do not restore them as `context`, `initial_prompt`, `hotwords`, or `vocabulary`
model inputs. Legacy CLI options remain accepted for compatibility and checking.

Read [references/asr-hotword-safety.md](references/asr-hotword-safety.md) for the
current prevention and repair contract. A fresh content check must pass before
Flow 1 reuse, selection, subtitle remapping, review-context extension, burn-in,
and final delivery. It checks adjacent cues as well as individual lines and
retains the source vocabulary snapshot. An earlier PASS or human-review status
does not authorize a subsequently changed subtitle file. A flag requires
source-audio verification; never silently remove spoken names.

Validate `references/general-hotwords.json` before transcription. It is the
skill-wide vocabulary for checking; it is not a decoder prompt.

```powershell
python scripts/general_hotwords.py validate
python scripts/general_hotwords.py export --output "slice-work/general-hotwords.txt"
```

Keep reusable terms here and creator/event terms in their narrower glossary.
Verified correction mappings still apply after recognition. Never select a clip
merely because a configured term appeared.

## 哎小呜观看 KPL 的选片限制

处理哎小呜时读取 [references/awu-kpl-review.md](references/awu-kpl-review.md) 和人物配置的 `selection_notes`。用户排除比赛进程、游戏高光与 BP/阵容复盘切片；保留独立成立的本人聊天。赛事解说不可当作主播台词；王者荣耀与 KPL 专名按录播当日证据校对，不作为 ASR 提示词。

## VirtuaReal proper nouns

Before transcribing a VirtuaReal recording, validate and use
`references/virtuareal-glossary.json`. It contains the current VirtuaReal
Project roster, commonly mentioned related members, verified fan names, fan
tags, and recurring ASR corrections.

```powershell
python scripts/virtuareal_glossary.py validate
python scripts/virtuareal_glossary.py hotwords --output "slice-work/virtuareal-hotwords.txt"
```

The FunASR scripts validate this glossary automatically. Use `--glossary` to
load another reviewed glossary. Legacy `--hotword` input is accepted but is not
sent to the decoder; reviewed corrections still apply after recognition.
Treat fan names, fan tags, and Bilibili fan badges as different fields. Do not
invent an unverified fan name. Recheck the online roster before updating member
status because departures and new generations change over time.

Use `雾深Girimi` / `Girimi` as the canonical spelling. Normalize the older typo
`Gimiri` to `Girimi` in subtitles and recording-specific term lists.

## Shared-source market benchmarking

For Sumire, Kioi, Viridis, Yuchu, or Chilly sources, read
`references/market-benchmarking.md` before final candidate ranking and again before title and
cover approval. Run `scripts/bilibili_creator_benchmark.py` to refresh recent exact-name Bilibili
results using comprehensive ranking; keep its CSV, JSON, metadata, and cover thumbnails in the batch
working/audit folder. Compare the current source against same-event competitors because other
cutters often have identical material. Use views and views/day as demand evidence, then choose
a clearer complete angle or an unsaturated adjacent story; never copy another uploader's
wording, image composition, or edit.

Treat market terms as editorial discovery vocabulary, not automatic ASR replacements. For
narrative covers, inspect several frames around the verified punchline, reaction, or on-screen
evidence and record the selected final-clip timestamp as `cover_time_seconds` in
`titles-and-covers.csv`. The 28% automatic frame is fallback only when no stronger reviewed
frame exists.

Read `references/selection-and-publish-tags.md` during candidate selection. Generate grounded
clip-specific tags with each 3–5 point candidate, preserve their evidence, and validate the
approved `tags` before cover delivery. Do not postpone tag authoring until upload.
## Narrative and highlight slicing

### 1. Transcribe the audio

Check the environment, then run:

```powershell
python scripts/content_slicer.py doctor
python scripts/content_slicer.py transcribe --input "recording.m4a" --output "slice-work" --language zh
```

This writes `transcript.csv`, `transcript.json`, `transcript.srt`, and an empty `edit-plan.csv`. Use `--device cpu --compute-type int8` when CUDA is unavailable.

Treat this whole-recording transcript as the only text authority for selection and final
subtitles. Flow 1 also writes `transcript-completeness.json`, `speech-activity.json`, and
`transcript-confidence-review.csv`. It uses non-batched inference, word timestamps, wide VAD
boundaries, a lower-threshold full-session VAD audit, and targeted retries before any edit plan
is consumed. If `transcript-completeness.json` is not `PASS`, stop: do not select, export, or
repair the omission after clipping. Review low-confidence rows against the full source and make
verified wording corrections in the authoritative CSV before selection.

After clips are exported, remap Flow 1 timestamps through the exact edit plan. Never run Whisper
against an exported clip and never let a later ASR result replace corrected Flow 1 text:

```powershell
python scripts/content_slicer.py slice-authoritative `
  --transcript "slice-work/transcript.filtered.csv" `
  --transcript-json "slice-work/transcript.json" `
  --completeness-report "slice-work/transcript-completeness.json" `
  --speech-json "slice-work/speech-activity.json" `
  --plan "slice-work/edit-plan.csv" `
  --manifest "review/export/slices.csv" `
  --output "review/asr" `
  --ass-output "review/clips" `
  --ass-template "assets/subtitles/narrative-v2.ass" `
  --ass-style Regular
```

This command writes same-stem editable ASS files and compatibility QA artifacts, but their
`origin` is `flow1-authoritative-remap`; no model is loaded and `gap-recovery.json` explicitly
records `forbidden-after-Flow1`. Non-contiguous joins and tail padding are handled by the same
edit-plan mapping used for video export. If a person corrects the authoritative CSV so its text
no longer matches stale word tokens, discard those tokens and distribute the corrected line over
its verified Flow 1 segment instead of restoring old ASR wording.

Load the persistent correction dictionary during Flow 1 normalization. Persist newly verified
recurring recognition errors so future full-session transcripts receive the same correction.
Every narrative and radio ASS used for review or delivery must stay visually single-line: set
`WrapStyle: 2`, remove explicit `\N`/`\n`, and do not split a continuous English word,
abbreviation, or number between cues.

For each clip, compare early, middle, and late anchors against the picture and waveform. Review
flagged `subtitle-vad-qa.csv` rows by listening, but do not create missing transcript text there.
If a promised sentence is absent, redo Flow 1 and resolve the full-session completeness report,
then regenerate Flow 2 onward. WhisperX, Stable-TS, or another ASR may challenge uncertain Flow 1
regions; any accepted correction must be written back to the full-session authority before clips
are rebuilt. They are never a Flow 3 patch source.
For reaction videos, watchalongs, and a creator playing another creator's clip, read and enforce [references/reaction-video-speakers.md](references/reaction-video-speakers.md). Attribute the watched source and the reacting host separately, split any mixed-speaker cue at the verified word boundary, and write titles as `A看B谈X` rather than assigning B's monologue to A.

If two or more anchor checks fail, multi-speaker turns remain ambiguous, or English words drift, use WhisperX forced alignment or Stable-TS silence suppression/refinement only as a challenger for the failed cues. Stable-TS development is paused, so do not make it a core dependency. Use ffsubsync only when every cue shares one global offset, never for local drift or non-contiguous joins. Keep the reviewed final-clip axis unless the challenger is verified against waveform and audio; diarization labels anonymous speakers and never establishes identity by itself.

### 2. Analyze the transcript

For Bilibili recordings, refresh or reuse the gift catalog and remove gift acknowledgements before analysis:

```powershell
python scripts/bilibili_gifts.py update --room-id 1791260756 --output references/bilibili-live-gifts.json
python scripts/content_slicer.py filter-gifts --transcript "slice-work/transcript.csv" --output "slice-work/transcript-editorial.csv" --removed-output "slice-work/gift-acknowledgements.csv"
```

Use `transcript-editorial.csv` for selection. Inspect `gift-acknowledgements.csv` as mandatory cut points; do not let a retained range span a removed gift-reading block. The detector requires a thanks phrase plus a known gift, gift context, or quantity and preserves ordinary lines such as “谢谢你陪我”. Refresh the catalog from Bilibili's public room gift API because activity and room-specific gifts change.

Remove spoken gift acknowledgements, not merely their subtitle text. A standalone “谢谢某某”“感谢某某的礼物” is a mandatory cut even when ASR did not recognize the gift name. For `Super Chat` / `SC` / `醒目留言` / `钢镚`, review the message body manually:

- if the paid-message body directly supplies setup, evidence, a question, or a payoff used by the slice, normalize only the recognition/thank-you preamble (`谢谢某某的 SC`, `谢谢某某的钢镚`, and equivalent variants) to `（谢谢SC）`, then preserve the complete SC body and the creator response;
- if the paid-message body is unrelated to the slice, remove the entire reading;
- never delete, summarize away, or replace a relevant SC body with `（谢谢SC）`; the marker replaces only the thank-you preamble.

When the user supplies danmaku and SC exports, align both to the source recording timeline before selection. Treat them as discovery evidence rather than proof that a clip is good:

```powershell
python scripts/analyze_engagement.py `
  --input "recording.xml" --output "engagement-xml" `
  --windows 10,30,60 `
  --reaction-lag-max 30 `
  --sc-response-window 180
```

Read `engagement-hotspots.csv`, `engagement-summary.json`, and `timeline-audit.json` before opening individual scale tables.

- summarize danmaku volume in short fixed windows and flag local bursts, sustained high-flow periods, and abrupt changes relative to the same recording;
- inspect the transcript and audio before, during, and after every strong burst so the retained range includes the setup and complete payoff rather than only the noisiest seconds;
- classify each SC as `trigger`, `setup`, `evidence`, `payoff`, `callback`, or `unrelated`; preserve a relevant message body and the creator response when it genuinely steers the content, while still cutting the thank-you preamble;
- record the danmaku and SC evidence in the candidate ranking, including the aligned time range and whether the signal confirmed or merely surfaced the candidate;
- never manufacture synchronization when an export uses an unknown clock or offset. Establish the mapping from shared timestamps/events first.

- inspect `cause_search_start` through `payoff_search_end`; a danmaku peak may trail the source event by several seconds, so never center the cut mechanically on the peak;
- compare `user_coverage_ratio`, `same_user_repeat_ratio`, and `spam_risk`; treat many distinct users repeating one line as possible crowd consensus and repeated sender-message pairs as stronger spam evidence;
- use each SC `post_peak_*` only to find where to listen next, then fill the confirmed creator response range manually and preserve the complete steering chain.

Treat delivery passwords, mysterious-number recitations, takeaway discount codes, coupon codes, and equivalent `外卖口令/神秘数字/外卖优惠口令` material as hard exclusions. They damage pacing and must not enter a candidate, title, subtitle, or cover merely because danmaku volume rises around them.

Read the complete filtered transcript in chronological chunks. Do not choose clips from keywords alone. Reconstruct context across adjacent chunks and identify:

- the outline and purpose of each meaningful section;
- self-contained story arcs with setup, escalation/turn, and payoff;
- surprising, funny, emotional, controversial, revealing, useful, or highly quotable moments;
- moments whose hook makes sense quickly to a new viewer;
- reversals, self-owns, failed attempts, awkward friction, strange logic, escalating bits, creator commentary on people or events, truthful suggestive double meanings, and reactions that a non-fan would immediately understand;
- repeated, empty, private, risky, or context-dependent material that should be dropped.

Read `references/editorial-guide.md` before authoring the plan. Default to the perspective of an internet passerby looking for a good story or laugh, not a fan account obligated to affirm the creator. Produce a brief whole-recording outline and candidate ranking that states both outsider entertainment value and fan-only value, then fill `edit-plan.csv`.

Create `topic-map.csv` and use these preferred `candidate-report.csv` columns:

```text
candidate_id,candidate_title,source_ranges,topic_key,candidate_types,setup,turn,payoff,outsider_value,editorial_score,danmaku_signal,sc_signal,user_preference_adjustment,duplicate_risk,publish_tags,tag_evidence,vr_topic,decision,decision_reason
```

Generate candidates independently from semantic story islands, engagement peaks, SC steering chains, audible or permitted visual reactions, explicit editorial searches, and repeated user preferences. Merge overlapping proposals only when they share the same causal story and payoff. Do not let a keyword, silence detector, scene cut, chat peak, SC price, or model score select a clip alone. Treat low-energy spans as deletion suggestions; preserve hesitation, awkward pauses, delayed laughter, breaths, and silence when they carry timing or emotion.

The automated Flow 2 prompt must follow the same editorial order: first reject material that is only a topic, ordinary Q&A, fan-only affection, repeated information, or an isolated line without escalation or payoff; then reconstruct each eligible semantic story island; only then choose timestamps and author packaging. Engagement is navigation evidence, never eligibility. Give every hotspot/SC/keyword neighborhood at least 120 seconds before and 180 seconds after in the bounded context, merging overlap before charging the context budget. When the core beat is shorter than 90 seconds, explicitly search those neighboring rows backward to the earliest same-thread setup and forward through the last result, reaction, qualification, or true close. Keep only the same causal thread; if setup → turn/escalation → payoff still cannot be stated from source lines, reject rather than padding with another topic or making the title invent the story.

When video analysis is allowed, inspect deduplicated scene-aware keyframes only for eligible candidates. Align frames to transcript time, reject transition/loading/blur frames as covers, and use visuals to verify rather than invent implications. Skip this pass for audio-only requests.

Keep editorial scoring separate from user feedback. With every review batch, create a blank `clip-evaluation.csv` with these columns:

```text
source_id,clip_id,title,user_score,score_scale,user_verdict,user_notes,editorial_tags,topic_key,hook_type,title_style,duration_seconds,danmaku_signal,sc_signal
```

After the user returns ratings, preserve their scale verbatim and compare high- and low-rated clips by hook timing, topic, candidate type, duration, engagement signals, and title style. Use repeated preferences to rerank later videos; do not overfit one rating or silently rewrite the user's scores.

Summarize repeated ratings before ranking a later batch:

```powershell
python scripts/summarize_clip_feedback.py --input "batch-a/clip-evaluation.csv" "batch-b/clip-evaluation.csv" --output "feedback-profile"
```

Read both the ledger and profile. Apply only the emitted -1/0/+1 adjustment after at least three comparable rated examples. Check `topic_key`, event, payoff, and title angle against the ledger; a renamed or shortened version of the same event remains a duplicate.

Advance only 3–5 point narrative candidates. Reject 1–2 point material before rendering and do not retain, repair, extend, split, repackage, title, subtitle, or create cover assets for it. A 3-point candidate may receive only the shortest source context required to make its existing event and causal meaning complete; do not expand it into a different story. Prioritize 4–5 point candidates. User ratings override an earlier editorial estimate for future calibration.
Record every surfaced candidate in `candidate-report.csv` before rejecting it, including 1–2 point material and candidates left at review status. Give each rejected row a short audit-only `candidate_title` and `decision_reason`; this label exists only for the post-publish backlog report and is not permission to render, repair, package, or author publish-ready title/cover assets.


### 3. Author a non-contiguous edit plan

Use exactly these columns:

```text
slice_id,order,start_seconds,end_seconds,title,outline,hook,reason,keep
```

Each row is one retained source interval. Reuse `slice_id` for intervals that belong to the same final clip and number them with `order`. Intervals may be far apart; the exporter joins them in order. Omit 1–2 point candidates entirely from the retained plan and delivery outputs.

Cut on phrase and thought boundaries. Preserve enough setup for the payoff to make sense, but delete greetings, repetition, dead air, detours, housekeeping, and weak transitions. Never reorder words to manufacture a claim, hide a material qualification, or change the speaker's meaning. Treat every retained interval boundary as a review gate: listen at least three seconds on both sides in the source, keep connectors that still complete the current thought, and leave roughly 0.2–0.5 seconds of natural tail or silence after the final audible syllable. ASR segment ends and successful media decoding are not proof of a complete spoken cut.

For narrative and highlight review clips, the concatenated rendered duration must be 30–300 seconds. Calculate each slice's total retained duration before export and shorten or reject anything outside that band. Treat game dialogue, cutscene narration, menus, tutorial text, and read-aloud exposition as supporting evidence rather than material to preserve wholesale: retain only the minimum lines needed to understand the creator's reaction, escalation, and payoff, and remove redundant or low-energy narration.

Structure each retained clip as hook → minimum setup → escalation → payoff. When a chronological story needs too much setup, use a 3–15 second later reaction as a cold open, return to the earliest necessary setup, then finish on the complete payoff. Do not replay more than the brief teaser excerpt when the timeline catches up, and do not end before the final spoken thought or reaction is complete.

For every reverse edit, label each retained range in `reason` as `cold open`, `minimum setup`, `return`, or `payoff`. Use the sequence later 3–15 second hook → progressive Gaussian-blur transition → earliest minimum setup → progressive Gaussian-blur transition at the chronological return when present → complete payoff. Do not replay the full hook when the timeline catches up.

Apply the transition at every non-chronological time jump, not merely the first join. Ramp the complete composed frame from clear to Gaussian sigma 8–12 during the final 0.3 seconds before the cut, place maximum blur exactly on the edit boundary, then ramp back to clear during the first 0.3 seconds after the cut. Preserve the original audio without a fade. If the exporter only hard-concatenates ranges, post-process these boundaries before review; a reverse edit without rendered and visually verified transitions is incomplete. Do not blur ordinary chronological cuts.

Use the bundled post-processor after calculating every join on the final clip axis. It only runs the expensive blur filter inside the short transition windows and preserves the original audio stream:

```powershell
python scripts/apply_reverse_blur_transitions.py `
  --input "review.mp4" `
  --output "review-with-transitions.mp4" `
  --boundaries "18.29,127.39" `
  --radius 0.32 `
  --sigma 12
```

Extract frames immediately before, at, and after every listed boundary. Maximum blur must be visible at the exact join and both surrounding frames must return smoothly to clear.

Use callbacks or flashbacks when a later remark explicitly recalls, reframes, or pays off an earlier event. A valid structure is later cold open → earlier source event → later callback/payoff. Make the temporal jump understandable from the spoken context or an explicit edit note. Do not use non-chronological order merely to place unrelated jokes together.

For personality-led creator clips, prioritize independently legible entertainment: a confident claim colliding with reality, an accidental self-exposure, a failed attempt, mock-serious logic escalating into absurdity, social friction, or a reaction that confirms the bit landed. Treat “cute,” praise, competence, affection, and fan service as supporting texture, not sufficient payoffs. If a stranger must already like the creator for the clip to work, rank it below a coherent outsider-readable joke or reversal.

For Kioi specifically, also scan for compact, observable cute beats: a short vocalization, sudden playful delivery, stifled laugh or yelp, model/prop interaction, or another unplanned micro-reaction. These may qualify without a long reversal when the clip still contains the shortest outsider-readable setup, the visible or audible action, and its reaction/landing. Do not keep an isolated catchphrase or single sound that works only through existing fan affinity. The durable Chinese wording lives in `assets/creator-profiles.json` under Kioi `selection_notes` and is injected into generated selection prompts.

Validate before rendering:

```powershell
python scripts/content_slicer.py validate-plan --plan "slice-work/edit-plan.csv" --audio "recording.mp4" --gift-acks "slice-work/gift-acknowledgements.csv"
```

The validator must fail if a retained range still overlaps a gift acknowledgement. Record each final range end at the completed spoken thought. The exporter adds five seconds only after the final range of each slice; it does not pad callback or flashback joins. Subtitle slicing shifts both subtitle start and end 0.1 seconds later.

### 4. Apply the approved source timeline

Use the same recording file for both internal audio and video arguments. Do not substitute another audio file. Then run:

```powershell
python scripts/content_slicer.py export --audio "recording.mp4" --video "recording.mp4" --plan "slice-work/edit-plan.csv" --gift-acks "slice-work/gift-acknowledgements.csv" --output "final-slices"
```

The exporter keeps its internal `--audio` and `--video` arguments for media processing, but both must point to the same source recording. It renders each retained range and concatenates all ranges sharing a `slice_id`. Review `final-slices/slices.csv` and every exported clip.

### 5. Mandatory review gate before subtitle burn-in

For ordinary landscape narrative or highlight slices, first deliver the unsubtitled rendered MP4 and one matching editable `.ass` subtitle file in the same folder. Do not also deliver `.srt` unless the user explicitly requests it. Stop at this review stage and wait for the user's explicit approval of content completeness, cut boundaries, joins, wording, and subtitle timing. Do not burn ordinary landscape subtitles into the final video until that approval is received. A vertical livestream/radio layout is the exception: its right-hand subtitle panel is part of the composition, so follow the burned visual-review workflow in `references/vertical-radio-layout.md` and keep the matching editable ASS beside it. A burned radio review is still a draft until the user approves the content. When revising a rejected draft, replace abrupt micro-cuts with longer coherent ranges where possible and include the complete final spoken thought, reaction, or farewell before exporting the next review version.

Before handoff, play or decode every review clip and inspect its ASS at the beginning, middle, and end. Confirm that the displayed wording matches the audible speaker, the timing follows the final exported clip rather than the source recording, no line begins or ends on a clipped syllable, and no continuous English token wraps between characters. For joined clips, verify at least one subtitle on each side of every edit boundary.
For Chinese narrative subtitles, require Simplified Chinese before burn-in. Run a deterministic Traditional-to-Simplified comparison such as OpenCC `t2s` on every reviewed ASS; any change blocks delivery until the ASS is converted and re-reviewed. If Traditional text was already burned into a draft, rebuild from the unsubtitled review or radio-layout master—never burn corrected Simplified text over an already subtitled MP4. Keep timing, styles, speaker names, and effects unchanged during text conversion, then fully decode the rebuilt video.
Run `scripts/audit_media_subtitles.py` against the final MP4 and matching ASS. For narrative clips, require clip-local ASR JSON. Any subtitle with no audible anchor, any retained spoken line missing from the remapped axis, or any last cue colliding with the media edge blocks burn-in.

Before that audit, clamp the reviewed ASS to leave a clean media tail, then require a clip-local speech anchor near every remaining cue:

```powershell
python scripts/clamp_ass_to_media.py --media "review.mp4" --ass "review.ass" --tail 0.25
python scripts/audit_media_subtitles.py `
  --media "review.mp4" `
  --ass "review.ass" `
  --asr-json "work/clip-local/transcript.json" `
  --speech-json "work/clip-local/speech-activity.json" `
  --min-tail 0.2
```

If clip-local Whisper collapses many seconds into one giant subtitle segment, do not ship that raw ASS. Re-transcribe the final audio in short chunks or rebuild granular cues with `scripts/build_review_ass.py`, correct the wording by listening, and rerun the same final-media audit.

If source durations differ by more than one second, do not guess the offset. Establish synchronization first or ask the user for an aligned source. Use `--timeline-tolerance` only for a known harmless container-duration difference.

### 6. Per-video hotword review

After producing the review clips for each source video, also deliver one
`video-hotword-candidates.txt` in the review root. Do not place a separate list
beside every clip. Build the list from names, nicknames, locations, organizations,
recurring topics, memes, English terms, numbers, and domain terms that were
actually heard in the source or needed for clip-local ASR.

Use three concise sections:

```text
本次临时热词：
词语｜类别｜出现位置或证据

建议加入常驻：
词语｜理由

已在常驻词库：
词语
```

Verify every candidate against the audio and corrected subtitles. Exclude ASR
hallucinations, ordinary function words, sensitive personal information, and
one-off phrases with no likely reuse. Compare candidates with
`references/general-hotwords.json` and `references/virtuareal-glossary.json` so already-persistent terms are not
presented as new promotions. Do not modify the persistent glossary automatically;
wait for the user to select terms. After explicit selection, add only those terms,
update `updated_at`, validate the glossary, and report the new unique-hotword count.

## Lyrics-first multilingual song cutting

Transcribe the source recording's own embedded audio before running any speaker or music detector:

```powershell
python scripts/content_slicer.py transcribe-multilingual --input "concert.mp4" --output "lyrics-work" --languages zh,ja,en --chunk-seconds 60
```

Read the complete multilingual transcript chronologically. Identify repeated choruses, announced titles, distinctive lyric phrases, and continuous lyric-heavy regions. Run `scripts/find_songlike_transcript.py` only as a high-recall candidate scan; never treat its rows as authoritative song boundaries. Search transcript and XML context for spoken or on-screen leads such as 唱歌、唱一首、来一首、我们唱歌吧、歌名、下一首 and 歌名没换, then inspect the surrounding source video for title-overlay changes and continuous accompaniment. Match a song from several distinctive lines rather than a single short phrase. Treat transcript timestamps as the vocal core only. Inspect the original audio around every core and expand to musical silence or accompaniment changes so the intro, interlude, and ending remain. Split adjacent songs at intervening speech, silence, applause, or an accompaniment reset. Record every discovered attempt, accepted or rejected, with reason in CSV before exporting from the source recording's own picture and embedded audio. A title overlay is identity evidence, not boundary authority; when the creator says the displayed title was not changed, trust the heard performance and later corroboration instead.
Detect abandoned starts and restarts before choosing boundaries: repeated opening lyrics, a repeated title announcement, an accompaniment reset, or spoken 重来/再来/错了/等一下 creates a new attempt. Select one continuous complete attempt when available. If only a partial take exists, label every asset and publishing field as 片段、一段, or 短版; never let lyrics from the abandoned take continue across the restart.

Before final song burn-in, verify the live arrangement and tail against the candidate audio rather than trusting studio LRC timing. Reference LRC/KRC/YRC timestamps are text-and-order hints only and must never become the final live-performance axis. Drop unsung or reordered studio lines and retime tempo-changed passages to the performance. Run no-VAD multilingual word-timestamp ASR against the exact final unburned render and inspect a tail waveform past the proposed end. Place the boundary after the last sung syllable and musical release but before the first post-song speech. Reject a master whose final lyric event exceeds the video or whose candidate contains an unreviewed restart, repeated opening, or speech immediately after the selected cut. Do not hide a wrong boundary with a generic fade-out.

The live-performance lyric-axis gate is mandatory. Run `scripts/retime_reviewed_lyrics_from_asr.py` against reviewed lyric text and the final-render ASR. For every song require reliable line-anchor coverage of at least `0.30`, anchor MAD no greater than `1.20 s`, at least `max(3, ceil(20% of lyric lines))` reliable anchors, and at least one reliable anchor in each third of the song. A reliable automatic anchor must match the first lyric unit and belong to the highest-scoring monotonic anchor chain across the whole song; independent repeated-chorus matches cannot validate one another. Preserve every reliable vocal onset exactly; if adjacent cues overlap, shorten the earlier cue instead of delaying the next sung line. Iterate the retiming check to convergence and block the song when any additional applied adjustment or reliable-anchor residual exceeds `0.45 s`. If the automatic gate lacks anchors, create or correct a manual performance-anchor sheet and rerun it; never weaken the thresholds to make a song pass.

Build the corrected ASS from the converged performance axis and burn it onto the unburned master exactly once. Never overlay corrected subtitles onto an MP4 that already contains subtitles. Before preview or upload, run `scripts/audit_song_publish_gate.py` for the final MP4, matching ASS, timing report, and clip id. It writes a same-stem `.song-timing-gate.json` whose PASS state is bound to the exact MP4 and ASS SHA-256 hashes. `scripts/biliup_publish.py` must block every `content_type=song` row when this sidecar is absent, failed, or invalidated by any later video/ASS change.

Never burn raw ASR lyrics. Generate a review sheet, correct every wrong lyric against an authorized or user-supplied lyric source and the recording, mark every row reviewed, then burn the corrected file:

```powershell
python scripts/song_lyrics_renderer.py template --srt-dir "song-work/recognized-subtitles" --output "song-work/lyric-corrections.csv"
# Fill corrected_text and set reviewed=yes for every row.
python scripts/song_lyrics_renderer.py burn --clips-dir "songs/clips" --lyrics "song-work/lyric-corrections.csv" --output "songs-with-lyrics" --ffmpeg "ffmpeg.exe"
```

The burner refuses empty or unreviewed lyrics and creates the creator-profile-colored ASS and subtitled MP4 only from the reviewed timing sheet. XML/danmaku lyric hints produced by `scripts/xml_lyric_hints.py` are discovery evidence only: inspect every row, correct it against the recording, and set `reviewed=yes`; never burn the hint output directly.

## Subtitle profiles

Keep narrative-slice and song-cut typography separate:

- Narrative and highlight slices use the reviewed v2 style in `assets/subtitles/narrative-v2.ass`: `Smiley Sans Oblique` from `assets/fonts/smiley-sans-v2.0.1/SmileySans-Oblique.otf`, size 70, bold, outline 3.5, shadow 3, and bottom margin 72. The single-speaker/柚雨 style uses restored ice-mist light-blue fill `#BFDFF4` with deep blue-gray outline `#334A60`; do not use the former dark fill `#2497C5` or a purple-looking palette. 昼夜 Chilly must use `#9B88B4` as the actual subtitle fill (ASS `&H00B4889B`) with a dark plum-gray outline; never substitute white fill with a `#9B88B4` outline. 雾深 Girimi uses white fill with deep-blue outline `#4F5F9A`, but only for dialogue verified to be spoken by 雾深; other creator profiles retain their reviewed role-color treatment. Do not use AR WeiBeiGBStd for narrative slices.
- When two people speak simultaneously, keep 柚雨 on the lower line and the other speaker on the upper line. Every overlap/high-position style must inherit the complete current base speaker style—font, size, boldness, fill, outline, border thickness, and shadow—and may differ only in its style name and vertical margin. Use margin 72 for the lower line and 164 for the upper line. After the user changes a base style in Aegisub, resync its overlap counterpart before burn-in; never leave an overlap style on an older template.
- Match dialogue style to the reviewed `Name` field before burn-in. A cue named `柚雨` must use the Kioi family and a cue named `雾深` must use the Girimi family. Detect actual time overlaps after speaker corrections and assign both cues their corresponding overlap styles so they cannot cover each other.
- Song cuts use `AR WeiBeiGBStd BD` (AR WeiBeiGBStd) for Chinese-only lines. Any line containing Japanese hiragana or katakana must override the lyric font to bundled `Yuji Syuku` from `assets/fonts/yuji-syuku/YujiSyuku-Regular.ttf`, and the burner must pass `--fontsdir assets/fonts/yuji-syuku`; do not depend on system fallback. Use the creator profile palette with a sufficiently dark outline and shadow so it survives bright and dark footage.
- Make Aegisub/ASS native unit-level reveal the default for every song cut. Keep the whole line in a stable layout and future units fully transparent. At each verified unit start, fade it smoothly from transparent to opaque over roughly 180-240 ms; keep its position and scale fixed. Do not use overshoot, bounce, per-unit movement, or PowerPoint-rendered animation. Chinese characters and Japanese kana/kanji may reveal one glyph at a time; continuous English words, abbreviations, and numbers remain atomic. Store the measured unit axis in the reviewed CSV `timed_units` field and let `scripts/make_word_fade_ass.py` consume it. Never distribute a line duration evenly across characters and call that rhythm synchronization; when no verified unit axis exists, use a whole-line smooth fade or stop for manual alignment.
- Treat every continuous English word, abbreviation, and number as an atomic wrapping unit. Never split inside tokens such as `call`, `Super Chat`, `KTV`, or `VR`.

Require a file-backed font that Aegisub/libass/FFmpeg can load. If AR WeiBeiGBStd is visible only inside an application, do not accept a system fallback and do not render it through PowerPoint; request the font file or an approved substitute.

## Vertical livestream and radio layout

Classify the source before creating user-facing review files. The workbench automatically selects this route when ffprobe reports portrait or square display dimensions—including a 90°/270° rotation tag—or the source name contains 电台/radio. Ordinary landscape remains on the normal layout. For a 9:16 livestream or
any radio-style source with little useful motion, read `references/vertical-radio-layout.md`
and route every approved clip through that workflow. Use its 1920×1080
left-original/right-information template, role-color treatment, subtitle placement,
three-bundle publishing-copy format, and title/cover-mode validation rules. Run
`scripts/render_vertical_radio_layout.py` instead of rebuilding the filter graph by hand.

Treat raw portrait clips and unsubtitled layout MP4s as internal intermediates. Do not expose
them beside the radio review and do not make an optional `vertical-layout` sibling that can
be mistaken for the final delivery. The user-facing radio review must contain only the
template-composed MP4 with its subtitles visibly burned into the right panel, plus the
same-stem editable ASS. Read dialogue_font, dialogue_font_size, radio_subtitle_size, and role_color from the selected creator profile; 65 remains the default radio size but a reviewed per-person template may override it. Verify the ASS style and visible burned result; a blank right panel, a style that ignores the profile, or a plain portrait/standard-layout duplicate fails delivery.

## Recoverable automation for new recordings

When the user wants future recordings processed repeatedly, read
`references/recording-automation.md` and use `scripts/recording_automation.py` instead of
reconstructing the pipeline in each conversation. The program owns discovery, file-stability
checks, transcription, danmaku/SC analysis, rating aggregation, export, clip-local ASR,
subtitle burn-in, cover rendering, audits, preview, locks, redacted logs, and resumable state.

When the user wants to launch and control Flow 0 plus the five editorial stages from a local desktop application,
read [references/local-workflow-app.md](references/local-workflow-app.md) and use the
workspace-root `切片工作台.cmd`. Manual projects treat the imported three-field selection JSON as the editorial decision and keep human review plus exact preview confirmation. Narrative/song files are arrays; mixed files contain “普通切片” and “歌切” arrays, while every item still has only title, subtitle, and timestamps.
The desktop has a third 切片审核台 tab. Its default global pool scans every project batch and lists all `pending` and `revise` clips across creators and sessions, while each decision remains in that batch's `review-decisions.json`. Use the embedded VLC preview and the two-track editor for fast review: the upper track shows retained video segments over the asynchronously generated waveform, and the lower track shows editable ASS cue blocks. `Space` toggles play/pause; the mouse wheel pans the timeline horizontally, while `Ctrl+wheel` zooms at the pointer; `C` splits at the playhead; clicking an upper segment then pressing `Delete` performs ripple deletion and closes the gap; `Ctrl+Z` restores the prior cut, deletion, or subtitle edit. Show every ASS cue as an independent selectable lower-track block. Drag its center to move the whole cue without changing duration, or drag either edge to resize its duration; keep continuous English words atomic. Ripple deletion must remove, clip, or shift ASS cues by the same edited-time interval. Persist video edits as a pending non-destructive draft, retain a source backup when applying, and block Flow 4 while a draft is unapplied. The built-in timeline is the primary review surface; external ASS editors are optional and must open the same clip-local media/ASS pair without adding source-time padding. Flow 4 accepts only `approved`; `revise` blocks, while `skipped` keeps the files but excludes them from burn and upload.

For an unattended new-recording queue or an explicit historical time-range batch, use
`scripts/workflow_auto.py`: baseline existing media by default; group BililiveRecorder
reconnect fragments by folder, room, date, title, and bounded end-to-next-start gap. Before
Flow 0, enforce a reconnect-observation grace period (default 900 seconds). Any newer media
activity from the same recorder folder, room, and recording date resets the grace period,
including a fragment that is not stable enough to probe, is still growing, or has a refreshed
title. Persist these waits in queue state and show them in the desktop; do not probe, transcribe,
or invoke Codex for that session until the whole activity group has stayed idle for the grace
period. Flow 0 then recursively scans stable `.flv` and `.mp4` files, probes every fragment
independently, records zero-byte or undecodable fragments without moving or deleting the
source, and excludes only the rejected signatures. It then materializes one continuous source and offset XML timeline
from accepted fragments and writes `continuity/preflight-report.json`; Flow 1 starts only after
this succeeds. Then invoke an installed,
authenticated Codex CLI through ephemeral read-only `codex exec` with an output schema.
A time-range batch includes every stable complete session whose duration intersects the
inclusive local start/end range, reuses existing job ids, and must not make unrelated
baseline files appear new to the watcher. Preserve the application review and preview
digests and do not reconstruct deterministic stages in chat. Persist each automatic job's
current stage and monotonic progress percentage in queue state; the desktop must expose every
job in an auto-refreshing task table rather than requiring log inspection. Expose Flow 0
progress and the invalid-fragment count in that UI. If a fragment signature changes, probe it
again; if the accepted manifest changes, invalidate downstream Flow 1–5 outputs before reuse.

Whenever a user selects one recording manually, automatically bind its single non-empty
same-stem Bilibili XML when present and show the binding in the desktop UI. Preserve a
manually chosen XML, and leave the field unresolved when no usable match exists or multiple
prefix matches are ambiguous. Automatic session manifests apply the same rule per reconnect
fragment before their XML timelines are merged.

Unattended burn and upload are separate, session-scoped desktop opt-ins. Machine-only burn
may proceed for narrative clips only when every subtitle/VAD QA row is already resolved and
all original audits pass; unresolved narrative QA and every unreviewed song lyric row pause
at `awaiting_delivery_review`. Automatic upload requires the exact per-launch authorization,
still runs preview first, and keeps the existing content-bound digest, five-item limit,
collection add, and read-back gates. A completed session that later receives another fragment
must pause as `late_segment` instead of republishing.

For narrative Flow 4, shorten only the silent lead and tail of each existing cue toward the already-generated independent `speech-activity.json` before the final subtitle audit. Preserve the approved subtitle text. Never silently remove a cue with insufficient speech overlap or relax the audit thresholds: leave it unresolved so delivery blocks until an explicit retime, restore, split, or removal decision is recorded. Apply this gate again after manual ASS edits, because approved wording does not prove that edited timestamps still match audible speech.

Keep model or human attention for semantic candidate choice, transcript correction,
title/cover judgment, and the final publication decision. The three approvals are
content-bound digests: `editorial`, `delivery`, and `publish`. If an approved plan, ASS,
title CSV, cover, video, or publishing configuration changes, the old approval must become
invalid. Never bypass these gates merely because `watch` is running.

The first init must baseline existing recordings unless the user explicitly asks to backfill them. In the desktop queue, first match the room id against assets/creator-profiles.json. If a new numeric room id has no profile, create one stable room_<room_id> draft using the immediate recorder-folder name when available, assign a unique theme color, and continue Flow 0–4. The draft must have upload.enabled=false; do not invent a Bilibili description, VirtuaReal affiliation, tags, or collection and do not publish until the user completes the profile. The 人物与字幕模板 editor may archive an unused profile after checking that no unfinished manual or automatic task references it. Archiving hides the person from recognition and selection, disables publishing, preserves all profile data and media, and prevents the same room from being auto-added again; saving the same key manually restores it. A roomless or ambiguous source still pauses at needs_creator; never apply another person's subtitle or cover profile. Keep state, logs, drafts, and approvals under the automation work root, never in the flat delivery folder. The Cookie remains in private local application data outside OneDrive and is read only for an explicitly approved execution.

## Local delivery folders

For every locally deliverable batch, read `references/local-cover-and-publishing.md` and
`assets/creator-profiles.json`. Treat the profile as the single source of truth for creator
name, role color, dialogue font, ordinary/radio subtitle sizes, and hotwords. The desktop 人物与字幕模板 editor is the normal manual-add/edit entrypoint, and auto-discovered room profiles use the same file. Ignore upload-only fields during ordinary local-only delivery. Do not hand-copy colors into one-off render commands; Viridis must remain #8EB056, and 哎小呜 must remain #F9A699.

For explicitly requested publishing, build every video description from the selected creator
profile. Treat the row `description`, or CLI `--description` when the row is empty, as an
optional one- or two-sentence clip-specific lead. Append one blank line and that profile's
complete `upload.description` as the mandatory fixed block. If the lead already contains the
complete fixed block, keep it once instead of duplicating it. Never let a custom lead replace
the creator block, reuse another creator's wording, mix profile links, or paraphrase the
supplied copy. Keep `assets/creator-profiles.json` as the only source of the fixed text: Kioi,
Sumire, Viridis, Yuchu, Komichi, and Chilly each have a distinct block. Newly added profiles, including 哎小呜 until her links and copy are supplied, remain non-publishable drafts.

Use each profile's fixed `upload.tags` as authoritative. Merge those fixed profile tags first,
then require the approved row's 3–5 evidence-backed clip-specific tags; do not silently publish
with profile defaults alone. Keep `VirtuaReal` first only for profiles that actually belong to
VirtuaReal. Chilly is not a VirtuaReal or VR streamer.

Route every submission through the selected profile's `upload.collections` mapping. Ordinary
clips go to the creator's Chinese-only `XX切片` collection and song cuts go to the
Chinese-only `XX歌` collection. Derive `XX` from the creator's Chinese name only; never
include romanized or English suffixes such as Kioi, SUMIRE, Viridis, or chu2u. Display the
collection name, `season_id`, and `section_id` in preview; a new profile may preview the
intended title with empty IDs without connecting to Bilibili. Only after explicit upload
confirmation, resolve an exact same-title collection or create it from the approved clip
cover, read back its positive `season_id` and `section_id`, and persist them in the profile.
Always prepare `XX切片` first for a new creator, even for a song-only first batch, then prepare
`XX歌` when the batch contains song cuts. Refuse duplicate exact titles, incomplete sections,
or any item still missing a verified route before publishing. `season_id` is submission
metadata only and is not proof that the video entered the collection.

Use `scripts/local_publish.py render-local` to render deterministic 1920x1080 covers
directly beside their matching videos. For each approved clip, keep the MP4, same-stem ASS,
and `<video-stem>-cover.jpg` in one flat delivery folder. Keep `titles-and-covers.csv` there
for human review. Do not create a publishing manifest, upload state, category-id sheet,
credential template, BV/AV record, or other API-convenience artifact unless the user later
explicitly requests API publishing.

When the user explicitly requests Bilibili submission, read
`references/biliup-publishing.md` and use `scripts/biliup_publish.py`. Set
`content_type=song` for song cuts (Music General, `tid=130`); choose a suitable current
category for other clips, falling back to Animation General (`tid=27`) when uncertain.
A positive row `tid` overrides routing. Preview without `--execute` first. Treat each row as
one independent submission, not one part of a multi-P submission. After each successful
upload, use the returned BV id to obtain the exact aid/cid/title. Use the `web` submit channel
by default; the `app` channel can return Bilibili code 21566 after the media upload even with
current biliup, so do not retry it blindly. When the web submit channel prints no BV id,
poll the authenticated recent-archive list for up to 21 seconds and accept
only a title-exact item created no earlier than two minutes before this upload began. Add that
exact video through the collection section endpoint, then read the section back and require
the aid to be present. Do not mark the row submitted, start cooldown, or continue after an
unrecoverable BV id, title mismatch, collection API failure, or failed read-back. Re-running
this step must be idempotent: skip the add when the aid is already present and still perform
the read-back.
After the final successful upload of a high-score batch and verified collection read-back, do not stop with BV ids. Read the mandatory post-publish procedure in `references/biliup-publishing.md`, compare the complete candidate inventory against final delivery and successful uploads, and report every same-batch candidate that was not made. Group the list by source date and creator, include the audit title, content type, score/status, and rejection reason, preserve duplicate instances from different sessions, and state both instance and unique-title counts. Put selected-but-held items such as clips mentioning other creators in a separate `已入选但暂缓投稿` section; never mislabel them as unselected. If none remain, explicitly say so.


Do not request, display, or accept Cookie values in chat. Use the private `cookies.json` for
upload and the private `web-cookies.json` for collection add/read-back. If either credential
is missing during execution, pause at the script's local interface so the user can fill or
import it privately. Keep both credential files outside OneDrive, the skill, every delivery
folder, Git, logs, and shared ZIPs. Submit at most five independent videos in one batch. Do
not add an artificial per-item delay inside an approved batch; after a non-final batch has
five verified submissions, wait a full ten minutes before starting the next batch.

When the user requests OneDrive archiving and no retained local raw, move only sources already represented in a verified source manifest and only after every derivative passes final delivery audit. Preserve the raw and matching XML together. Set the destination to online-only, then verify OneDrive reports the file as offline/online-only before claiming disk space was released. A file merely moved under the OneDrive path or marked unpinned can still occupy local extents while upload is pending. Never delete the original or a pending cloud copy to force space recovery; report the queued state and continue monitoring instead.

## Deliverables

For narrative slicing, retain:

- `transcript.csv` and `transcript.srt`;
- a whole-recording outline and ranked candidate list;
- `topic-map.csv` and `candidate-report.csv`, including proposal sources, narrative roles, signal evidence, preference adjustment, duplicate risk, and decision;
- when prior rated batches exist, `feedback-ledger.csv` and `feedback-profile.json` in the working audit folder;
- the completed `edit-plan.csv`, including reasons for retained ranges;
- `slices.csv` and the rendered MP4 files.
- for ordinary landscape slices, the pre-burn review MP4 and matching editable ASS; for vertical/radio slices, the burned template review MP4 and same-stem editable ASS, submitted for approval.
- one source-level `clip-evaluation.csv` for the user's ratings and notes.
- one source-level `video-hotword-candidates.txt` for the user's promotion review.
- when engagement data exists, the generated `engagement-hotspots.csv`, `engagement-summary.json`, `timeline-audit.json`, reviewed `superchats.csv`, and relevant scale-specific window tables.
- one local 1920x1080 AutoClip-style `<video-stem>-cover.jpg` beside each approved MP4 and its same-stem ASS;
- one human-readable `titles-and-covers.csv` in that same flat delivery folder;
- no publishing manifest, upload metadata, original-frame PNG, or prompt file unless the user explicitly requests that separate artifact.

## Local covers and optional prompt handoff

Local deterministic covers are the default for locally deliverable batches. Read
`references/local-cover-and-publishing.md` and render them with
`scripts/local_publish.py render-local`. Write every cover directly beside its matching
video; do not create a separate covers directory. Narrative covers must reproduce AutoClip
`generate_covers.py` `style1` exactly: ungraded video frame, centered 16:9 crop,
1920x1080 JPEG, MaoKenShiJinHei, white main text at 20% height, yellow supporting text
at 75% height, and the original 12-pass black MaxFilter stroke. Do not add a custom
vignette, gradient, blur, exposure change, accent bar, creator tag, card, panel, or
subject cutout.
Treat a final clip as multi-person only when verified speech from another person remains audible in that final clip. A collaboration room title, another character on screen, a guest mentioned in dialogue, or guest speech elsewhere in the recording must never activate multi-person handling by itself. If the final clip contains only the target creator's audible speech, handle it as an ordinary single-person clip: render the normal AutoClip frame-and-text cover, keep it eligible for upload, and do not add a guest emote. When verified guest dialogue does remain audible, paste the reviewed guest-character emote between the source frame and cover text and apply the multi-speaker subtitle rules.


For Sumire, Kioi, Viridis, Yuchu, and Chilly song cuts, use `cover_mode=song`. The selected creator
profile supplies the bundled background, fixed prefix, and vertical position. Use the
background unchanged as the sole image and draw exactly one centered yellow,
black-outlined line at 78% height so it stays below the face. The exact formats are
`【枝堇歌】歌名`, `【柚雨歌】歌名`, `【松绿歌】歌名`, `【羽啾歌】歌名`, and
`【昼夜歌】歌名`; use the complete song title and
do not add a subtitle or second line. `cover_mode=viridis-song` remains accepted only as
a legacy migration alias.

Yuchu narrative cuts follow the ordinary narrative rule: use an ungraded frame extracted
from the delivered clip as the AutoClip background. Only Yuchu song cuts use the bundled
`covers/yuchu-base.png` background with the one-line `【羽啾歌】歌名` format at 78%
height. Inspect both layouts at 1920x1080 and 320x180 and reject any result that covers
the face.

Create exactly one `cover-prompts.txt` for the entire source/review batch only when the
user explicitly requests prompt or ImageGen handoff. Never create separate per-clip
Prompt TXT files. Do not provide original-frame screenshots during ordinary delivery.
When prompt handoff is explicitly requested, provide one original-frame screenshot per
approved video because each narrative clip needs its own source reference. Cover-mode labels may still
organize prompt-only handoff, but they must not change the deterministic AutoClip local
layout.

For an explicitly requested prompt handoff, choose the narrative screenshot from the delivered clip itself. First scan the matching
ASS for the main punchline or reaction, inspect several nearby frames, and select the
clearest low-blur frame. Save the unedited screenshot as a PNG such as
`001-cover-reference.png`. The screenshot is the actual AutoClip background, not an
expression reference for a custom local composition.
Each prompt must assume the user will separately upload one or more relevant character images plus the supplied frame screenshot to the image-generation chat. State that the character images define identity, appearance, clothing, and visual style, while the screenshot defines the desired expression, emotional beat, pose, and approximate composition. If an uploaded image contains text, UI, or background elements, use only the intended reference information unless the user says otherwise.

Each prompt must include:

- explicit roles for the user-supplied images as character identity, appearance, clothing, expression, or style references;
- the selected `cover_mode` and the exact one- or two-line Chinese cover copy, quoted verbatim;
- a punchy livestream-thumbnail composition with either a sharp expressive close-up, an original evidence panel plus reaction, or a clear versus layout;
- very large yellow/white headline typography with a thick dark outline and optional red consequence accent; keep the copy readable at small feed size;
- a scene-derived or complementary background with enough separation for the subject and words; use blur only where it improves depth, and explicitly forbid a solid-black or black-screen background;
- a 16:9 canvas whose complete Chinese text remains inside the centered 4:3 safe area, so a centered 4:3 crop loses no characters;
- constraints against misspelled Chinese, cropped text, extra words, watermarks, and replacing or redesigning the character.

When prompt handoff was requested, place the single `cover-prompts.txt` beside the review media and requested reference PNGs. Verify that the number of labeled prompt sections equals the number of approved videos and that no other `*Prompt*.txt` files exist in the delivery folder. Do not invoke an image-generation tool merely because cover prompts and reference frames are requested.

For song cutting, retain `segments.csv`, the candidate clips, recognized SRT, `lyric-corrections.csv`, and generated ASS files when lyrics are burned.

## Guardrails

- Process media only when the user has permission.
- Never delete or overwrite source recordings.
- Keep source timestamps auditable in CSV files.
- Treat transcription as a draft; verify names, numbers, and ambiguous speech against the audio.
- Avoid decontextualizing sensitive statements. Keep necessary qualifications and attribute speakers correctly.
- Prefer fewer strong, coherent clips over many weak fragments.
- Review joins for clipped words, abrupt breaths, audio clicks, and logical discontinuity.
- Never run a biliup upload from an unreviewed plan. Require the user's explicit approval of the displayed title, cover, collection, category, copyright declaration, source, and tags immediately before `--execute`.
- A successful upload response is not a successful publishing row until the intended collection section has been read back and contains the exact uploaded aid.
- For automated runs, accept only a current `publish` approval whose digest still matches every delivery asset and publishing field; file changes invalidate approval.

