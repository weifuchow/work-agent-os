---
name: feishu_card_builder
description: 将业务 skill 的结论整理成结构化摘要，并渲染成通用 Markdown 或已完成的飞书卡片 payload；只产出 reply contract，不直接发送飞书、不写 DB。
---

# Feishu Card Builder

把业务结论转换成稳定的结构化摘要，再按目标渠道渲染成最终可投递的 `reply` payload。这个 skill 负责摘要结构和展示策略，core 只负责把 payload 交给 channel adapter。

## Trigger

Use this skill when:
- 业务 skill 已经给出事实、证据、结论或缺口，需要输出结构化摘要。
- 结构化摘要需要转换成 `reply.type = markdown` 或 `feishu_card`。
- 回复需要表格、分段、补料提示或飞书卡片展示。
- 多个业务 skill 需要共享同一种回复结构。

Do not use this skill when:
- 业务 skill 已经直接输出完整 `reply` contract。
- 当前只需要纯文本回复。
- 还没有事实输入，需要先做工单 intake、日志检索、代码 review 或根因分析。

When uncertain:
- 飞书工作消息里的结构化摘要、项目分析、ONES 分析、日志排障结论，默认渲染 `feishu_card`。
- 只有目标 channel 不支持卡片、用户明确要求纯文本，或当前只是极短确认时，才输出 `markdown` / `text`。

## Workspace Rule

- 先读取 `workspace/input/artifact_roots.json`，确认当前
  `data/sessions/session-<id>` 是唯一上下文容器；如需目录导航，再读取
  `artifact_roots.session_dir/session_workspace.json`。
- 输入可以来自 `workspace/output`、上游 skill 的 JSON 文件或 stdin；不要重新读取平台、DB 或直接发送飞书。
- 如果需要保存标准化摘要、卡片输入或最终 `reply_contract`，写入当前 `workspace/output`。
- 如果需要临时文件或草稿，写入 `artifact_roots.scratch_dir`。
- 不要在项目根目录创建新的 `.ones/`、`.triage/`、`.review/` 或 `.session/` 目录。

## Contract

输入来自 workspace/output 或 stdin JSON，推荐字段：

```json
{
  "title": "标题",
  "summary": "一句话总结",
  "conclusion": "阶段性结论",
  "confidence": "high|medium|low",
  "facts": ["已确认事实"],
  "evidence": ["关键证据"],
  "risks": ["风险"],
  "missing_items": ["还缺什么"],
  "next_steps": ["下一步"],
  "sections": [{"title": "结论", "content": "内容"}],
  "steps": [
    {"title": "收到前端订阅请求", "detail": "网关建立 WebSocket 连接并校验参数"}
  ],
  "mermaid": "flowchart TD\nA[\"开始\"]-->B[\"结束\"]",
  "flowcharts": [
    {
      "title": "WebSocket 数据链路",
      "description": "从前端订阅到后端推送的关键路径。",
      "source": "flowchart TD\nA[\"前端订阅\"]-->B[\"后端鉴权\"]"
    }
  ],
  "code_blocks": [
    {
      "title": "轻量车辆帧 DTO",
      "path": "packages/presentation/src/main/java/.../RobotLiteDTO.java",
      "language": "java",
      "code": "public record RobotLiteDTO(...) {}",
      "note": "只保留地图渲染必要字段"
    }
  ],
  "table": {
    "columns": [{"key": "fact", "label": "事实"}],
    "rows": [{"fact": "已确认"}]
  },
  "fallback_text": "纯文本兜底"
}
```

输出必须是 core 通用 contract。飞书工作消息默认输出 `reply.type = "feishu_card"`，`reply.payload`
为完整卡片 JSON，`reply.content` 保留纯文本兜底。

只有明确需要 Markdown 时，才输出 `reply.type = "markdown"`，并在 `reply.payload.structured_summary`
保留机器可读摘要：

```json
{
  "action": "reply",
  "reply": {
    "channel": "feishu",
    "type": "markdown",
    "content": "结构化摘要 Markdown",
    "payload": {
      "structured_summary": {
        "title": "标题",
        "summary": "一句话总结",
        "conclusion": "阶段性结论",
        "confidence": "medium",
        "facts": [],
        "evidence": [],
        "risks": [],
        "missing_items": [],
        "next_steps": [],
        "sections": [],
        "table": null
      }
    }
  },
  "skill_trace": [{"skill": "feishu_card_builder"}]
}
```

飞书卡片输出必须包含 `schema = "2.0"`，并优先使用表格承载“检查项 / 结果 / 证据 / 缺口 / 下一步”。
当 `reply.type = "feishu_card"` 时，`reply.payload` 必须只包含飞书卡片 JSON 本体；不要把
`structured_summary`、调试信息、artifact 路径、skill metadata 或其他非飞书字段放进
`reply.payload` 根节点。需要机器可读摘要时，写入 `workspace/output` 的独立 JSON，或只在
Markdown 回复的 `reply.payload.structured_summary` 中保留。飞书会拒绝未知的卡片根字段。
如果 `workspace/input/project_workspace.json` 或 `project_context.json.project_workspace` 中存在已加载项目，
ONES/现场问题卡片必须包含“运行上下文”区块：本次实际使用的项目名、版本来源、normalized_version、
checkout_ref/target_branch、current_branch、worktree_path/execution_path、execution_version/execution_describe，
以及 execution_commit_sha/current_commit_sha。

## Summary Rules

- `summary` 必须是一句话，写清当前最重要的判断。
- `conclusion` 可以是阶段性结论；证据不足时要明确写“不足以下根因”。
- `confidence` 只允许 `high` / `medium` / `low`。
- `facts` 只放已确认事实，不放推测。
- `missing_items` 用于补料清单，不能混进结论。
- `next_steps` 必须是可执行动作。
- 涉及流程、链路、时序、状态流转、调用顺序、排障步骤、跨项目协作步骤时，优先输出 Mermaid 流程图：
  - 简单场景可直接填 `mermaid`。
  - 多张图使用 `flowcharts`，每项写清 `title/description/source`。
  - 同时需要文字步骤时使用 `steps`，不要只堆普通段落。
  - Mermaid 源码必须可被 mermaid-cli 解析；含中文、空格、箭头、括号、斜杠、冒号或等号的节点标签用双引号包住，
    例如 `A["actuator存在 -> nextStage(Cancel)"]`。
  - 飞书发送前会把 ` ```mermaid ` 块渲染成图片；不要把流程图放进 `code_blocks`。
- 需要展示代码时，优先使用 `code_blocks`；每个代码块写清 `title/path/language/code/note`。
  不要把大段代码混在普通正文里。代码片段应控制在关键方法或关键类型，避免整文件粘贴。
  如果上游只能提供 Markdown 代码块，也可以放在 section `content` 中，渲染器会拆成独立代码展板。
- 不要在这个 skill 内重新检索日志、读取工单或调用项目分析；需要这些能力时交给上游业务 skill。

## Scripts

```bash
python .claude/skills/feishu_card_builder/scripts/render_feishu_card.py --type markdown --input result.json
python .claude/skills/feishu_card_builder/scripts/render_feishu_card.py --type feishu_card --input result.json
```

脚本只渲染结构化摘要 / payload，不调用 Feishu API。
