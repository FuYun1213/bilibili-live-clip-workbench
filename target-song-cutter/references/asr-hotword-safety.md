# 热词复读防护

当前策略：`audio-only-no-hotword-prompt-v2`；字幕检查版本：`asr-hotword-guard-v2`。

## 原因与方案

识别提示中的热词不是“只许识别这些词的词典”。本地 Qwen3-ASR 曾在短声音、停顿和背景音片段把 context 整串抄成字幕；随后旧流程无条件标为 PASS，错误沿源转写进入切片。故障由原始转写和原音频对照确认。

生产解码入口不再注入词表：本地 Qwen 的模型 context 始终为空；guard_terms 只保存在适配器元信息里；云 Qwen 不发送 vocabulary；Whisper 不发送 initial_prompt/hotwords；FunASR 的 hotword 参数为空。降级模型和补转写沿用同一限制。专名词表继续保存，已核实的纠错映射在识别后应用。

增加一句“不要输出热词”、缩短词表或换更大的模型，都不能从机制上消除提示词被复制的可能性。移除词表注入消除了这条污染来源；独立字幕检查用于阻止历史缓存或后续编辑再次带入同类词表。

## 检查与恢复

- 源结果、失败缓存复用、选片上下文、源字幕重映射、审核前后文、烧录和交付分别检查有效字幕。
- 按当前字幕内容重新检查，并记录 SHA-256；不能只凭旧 PASS 或人工通过标记放行。
- 检测单行整串、无分隔符、样式标签、全角字母及连续多行词表；单独提及姓名及正常叙述不因包含热词而被删除。
- 保存源批次词表快照，后续改词表也不失去检查旧字幕的依据。
- 命中时阻断后续生成并要求从原录播时间核实，不能用全局替换或一律删名处理。
- 修复只更新仍存在的污染字幕及相关源缓存，保留人审修改、删除和时间轴；已发布的线上稿件另行处理，不能把本地修复当成线上替换。
- 热词隔离不能保证识别模型从此零错字、零幻觉。对无提示仍不确定的短声音，结合前后文和独立识别核对；没有对白依据的猜词不得采用。

相关回归：`test_asr_hotword_guard.py`、`test_hotword_pipeline.py`、`test_general_hotwords.py`、`test_qwen_asr.py`、`test_qwen_local_asr.py`、`test_content_slicer.py`。

## 依据

- [Qwen3-ASR 官方实现](https://github.com/QwenLM/Qwen3-ASR/blob/main/qwen_asr/inference/qwen3_asr.py)：context 是模型输入上下文。
- [Qwen3-ASR 上游同类问题](https://github.com/QwenLM/Qwen3-ASR/issues/140)：0.6B 热词复读反馈；本地结论仍以本批原音频与转写为准。
- [faster-whisper 官方实现](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py)：initial_prompt/hotwords 进入解码提示，所以备用入口也禁用词表注入。
