# 直播切片工作台

一个面向 Windows 的本地直播切片工作台，把录播预检、转写、选片、字幕、封面、审核、烧录和可选的 B 站投稿串成一套可恢复的流程。

## 主要功能

- 扫描录播分段，预检并合并同场直播素材。
- 本地 ASR 转写，支持弹幕、SC 和热点信号分析。
- 辅助生成选片结果，并通过人工审核门禁控制后续处理。
- 生成切片、ASS 字幕、封面和审核素材。
- 集中的字幕与时间轴审核台，支持修改和重做。
- 在明确授权后通过扫码登录执行 B 站投稿。

## 快速开始

1. 使用 Windows 10/11，建议预留至少 15 GB 空间。
2. 安装 FFmpeg/FFprobe 并加入 `PATH`；如需内嵌预览，另安装 64 位 VLC。
3. 双击 `首次安装.cmd`。安装器会查找 Python 3.12，未安装时尝试通过 winget 安装，然后创建独立环境并安装依赖。
4. 双击 `启动工作台.cmd`。
5. 需要投稿时，在工作台中使用“扫码登录”；不使用投稿功能时无需登录。

首次转写可能会下载数 GB 的模型。自动选片还需要已安装并登录的 Codex CLI；手动流程不依赖它。

## 目录结构

```text
target-song-cutter/
  scripts/       工作流、媒体处理和投稿代码
  assets/        界面、字幕、封面和人物模板
  references/    工作流规范与使用说明
  tests/         回归测试
录播/             本地录播目录（不入库）
workflow-projects/ 任务工程与审核产物（不入库）
models/           本地模型缓存（不入库）
```

## 隐私与安全

这个仓库不包含录播、弹幕、既有工程、已完成视频、模型权重、Cookie 或 API 密钥。公开的人物配置保留合集标题，但移除了与原投稿账号绑定的 `season_id` / `section_id`。

扫码登录后的 Cookie 应只保存在当前 Windows 用户的私有应用数据目录。不要提交 Cookie、实际录播、工程目录或包含个人信息的日志。

## 更多说明

- [本地工作台](target-song-cutter/references/local-workflow-app.md)
- [安装与调优](target-song-cutter/references/setup.md)
- [端到端流程](target-song-cutter/references/end-to-end-workflow.md)
- [B 站投稿](target-song-cutter/references/biliup-publishing.md)

