# Reaction-video speaker attribution

Use this gate when the retained clip contains a host watching or replaying another creator's video, stream, or audio.

## Identity and timing

- Treat ASR as wording and timing evidence only. Do not infer speaker identity from the channel owner or recording filename.
- Identify the watched-source speaker from native captions, embedded player identity, self-introduction, and reviewed voice anchors. Identify the reacting host from the overlay avatar, mouth motion, conversational role, and reviewed voice anchors.
- Compare every proposed cue with the watched video's native caption at the cue midpoint. A wording match supports the watched-source speaker; a simultaneous non-matching reaction supports the host.
- Write `speaker-audit.csv` in the working folder with time range, speaker, rendering treatment, evidence, and `reviewed=yes`.
- When one ASR cue contains both speakers, split it at the final-media word timestamp. Never assign the whole merged cue to either person.

## Rendering

- If the watched source already has accurate, readable native captions, keep those captions as its visible subtitle authority. Do not duplicate the full monologue with a second subtitle layer.
- Add a short opening legend such as `粉色原字幕＝B｜紫边新字幕＝A`, then render only the host's reactions in the host profile style with an explicit `A：` prefix.
- If native captions are missing, inaccurate, or unreadable, render both speakers with distinct defined ASS styles and reviewed `Name` fields. Resolve overlaps with separate vertical positions.
- Fully decode the burned result and inspect the legend plus every host-reaction cue. Confirm that native source captions remain readable and no host cue covers them.

## Publishing copy

- State the relationship explicitly in title and description: `A看B谈X`, `A观看B拆解X`, or another factually equivalent form.
- Never describe B's analysis, story, or opinion as A's merely because A owns the livestream recording.
- Include both creators in grounded tags only when both are actually retained in the final clip.
