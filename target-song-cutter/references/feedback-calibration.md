# Clip feedback calibration

Read this reference before ranking a new batch whenever rated `clip-evaluation.csv` files exist.

## Preserve the user's judgment

Keep one source-level evaluation table with these preferred columns:

```text
source_id,clip_id,title,user_score,score_scale,user_verdict,user_notes,editorial_tags,topic_key,hook_type,title_style,duration_seconds,danmaku_signal,sc_signal
```

- Preserve `user_score` and `score_scale` verbatim. Never silently convert a missing or ambiguous scale.
- Use stable tags such as `self-own`, `suggestive-double-meaning`, `commentary`, `mishap`, `escalation`, `emotion`, and `sc-steered`.
- Use `topic_key` as a short semantic identity such as `mother-address-restart` or `douluo-commentary-cultivation`; do not use only the title text.
- Record the actual title construction in `title_style`, not a later guess.
- Leave unrated rows blank. Never treat no rating as rejection.

## Build the cross-video profile

After the user returns ratings, run:

```powershell
python scripts/summarize_clip_feedback.py `
  --input "batch-a/clip-evaluation.csv" "batch-b/clip-evaluation.csv" `
  --output "feedback-profile"
```

Read both outputs:

- `feedback-ledger.csv`: auditable source rows and normalization status;
- `feedback-profile.json`: repeated preferences grouped by editorial tag, hook type, title style, danmaku signal, and SC role.

The default requires at least three rated examples before emitting a preference adjustment:

- `+1`: repeatedly high-rated pattern;
- `0`: insufficient or mixed evidence;
- `-1`: repeatedly low-rated pattern.

Apply the adjustment only after the candidate passes completeness, outsider clarity, fairness, and source-truth gates. A preference cannot rescue an incomplete story, misleading title, privacy problem, or fabricated implication. Do not raise the adjustment beyond one point and do not overfit one creator, one stream, or one unusually strong clip.

## Prevent repetitive output

Before advancing a candidate, compare its `topic_key`, core claim, source event, hook type, and intended title angle with the ledger.

- Mark `duplicate-risk` when the same anecdote, opinion, SC prompt, or payoff has already been delivered.
- A later callback may be eligible only when it adds a new consequence, reversal, answer, or stronger reaction.
- A different title or shorter duration does not make the same event new.
- Keep the better version when two candidates express the same mini-story; do not publish both merely because both scored well.

Record duplicate review in `candidate-report.csv`:

```text
candidate_id,source_ranges,topic_key,candidate_types,setup,turn,payoff,outsider_value,editorial_score,danmaku_signal,sc_signal,user_preference_adjustment,duplicate_risk,decision
```

## Learn from notes, not just numbers

Compare high- and low-rated examples for:

- how quickly the conflict or curiosity appears;
- whether the setup was sufficient but not bloated;
- whether the final response or aftershock was retained;
- candidate type, topic, duration, and speaker relationship;
- whether danmaku discovered the moment or only confirmed it;
- whether an SC genuinely redirected the stream;
- whether the title accurately promised the strongest beat;
- whether the cover showed readable evidence or a meaningful reaction.

Convert repeated note patterns into a short batch calibration note. Keep it evidence-based, for example: `commentary clips need a named target and direct verdict` or `high chat volume without a reversal has rated poorly`. Do not turn a one-off comment into a permanent rule.
