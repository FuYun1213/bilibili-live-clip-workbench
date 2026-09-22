# Bilibili market benchmarking for shared-source clips

Use this reference when selecting, titling, or covering clips for Sumire, Kioi, Viridis,
Yuchu, or Chilly. Public view counts and rankings change, so refresh them for each new source
instead of treating the snapshot below as permanent truth.

## Refresh and audit

Run the exact creator-name search with the current comprehensive ranking and a recent-date window:

```powershell
python scripts/bilibili_creator_benchmark.py `
  --output "work/market-benchmark" `
  --pages 2 --top 25 --cover-count 12 `
  --order totalrank --recent-days 60
```

Read the CSV and inspect all downloaded cover thumbnails. Separate narrative cuts, songs,
official posts, long replays, and unrelated results manually; `content_type_hint` is only a
filtering hint. Compare absolute views with `views_per_day`, upload age, uploader size, and
collaborator reach. A famous collaborator or old established upload can inflate views without
proving the title formula itself.

For the current recording, also search the creator plus the stream date/title and distinctive
verified quotes. Record same-event competitors in the working audit, not the flat delivery.
Add `market_signal`, `market_duplicate_bvids`, `market_angle_gap`, and
`cover_time_seconds` to the candidate/title audit when applicable.

Do not copy another uploader's title, cover text, frame composition, or edit. Use the market
scan to identify audience-readable events and gaps, then derive every claim and asset from
the authorized source recording.

## Shared-source selection

When several cutters have the same recording:

- Prefer a complete event with a visible or audible trigger, escalation, and consequence.
- Treat an already-high-view same-event clip as demand evidence, not an automatic rejection.
  Compete only with a materially clearer hook, missing setup/payoff, stronger verified angle,
  or better reaction/evidence frame.
- Mark an event saturated when several current clips already use the same quote, angle, and
  payoff. Prefer an uncut adjacent story with the same structural strength.
- Rank relationship friction, identity exposure, family interruption, failed attempts,
  self-owns, strong reactions, and evidence-backed reversals above generic cuteness, praise,
  or fan-only affection. Use a specific number only when it materially drives the event;
  never generalize one number meme into a creator-wide preference.
- Keep suggestive or controversial wording only when it is genuinely spoken and the retained
  context preserves its meaning.

## Title learning

Write an explicit subject followed by a concrete trigger and a turn or consequence. Strong
connectors include `结果`, `却`, `最后`, `当场`, `直接`, `原来`, and `光速`; use them only
when the sequence is true. Preserve verified names, relationship roles, amounts, hardware,
heights, ranks, and quoted phrases because specificity consistently outperforms vague topic
labels. Put the main hook early and remove generic openings such as `主播聊了聊`.

Good structures:

- `人物 + 自信宣称，证据出现后当场反转`
- `人物A做了具体动作，人物B误解／回击后出现后果`
- `具体数字／身份被揭露，主播给出强反应`
- `弹幕／SC提出一句问题，主播越解释越暴露`

Never exaggerate a mild response into `破防`, `暴怒`, `哭`, or `黑化`. Exact source truth
still outranks market vocabulary.

## Cover learning

For narrative cuts, keep the required AutoClip frame template but choose the frame deliberately.
Inspect at least three low-blur frames around the verified punchline, strongest expression, or
on-screen evidence. Write the chosen final-clip time to `cover_time_seconds`; do not accept the
28% fallback when a clearly stronger reviewed frame exists.

Use a white upper setup/quote/question region and a yellow lower action/turn/consequence
region. Each region may use one or two lines and must remain legible at 320×180. Keep every
line inside the centered 4:3 safe area. Favor a close expression, a naturally visible SC/chat/configuration, or
two actually participating speakers. Do not add a guest who is merely mentioned. Do not add
custom panels, cutouts, or invented evidence outside the approved AutoClip rules.

The 2026-09-03 recent-comprehensive audit reinforced three patterns: successful narrative
covers often use two to four total lines rather than enforcing a ten-character ceiling;
the largest words describe a concrete quote, question, object, or abnormal situation; and
the following line supplies the reaction or consequence. Treat this as a structure to test,
not wording to copy. Reject pairs whose only information is generic intensity such as
`高能`, `爆笑`, `震惊`, `破防`, `认真`, `完整`, or `当场`.

Song covers remain separate: use the creator's designated background and complete song title.
Do not apply narrative conflict copy to a song cover.

## Creator-specific editorial search terms

These are content-discovery terms, not automatic ASR replacements.

- **Sumire:** family interruption, 妹妹／姐姐, 堇今色, 妈妈／紫色妈妈, 人设崩塌,
  文学少女, Staff／背调, 独轮车, 提督／红SC, 逆天ID, 被钓, 同期关系,
  小粉螈（粉丝牌）, 得意（直播热词）.
- **Kioi:** 花礼, 灰泽满, 犬绒, 能能, 雾深, 栞栞, 267, 美国人／绿卡,
  鱿鱼／前辈, 记错／忘记, 小学生吵架, 取外号, 踢群, 现场逮捕, 沉默／恼羞成怒.
- **Viridis:** 植物, 球茎／硬茎, 枼绿素, 孙女／小孙女（直播热词）, 绿神／绿考子,
  蟑螂／标本, 椰子鸡, 讲道理／互相攻击, 联动, 3050／3060, 闭麦／麦克风没关,
  弹幕姬, 卡关／操作.
- **Yuchu:** 宇宙猫, 外星人, SC钓鱼, 型月／原神／ylg, ROG／电脑配置,
  身高152, 百舰, 社保／上班, 唱歌评价, 暴露同期好感, 表白／拒婚, 道歉／压力.
- **Chilly:** 昼你鸭（粉丝牌）, 第一志愿, 县城爱情, 学历, 985／大专,
  炒股／家庭损失, 虚拟的0和1, 初恋／复合／私奔, 下头弹幕; songs favor a concise
  emotional lyric hook plus the complete song title while retaining the designated Chilly
  song background. Chilly is not a VR streamer; search or tag VR only when the current
  retained event actually discusses VR.

Market evidence verifies `堇今色`, `枼绿素`, `宇宙猫`, and `孙女` as recurring public
usage. User-confirmed ownership is authoritative for this workflow: `小粉螈` is Sumire's
fan badge, `昼你鸭` is Chilly's fan badge, `得意` is a recurring Sumire stream term, and
`孙女` is a recurring Viridis stream term. Keep `今色` as a reviewed Sumire ASR variant.

Yuchu's `八分钱` / `飞八分钱` is a meme tied to that event. Do not turn it into a general
selection rule favoring exact numbers; exact amounts or measurements still need their own
causal payoff in the current source.
