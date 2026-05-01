---
name: daily-report
description: 汇总前一日工作会话，生成结构化日报并保存
---

# Daily Report — 日报生成

每日自动汇总前一日所有工作会话，生成结构化日报。

## 使用方式

直接调用（无需传入参数），Daily Report 会：

1. 查询前一日所有有效 work session
2. 读取摘要、待办、风险标签
3. 生成日报 Markdown
4. 保存到 `data/reports/daily/{date}.md`

## Workspace 约束

- Daily Report 是全局报表产物，最终日报仍保存到 `data/reports/daily/{date}.md`。
- 如果它是在某条消息的 workspace 中被触发，必须先读取
  `workspace/input/artifact_roots.json`；如需目录导航，再读取
  `artifact_roots.session_dir/session_workspace.json`。临时文件、草稿或调试输出写入
  `artifact_roots.scratch_dir`，不要写到项目根目录。
- 汇总单个 work session 时，优先读取该 session 自己的
  `data/sessions/session-<id>/workspaces`、`.ones`、`.triage`、`.review`、
  `uploads`、`attachments` 和 `scratch` 上下文。

## 关联脚本

- `scripts/generate_daily.py` — 手动触发日报生成

## 定时触发

通过 APScheduler 配置每天 08:00 自动运行。
