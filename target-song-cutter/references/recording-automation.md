# Recoverable recording automation

Read this reference when the user wants new recordings discovered and processed repeatedly,
or wants deterministic media work moved out of conversation. The automation is a local
state machine, not an autonomous editorial authority.

## What the program automates

`scripts/recording_automation.py` handles:

- recursive discovery of stable new recordings;
- matching same-stem Bilibili-live XML when available;
- source transcription and gift-acknowledgement filtering;
- lag-aware danmaku, burst, repetition, keyword, and Super Chat analysis;
- cautious aggregation of prior clip ratings;
- edit-plan validation and non-contiguous review export;
- clip-local ASR, ASS draft generation, subtitle timing audit, and burn-in;
- same-folder MP4, ASS, cover, and title CSV delivery;
- final delivery audit, biliup preview, locks, redacted logs, retry, and durable state.

Do not spend model tokens repeating these commands. Spend semantic review on deciding which
complete stories are worth keeping, checking names and ambiguous speech, improving subtitles,
writing high-curiosity but truthful titles and cover copy, and approving the final release.

## One-time configuration

The workspace launcher prefers the bundled Python 3.12 runtime:

~~~powershell
recording-automation.cmd doctor
~~~

`recording-automation.json` is shareable and contains no credentials. Configure:

- `watch_roots`: folders receiving new recordings;
- `stable_seconds`: minimum time since the last file write;
- `creator_rules`: path fragments mapped to an existing creator profile;
- `asr`: model, language, device, and compute type;
- `editorial.keywords`: discovery terms, including innuendo and creator commentary;
- `editorial.feedback_globs`: prior rating CSVs;
- `publish.tid`: fallback Animation General tid, currently configured as `27`;
- `publish.song_tid`: Music General tid for song rows, currently configured as `130`;
- `publish.copyright` and `publish.source`: an explicit rights decision;
- `cookie_file`: private local path only, normally under `%LOCALAPPDATA%`.

The reviewed `titles-and-covers.csv` contains `content_type` and an optional row `tid`.
`content_type=song` or `music` selects `publish.song_tid`; a positive row `tid` overrides
automatic routing for a more specific category. Other or uncertain clips use `publish.tid`,
the Animation General fallback. Reconfirm platform category ids when Bilibili changes its
submission tree. The flat delivery folder must not contain state, logs, approval files,
credentials, upload manifests, or returned platform ids.

Create the private Cookie template only after explicit user approval:

~~~powershell
python target-song-cutter\scripts\biliup_publish.py credentials --create-template
~~~

The user fills `SESSDATA` and `bili_jct` in that local file. Never read their values into
chat. `doctor` may report only the status.

## Initialize without reprocessing old material

The safe first run records every existing media file as a baseline:

~~~powershell
recording-automation.cmd init
~~~

Keep `initial_scan` as `ignore-existing` unless the user explicitly requests a historical
backfill. Initialization is durable. Do not use `--force` on an existing state file unless
the user explicitly wants to replace that state.

Then either run on demand:

~~~powershell
recording-automation.cmd scan
recording-automation.cmd run
recording-automation.cmd status
~~~

or keep a dedicated local terminal running:

~~~powershell
recording-automation.cmd watch
~~~

The watcher never grants review approval. It stops at each gate and can resume after a
restart.

## States and gates

A normal job moves through:

~~~text
discovered
  -> awaiting_editorial
  -> awaiting_delivery_review
  -> delivery_ready or awaiting_publish
  -> published
~~~

`needs_creator` means no creator rule matched. Assign a reviewed profile:

~~~powershell
recording-automation.cmd assign-creator --job JOB_ID --creator sumire
~~~

`failed` stores the last durable stage and a redacted log. After fixing the cause:

~~~powershell
recording-automation.cmd retry --job JOB_ID
recording-automation.cmd run --job JOB_ID
~~~

### Editorial approval

Automated analysis writes `review-packet.md` and an empty or draft `edit-plan.csv`. Review
the source, transcript, engagement hotspots, SC cause/response chains, and feedback profile.
Keep complete setup-to-payoff stories and avoid duplicate events. Fill at least one row with
`keep=1`, then approve:

~~~powershell
recording-automation.cmd approve --job JOB_ID --stage editorial
recording-automation.cmd run --job JOB_ID
~~~

### Delivery approval

The program exports review clips, creates clip-local ASR and ASS drafts, and writes
`anchor-qa.csv` plus `titles-and-covers.csv`. Correct the ASS against audio, mark every anchor
QA row verified, and review each title, `content_type`, optional row `tid`, cover instruction,
description, and either the five model-authored tags or 1–5 reviewer-authored evidence-backed tags created during candidate
selection. Run `validate_selection_tags.py`; fixed profile tags are merged first but never
replace row-specific tags. Keep `VirtuaReal` first only for VirtuaReal profiles; Chilly is
not a VirtuaReal or VR streamer. Then approve:

~~~powershell
recording-automation.cmd approve --job JOB_ID --stage delivery
recording-automation.cmd run --job JOB_ID
~~~

The program copies reviewed ASS into a packaging area before clamping, so it does not mutate
the reviewed master. It burns subtitles, renders same-folder covers, and runs final audits.

### Publishing approval

If one-time publishing metadata is complete, the program runs a local preview without
reading Cookie data. Review video, cover, title, category, rights declaration, source, tags,
and description. Approve only that exact delivery:

~~~powershell
recording-automation.cmd approve --job JOB_ID --stage publish
recording-automation.cmd run --job JOB_ID
~~~

Every approval stores a digest of its inputs. Editing `edit-plan.csv`, ASS, anchor QA,
`titles-and-covers.csv`, MP4, cover, or publishing configuration invalidates the matching
approval. Re-review and approve again. This prevents changed-after-review content from being
published.

## Recovery and logging

The state file is written atomically and a lock prevents concurrent workers. Each external
stage writes one log under the job work folder. Logs redact common credential names and show
the Cookie path as `<cookie-file>`. Do not move logs into delivery or share them blindly.

The program never deletes or overwrites source recordings. It can overwrite reproducible
draft/render outputs only while resuming the same job; reviewed inputs are copied before
packaging. Source archival remains a separate verified workflow.

## Desktop unattended queue

The desktop workbench now adds `scripts/workflow_auto.py` for the user's `录播` directory.
Keep this separate from the legacy single-file `recording_automation.py` state so existing
jobs remain recoverable. The desktop queue runs explicit Flow 0 before Flow 1. It recursively discovers stable FLV
and MP4 files, probes every fragment independently, records zero-byte or undecodable files
without moving or deleting them, and groups only accepted BililiveRecorder reconnect parts.
Flow 0 produces a continuous source, offset XML, and `continuity/preflight-report.json`; the
queue then reuses `workflow_app_core.py` for Flow 1–5. Manual video selection uses the same
non-empty same-stem XML resolver, so the user does not have to select the matching danmaku twice.

The desktop queue resolves creators dynamically from assets/creator-profiles.json. For a
previously unseen numeric room id, auto_add_creators=true creates one stable, non-publishable
room_<room_id> draft and lets local Flow 0–4 continue. Prefer an immediate recorder folder
named roomid-creator to seed the display name. The draft has a unique color, default
Microsoft YaHei dialogue font, ordinary size 70, radio size 65, and upload.enabled=false.
Roomless or ambiguous media still stays at needs_creator. The desktop “人物与字幕模板”
editor is the manual create/edit path; do not invent platform links, affiliation, fixed copy,
tags, or collections for an automatic draft.
For explicit historical work, use the desktop time-range batch instead of toggling a broad
permanent backfill. It selects stable sessions whose complete duration intersects the
inclusive local range, includes every reconnect fragment in those sessions, reuses existing
jobs, and baselines unrelated historical files so the later watcher still reacts only to new
or changed recordings.

After Flow 1, use an installed and authenticated Codex CLI through ephemeral read-only
`codex exec`, `--output-schema`, and `--output-last-message`; never invoke the inaccessible
Microsoft Store desktop entry as though it were the CLI. Validate the returned title,
subtitle, timestamp, and derived tags locally. Empty JSON means no eligible clip.

The desktop process owns two session-scoped opt-ins: automated QA burn and automated upload.
Unresolved subtitle/VAD rows or unreviewed song lyrics pause without polling repeatedly.
Upload authorization expires when the watcher stops, and every row still requires the
unchanged preview digest and collection read-back. Late fragments for a completed session
pause instead of reopening or republishing the job.

