# Local covers and delivery folders

Use this workflow for every locally deliverable batch. It keeps repetitive rendering and
validation local, so model effort is reserved for selecting clips, editing narratives,
and writing strong copy.

## Single source of truth

Read assets/creator-profiles.json before transcription, ASS generation, cover
rendering, or delivery preparation. Do not retype a creator color or hotword list in a
batch script. The profile role_color, dialogue_font, dialogue_font_size, and
radio_subtitle_size must reach ordinary dialogue ASS and radio layout; the role color
also reaches covers. Viridis is #8EB056, and 哎小呜 is #F9A699; never inherit another
creator's palette.

Use the desktop “人物与字幕模板” editor for ordinary manual additions. The watcher may
create a room_<room_id> draft for an unknown numeric room. Such a draft is valid for
local Flow 0–4 but has upload.enabled=false: never invent affiliation, description,
fixed tags, space/live links, or collection ids. Do not read, require, or populate
upload-only profile fields during ordinary local delivery.

## Default batch flow

1. Finish editorial review and retain only 3-5 point narrative clips.
2. Create one flat delivery folder for the batch. Put every approved MP4 and its same-stem
   editable ASS directly in that folder; do not create a separate `covers/` folder.
3. Put one approved title and cover bundle per clip in a human-readable UTF-8 CSV named
   `titles-and-covers.csv` in the same delivery folder.
4. Render deterministic covers directly beside the matching videos:

```powershell
python scripts/local_publish.py render-local `
  --copy "delivery/titles-and-covers.csv" `
  --clips-dir "delivery" `
  --creator viridis
```

For `001-topic.mp4`, write `001-topic-cover.jpg` in the same directory. Reuse an existing
same-name cover on repeat runs; pass `--force` only when replacing it intentionally.

The local title-and-cover CSV accepts these preferred fields:

`clip_id,video,title,cover_mode,cover_text_primary,cover_text_secondary,cover_time_seconds,reference_image,evidence_image,tags,tag_evidence,vr_topic`

Legacy `title_a`, `cover_text_1`, and `cover_text_2` fields remain accepted during
migration. Titles and cover text must not contain an English single quote.
For this account, every narrative platform title starts with exactly one creator label
`【中文名English】`; the body after it remains a complete explicit-subject sentence. The
label contains no hook summary. Strip filename-only indices such as `001`, `002B`, or
`003_` from the platform title while preserving them in local filenames for sorting.

Do not add `tid`, copyright, no-reprint, upload state, attempts, BV/AV ids, credentials,
or other API-oriented fields to a normal local-delivery CSV. Do not generate
`publishing-manifest.json`, upload logs, credential templates, or resumable state files.

When the user has explicitly enabled the manifest-free automation publishing workflow,
the same human-review CSV may additionally contain `content_type`, `tid`, `description`,
and collection overrides. Keep it beside the media; do not create a second API-oriented CSV
or manifest. Generate `tags` during candidate selection according to
`selection-and-publish-tags.md`; new model output carries five clip-specific tags and their `tag_evidence`; an approved human
edit may contain 1–5 clip-specific tags because the creator profile later supplies fixed base tags.
`content_type=song` selects the configured music tid, a positive row `tid` overrides it,
and an empty/other content type uses the configured animation fallback.

Before cover handoff, run `scripts/validate_selection_tags.py` on the CSV with the selected
creator. A missing dynamic tag set, generic-only tag set, or an unconfirmed Chilly VR tag
blocks delivery and publishing.

Before handoff, confirm each approved video, editable ASS, and `-cover.jpg` are in the
same delivery folder and that no API-publishing artifact was created.

## Local cover rules

The deterministic local renderer is a direct port of
`hannari520/auto-clip/core/generate_covers.py` `style1`. Treat the following numbers as
fixed template parameters, not creative suggestions:

- center-crop the source frame to 16:9, resize to 1920x1080, sharpen once, and save JPEG
  quality 95;
- use bundled `assets/fonts/autoclip/MaoKenShiJinHei-2.ttf`, shared narrative base size 158;
- keep both text regions within the centered 4:3 crop and use an additional 60-pixel inner inset, producing the normal text range x=300–1620;
- draw the `cover_text_primary` upper region around y=230 in white `(255,255,255)`;
- draw the `cover_text_secondary` lower region around y=830 in yellow `(255,225,0)`;
- allow 2–3 narrative phrases of 3–22 effective characters each. The upper and lower areas each use at most two rows. Balance automatic breaks by rendered width, gently favoring nearby punctuation and keeping English words intact. A single phrase retains its manual break; when the lower area contains two phrases separated by ｜, reflow their complete copy together into at most two rows, including any legacy manual breaks;
- use black narrative text strokes produced by 11 repeated 3x3 MaxFilter passes;
- choose one shared font size for all upper and lower rows, fitting the longest row together with its outline and any emoji inside the safe width. Do not independently shrink the lower phrases. Keep an area on one row when it fits at the shared size; never omit copy to force a fit.

For a narrative cover, inspect at least three low-blur frames around the verified punchline,
strongest reaction, or naturally visible evidence. Put the chosen time on the final clip axis in
`cover_time_seconds`. A supplied `reference_image` still overrides extraction. Use the 28%
automatic frame only as a fallback when no stronger reviewed frame exists.
Use the source frame unchanged apart from the crop, resize, text, and final sharpen. Do
not apply custom brightness, contrast, saturation, blur, vignette, gradient, colored
accent, creator label, evidence card, pasted subject, radio-panel crop, or other local
composition. `cover_source_region=radio-left` keeps the same vertical centers but moves
the text into x=820–1620, still within the centered 4:3 safe crop, so it does not cover
the left-side presenter. Other source-region values use the centered text range.
The only collaboration-cover exception is a guest-character emote placed between the
source frame and cover text when verified dialogue from that guest remains audible in
the final cut. Decide this on the final clip's audible track, not on the room title,
recording label, visible guest avatar, a mention, appearance elsewhere in the stream,
or uncertain diarization. If no verified guest dialogue remains, classify the result as
an ordinary single-person clip, render the normal AutoClip frame-and-text cover with no
guest emote, and keep it eligible for upload.


For a Sumire, Kioi, Viridis, Yuchu, or Chilly song cut, set `cover_mode=song`. Ignore the video frame.
The selected creator profile supplies the bundled background image and fixed prefix.
Center-crop that background to 16:9, then draw exactly one centered yellow line at 78%
height with the same font and 12-pass black stroke:

- Sumire: `【枝堇歌】完整歌名`
- Kioi: `【柚雨歌】完整歌名`
- Viridis: `【松绿歌】完整歌名`
- Yuchu: `【羽啾歌】完整歌名`
- Chilly: `【昼夜歌】完整歌名`

Use the complete song title from the final video filename, allow 1-24 title characters,
and keep `cover_text_secondary` empty. The low position is mandatory so the line does
not cover the face. Do not add the words `完整歌切`, `平滑歌词字幕`, a creator label,
or any second line. `cover_mode=viridis-song` remains accepted only as a legacy
migration alias.

Inspect every rendered cover at 1920x1080 and at 320x180. Reject it if text is outside the
centered 4:3 safe area, the wrong background is used, the creator-specific song prefix is missing, or
any element not present in the AutoClip template appears.

Yuchu narrative covers follow the ordinary narrative rule: extract an ungraded frame from
the delivered clip and keep the ordinary two-region AutoClip text layout. Only Yuchu
song covers use the bundled `covers/yuchu-base.png` background with the fixed yellow
`【羽啾歌】完整歌名` line at 78% height. Inspect both sizes to ensure neither layout
covers the face.

Prompt-only delivery is optional. Use ImageGen only when the user explicitly requests a
custom illustrated cover; it does not replace the deterministic AutoClip batch template.
## API publishing is opt-in only

Do not run `local_publish.py prepare`, `local_publish.py render-covers`,
`local_publish.py check`, or `bilibili_publish_manifest.py` during the default workflow.
Keep those legacy commands dormant. When the user explicitly requests Bilibili submission,
use the manifest-free biliup workflow in `biliup-publishing.md` instead. The preview must
reuse the flat delivery folder and must not create upload metadata beside the media.
