---
name: session-behavior-review
description: 分析 work-agent-os 的某个 session 行为并推动助手逻辑闭环优化。用于用户只提供 sessionId/session-xx，要求解释为什么会出现异常行为、worktree/session/agent_session/飞书回复/ONES/项目路由/测试数量/processing 卡住等问题；或要求还原现场、重放 session 数据、mock 飞书发送、验证修复效果、从复盘到代码修复、测试补充和标准用例沉淀。
---

# Session 行为复盘

用于复盘 `data/sessions/session-xx` 与 DB 中对应会话的完整行为链路，并把发现转成可验证的代码与测试改进。

## 输入

接受以下任一形式：

- `session-173`
- `173`
- `data/sessions/session-173`
- 同时给出消息 ID、ONES 编号、飞书话题或截图时，一并纳入证据。

若没有明确 sessionId，先要求用户补充。

## 标准流程

1. **采集事实**
   - 优先运行只读脚本：
     ```powershell
     python .claude\skills\session-behavior-review\scripts\collect_session_facts.py --repo . --session 173
     ```
   - 如果脚本失败，手动读取同等信息：`sessions/messages/agent_runs/audit_logs`、`session_workspace.json`、`project_workspace.json`、`workspace/input/session.json`、`history.json`、相关 `.ones/.triage/worktrees`。

2. **重建时间线**
   - 按消息 ID 和时间列出：用户消息、bot 回复、pipeline 状态、agent run、delivery、audit 事件。
   - 标记主编排 Agent、项目子编排 Agent、repair Agent、ONES summary 子任务。
   - 标记每次 worktree 创建/覆盖、active_project 改变、agent_session_id 写入位置。

3. **判断架构边界**
   - 飞书 `thread_id -> DB session` 是否正确复用。
   - `sessions.agent_session_id` 是否只作为主编排 resume id。
   - 项目任务是否进入 `dispatch_to_project`。
   - 项目级 id 是否写入 `project_workspace.agent_sessions.projects[project]`。
   - 项目子编排是否避免递归 `dispatch_to_project`。

4. **定位行为偏差**
   - 区分现象、直接原因、根因、影响范围。
   - 不把“测试通过”当作行为正确的充分证据；必须说明缺失的测试面。
   - 常见偏差：
     - ONES 版本 worktree 被 direct project alias 覆盖。
     - 主编排直接做项目分析，未进入项目子编排。
     - 全局 `agent_session_id` 被误用为项目 Agent id。
     - 飞书卡片包含不支持元素，如 `{"tag":"mermaid"}`。
     - repair 流程污染主编排上下文。
     - processing 卡住、thinking 通知未推送、投递失败未恢复。

5. **提出并实施修复**
   - 优先小范围修改生产代码，保持与现有架构一致。
   - 修改必须包含测试；没有测试的修复不算闭环。
   - 测试应尽量覆盖标准边界，而不是只覆盖某个 session 的偶发现象。

6. **验证闭环**
   - 至少运行与改动相关的定向测试。
   - 如果需要还原真实链路，运行 session replay：
     ```powershell
     python .claude\skills\session-behavior-review\scripts\replay_session.py --repo . --session 175 --message 1080 --pretty
     ```
   - replay 会创建 `.tmp/session-replays/session-replay-<sessionId>-<timestamp>`，复制 DB 和 session 附件，重置指定用户消息，通过真实 `MessageProcessor -> Agent -> MCP tools -> ResultHandler` 链路处理，并 mock 飞书投递。
   - replay 默认设置 `WORK_AGENT_REPLAY_DB_PATH` 和 `WORK_AGENT_REPLAY_SESSIONS_DIR`，让 MCP 的 `query_db`、`prepare_project_worktree`、`dispatch_to_project` 读写 replay DB/session，而不是生产 DB/session。
   - 只想生成 replay 数据、不运行 agent 时，加 `--no-run`。
   - 如果用户要求“标准用例”，把测试放入稳定测试文件，如：
     - `tests/test_orchestrator_tool_scopes.py`
     - `tests/test_dispatch_to_project.py`
     - `tests/baseline/test_message_processor_contract.py`
     - `tests/test_direct_project_context.py`
   - 汇报：改了什么、为什么、哪些测试覆盖、哪些服务需要重启。

## 输出格式

默认用中文，按这个结构简洁输出：

```text
结论
- 一句话说明是否有问题。

证据
- session / message / agent_run / project_workspace / audit 的关键事实。

原因
- 直接原因。
- 根因。

修复
- 已改文件和行为变化。

测试
- 新增或更新的测试。
- 实际运行结果。

注意
- 是否需要重启服务。
- 是否有正在 processing 的消息不能打断。
```

## 脚本

- `scripts/collect_session_facts.py`：只读收集 session 事实，输出 JSON。用于统一分析入口。
- `scripts/replay_session.py`：创建 `session-replay-xxx` 隔离副本并重放指定消息，mock 飞书发送，用于验证修复是否真实改变线上链路行为。

`collect_session_facts.py` 不会修改数据库、不会删除文件、不会重启服务。

`replay_session.py` 会写入 `.tmp/session-replays/session-replay-*` 下的 DB 副本和 session 副本；默认不写生产 DB、不真实发送飞书。若现有 MCP 工具未尊重 replay 环境变量，必须先修复工具路径隔离后再把 replay 结果当作闭环证据。
