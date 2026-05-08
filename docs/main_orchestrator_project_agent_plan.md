# 主编排器与项目 Agent 职责优化计划

## 目标

把多项目处理收敛成一条清晰链路：

1. 用户消息先进入主编排器。
2. 主编排器基于当前问题和历史上下文做项目匹配。
3. 主编排器决定派发一个或多个项目、每个项目加载哪个 skill、传入什么上下文。
4. 项目 Agent 只在指定项目 worktree 内执行分析。
5. 项目 Agent 返回后，主编排器检查结果，决定直接输出、纠正、补派或追问。
6. 最终飞书卡片只由主编排器输出。

核心原则：主编排器是唯一长期会话与决策者，项目 Agent 是一次性项目执行单元。

## 职责边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| MessageProcessor | 准备 workspace、ONES 预取、启动主编排、结果投递 | 不直接派发项目、不做项目分析 |
| 主编排 Agent | 理解用户问题、匹配项目、选择 skill、组织上下文、调用项目 Agent、检查结果、决定输出/纠正/二轮调整 | 不直接读项目代码/日志完成业务分析 |
| dispatch_to_project | 按主编排给定的 project、skill、context 执行一次项目分析并落盘 | 不判断项目、不自动选 skill、不复用隐式项目会话 |
| 项目 Agent | 在指定项目 worktree 内分析，加载指定项目 skill，输出结构化结论和分析产物 | 不跨项目调度、不递归派发、不生成最终飞书卡片 |
| skill | 承载工作流方法论和领域流程 | 不承载不可违反的系统边界 |

## 推荐流程

1. `MessageProcessor` 收到消息。
2. Core 准备事实输入：
   - `workspace/input/message.json`
   - `workspace/input/session.json`
   - `workspace/input/history.json`
   - `workspace/input/media_manifest.json`
   - `workspace/input/project_workspace.json`
   - `workspace/input/skill_registry.json`
   - session 级 `session_workspace.json`
3. 无论单项目还是多项目，项目相关问题都必须进入主编排 Agent。
4. 主编排读取当前消息、历史、项目注册表、已有 workspace 记录。
5. 主编排生成本轮 orchestration plan：
   - 当前问题关联哪些项目；
   - 每个项目为什么相关；
   - 哪些项目被排除以及原因；
   - 每个项目使用什么 skill；
   - 给每个项目 Agent 的 task/context；
   - 是否需要多项目派发；
   - 是否需要二轮纠正或补派；
   - 最终输出形态。
6. 主编排调用 `dispatch_to_project`。一次调用只处理一个项目。
7. `dispatch_to_project` 创建项目分析目录，运行项目 Agent。
8. 项目 Agent 写入分析过程和结构化结果。
9. 主编排检查项目结果：
   - 信息充分：汇总输出；
   - 结论不稳：纠正同一项目 Agent；
   - 缺少相关系统信息：补派其他项目；
   - 证据不足：向用户追问。
10. 主编排输出最终 JSON contract，由 `ResultHandler` 渲染并投递飞书卡片。

## 关键设计决策

### 1. 只复用主编排 agent_session

保留：

- `sessions.agent_session_id`：主编排 Agent 的长期 resume id。

废弃默认复用：

- `project_workspace.agent_sessions.projects.*.session_id`
- `.triage/00-state.json.agent_context.session_id`
- `dispatch_to_project.input.session_id`

这些 id 可以作为审计记录保存，但不作为下一次项目 Agent 的自动 resume 输入。

原因：

- 多项目场景里，长期上下文必须由主编排统一持有。
- 项目 Agent 自带隐式记忆会削弱主编排的上下文传递职责。
- 每次项目分析应该由主编排明确传入本轮所需上下文，避免旧项目会话污染新问题。

### 2. dispatch_to_project 是执行器，不是路由器

推荐输入：

```json
{
  "project_name": "allspark",
  "skill": "riot-log-triage",
  "task": "分析 ONES #150745 的订单执行问题",
  "context": "主编排整理后的当前问题、历史摘要、附件索引、关键约束",
  "db_session_id": 176,
  "orchestration_turn_id": "message-1088-turn-1"
}
```

行为约束：

- `project_name` 必须由主编排提供。
- `skill` 必须由主编排显式指定。
- 不从 `analysis_mode`、`.triage/00-state.json`、`active_project` 自动推断 skill。
- 不自动恢复项目 Agent session。
- 每次 dispatch 必须创建分析目录。
- 返回项目结果、分析目录、AgentRun id、结构化摘要。

### 3. active_project 只是提示，不是路由结果

`project_workspace.active_project` 只能作为历史提示。

每一轮用户问题都必须重新判断项目相关度：

- 当前消息是否仍属于 active project；
- 是否切换到了另一个项目；
- 是否同时涉及多个项目；
- 是否只是引用历史项目上下文，但当前问题不是项目分析。

### 4. 单项目和多项目都必须经过主编排

单项目不是特殊捷径。

允许保留“进入项目环境”的 fast path，例如用户只说“进入 allspark 项目”，可以返回项目环境确认；但只要是分析、排障、代码、ONES、日志、版本问题，都必须进入主编排，再由主编排调用 `dispatch_to_project`。

## 落盘设计

### 1. session_workspace.json

增加主编排与分析目录索引：

```json
{
  "orchestration": {
    "dir": ".orchestration",
    "policy": {
      "main_orchestrator_required": true,
      "project_routing_owner": "main_orchestrator",
      "project_agent_resume": false,
      "final_reply_owner": "main_orchestrator"
    }
  },
  "analysis": {
    "dir": ".analysis",
    "project_agent_outputs_required": true
  }
}
```

同时在 `write_policy` 增加：

```json
{
  "orchestration_plan": "session_artifact_roots.orchestration_dir/<message-id>/plan.json",
  "orchestration_review": "session_artifact_roots.orchestration_dir/<message-id>/review.json",
  "project_analysis": "session_artifact_roots.analysis_dir/<message-id>/<project>/<dispatch-id>"
}
```

### 2. 主编排目录

建议目录：

```text
data/sessions/session-176/
  .orchestration/
    message-1088/
      plan.json
      dispatch-001.json
      dispatch-002.json
      review.json
      final_decision.json
```

`plan.json` 示例：

```json
{
  "message_id": 1088,
  "current_question": "用户原始问题",
  "candidate_projects": [
    {
      "project": "allspark",
      "matched": true,
      "reason": "ONES 项目和订单执行链路指向 allspark",
      "skill": "riot-log-triage"
    },
    {
      "project": "riot-frontend-v3",
      "matched": false,
      "reason": "当前问题未涉及前端展示或交互"
    }
  ],
  "dispatches": [
    {
      "dispatch_id": "dispatch-001",
      "project": "allspark",
      "skill": "riot-log-triage",
      "task_summary": "排查订单未继续执行的后端链路",
      "context_summary": "包含 ONES 摘要、附件日志、历史纠正点"
    }
  ],
  "decision_policy": {
    "after_project_result": ["final_reply", "correction_turn", "dispatch_more_projects", "ask_user"]
  }
}
```

### 3. 项目分析目录

建议目录：

```text
data/sessions/session-176/
  .analysis/
    message-1088/
      allspark/
        dispatch-001/
          input.json
          prompt.md
          result.json
          analysis_trace.md
          artifacts/
      riot-frontend-v3/
        dispatch-002/
          input.json
          prompt.md
          result.json
          analysis_trace.md
          artifacts/
```

`input.json` 记录：

- `message_id`
- `db_session_id`
- `project_name`
- `skill`
- `task`
- `context`
- `worktree_path`
- `checkout_ref`
- `execution_commit_sha`

`result.json` 记录：

- 项目 Agent 输出文本；
- 是否失败；
- token/cost；
- project Agent session id，作为 record-only；
- 关联的 `.triage` 或 `.ones` 目录；
- 结构化证据摘要。

如果项目 skill 是 `riot-log-triage`，可以继续使用 `.triage/<topic>` 作为工作流目录，但 `.analysis/.../result.json` 需要记录真实 triage 目录路径。

### 4. project_workspace.json

保留它作为项目 worktree 注册表：

```json
{
  "active_project": "allspark",
  "project_order": ["allspark", "riot-frontend-v3"],
  "projects": {
    "allspark": {
      "worktree_path": "...",
      "checkout_ref": "release/6.13.x",
      "execution_commit_sha": "..."
    }
  }
}
```

废弃它的项目 session 自动 resume 语义。

可以新增运行索引：

```json
{
  "project_agent_runs": {
    "allspark": [
      {
        "message_id": 1088,
        "dispatch_id": "dispatch-001",
        "skill": "riot-log-triage",
        "analysis_dir": "...",
        "agent_session_id": "record-only"
      }
    ]
  }
}
```

## skill 与代码强逻辑的边界

### 放在代码里的硬约束

- `MessageProcessor` 不直接 dispatch，不直接项目分析。
- 所有项目相关分析都必须经主编排 Agent。
- 项目 Agent tool scope 不包含 `dispatch_to_project`。
- `dispatch_to_project` 必须显式接收 `project_name`。
- `dispatch_to_project` 不自动选项目、不自动选 skill。
- `dispatch_to_project` 不默认 resume 项目 Agent session。
- 每次项目 dispatch 必须落盘。
- 最终 reply contract 只由主编排输出。

### 放在主编排 skill/playbook 里的软逻辑

- 如何判断用户问题关联哪些项目。
- 如何拆分多项目问题。
- 如何决定 skill，例如 `riot-log-triage`、`ones`、review 等。
- 如何提炼给项目 Agent 的上下文。
- 如何检查项目 Agent 结果是否充分。
- 什么时候纠正、补派、追问或输出。
- 飞书卡片怎么组织。

结论：边界用代码强约束，决策方法用主编排 skill/playbook。

## 主编排 prompt 优化

需要明确写入：

- 任意项目相关问题，无论单项目还是多项目，都必须由主编排先决策。
- `active_project` 只是 hint，不是路由结论。
- 每轮必须依据当前用户问题和历史上下文重新判断项目相关度。
- 主编排必须显式选择 `project_name` 和 `skill`。
- 对 ONES、现场日志、订单/车辆执行链路、截图、附件排障，派发项目时必须传 `skill="riot-log-triage"`。
- 项目 Agent 返回后，主编排必须检查结果。
- 主编排可以选择：
  - 输出最终答案；
  - 纠正同一项目 Agent；
  - 派发另一个项目；
  - 要求用户补充信息。
- 最终飞书卡片由主编排生成。

## 项目 Agent prompt 优化

项目 Agent 只接收：

- 当前项目名称；
- 当前项目 worktree；
- 指定 skill；
- 主编排传入的 task/context；
- 项目分析目录；
- 输出 schema。

明确禁止：

- 自行判断其他项目；
- 调用 `dispatch_to_project`；
- 生成最终飞书卡片；
- 依赖历史项目 Agent session；
- 把源仓库目录当执行目录。

## 测试计划

### MessageProcessor 合约

- 项目相关消息必须先进入主编排 Agent。
- 不再出现 `core_project_dispatch_preflight_*`。
- direct project entry 只处理“进入项目环境”，不处理项目分析。

### dispatch_to_project 合约

- 不传 `session_id` 给项目 Agent。
- 即使 `project_workspace` 有旧项目 session，也不会 resume。
- 即使 `.triage/00-state.json` 有旧 agent_context，也不会 resume。
- 未显式传 `skill` 时，不自动加载 `riot-log-triage`。
- 显式传 `skill="riot-log-triage"` 时，项目 Agent 必须加载该 skill。
- skill 不存在时，dispatch run 标记 failed。
- 每次 dispatch 创建 `.analysis` 目录和运行记录。

### 主编排 prompt/tool scope

- 主编排 runtime 不暴露 Read/Write/Edit/Bash/Glob/Grep。
- 主编排可见 `dispatch_to_project`。
- 项目 runtime 不暴露 `dispatch_to_project`。
- prompt 包含：
  - 主编排必经；
  - active_project 只是 hint；
  - 每轮重新判断项目；
  - 检查/纠正/补派/输出决策；
  - `riot-log-triage` 显式 skill 规则。

### 落盘测试

- 初始化 session workspace 时创建 `.orchestration` 和 `.analysis` 根目录。
- `session_workspace.json` 包含 orchestration/analysis policy。
- `dispatch_to_project` 写入：
  - `.orchestration/message-*/dispatch-*.json`
  - `.analysis/message-*/<project>/dispatch-*/input.json`
  - `.analysis/message-*/<project>/dispatch-*/result.json`
- `project_workspace.json` 记录 `project_agent_runs`，但不作为 resume 来源。

### replay 验证

对 `session-176` 重放目标消息：

- 链路应为 `agent_run_started -> dispatch_to_project -> project analysis artifacts -> agent_run_completed -> reply_delivery_started`。
- 不应再出现 `core_project_dispatch_preflight_started/completed/applied`。
- `.triage` 不再为空，或者 `.analysis` 中明确索引项目分析结果。
- 飞书卡片由主编排最终输出。

## 落地顺序

1. 修改 prompt 和工具描述，明确主编排/项目 Agent 边界。
2. 移除项目 Agent 自动 resume。
3. 增加 `.orchestration` 和 `.analysis` 初始化。
4. 修改 `dispatch_to_project`，每次 dispatch 写入运行目录。
5. 调整 `project_workspace`，把 agent session id 降级为 run record。
6. 增加定向测试。
7. 重放 `session-176` 验证真实链路。

## 最终判断

这套结构会比当前多项目逻辑更简单：

- Core 不做业务判断。
- 主编排只负责协调和最终决策。
- 项目 Agent 只负责单项目执行。
- skill 负责方法论，不负责系统边界。
- 长期上下文只复用主编排 session。
- 每次项目分析都有明确落盘过程。

