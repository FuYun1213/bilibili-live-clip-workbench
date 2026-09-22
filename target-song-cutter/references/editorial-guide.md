# Editorial selection guide

Use this guide when turning a transcript into a non-contiguous edit plan.

## Default viewer stance

Edit as an internet passerby looking for a satisfying story, surprise, or laugh.
Do not assume fandom, loyalty, prior lore, or a duty to praise the creator. Ask of
every candidate:

1. Would a stranger stop scrolling within the first 3–8 seconds?
2. Is there a legible collision, such as claim versus reality, confidence versus failure, plan versus consequence, or composure versus reaction?
3. Does the setup escalate or change rather than merely continue?
4. Is there a complete payoff worth quoting, replaying, or sending to someone else?
5. Does the clip still work if the viewer feels neutral about the creator?

Treat spoken “别切”“这个不能切”“不许发” and similar remarks as candidate
signals, not editorial vetoes, when they are clearly playful or part of the bit.
Keep the setup and payoff that explain why the remark is funny. Honor a genuine
privacy, safety, consent, legal, or off-record request.

Looking for fun does not authorize bullying, invented scandal, sexualizing neutral
behavior, misquotation, or context stripping. A genuinely suggestive joke or double
meaning may be a strong candidate when the adult context, wording, and reaction are
present in the source. Preserve its ambiguity and do not escalate it into an explicit
claim the creator did not make. If age, consent, or context is unclear, use neutral
wording or reject the candidate. Let the creator's actual words, timing, and
consequences supply the joke. General ASR hotwords such as “脚” and “腿” are
recognition aids, not automatic clip-selection triggers.

## Analysis pass

1. Build a chronological section outline before selecting clips.
2. For each candidate, record the setup, collision or escalation, payoff, and why a neutral viewer would care.
3. Score candidates from 1–5 on scroll-stop hook, payoff, outsider clarity, contrast or reversal, emotional reaction, and title/replay potential.
4. Require payoff and outsider clarity to score at least 3. Allow a weaker outsider-clarity score only when no more than eight seconds of setup makes the moment self-contained.
5. Penalize repetition, long setup, missing visual dependence, privacy risk, unverifiable claims, inside jokes, praise-only framing, and moments that work only through existing affection.
6. Rank outsider entertainment above fan-service value by default. Keep a fan-only moment only when the user requests it or it also has an independent joke, reveal, or emotional turn.
7. Keep only candidates scoring 3–5 with a complete payoff and a defensible reason. Reject 1–2 point candidates without retaining, repairing, extending, splitting, repackaging, titling, subtitling, or creating cover assets for them. A 3-point candidate may receive only the shortest source context required to make its existing event and causal meaning complete; do not expand it into a different story. Prioritize 4–5 point candidates and do not fill a quota.
8. When synchronized danmaku and SC exports exist, attach their evidence to each candidate but score the actual content independently.
9. Consult prior user ratings before final ranking. Keep the user's score scale distinct from the editorial 1–5 scores.

## Topic map and proposal lanes

Create `topic-map.csv` before final candidate ranking:

```text
section_id,start_seconds,end_seconds,topic,participants,opening_state,turn_or_change,outcome,callbacks,notes
```

Mark a new section when the subject, participant goal, emotional state, or causal thread changes. Use semantic changes across complete sentences rather than fixed duration or keyword hits. Build boundaries at multiple scales: short beats inside a discussion and the larger discussion they belong to. A hotspot may move the start earlier or the end later, but it must not cross into an unrelated topic merely to fill duration.

Generate a union of independent proposal lanes:

- `semantic-story`: topic island contains setup, change, and outcome;
- `engagement`: normalized danmaku burst, sustained plateau, or sharp drop surfaces a possible cause;
- `sc-steered`: an SC triggers, redirects, challenges, or later returns to a subject;
- `reaction`: audible laughter, shock, frustration, silence, or visible reaction strengthens an event;
- `preference-recall`: a pattern repeatedly rated high suggests a reranking adjustment;
- `editorial-search`: explicitly search for self-owns, persona collapse, suggestive double meanings, creator commentary on people/events/works, social friction, failed attempts, reversals, and quotable verdicts.

Deduplicate overlapping proposals by topic and payoff. Do not let any one lane automatically create a clip. Record the proposal sources in `candidate-report.csv`, then reconstruct:

```text
setup -> trigger/collision -> escalation or interpretation -> payoff -> optional aftershock
```

Use this conceptual score only after completeness and fairness pass: content strength + narrative completeness + normalized engagement + SC steering evidence + audible/visual reaction + repeated user preference - spam risk - duplicate risk - context loss.

Never publish the numeric combination as an objective quality score. It is an auditable ranking aid. Engagement and preference can reorder eligible candidates but cannot rescue a missing payoff or misleading implication.

When video analysis is allowed, inspect scene-aware, deduplicated keyframes only after a candidate becomes editorially eligible. Align each frame to transcript time and use it to verify visible reactions, on-screen evidence, joins, and cover choices. Reject transition, fade, loading, blurred, or duplicated frames as covers. Do not infer an off-screen act from a suggestive line or neutral motion. If the user requests audio-only work, skip this visual pass entirely.

## Candidate types

- Story: a complete anecdote with an outcome or realization.
- Reveal: unexpected information, reversal, confession, or strong opinion.
- Humor: premise and punchline survive the cut.
- Self-own: the speaker's confident claim, boast, or rule immediately collapses.
- Mishap: an attempt fails, backfires, or produces an unintended consequence.
- Escalation: a small phrase, odd premise, or mistake grows through distinct beats.
- Contrast: words and actions, expectations and results, or two speakers sharply diverge.
- Social friction: teasing, disagreement, awkwardness, or negotiation remains funny and fair with context.
- Emotion: delight, frustration, vulnerability, tension, or relief is audible.
- Insight: specific, useful, or counterintuitive explanation.
- Conflict: disagreement or obstacle with enough context to remain fair.
- Quote: concise, memorable wording that accurately represents the speaker.
- Suggestive double meaning: the source itself creates adult innuendo, ambiguous wording, or a reaction that makes the implication legible without inventing explicit conduct.
- Commentary or opinion: the creator evaluates, roasts, praises, compares, or takes a distinctive position on a person, event, work, or trend. Preserve the target and qualifications and label opinion as opinion.

## Engagement signals and rating feedback

Use synchronized engagement data to search more intelligently, not to replace editorial judgment:

- Measure danmaku flow in fixed short windows and compare each window with the same recording. Flag sharp bursts, sustained plateaus, and sudden drop-offs; absolute counts are not comparable across channels or streams without normalization.
- Inspect enough time before a burst to recover its cause and enough time after it to capture the final reaction. A spike may reflect spam, greetings, raids, technical problems, or repeated emotes rather than a usable payoff.
- Treat SC as a possible steering event. Mark whether the message asks a question, introduces a topic, challenges a claim, supplies evidence, or triggers a later callback. Keep the relevant body and response, not the generic thank-you preamble.
- Record `danmaku_signal` and `sc_signal` in the candidate report as time-stamped evidence. Write `none` when no useful signal exists rather than forcing a connection.
- After review, collect the user's score, scale, verdict, and notes in `clip-evaluation.csv`. Compare repeated high- and low-rated examples by hook speed, topic, candidate type, duration, title construction, danmaku pattern, and SC involvement. Treat stable cross-video patterns as preferences and isolated ratings as examples, not permanent rules.
- Treat the creator's attitude toward a repeated danmaku label as part of the causal story. High frequency can signal annoyance or rejection rather than demand or popularity. For example, if 妈妈 spam causes the creator to want a restart so there will be fewer mother-addressing comments, preserve that direction; never invert it into 妈妈感淡了所以重开 unless the source explicitly says so.
- Current calibration examples: a brand-name slip followed mainly by repetitive chat spam is 1 point and rejected; an explanatory but mild reveal is 2 points and rejected; a 3-point candidate is viable only when the shortest preceding cause makes the payoff and meaning complete.

## Non-contiguous editing

Combine separated ranges only when they belong to the same subject and the join preserves meaning. A typical sequence is:

1. cold-open hook;
2. minimum necessary setup;
3. escalation or key evidence;
4. payoff;
5. optional short reflection.

Remove greetings, calls to action, repeated explanations, irrelevant tangents, technical pauses, dead air, and gift-reading acknowledgements. Run the Bilibili gift filter before analysis and treat every removed gift row as a mandatory cut point. Delete the spoken “谢谢某某” portion instead of hiding only its subtitle. A relevant SC／醒目留言／钢镚 body may remain when it directly supports the selected material, but its thank-you preamble must still be cut; remove an unrelated paid-message reading in full. Do not splice separate sentences into a statement the speaker never made. Keep audible qualifications such as “可能”“我猜”“在这个例子里” when they materially affect the claim.

Treat silence and low-energy detection as edit suggestions, not automatic deletions. Preserve hesitation, awkward pauses, breaths, delayed laughter, and silence when they create tension, embarrassment, timing, or reaction. For approved deletions, prefer asymmetric margins and leave enough tail to avoid clipped syllables or mechanical joins.

## Callbacks and flashbacks

Use non-chronological order only when the relationship between the ranges is explicit:

1. open on a later hook, recap, or surprising payoff;
2. return to the earlier event that created the phrase, conflict, or running joke;
3. come back to the later callback, escalation, or reflection.

Record the editorial role of every range as `cold open`, `flashback`, `return`, or `payoff` in the plan reason. Preserve speaker turns within each range. Do not imply that separated speakers heard or answered one another when they did not.

Every reverse or flashback edit must mark each non-chronological jump and render a transition there. Over the final 0.25 seconds before the boundary, ramp the full composed frame from clear to Gaussian blur at sigma 8–12; peak at the cut, then recover from the same blur to clear over the first 0.25 seconds after it. Keep audio unchanged with no fade. Apply this both from cold open to setup and from setup back to the later chronology when a return exists. A hard cut at either jump fails review; ordinary chronological deletions remain hard cuts without blur.

Avoid callback edits when the later reference requires unavailable visual context, when the repeated phrase is coincidental, or when the reordered version changes causality.

## Creator personality

For creator- or streamer-led clips, score personality separately from fan affinity. Look for:

- playful stubbornness or mock seriousness;
- self-aware exaggeration and self-teasing;
- affectionate bickering and quick rapport;
- turning a small phrase into a running joke;
- contagious laughter or other speakers validating the bit;
- a contradiction between stated intent and immediate behavior;
- misplaced confidence, accidental confession, failed composure, or a plan backfiring.

Do not promote a clip merely because the creator is cute, wholesome, talented, or
beloved. Those qualities can strengthen a clip, but the selection still needs a
setup, turn, and payoff that a neutral viewer can recognize. Do not reduce “cute”
to infantilizing language; show personality through observable delivery, choices,
timing, and interaction.

## Publishing title-cover bundles

Treat the publishing title and thumbnail as complementary hooks. The title tells the mini-story; the cover exposes one conflict, quote, or proof point. Do not paste the title onto the cover.

Produce three genuinely different bundles for each approved clip before the user chooses a final direction:

```text
bundle_id,title,cover_mode,cover_text_primary,cover_text_secondary,evidence_frame
```

1. `quote-collision`: lead with the strongest accurate line, then reveal the immediate contradiction or consequence.
2. `narrative-reversal`: state the persona, plan, or promise, then show how reality overturns it.
3. `relationship-challenge`: foreground a real person, rivalry, misunderstanding, audience reaction, or provocative question that drives the clip.

### Title rules

- Write one complete narrative sentence with an explicit grammatical subject, a concrete action or event, and a consequence, reversal, or emotional response. The creator tag is metadata and never counts as the sentence subject.
- Name the creator or another concrete participant in the title body. Use a pronoun only when its antecedent already appears earlier in the same title. After removing any creator tag and emotional prefix, the remaining title must still stand as a complete sentence.
- Reject topic labels, persona fragments, and subjectless hooks such as `社恐到见人腿软`, `一袋桃全是坏的`, or `当场破防了`. They may become cover copy, but not publishing titles.
- Put the strongest source-backed hook first: an exact quote, named person or work, concrete action, contradiction, consequence, or emotional reaction.
- Use one creator tag exactly once. This account's channel convention puts `【中文名English】` at the beginning. The label contains only creator identity and never replaces the grammatical subject in the body. Never repeat it at the end.
- Prefer a spoken, feed-native rhythm over a formal summary. One emoji, one rhetorical question, ellipses, or a short punctuation burst may strengthen a real beat; they cannot substitute for one.
- Allow a long title when each clause adds setup, escalation, or payoff. Remove generic labels such as “爆笑切片”“精彩回顾” and empty shock words.
- Preserve attribution and uncertainty. For commentary, make clear whose view it is. For suggestive material, title the actual double meaning or reaction without turning neutral behavior into explicit conduct.

Useful structures include:

- `【主播名】[主播或参与者]说出[准确原话]，结果[即时后果或反转]`
- `【主播名】[主播或参与者]立下[保证或计划]，下一秒却[现实结果]`
- `【主播名】[某人/SC/弹幕]说了[具体内容]，让[主播名]出现[明显反应]`
- `【主播名】[主播或参与者]宣战[具体作品或对象]，[另一方/现实]却[真实结果]`
- `【主播名】[主播名]做了[具体动作]，然后[后果或情绪反应]`

### Cover modes

Choose exactly one mode per bundle:

- `quote-impact`: use an expressive close-up plus one oversized quote or verdict. Best for self-owns, outbursts, and memorable wording.
- `evidence-reaction`: preserve an original SC, chat, post, message, or on-screen proof panel on one side and place the creator reaction on the other. Do not redraw evidence text with an image model.
- `clash-challenge`: split two people, works, claims, or outcomes into a visible opposition with a short versus-style hook.

Use `cover_text_primary` as the required upper setup/quote/question region and `cover_text_secondary` as the required lower action/contrast/consequence region. Each region contains 3–22 effective characters and may contain one manual line break; with no break, the renderer balances it across at most two lines. Keep both regions inside the centered 4:3 safe area so the same cover survives both 4:3 and 16:9 display. Use yellow or white as the default headline with a thick near-black outline. Never reduce the character to a tiny corner or shrink a full title to fit.

The cover may use a verified source screenshot instead of a generated character portrait when the screenshot itself is the proof or the facial reaction is unusually strong. Keep original screenshots, SC text, and UI evidence legible. Blur or simplify only the non-evidence background.
## Boundary rules

- Start 0.15–0.4 seconds before speech when possible.
- Record the edit-plan end at the final word or complete reaction; the exporter appends a five-second clean tail to the final retained range.
- Do not include a new topic or a gift acknowledgement merely to fill the five-second tail.
- Avoid cutting inside breaths, laughter, words, or overlapping speech.
- Prefer natural silence or topic transitions.
- Keep reaction audio when it supplies the payoff.
- After rendering, listen across every join and adjust the source boundaries if the edit sounds mechanical.
- Re-transcribe each final exported MP4 with clip-local Faster-Whisper word timestamps. Use that pass as the subtitle-axis authority; never reuse the whole-recording discovery axis after cuts or joins.
- Shift subtitle events 0.1 seconds later than the clip-local Whisper timestamps; shift both start and end so display duration stays stable.
- Treat Whisper wording and speaker identity as drafts. Correct text and assign speakers without replacing the verified final-clip timing with stale source timestamps.

## Reporting format

Before export, summarize each candidate with:

- title;
- source ranges;
- one-sentence outline;
- hook or strongest moment;
- outsider entertainment value and the specific collision, escalation, or payoff;
- fan-only value, if any, clearly separated from the main reason for keeping it;
- aligned danmaku-flow and SC evidence, or `none`;
- three distinct title-cover bundles, each with a title, cover mode, primary text, optional secondary text, and evidence frame;
- any context, sensitivity, or transcription caveat.


