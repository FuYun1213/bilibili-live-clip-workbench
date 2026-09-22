# Engagement signal fusion

Use this reference whenever danmaku, Super Chat, gift, or user-rating data accompanies a livestream.

## When engagement data is absent

Danmaku and SC are optional discovery signals. An omitted or missing XML, a
zero-byte XML, or a valid XML containing only recorder metadata must not block
Flow 1 or selection. The analyzers still write CSV headers and an explicit
`engagement-summary.json` with zero counts and `discovery_mode` set to
`transcript_audio_only`; continue discovery from the recording audio and
transcript. Do not invent engagement peaks. If SC exists without danmaku,
preserve its original text, timing, and response windows (`transcript_audio_sc`).
Reruns overwrite old engagement tables so stale peaks are not reused. Malformed
nonempty XML and other I/O errors remain explicit failures.

## Preserve an auditable timeline

- Keep source timestamps immutable. Store corrected/aligned timestamps separately.
- Establish an offset only from a shared audible or visible event. Record the anchor and offset in `timeline-audit.json`.
- Never clamp danmaku or SC timestamps to the nearest ASR speech segment. Silent reactions, pauses, or on-screen events may be the cause of engagement.
- Treat the source recording's own audio as the editing timeline; apply its approved timestamps to that recording's video only after alignment is established.

## Generate the candidate library

Run:

```powershell
python scripts/analyze_engagement.py `
  --input "recording.xml" `
  --output "engagement-xml" `
  --windows 10,30,60 `
  --reaction-lag-max 30 `
  --sc-response-window 180
```

If a verified shared event shows that XML time is 2.4 seconds behind the audio, add:

```powershell
--offset-seconds 2.4 --alignment-anchor "00:31:12 spoken-SC-read"
```

Interpret the scales differently:

- 10 seconds: sharp laugh, shock, reversal, or sudden spam.
- 30 seconds: short comedic beat or reaction sequence.
- 60 seconds: sustained discussion, conflict, song reaction, or audience-guided topic.

The analyzer includes empty windows in its baseline, compares each window with a local five-minute neighborhood, marks sharp rises and sustained flow separately, and penalizes repeat-heavy spam. Read `engagement-hotspots.csv` first, then inspect the scale-specific tables when deciding why a hotspot appeared.

Do not assume the event and chat peak occur at the same second. View each hotspot as:

```text
cause_search_start -> possible source event -> chat_peak_start/end -> payoff_search_end
```

Start from `cause_search_start`, then find the actual spoken or audible trigger. Chat commonly arrives after the event. Treat `reaction-lag-max` as a search allowance, not an automatic shift applied to every timestamp.

Use `user_coverage_ratio`, `same_user_repeat_ratio`, and `spam_risk` together. A repeated line sent by many distinct users is `crowd-repeat` and may be genuine audience consensus; repeated user-message pairs indicate stronger spam risk. When sender coverage is low, keep the spam judgment uncertain and verify the raw XML. Never compare absolute danmaku counts across different channels without normalization.

## Reconstruct cause and payoff

For every hotspot, inspect at least the generated `inspect_start` through `inspect_end` range in transcript and audio. Record:

1. the event before the rise;
2. what the creator or another participant did;
3. the audience response;
4. the final reaction, reversal, or conclusion;
5. whether the engagement signal discovered the candidate or only confirmed it.

Reject greetings, raids, technical failures, copy-paste spam, gift acknowledgements, and contextless emote waves unless the creator's reaction itself forms a complete, fair story.

## Review Super Chats as steering events

Do not rank an SC by price alone. Fill the review columns in `superchats.csv` and classify every potentially relevant message as:

- `trigger`: starts a new subject or action;
- `setup`: supplies context required by the response;
- `evidence`: supports or challenges a claim;
- `payoff`: creates or completes the strongest reaction;
- `callback`: becomes relevant again later;
- `unrelated`: does not support the candidate.

Keep the original SC body verbatim. Cut the generic thank-you preamble even when the body and response are retained. Link a relevant SC to a candidate ID and record the complete response range.

Use `response_search_start_seconds` through `response_search_end_seconds` to inspect the possible steering chain. `post_peak_*` only identifies the strongest later chat window; it does not prove that the SC caused it. Fill `response_start_seconds` and `response_end_seconds` only after hearing the creator read, answer, resist, follow, or later recall the message.

Model the complete chain as `SC body -> creator interpretation -> action/opinion -> audience response -> aftershock or callback`. An expensive SC with no meaningful response remains unrelated; a low-price SC that changes the topic can be a strong trigger.

## Keep editorial eligibility separate from engagement

First score the actual story from 1?5 using hook, outsider clarity, escalation, payoff, fairness, and title/replay potential. A candidate remains ineligible if payoff or outsider clarity is below 3.

Then attach engagement evidence:

- danmaku: `none`, `surface`, `confirm`, or `strong-confirm`;
- SC: role plus message time and response range;
- user calibration: `-1`, `0`, or `+1` rank adjustment only after a preference repeats across multiple rated clips.

Engagement can change ranking among eligible candidates, but it cannot rescue an incomplete or misleading clip. Preserve the user's own rating scale separately from the editorial score.

## Apply evidence constraints to titles and covers

- Quote danmaku and SC exactly; do not correct or paraphrase text inside quotation marks.
- Use only measured statistics. Do not invent counts, ratios, timing, or audience intent.
- Make creator commentary visibly attributed as opinion and preserve qualifications.
- Use suggestive wording only when the source itself contains adult innuendo, ambiguity, or a legible reaction. Preserve ambiguity; never turn a neutral action into explicit conduct.
- Prefer source-backed title angles: exact-line collision, persona/plan reversal, relationship challenge, creator commentary, or SC-triggered reaction.
- Let the title tell the complete mini-story and the cover expose one quote, conflict, or evidence point.

## Promote terminology only after review

Extract recurring names, nicknames, works, memes, and corrections into `video-hotword-candidates.txt`. Exclude ordinary words, one-offs, private information, and uncertain ASR guesses. Never promote candidates automatically; wait for explicit user selection, then update the persistent glossary and validate it.
