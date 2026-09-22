# 直播切片工作台功能盘点

审计日期：2026-09-05。范围：当前桌面工作台、共享处理核心、自动队列和审核工作区；依据当前源码与调用关系核对。此文件不包含录播内容、账号凭证或投稿回执。

## 主要构成

| 层次 | 主要文件 | 职责 |
| --- | --- | --- |
| Windows 启动入口 | 工作区根目录 `切片工作台.vbs`、`切片工作台.cmd` | 选择工作区 Python、补充 FFmpeg 路径，启动桌面程序；VBS 隐藏命令行窗口 |
| 桌面界面 | `scripts/workflow_app.py` | Tkinter 页面、全局工具、后台命令队列、任务进度、审核编辑器和日志 |
| 单项目处理核心 | `scripts/workflow_app_core.py` | 项目状态、转写、选片输入与导入、审核素材、烧录审计、投稿预览与上传 |
| 自动队列 | `scripts/workflow_auto.py` | 录播发现与归组、连续素材准备、调用 Codex、阶段推进、暂停/重试/重做、定时交付和清理计划 |
| 审核工作区 | `scripts/review_workspace.py` | 审核决定、ASS 编辑、源视频与成片时间映射、剪辑草稿、波形、预览媒体和内嵌 VLC |
| 人物与素材配置 | `scripts/creator_profiles.py`、`scripts/virtuareal_glossary.py`、`scripts/cover_emotes.py` 等 | 人物、字幕样式、专名纠错、封面素材及相关校验 |
| 媒体处理和投稿工具 | `scripts/content_slicer.py`、各导出/字幕/审计脚本、`scripts/biliup_publish.py`、`scripts/local_publish.py` | 执行 ASR、FFmpeg、封面与字幕处理，以及 B 站投稿、合集和回执管理 |

手动项目和自动任务调用同一套处理核心。两者的区别主要在素材入口、队列调度和用户如何启动阶段，不是两套独立的切片算法。

处理顺序为：流程 0 预检与合并素材 → 流程 1 转写与互动分析 → 流程 2 Codex 或人工选片 → 流程 3 视频、字幕与封面 → 人工审核或已授权的自动处理 → 流程 4 烧录与审计 → 流程 5 预览与投稿。手动单素材项目从流程 1 开始；自动任务负责流程 0。

## 当前功能清单

### 任务与流程

| 功能组 | 用户可以完成的操作 | 源码入口示例 |
| --- | --- | --- |
| 自动监控 | 配置录播目录与输出目录；启动/停止监控；只执行已排队任务；设置文件稳定及断线观察时间 | `start_monitor`、`stop_monitor`、`start_pending_queue` |
| 录播识别与归组 | 发现支持的音视频文件；按直播间识别人物；等待录播稳定；把断线分段整理为同场连续素材和 XML 时间轴 | `workflow_auto.discover_media`、`group_recordings`、`scan_state` |
| 自动处理设置 | 配置 Codex CLI；选择转写设备、精度和模型；配置未审核自动烧录/投稿、首次回填和每日交付时间 | `save_auto_config`、`_auto_permission_changed` |
| 历史批处理 | 按本机起止时间把已稳定的完整场次纳入处理 | `start_batch` |
| 队列状态 | 显示直播时间、主播、场次、当前阶段、进度、状态和最近说明；多选任务 | `refresh_auto_status`、`selected_auto_job_ids` |
| 队列控制 | 暂停/继续任务、移到队首；运行中的任务在安全阶段边界响应 | `toggle_pause_selected_auto_job`、`prioritize_selected_auto_job` |
| 直播中快速切片 | 从任务右键菜单回溯最近 5 分钟创建切片任务 | `quick_clip_selected_live_job` |
| 失败恢复与重做 | “更多操作”中重新调用 Codex；从流程 0–5 指定起点重做；失败任务重试 | `retry_selected_auto_codex`、`redo_selected_auto_flow`、`retry_failed_auto_job_on_click` |
| 晚到分段 | 从“更多操作”将尚未处理的新录播分段拆为续任务 | `continue_selected_late_segments` |
| 交付与清理 | 打开所选任务审核台/交付目录；所选任务烧录并上传；按所选任务清单废弃或清理 | `open_selected_auto_review`、`publish_selected_auto_job`、`discard_selected_auto_job` |
| 手动项目 | 在“手动项目”面板选择/载入项目、录播与 XML、自动匹配同名 XML、选择人物；在共用设置中选择普通、歌切或混合模式 | `load_project`、`save_project`、`_sync_xml_for_source` |
| 手动分阶段操作 | 转写；生成/复制 Prompt；导入选片 JSON；生成审核素材；烧录；预览与上传；打开阶段输出目录 | `run_core`、`copy_prompt`、`choose_selection_json`、`start_burn`、`start_upload` |

### 切片审核台

| 功能组 | 用户可以完成的操作 | 源码入口示例 |
| --- | --- | --- |
| 全局审核池 | 汇总所有人物与场次的待审核/退回条目；显示通过历史；载入指定批次 | `load_global_review_pool`、`load_review_directory` |
| 视频预览 | 内嵌播放、暂停、停止、前后跳转、倍速、外部播放器；显示编辑中的字幕 | `play_current_embedded`、`set_review_playback_rate`、`open_current_video` |
| 时间轴剪辑 | 视频/字幕双轨、波形、切开、删除片段、延伸上下文、恢复中间空隙、拖动/缩放；应用或放弃剪辑草稿 | `cut_timeline_at_playhead`、`delete_selected_timeline_segment`、`extend_timeline_context`、`apply_timeline_edit` |
| 字幕编辑 | 按文字/说话人/时间检索；新增/删除字幕；改文字、起止时间及角色；自动写回；撤销/重做；强制单行与重叠修复 | `filter_review_subtitles`、`_autosave_subtitle_line`、`repair_current_subtitle_overlaps` |
| 说话人与嘉宾 | 选择参与嘉宾、编辑多人素材类型与依据、确认嘉宾台词；把整组说话人对应到人物模板 | `open_participant_selector`、`open_speaker_role_mapping` |
| 投稿文案 | 编辑标题、封面上下区文字、简介、Tag、合集；即时检查 Tag | `save_current_review_copy`、`refresh_review_tag_status` |
| 封面制作 | 按当前文案重生成封面；调整模板与画面、取帧时间、自选图片、表情包，并预览结果 | `open_cover_maker`、`rerender_current_review_cover` |
| 审核决定 | 写入通过、不通过；回退上次通过；批次决定完成后继续交付；最后一条通过留有撤销时间 | `set_current_review_decision`、`undo_last_review_approval`、`maybe_auto_burn_review_batch` |
| 已发布修订 | 修改已通过/已发布条目，重新烧录并替换已投稿源 | `_ensure_current_approved_revision`、`replace_selected_published_job` |
| 整场人工选片 | 播放整场、检索完整转写、按播放位置标记范围，填写文案后新建切片并进入审核 | `open_session_clipper` |

### 全局工具

- 人物与字幕模板：新增/归档人物，配置房间、热词、字幕色/字体/字号、封面素材、固定简介、Tag、合集和投稿开关，并预览样式。
- 热词设置：维护通用及人物热词；用于专名核对和字幕校验。
- 纠错词典：检索、新增、启用/停用规则；审核中可新增词组并应用到当前字幕。
- 投稿账号：扫码登录，以及凭证模板和已有 Cookie 导入管理。
- 环境检查、保存与 Ctrl+S、统一刷新、运行/排队任务取消、可展开或收起的运行日志。

## 本次界面精简

| 内容 | 处置 | 原因 |
| --- | --- | --- |
| 独立“主页”和“新录播自动监控与进度”页 | 合并为“任务与流程”；另一页保留“切片审核台” | 两个入口共用处理核心，日常使用以队列为主 |
| 常驻手动五阶段设置与操作区 | 移入“手动项目”折叠面板 | 保留人工单素材处理能力，减少日常队列页占用 |
| 监控参数与历史起止时间 | 分别放入“监控与处理设置”“历史批处理”折叠面板 | 将低频配置从主要进度区收起 |
| 三个辅助面板 | 同时最多展开一个 | 保持队列和任务动作可见 |
| 重复的人物模板及分散的通用设置入口 | 在“任务与流程”顶部工具区集中提供模板、热词、纠错；账号、环境检查和目录操作使用菜单 | 不再需要返回原主页寻找通用工具 |
| 分散的状态刷新按钮 | 统一刷新入口 | 手动项目和自动队列状态统一更新；原有周期刷新保留 |
| 重复的多选、流程及页面说明 | 缩短并合并 | 操作说明保留在对应控件附近，减少重复阅读 |
| `Workbench.open_selected_auto_job` | 移除无调用的旧方法 | 原方法仅打开选中项目目录；生产代码、测试和文档无引用，UI 没有绑定它 |

“保存”和 Ctrl+S 按当前上下文执行：在审核页保存当前文案/字幕；在“任务与流程”且展开“手动项目”时保存手动项目；在其余面板或全部收起时保存自动流程配置。手动项目内还保留“保存/更新项目”按钮。模式和 ASR 参数在“监控与处理设置”中共用；启动手动处理阶段时会先保存当前项目。

## 保留及未改动部分

- “重新调用 Codex”和“从流程 2 重做”保留。前者针对失败/等待状态的选片重试；后者属于可指定阶段的成果归档与重算，适用状态和后续处理不同。
- “执行已排队任务”、按时间历史批处理和首次回填保留。三者分别处理已有队列、指定时间范围以及监控初始已有素材，不能互相替代。
- 手动项目、全局审核池、单批次载入、整场人工新建切片、多人字幕、已发布换源等独立能力保留。
- 确认范围仅限界面组织与明确无引用的方法；`workflow_auto.py` 和 `review_workspace.py` 的退役逻辑候选本次不删。
- 静态扫描还发现 `_creator_subject_name` 和 Aegisub 探测/启动方法无生产调用；字幕编辑器状态辅助函数、旧代理生成及边界扩展方法仍有测试或兼容背景，单凭“只有定义/测试引用”不足以扩大删除范围。
- 不清理录播、项目状态、任务记录、成片、历史备份、脚本集合、运行日志或凭证。界面中的任务清理功能仍由用户按实际任务发起。

## 验证边界

本盘点属于源码和调用关系审计。已对照整理前备份核查界面构建函数的操作绑定：除两个旧页面构建器被替代、两种刷新合为一个入口外，原有操作方法均有界面绑定；旧标签页引用已替换，保存分支仍区分手动项目、自动设置和审核内容。

界面及处理核心的回归测试只能确认对应测试覆盖的行为；不代表本次实际执行了整场转写、生成切片、烧录或公开投稿。没有使用真实稿件的上传来验证界面整理。
