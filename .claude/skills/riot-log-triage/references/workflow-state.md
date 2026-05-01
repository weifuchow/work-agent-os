# Structured Workflow State

在以下场景进入结构化状态流：

- 附件或日志文件较多
- 预计需要多轮补料
- 问题时间、版本、模块存在不确定性
- 需要跨多个日志文件建立证据链
- 用户后续大概率会要求正式报告或 `5 why`

## Lightweight State Contract

复杂问题不需要照搬 `fix-issue` 的完整工单状态机，但需要维护一个轻量状态对象。可以放在会话记忆里，或在复杂场景写到
`artifact_roots.triage_dir/<topic>/00-state.json`。`artifact_roots` 来自当前
`workspace/input/artifact_roots.json`；session 级目录导航在
`artifact_roots.session_dir/session_workspace.json`，不要写到项目根目录 `.triage/`。

`artifact_roots.triage_dir/<topic>` 必须保持下面的执行目录契约：

- `01-intake/messages`：用户消息、历史消息、输入快照。
- `01-intake/attachments`：本问题关联附件 manifest、下载/解压入口和附件索引。
- `02-process`：路由决策、分析轨迹、最终决策和中间分析产物。
- `search-runs/<run-id>`：每轮搜索的 `search_results.json` 和 `evidence_summary.md`。
- 根目录：`00-state.json`、`keyword_package.roundN.json`、`query.roundN.dsl.txt`。

不要创建同级 `search-round1`、`round1-output` 等目录；搜索输出统一进入 `search-runs`。

建议字段；如果问题牵涉时间换算或订单执行链路，优先把时区与证据锚点显式写进状态：

```json
{
  "project": "allspark",
  "mode": "structured",
  "phase": "keywords_ready",
  "problem_summary": "",
  "primary_question": "为什么 AG0019 在订单 358208 上没有继续下发下一段移动",
  "current_question": "问题时间点车辆处于什么状态、卡在哪个流程门禁",
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
    "problem_timezone": "",
    "log_time_format": "",
    "log_timezone": "",
    "normalized_problem_time": "",
    "normalized_window": {
      "start": "",
      "end": ""
    },
    "status": "aligned | mismatched | unknown"
  },
  "evidence_anchor": {
    "issue_type": "order_execution | other | unknown",
    "order_id": "",
    "vehicle_name": "",
    "task_id": "",
    "key_time": "",
    "status": "weak | partial | strong"
  },
  "order_candidates": [],
  "incident_snapshot": {
    "vehicle_system_state": "",
    "vehicle_proc_state": "",
    "order_stage": "",
    "process_stage": "",
    "current_action": "",
    "expected_next_action": "",
    "status": "unknown | partial | confirmed",
    "source_evidence": []
  },
  "module_hypothesis": [],
  "hypotheses": [],
  "noise_candidates": [],
  "target_log_files": [],
  "call_chain_status": "pending | built | partial",
  "keyword_package_status": "pending | ready | revised",
  "search_status": "pending | delegated | returned",
  "evidence_chain_status": "weak | partial | complete",
  "confidence": "low | medium | high",
  "missing_items": [],
  "narrowing_round": {
    "current": 0,
    "history": []
  },
  "delegation": {
    "search_mode": "subagent_or_skill | local",
    "status": "pending | running | completed",
    "last_scope": "",
    "notes": []
  },
  "user_confirmation": {
    "formal_report": false
  },
  "workflow_dirs": {
    "intake_dir": "",
    "intake_messages_dir": "",
    "intake_attachments_dir": "",
    "process_dir": "",
    "search_runs_dir": ""
  }
}
```

## Recommended Phases

- `initialized`
- `artifact_profiled`
- `completeness_checked`
- `incident_snapshot_built`
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

- `keywords_ready` 之前，不要启动大规模日志搜索；但可以做轻量文件盘点、时间换算、日志路由判断和代码词提取。
- `incident_snapshot_built` 表示已经建立可继续搜索的初始快照，不表示所有状态都已确认。对订单执行问题，首轮允许 `incident_snapshot.status=partial`，至少要有时间口径、车辆/订单候选、问题类型、目标日志范围和下一轮 `focus_question`。
- `incident_snapshot_built` 之前，不要把“没有规划路径”“HttpAct 卡住”“定位抖动”这类现象直接包装成主因；先明确还缺哪些状态或门禁证据。
- `search_delegated` 阶段只允许把最小搜索输入交给子 agent，不要把大量原始日志塞回主线程。
- `evidence_reviewed` 阶段必须判断证据链是否完整，以及置信度是否足够高。
- `订单 / 车辆任务执行问题`在 `evidence_reviewed` 阶段至少要检查 `订单ID + 车辆名称 + 时间` 是否闭合；缺少订单ID时，不要推进到高置信结论。
- `订单 / 车辆任务执行问题`在 `evidence_reviewed` 阶段应补齐：问题时间点车辆系统状态、车辆执行状态、当前子任务/动作、流程门禁、理论下一步动作；如果仍缺，保持 `partial` 并写入 `missing_items`。
- 对 `订单 / 车辆任务执行问题`，车辆名称是全程主锚点；如果问题窗口内同一车辆牵出多个订单，不要只保留一个模糊印象，必须把 `order_candidates` 显式记下来，再判断哪一个订单真正对应主问题。
- `RIOT3` 要在状态里明确“现场时区”和 `UTC+0` 日志时区；`RIOT2 / FMS` 要明确服务器时间口径。
- `ready_for_report` 仅表示“可以出正式报告”，不代表应该立刻输出。
- `report_confirmed` 只有在用户明确同意后才能进入。

## Search Round Contract

- `round1 / order_discovery`：用于找订单候选、状态候选和流程门禁。缺订单号时，允许 `车辆名称 + 时间窗 + 业务门禁词` 宽召回；输出必须标注哪些证据未订单闭合。
- `round2+ / verification`：用于验证具体假设。默认用 `订单ID + 车辆名称 + 时间窗 + 门禁/代码关键词` 收紧，并记录保留、删除、降级的关键词。
- 高分排序只决定阅读优先级；结论必须按 `timeline_hits` 或原始日志时间顺序复盘。
- 如果搜索轮次没有解决当前 `focus_question`，不要推进 `confidence`，先修改下一轮 `keyword_package`。

## Search Worker Contract

把大规模搜索下沉到 `search_worker.py`、子 agent 或子 skill 时，输入尽量收敛：

- 问题摘要
- 时间窗口
- 时间窗口对应的时区口径
- 本轮 focus question
- `keyword package`
- 目标日志文件或目录
- 当前假设列表

执行器返回时只保留：

- 命中的关键日志条目摘要
- 文件路径、时间点、命中次数
- 对`订单 / 车辆任务执行问题`，是否命中同一 `订单ID`
- 精选的原始日志片段，以及它是如何匹配到的
- 对应关键代码类/方法
- 未命中或噪音过大的说明
- 本轮新增/删除了哪些关键词、时间窗如何进一步收窄

不要回传整份原始 grep/zgrep 输出。

## Interim Update Contract

每轮中间汇报保持短格式：

- `阶段`
- `主问题`
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
- `订单 / 车辆任务执行问题`已经拿到 `订单ID`，或明确说明为什么当前还拿不到
- 置信度至少为 `medium`

用户未确认前，不输出完整 `5 why`。
