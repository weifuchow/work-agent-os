---
name: ones
description: 处理 ONES 工单下载、正文与图片联合读取、结构化摘要生成、附件补齐、项目推断与问题分析前置 intake。Use when Codex needs to 识别 ONES 链接、先抓工单与附件、读取正文和截图、生成 summary_snapshot.json 这类后续可直接消费的结构化数据，或把 ONES 问题作为项目分析/日志排障/review 的前置工作流。
---

# ONES

对 ONES 问题执行“先抓工单、再读正文、再读图片、最后输出结构化摘要”的 intake 工作流。

## Core Rule

- ONES intake 的目标不是直接下根因，而是先生成一份后续链路可直接消费的结构化摘要。
- 默认产物应包含：
  - `summary_text`
  - `problem_time`
  - `version_text`
  - `version_fields`
  - `version_from_images`
  - `version_normalized`
  - `business_identifiers`
  - `observations`
  - `image_findings`
  - `missing_items`
- 一旦 `summary_snapshot.json` 已生成，后续项目分析应优先只读这份摘要，不再反复直接读取原始图片。

## Workflow

1. 识别 ONES 链接或 task id。
2. 下载工单、评论、描述图和附件。
3. 优先读取正文，生成 `description_local`。
4. 结合描述图补充读取。
5. 生成 `summary_snapshot.json`。
6. 后续项目分析、review 或日志排障优先只消费这份 snapshot。

## Artifacts

默认产物位于 `.ones/<task-number>_<task-uuid>/`：

- `task.json`
- `task.raw.json`
- `messages.json`
- `report.md`
- `desc_local.md`
- `summary_snapshot.json`
- `attachment/`

## Script Location

优先复用仓库内 skill 脚本：

```bash
python .claude/skills/ones/scripts/ones_cli.py fetch-task "<ONES 链接>"
```

只有在仓库内脚本不存在时，才回退到 `C:\Users\Standard\.codex\skills\ones\scripts\ones_cli.py` 这类全局副本。

如果当前链路已经通过 pipeline 自动抓取了工单，则直接读取已有的 `task.json` / `summary_snapshot.json`，不要重复抓取。

## Output Contract

如果当前还处在 intake 阶段，只输出结构化摘要和缺口，不直接给最终根因。
