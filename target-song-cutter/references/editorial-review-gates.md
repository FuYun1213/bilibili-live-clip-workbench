# 直播叙事切片复盘门禁

这份规则适用于横屏、竖屏和电台叙事切片。它把用户审片反馈转化为交付前的硬门禁；任何一项失败，都不能把切片标成完成。

## 1. XML 弹幕必须参与选材

同目录存在 Bilibili 录播 XML 时，选材前必须运行：

```powershell
python scripts/analyze_engagement.py --input "recording.xml" --output "engagement-xml" --windows 10,30,60
```

Before final ranking, create `topic-map.csv` and `candidate-report.csv`. Build candidates from semantic story islands, lag-aware engagement peaks, SC steering chains, reactions, explicit editorial searches, and repeated user preferences. Deduplicate proposals by topic and payoff. No individual lane may automatically create a clip.

Inspect each hotspot from `cause_search_start` through `payoff_search_end`, not only at the chat peak. Check `user_coverage_ratio`, `same_user_repeat_ratio`, and `spam_risk`; preserve many-user consensus and penalize repeated sender-message pairs. For every relevant SC, review its search range, hear the creator response, fill the confirmed response range, and never claim the later chat peak was caused by the SC without source evidence.

至少查看 `danmaku-bursts.csv`、`repeated-messages.csv`、`keyword-context.csv` 和 `superchats.csv`。高频窗口不是自动成片结论，而是候选素材库：把弹幕峰值、重复词、SC 原文映射回音视频，再核对前因、转折和后果。不得只依赖 ASR 关键词。

Also inspect `engagement-hotspots.csv`, `engagement-summary.json`, and `timeline-audit.json`. Review and classify relevant rows in `superchats.csv`; do not leave an SC-driven candidate at `unreviewed`.

XML 时间轴可能和播放器或分段录播有偏移。先用一条可听见或可看见的共享事件校准，再应用到所有候选点。把高频窗口、原始弹幕/SC 文本、映射后的音视频时间和候选分数留在工作目录中。

只处理 3–5 分素材。1–2 分直接淘汰，不进入剪辑计划、复审目录、标题表、字幕、封面或候选存档，也不通过补上下文、拆片、改短梗或倒叙进行扩展。3 分只允许补足使原事件和因果关系成立的最短前文；4–5 分优先。

When prior rated evaluation files exist, run `scripts/summarize_clip_feedback.py` and read both feedback outputs before ranking. Apply only the repeated -1/0/+1 adjustment described in `feedback-calibration.md`; user preference never overrides completeness, fairness, or duplicate rejection.

## 2. 第一条字幕完整性门禁

整场转写只用于发现素材。成片导出后，必须对实际 MP4 做 clip-local Whisper，并使用词级时间轴生成 ASS。

每条成片必须人工听前 8–12 秒，并核对：

- 第一条字幕必须是画面中真正说出的第一句，不能从旧时间轴带入前一句，也不能漏掉起因。
- 单条字幕不可把 10–30 秒整段转写压在首屏。词级字幕按停顿、标点、约 18 个汉字或约 4.2 秒拆分。
- 乱码、替换字符、错误人名、数字和专有名词必须人工修正；ASR 只是时间轴，不是原文真相。
- ASS 的角色 `Style` 和 `Name` 必须实际存在且属于当前主播，禁止沿用其他角色名或色号。
- 中文叙事字幕必须为简体中文。烧录前对每个 ASS 做 OpenCC `t2s` 等确定性对比；只要转换结果发生变化就阻断交付，先转换文本并复核。若繁体字幕已经烧入草稿，必须从无字幕审阅母版或 `radio-layout` 母版重压，严禁在旧烧录版上叠加简体字幕。
- 烧录前必须检查 ASS 事件按开始时间单调递增、结束时间不超过实际 MP4 时长，并删除被更短顺序分段覆盖的低置信度长段；禁止把重复转写叠在同一画面上。
- 片头、片中、片尾各抽一个语音锚点。任何锚点漂移都要重跑或重切。

第一句错误、首屏大段堆叠、角色样式错误，任一项都直接阻断烧录和交付。

## 3. 完整叙事与冗余裁剪

每条叙事切片必须能用一句话说明“起因 → 反应/升级 → 结果或反转”。没有结果的片段要继续向后找原文；如果后文已经转入另一个梗，就拆成两个候选，不能强行拼接。

逐个检查每一段保留区间的开头和结尾，不只检查整条成片的首尾。剪点必须落在完整语句、完整反应或可听见的自然停顿之后，禁止把“然后、所以、但是、就是、好不好、好吗”等仍在完成当前意思的接句切掉。转写分段边界不等于可用剪点；必须回听源录播，并在最后一个音节后保留约 0.2–0.5 秒自然尾音或静音余量。若下一句仍在补充当前因果、否定、限定或情绪落点，应继续保留到意思完整，而不是为了压时长截断。

所有非连续拼接都要单独回听切点前后各至少 3 秒，确认没有半个字、吸气后缺失的后半句、突兀接词、被切掉的回答或音频爆点。任何一个内部剪点失败，都必须回源重切并同步重做字幕；视频能完整解码不代表语言剪辑合格。

SC 和弹幕驱动的片段必须保存并核对原始文本。标题和字幕要让观众知道主播究竟在回应什么，不能只留下泛泛的“谢谢”或脱离问题的回答。

剪掉以下内容：

- 与主线无关的书、游戏或生活岔题。
- 随口读弹幕、无反应的问答、礼物感谢和重复解释。
- 同一句无意识说了两次时，保留更清楚或情绪更强的一次。

如果同一短句连续出现 4 次或更多，并且形成预期、递进或节奏梗，可以保留；否则压缩。保留重复时要能说明每一次如何升级情绪或信息。

倒叙适合“先有强烈反应，后有必要背景”的素材。固定流程为：后置爆点冷开场（3–15 秒）→ 渐进式高斯模糊 → 最早且最短必要背景 → 如需回到后文，再用一次渐进式高斯模糊 → 爆点后续和完整结果。不要为了倒叙重复整段爆点。

每一次非时间顺序跳转都必须实际渲染转场：切点前约 0.25 秒由清晰渐变到高斯模糊（sigma 8–12），在切点达到最大模糊；切点后约 0.25 秒由同等模糊恢复清晰。转场作用于完整合成画面，音频保持原声，不加淡入淡出。导出器若只能硬拼接，必须在导出后补做并目视检查；缺少任一处转场不得交付。普通顺序删减不加模糊。默认转场总长 0.5 秒；Prompt、JSON 与两个实际渲染入口的接口见 [media-packaging-and-callback.md](media-packaging-and-callback.md)。

## 4. 标题和封面组合

每条 3–5 分切片必须提供三套完整组合：

```text
bundle_id,title,cover_mode,cover_text_primary,cover_text_secondary,evidence_frame
```

三套分别覆盖原话冲突、叙事反转、关系/挑战三个角度。当前账号沿用前置频道标签：标题固定写成 `【中文名English】完整叙事句`，主播标签只出现一次，且 `【】` 内只写主播身份，不写爆点梗概。正文紧接最强钩子，并且不能在结尾重复标签。

标题正文必须是一句语法和叙事都完整的话，至少包含“明确主语 + 具体动作/遭遇 + 结果、反转或情绪反应”。`【主播名】` 只是频道标签，不算正文主语；“爆了”“破防了”“社恐到见人腿软”“一袋桃全是坏的”之类前缀或短语也不能代替主语。优先在正文直接写主播、人名、团体或事件参与者；只有同一句前文已经出现明确先行词时才使用“她/他/她们”。

合格示例：`【柚雨Kioi】柚雨刚说自己从小文静又听话，下一秒她就承认童年炸过一次马桶！`。不合格示例：`【柚雨Kioi】乖乖女人设撑不过十秒！`。情绪前缀可以使用，但删去标签和前缀后，剩余正文仍须独立成立为完整叙事句。

封面必须从 `quote-impact`、`evidence-reaction`、`clash-challenge` 中选择一种。普通切片副标题写 2–3 句，每句 3–22 个有效字；第一句放上区，其余 1–2 句放下区并用｜分隔，每句可手动换一行。文案按 [narrative-cover-prompt.md](narrative-cover-prompt.md) 写作：采用“具体事件＋观众吐槽”的 D 风格，优先每句 6–14 字；陌生观众不看标题也应看懂事件和吸引点。第三句只在补充不同事实、必要背景或具体观察时使用，不得重复或凑情绪尾巴。不能把完整标题缩小贴上去，也不能只写“高能、爆笑、震惊、破防、认真、当场”等空评价。

封面默认使用黄/白大字、近黑粗描边，并只用红色强调反转或后果。证据型封面必须保留原始 SC、弹幕、聊天、动态或画面证据，不得用生成模型伪造或重写。所有标题和封面字段都禁止英文单引号。
## 5. 发布前清单

- XML 高频候选库已经生成并实际用于选材。
- 交付中只有 3–5 分素材；1–2 分没有成片、字幕、标题、封面或扩展版本。
- 每条第一句已经听写核对，首屏没有长段堆叠。
- SC/弹幕原文、上下文和结尾完整。
- 每一个保留区间的首尾和每一个拼接点都已回听；没有半句话、半个字、缺失接句或硬切，末字后保留了自然尾音余量。
- 无意义岔题、读弹幕、礼物感谢和一次性复读已经删除。
- 倒叙片只保留最短必要背景，爆点在前且有自然回收；每一次非时间顺序跳转都已实际渲染并目视检查渐进式高斯模糊。
- 每条有三套不同角度的标题—封面组合；主播标签在最前且只出现一次，`【】` 中只有主播身份，封面模式、主字、副字和证据帧均已填写或明确留空。
- 每个标题删去主播标签和情绪前缀后，仍有明确主语、具体动作和结果/反转；不存在只有话题短语、状态短语或无主句概括的标题。
- 当前主播的字体、字色、ASS Style 和 Name 全部正确。
- 竖屏/电台切片只交付横屏电台模板烧录版和同名 ASS；右侧字幕实际可见且字号固定为 65，没有空右栏中间态或普通竖屏重复版。
