# Structured Workflow State

在以下场景进入结构化状态流：

- 附件或日志文件较多
- 预计需要多轮补料
- 问题时间、版本、模块存在不确定性
- 需要跨多个日志文件建立证据链
- 用户后续大概率会要求正式报告或 `5 why`

## Lightweight State Contract

复杂问题不需要照搬 `fix-issue` 的完整工单状态机，但需要维护一个轻量状态对象。可以放在会话记忆里，或在复杂场景写到 `.triage/<topic>/00-state.json`。

建议字段：

```json
{
  "project": "allspark",
  "mode": "structured",
  "phase": "keywords_ready",
  "problem_summary": "",
  "version_info": {
    "value": "",
    "status": "known | uncertain | missing"
  },
  "artifact_completeness": {
    "status": "complete | partial | unknown",
    "notes": []
  },
  "time_alignment": {
    "problem_time": "",
    "log_time_format": "",
    "timezone": "",
    "status": "aligned | mismatched | unknown"
  },
  "module_hypothesis": [],
  "target_log_files": [],
  "call_chain_status": "pending | built | partial",
  "keyword_package_status": "pending | ready | revised",
  "search_status": "pending | delegated | returned",
  "evidence_chain_status": "weak | partial | complete",
  "confidence": "low | medium | high",
  "missing_items": [],
  "user_confirmation": {
    "formal_report": false
  }
}
```

## Recommended Phases

- `initialized`
- `artifact_profiled`
- `completeness_checked`
- `config_routed`
- `call_chain_built`
- `keywords_ready`
- `search_delegated`
- `evidence_reviewed`
- `awaiting_input`
- `ready_for_report`
- `report_confirmed`
- `completed`

## Phase Rules

- `keywords_ready` 之前，不要启动大规模日志搜索。
- `search_delegated` 阶段只允许把最小搜索输入交给子 agent，不要把大量原始日志塞回主线程。
- `evidence_reviewed` 阶段必须判断证据链是否完整，以及置信度是否足够高。
- `ready_for_report` 仅表示“可以出正式报告”，不代表应该立刻输出。
- `report_confirmed` 只有在用户明确同意后才能进入。

## Search Worker Contract

把大规模搜索下沉到子 agent / 子 skill 时，输入尽量收敛：

- 问题摘要
- 时间窗口
- `keyword package`
- 目标日志文件或目录
- 当前假设列表

子 agent 返回时只保留：

- 命中的关键日志条目摘要
- 文件路径、时间点、命中次数
- 对应关键代码类/方法
- 未命中或噪音过大的说明

不要回传整份原始 grep/zgrep 输出。

## Interim Update Contract

每轮中间汇报保持短格式：

- `阶段`
- `当前结论`
- `关键证据`
- `关键代码`
- `当前置信度`
- `缺口`
- `下一步`

如果当前还不适合出正式报告，明确说明“暂不出正式报告，等待补料或用户确认”。

## Final Report Gate

只有满足以下条件时，才建议请求用户确认出正式报告：

- 版本、时间、模块基本明确
- 至少一条核心调用链已经建立
- 关键词搜索结果已经返回
- 证据链达到 `partial` 或 `complete`
- 置信度至少为 `medium`

用户未确认前，不输出完整 `5 why`。
