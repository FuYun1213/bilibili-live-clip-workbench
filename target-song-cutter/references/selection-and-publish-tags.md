# Clip tags at candidate selection

Generate publish tags during candidate selection, not after rendering. Every selected JSON
item carries exactly five unique `标签` plus a non-empty `标签依据`; these become
`publish_tags` / `tag_evidence` internally and are copied to `tags` in
`titles-and-covers.csv`. Legacy three-field selections remain readable, but new Codex output
must never rely on title-fragment heuristics.

Human review is authoritative: the workbench exposes `tags` and `tag_evidence` beside the title.
A reviewer may replace the model suggestions with 1–5 unique clip-specific tags. One or two
manual tags are upload-safe and should produce only a quality hint, not a publishing failure.
Saving mirrors tag edits into an existing Flow 4 delivery and invalidates the old preview digest.

The publishing layer merges creator-profile tags first. `VirtuaReal`, the canonical creator
name, `虚拟主播`, `直播切片`, and `VUP` are fixed profile tags for VR creators and must not
consume the five clip-specific slots. The normal final list is 8–10 useful tags. Following
observed Bilibili practice, dynamic tags use three layers: one or two searchable entities
(person, work, game, song, product, platform, place, event); one or two core subject,
relationship, or action nouns; and at most one genuinely useful format tag such as `歌切`,
`翻唱`, `SC回应`, or `游戏切片`.

Prefer canonical, searchable names. Chinese tags are normally 2–8 characters, while proper
names and song/game titles may be longer. Reject full sentences, long title fragments,
unrelated trending names, invented memes, duplicate synonyms, and filler such as `太搞笑了`,
`精彩切片`, `直播日常`, or `主播锐评`. Every tag must be supported by a timestamped
transcript phrase, a verified stream topic, a reported song name, or the retained performance
itself. A mentioned person is eligible only when they materially participate in the story.
`标签依据` should map each tag (or an explicit group of tags) to that evidence.

Conditional rules:

- Chilly is not a VirtuaReal or VR streamer. Do not use `VR`, `VR主播`, `VR信`, or
  `虚拟现实` as an identity tag. Use one only when the final clip directly discusses VR and
  set `vr_topic=yes` in the selected JSON and title/tag row. The field is retained
  through normalization, canonical selection, local validation, and review CSV export.
  New structured outputs require `vr_topic` to be either `yes` or an empty string;
  older selections default to empty and still require explicit topic confirmation.
- Yuchu's `八分钱` / `飞八分钱` is one recurring meme, not evidence that exact-number stories
  are generally preferred. Use it only for a clip that actually contains that meme.
- `小粉螈` is Sumire's fan badge and `昼你鸭` is Chilly's fan badge.
- `得意` is a recurring Sumire stream term; `孙女` is a recurring Viridis stream term.
- Fan badges and recurring terms are ASR/editorial vocabulary. Add them as publish tags only
  when they are audible, visible, or central to the retained event.

Validate the approved rows before cover delivery or publishing:

```powershell
python scripts/validate_selection_tags.py delivery/titles-and-covers.csv --creator sumire
```

