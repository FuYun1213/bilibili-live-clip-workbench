# Optional Bilibili publishing through biliup

Read this reference only after the user explicitly requests Bilibili submission. The
default skill workflow remains local-only.

## Safety boundary

- Treat biliup as a community client, not an official Bilibili developer API. Check the
  installed CLI with `biliup upload --help` before execution because platform and tool
  parameters can change.
- Respect biliup's current project disclaimer, copyright restrictions, and Bilibili's
  platform rules. Do not execute this workflow for commercial use while the published
  biliup disclaimer prohibits it.
- Never ask the user to paste `SESSDATA`, `bili_jct`, a browser Cookie header, or the
  contents of `cookies.json` into chat.
- Store credentials at the script default under the local application-data directory.
  Never put them in OneDrive, the skill, a delivery folder, Git, logs, screenshots, or a
  shared ZIP.
- Never install biliup, log in, renew credentials, or upload without the user's explicit
  approval. A dry-run preview is local and does not require Cookie access.

## Credential interface

The wrapper supports three local-only credential routes. QR login remains recommended:

~~~powershell
python scripts/biliup_publish.py credentials --login
~~~

When the user explicitly wants a file to fill, create a schema-checked empty template:

~~~powershell
python scripts/biliup_publish.py credentials --create-template
~~~

The upload credential defaults to
`%LOCALAPPDATA%\target-song-cutter\biliup\cookies.json` on Windows. Collection add and
read-back use the separate private
`%LOCALAPPDATA%\target-song-cutter\biliup\web-cookies.json`. Both files use the
`cookie_info.cookies` and `token_info` structure tested with biliup. The user fills
`SESSDATA` and `bili_jct` locally; never request or display their values. Credential checks
report only `missing`, `invalid`, `template`, or `ready`. An incomplete file must block
execution. The wrapper may import an existing `cookies.json` to the same private directory
without printing its contents.

Refresh an expired session with:

~~~powershell
python scripts/biliup_publish.py renew
~~~

## Preflight and preview

The delivery folder must remain flat. Every selected row in `titles-and-covers.csv` must
resolve to these same-folder assets:

```text
<video-stem>.mp4
<video-stem>.ass
<video-stem>-cover.jpg
titles-and-covers.csv
```

Check the installed biliup CLI without uploading:

```powershell
python scripts/biliup_publish.py doctor
```

Preview one or more independent submissions without reading Cookie data or connecting to
Bilibili:

```powershell
python scripts/biliup_publish.py upload `
  --delivery "delivery" `
  --creator viridis `
  --tid 27 `
  --song-tid 130 `
  --copyright 1 `
  --only 001 002
```

Require an explicit `--copyright 1` or `--copyright 2` decision. For reposted content,
require `--copyright 2 --source "source URL or attribution"`; never infer a rights
declaration. Confirm current Bilibili category ids rather than copying old ids blindly.

The wrapper resolves each row independently:

- a positive CSV `tid` wins;
- otherwise `content_type=song`, `music`, or `song-*` uses `--song-tid` (Music General `130`);
- otherwise `--tid` is the fallback (Animation General `27`).

Use a more specific current child tid when the clip clearly fits; leave it blank when
uncertain so the animation fallback applies. Fixed creator profile tags are mandatory and
merged first. New Codex selections supply five evidence-backed clip-specific `tags`. Human review may replace
them with 1–5 unique content tags plus a non-empty `tag_evidence`; profile defaults alone fail,
but there is no artificial minimum of three manual tags or eight merged tags.
Keep `VirtuaReal` first only for a VirtuaReal profile. Chilly is not a VirtuaReal or VR
streamer, and a Chilly VR tag requires `vr_topic=yes` for an actually retained VR discussion.
Build the final description with this formula:

```text
{optional clip-specific lead from CSV description, otherwise CLI --description}

{mandatory selected creator upload.description block}
```

Keep the lead to one or two truthful sentences that add context instead of repeating the
title. The selected creator's fixed block is mandatory and must retain its exact wording,
blank lines, space link, and live-room link. A CSV or CLI lead may not replace that block.
If the lead already contains the complete fixed block, keep one copy. Never combine two
creator blocks; for a duet, select the approved primary publishing profile and mention the
guest only in the lead unless the user explicitly approves another formula.

Resolve the target collection from the creator profile for every row: `narrative` uses the
Chinese-only `XX切片` collection and `song` uses the Chinese-only `XX歌` collection.
Never include romanized or English suffixes in `XX`. Preview the collection title and any
stored positive `season_id` and `section_id`; for a new profile, preview may show the derived
title with null IDs and must remain offline. After explicit execution confirmation, prepare
`XX切片` first for every new creator, even when the first batch is song-only, and then prepare
`XX歌` when song rows are present. Resolve one exact existing title before creating anything.
If missing, upload the approved clip cover, create the collection, read back its first positive
section, and atomically persist the route in `assets/creator-profiles.json`. Multiple exact
titles, a collection without a usable section, or an item still missing a full route must stop
before video upload. The wrapper may pass `season_id` through biliup's `--extra-fields` JSON,
but that field is only submission metadata and does not prove membership.

Review the displayed video, cover, title, collection, category id, copyright value, source, and tags.
Do not proceed if any field is uncertain.
For narrative rows, require exactly one leading `【中文名English】` creator label and a
complete explicit-subject sentence after it. The label is identity only, not a hook summary.
Remove local filename indices such as `001`, `002B`, or `003_` from the platform title.

Keep `--no-reprint 0` unless the user explicitly requests a prohibited-reprint declaration.
In Bilibili's submission UI this corresponds to `内容无需标记`; do not select
`未经作者授权，禁止转载` by default.

For duet/chorus submissions, use the concise title format 【合唱】《曲名》歌手A×歌手B.
Do not append arrangement copy such as 一人一句, 高潮合唱, or 轮唱合唱版 to the
platform title. Those details may remain in the description or in-video title card.

## Explicit execution

Only after the user approves the preview, rerun the same command with `--execute`. The
interactive terminal requires the exact confirmation text it displays. For a noninteractive
run, pass the same phrase through `--confirm`, for example `--confirm "发布 2 条"`, only
after receiving that approval. That one approval also covers the conditional collection-cover
upload and collection creation described above; neither action may occur before confirmation.
The wrapper must print the read-back routes and re-run the full routing gate before invoking
biliup.

The wrapper invokes biliup once per CSV row, so two rows become two independent Bilibili
submissions rather than one two-part video. Flow 5 persists one machine-wide submission
window: after five newly accepted archives it waits until the ten-minute cooldown deadline,
then automatically continues with the sixth row or the next project. The deadline survives
process restarts. `--cooldown` remains only the ordinary delay between adjacent rows and does
not weaken the five-item / ten-minute Flow 5 gate.

Use `--submit web` (the wrapper default). The `app` submit channel can finish uploading the
media and then fail with Bilibili code 21566; an exact-title recent-archive lookup must show
that no稿件 was created before changing channels or retrying. Do not immediately retry the
same `app` request.

For each successful upload, the wrapper first parses the returned BV id. Because the web
submit channel may print no BV, it may instead poll the authenticated recent-archive list for
up to 21 seconds and accept only a title-exact item created no earlier than two minutes
before the upload began. It then retrieves the exact aid/cid/title, adds that video with the
official collection-section endpoint, and reads the section back. A row is successful only
when the exact aid is present. An unrecoverable BV, title mismatch, API failure, or failed
read-back stops the batch. The add is idempotent: already-present aids are not added twice,
but are still verified.

`--limit 3` controls the maximum concurrent upload chunks for one video file; it does not
mean three videos and is already the wrapper default. Use `--line` only after real upload
tests show that automatic line selection is poor.

Do not write manifests, Cookie templates, upload logs, or returned BV/AV ids into the
delivery folder. Report biliup's visible result to the user without exposing credentials.

## Mandatory post-publish unmade-candidate report

After the final high-score submission in the approved batch has both a recovered BV id and
a successful collection-section read-back, produce the backlog report before ending the
user-facing response. A BV list alone is incomplete.

Re-open all same-batch candidate and outcome evidence that exists, including
`candidate-report.csv`, candidate ranking/addendum files, `topic-map.csv`,
`clip-evaluation.csv`, song discovery/boundary manifests, final delivery
`titles-and-covers.csv`, and the successful upload result. Match by stable candidate id first
and normalized title only as a fallback.

Classify as `未入选／未制作`:

- explicit `reject`, `drop`, or `not selected` rows;
- 1–2 point narrative candidates;
- candidates left at `review` or equivalent status that do not appear in final delivery;
- song attempts rejected for an incomplete take, restart, spoken interruption, mid-song
  entry, missing ending, or unusable alignment.

Do not classify a candidate as unmade when a matching final MP4 exists. Put a selected
clip that was withheld from upload because it mentions another creator, lacks separate
authorization, or has another publishing hold in a distinct `已入选但暂缓投稿` section.
Do not confuse rejected ASR words, VAD rows, or discarded edit intervals inside a completed
clip with rejected candidate material.

Every candidate must have a short audit title. Reuse its recorded candidate title when
available. If the source only contains a story and hook, create a neutral one-sentence audit
label and mark it `候选标题`; do not turn it into a publish-ready title package. Preserve two
identical titles when they come from different source sessions, and report both the candidate
instance count and unique-title count.

Write `unmade-candidates.csv` in the batch working/audit folder, never in the flat delivery
folder. Group the user-facing report by source date and creator, include audit title,
content type, score or status, and decision reason, and state both candidate-instance and
unique-title counts. Put selected publishing holds in a separate
`已入选但暂缓投稿` section. If no unmade or held candidates remain, say so explicitly.
