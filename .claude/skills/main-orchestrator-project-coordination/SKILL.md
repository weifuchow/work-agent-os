---
name: main-orchestrator-project-coordination
description: 主编排 Agent 的项目协同 playbook，覆盖项目匹配、session-xx 项目工程目录初始化、项目 Agent 派发、结果复核与最终回复决策。用于 work-agent-os 主编排判断用户消息是否关联已注册项目、ONES、日志、附件、版本、代码分析、多项目协作；准备项目 worktree/workspace；选择项目 skill；组织 dispatch_to_project 的 task/context；检查项目 Agent 结果；决定纠正、补派、追问或生成最终飞书回复。
---

# 主编排项目协同

这是主编排 Agent 的决策 playbook，只承载软逻辑；代码里的硬边界仍以系统约束和工具实现为准。

## 先读输入

按需读取最小上下文：

1. `workspace/input/message.json`
2. `workspace/input/session.json`
3. `workspace/input/history.json`
4. `workspace/input/project_workspace.json`
5. `workspace/input/skill_registry.json`
6. `workspace/input/media_manifest.json`
7. `workspace/input/artifact_roots.json`
8. 需要目录地图或写入策略时，再读 `project_context.session_workspace_path`

如果任务是审查职责边界、整理脚本归属或排查主编排职责混乱，先读 `references/responsibility-map.md`。

`project_workspace.active_project` 只作为历史提示，不是本轮路由结论。每轮都要结合当前消息和近期历史重新判断项目相关度。

## 判断项目相关度

用项目名、别名、ONES 上下文、附件、日志、版本、模块名、代码名和近期对话，把本轮归类：

- `project_analysis`：代码、日志、ONES、截图、附件、版本/worktree、构建配置、行为解释、bug、review 或项目专属问题。
- `project_entry`：用户只是要求进入或切换项目环境。
- `non_project`：闲聊、通用规划、work-agent-os 自身维护，且不需要项目 worktree。
- `ambiguous_project`：明显需要项目，但无法安全确定具体项目。
- `multi_project`：当前问题可能需要多个注册项目。

`project_analysis` 和 `multi_project` 不在主编排里直接读项目代码或日志。主编排负责在当前 `session-xx` 下准备项目工程目录，再派发项目 Agent。

## 初始化项目工程目录

主编排负责项目工程目录初始化和注册：

- 需要项目分析时，先确认 `workspace/input/project_workspace.json` 是否已有该项目 entry。
- 若项目未加载，调用 `prepare_project_worktree`，把项目准备到当前 session 的 `worktrees/<project>/<task-version>` 下。
- 初始化后复读或使用返回的 `project_workspace` entry，确认 `worktree_path/execution_path`、`checkout_ref`、版本和 commit。
- ONES 中若同时出现标题版本/迭代号和明确代码分支，以明确分支为准；例如 `RIOT 分支版本 origin/xxx` 应优先于 `【3.52.0】` 或同名 tag。标题里的 `3.xx` 只能作为版本/迭代线索，不能单独覆盖分支证据。
- 多项目问题逐个准备项目 worktree；每个项目只派发给对应项目 Agent。
- `source_path/project_path` 只用于识别源仓库，不作为项目 Agent 的执行目录。
- 项目 Agent 不负责创建、切换或加载其他项目工程目录；它只在主编排指定的 worktree 中分析。

如果 `prepare_project_worktree` 失败，先判断是否缺少项目注册、版本线索或 worktree 创建条件；无法自行恢复时再向用户说明缺口。

## 生成派发计划

派发前明确：

- 候选项目、命中原因、排除原因。
- `active_project` 是否仍相关，还是仅为历史上下文。
- 要准备 worktree 的项目和要派发的项目列表。
- 每个项目显式使用的 skill。
- 给项目 Agent 的任务摘要。
- 传入 context：当前问题、相关历史、ONES 摘要路径、附件索引、worktree/版本/commit、关键约束、不能假设的内容。
- 结果验收口径：需要哪些证据才算充分。

如果必须项目参与但无法确定项目，只问一个最小澄清问题。

## 选择 Skill

派发时显式选择 skill：

- `riot-log-triage`：RIOT/FMS/allspark 的 ONES、现场日志、执行链路、截图或附件排障。硬判断以 `core/orchestrator/routing_policy.py` 为准。
- `gitlab-issue-review`：GitLab issue/MR review。
- `ones`：只做 ONES intake 或结构化预处理。若 ONES 事实已准备好，且当前要做项目/日志分析，应派发项目并使用 `riot-log-triage`。
- 空 skill：普通项目代码、配置、实现说明，且不需要 workflow skill。

不要从 `analysis_mode`、`.triage/00-state.json`、`active_project` 或旧项目 Agent session 自动推断 skill。

## 派发规则

每次项目派发调用一次 `dispatch_to_project`，传入：

- `project_name`
- `skill`（需要 workflow 时必传）
- `task`
- `context`
- `db_session_id`
- `message_id`（如果可用）
- `orchestration_turn_id`（如果有稳定本轮 ID）

不要把 `workspace/input/session.json.agent_session_id` 作为 `session_id` 传给项目 Agent。项目 Agent session id 只是 record-only。

项目 Agent 的 context 应包含：

- 当前用户问题原文或紧凑摘要。
- 相关历史和用户纠正点。
- ONES 编号、路径、摘要。
- 附件、上传文件或索引路径。
- 项目、worktree、版本、分支/tag、commit 事实。
- 证据约束：缺订单号、车辆名、时间窗、环境、分支等。
- 项目 Agent 禁止事项：不跨项目路由、不调用 `dispatch_to_project`、不初始化其他项目工程目录、不生成最终飞书卡片。

## 检查项目结果

项目 Agent 返回后检查：

- 是否回答当前用户问题。
- 是否使用指定项目和 skill。
- 是否给出项目、worktree、版本、分支/tag、commit 证据。
- ONES/日志/订单/车辆问题是否闭合时间、车辆、订单或候选订单、关键日志、代码门禁。
- 是否诚实说明缺口，而不是过度包装根因。
- 是否提示需要补派其他项目。

然后选择：

- `final_reply`：结果充分，汇总回复。
- `correction_turn`：同项目漏约束或假设错误，带纠正 context 重新派发。
- `dispatch_more_projects`：需要补派其他项目。
- `ask_user`：关键证据缺失，当前产物无法推导。

## 最终回复

最终 reply contract 只由主编排输出。项目 Agent 输出是证据，不是最终飞书卡片。

最终回复应：

- 标明实际使用的项目、worktree、分支/tag/版本、commit。
- 分开已确认事实、可能结论、证据缺口和下一步。
- 流程、状态流转、跨项目说明优先用卡片友好的结构；必要时提供 Mermaid。
- 除非有助于解释证据限制，不暴露内部派发细节。

## 产物位置

项目派发产物应在：

```text
session_artifact_roots.analysis_dir/<message-id>/<project>/<dispatch-id>/
  input.json
  prompt.md
  result.json
  analysis_trace.md
```

主编排派发记录应在：

```text
session_artifact_roots.orchestration_dir/<message-id>/dispatch-*.json
```

需要复核时读取这些产物。不要把项目分析产物写到源仓库根目录。

## 可执行脚本

这些脚本只做确定性上下文整理和产物校验，不负责业务判断；脚本路径相对本 skill 目录：

- `scripts/collect_orchestration_context.py`：按 session/message 汇总 DB 消息、agent_runs、workspace、uploads 和 dispatch 文件。复盘或构造二次派发前优先运行。
- `scripts/validate_dispatch_artifacts.py`：校验某个 `.orchestration/message-*/dispatch-*.json` 是否有有效分析目录和 `result.json`。
- `scripts/mark_stale_dispatch.py`：维护脚本，用于把超时遗留的 `dispatch:*` running 记录标记为 failed；默认只预览，只有显式 `--apply` 才会写 DB 和工件。
- `scripts/recover_reply_delivery.py`：维护脚本，用于项目分析已成功但最终飞书回复投递失败时，从既有分析产物恢复并重新投递 markdown 兜底回复；不重跑主编排或项目 Agent。
- `scripts/e2e_scenario.py`：运行主编排多轮项目/非项目 E2E 场景；根目录 `scripts/e2e_scenario.py` 仅作为兼容入口。

示例：

```powershell
python .claude\skills\main-orchestrator-project-coordination\scripts\collect_orchestration_context.py --repo . --session 177 --message 1100
python .claude\skills\main-orchestrator-project-coordination\scripts\validate_dispatch_artifacts.py data\sessions\session-177\.orchestration\message-1100\dispatch-165.json
python .claude\skills\main-orchestrator-project-coordination\scripts\mark_stale_dispatch.py --repo . --older-than-minutes 30 --apply
python .claude\skills\main-orchestrator-project-coordination\scripts\recover_reply_delivery.py --repo . --session 180 --message 1129
python .claude\skills\main-orchestrator-project-coordination\scripts\e2e_scenario.py --dry-run
```
