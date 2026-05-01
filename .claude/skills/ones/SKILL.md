---
name: ones
description: 处理 ONES 工单下载、正文与图片联合读取、结构化摘要生成、附件补齐、项目推断与问题分析前置 intake。Use when Codex needs to 识别 ONES 链接、先抓工单与附件、读取正文和截图、生成 summary_snapshot.json 这类后续可直接消费的结构化数据，或把 ONES 问题作为项目分析/日志排障/review 的前置工作流。
---

# ONES

对 ONES 问题执行“先抓工单、再读正文、再读图片、最后输出结构化摘要”的 intake 工作流。

## Trigger

Use this skill when:
- 用户提供 ONES 工单链接、task uuid、任务号或 `#123456` 这类工单引用。
- 用户要求读取工单正文、评论、描述图、附件，或把工单事实转成结构化摘要。
- 其他 skill 需要先获得 ONES 工单事实、附件清单、缺口判断或 `summary_snapshot.json`。

Do not use this skill when:
- 用户只是闲聊、确认收到，或没有给出任何工单线索。
- 用户已经提供了完整的结构化工单事实，且当前任务只需要代码或日志分析。

When uncertain:
- 先读取 `workspace/input/artifact_roots.json`、`message.json` 和 `media_manifest.json`；
  如需目录导航，再读取 `artifact_roots.session_dir/session_workspace.json`。
- 如果仍无法确认是否是 ONES 工单，只返回一个最小澄清问题，不要臆造工单事实。

## Core Rule

- ONES intake 的目标不是直接下根因，而是先生成一份后续链路可直接消费的结构化摘要。
- 先读取 `workspace/input/artifact_roots.json`。所有 ONES 下载产物必须写入其中的
  `ones_dir`，不要写到项目根目录 `.ones/`。
- `fetch-task` 下载完成后必须同步生成并绑定 `summary_snapshot.json`：
  脚本默认只生成确定性摘要，不在 agent 内部再启动 `ones-summary` 子 agent；
  Core 会在主 agent 启动前用非嵌套的摘要流程读取描述图并补齐 `image_findings`。
  如果图片摘要不可用，仍必须写出确定性 `summary_snapshot.json` 并回写
  `task.json.summary_snapshot` / `task.json.paths.summary_snapshot_json`。
- 用户上传到当前消息的原始图片、文件或补充材料从 `artifact_roots.uploads_dir` /
  `workspace/input/media_manifest.json` 读取；如果需要解压或整理为分析材料，写入
  `artifact_roots.attachments_dir`。
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

默认产物位于 `artifact_roots.ones_dir/<task-number>_<task-uuid>/`：

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

调用脚本时必须显式传入 session-scoped 输出目录，例如：

```bash
python .claude/skills/ones/scripts/ones_cli.py fetch-task "<ONES 链接>" \
  --output-base-dir "<artifact_roots.ones_dir>"
```

默认摘要模式是 `--summary-mode deterministic`，不要在业务 agent 内部再显式使用
`--summary-mode subagent`。图片读取由 Core 的 ONES pre-intake 在主 agent 启动前完成，
业务 agent 只消费已有 `summary_snapshot.json`。

可复用的确定性辅助逻辑都在本 skill 内：

- `scripts/routing.py`：ONES 链接解析、项目路由线索评分、直接读取工单元数据后的候选项目判断。
- `scripts/intake.py`：ONES 工单产物读取、图片/附件清单、证据完整性检查、`summary_snapshot.json` 合并与持久化。

只有在仓库内脚本不存在时，才回退到 `C:\Users\Standard\.codex\skills\ones\scripts\ones_cli.py` 这类全局副本。

如果当前 workspace 或 `artifact_roots.ones_dir/<task-number>_<task-uuid>/` 已经有
`task.json` / `summary_snapshot.json`，直接读取已有产物，不要重复抓取。

## Output Contract

如果当前还处在 intake 阶段，只输出结构化摘要和缺口，不直接给最终根因。
