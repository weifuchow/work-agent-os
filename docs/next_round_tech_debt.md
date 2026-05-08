# 下一轮技术债修复清单

生成时间：2026-05-08

## P0：拆分 `core/orchestrator/tools.py`

现状：

- 文件已超过 1700 行。
- 同时包含 MCP tool 定义、项目 dispatch 状态机、artifact 写入、DB 更新、skill 选择、triage preflight、旧 Feishu 工具。
- 任何小改动都容易误伤主编排、项目 Agent、MCP 和测试。

建议拆分：

- `core/orchestrator/dispatch_service.py`：`dispatch_to_project` 主流程。
- `core/orchestrator/dispatch_artifacts.py`：`.orchestration`、`.analysis`、`.triage` 写入。
- `core/orchestrator/dispatch_skill_selection.py`：`riot-log-triage` 等硬兜底规则。
- `core/orchestrator/mcp_tools.py`：只保留 MCP tool 注册和薄包装。

验收：

- `core/orchestrator/tools.py` 降到 400 行以内。
- `dispatch_to_project` 的行为测试不变。
- `pytest tests/test_dispatch_to_project.py tests/test_orchestrator_tool_scopes.py -q` 通过。

## P0：整理 `tests/test_dispatch_to_project.py`

现状：

- 文件超过 1100 行。
- 大量重复创建 DB、session workspace、project mock、runtime mock。
- monkeypatch 深入 `handler.__globals__`，测试耦合过重。

建议拆分：

- 新增 `tests/fakes/dispatch_harness.py`。
- 抽出：
  - `make_dispatch_session(...)`
  - `register_project(...)`
  - `fake_project_runtime(...)`
  - `capture_project_agent_run(...)`
  - `simulate_preflight_timeout(...)`

验收：

- 单个 dispatch 测试只描述场景和断言。
- 删除 `handler.__globals__` 级 monkeypatch。
- 测试总行数减少至少 30%。

## P1：统一主编排 hard policy

现状：

- `runner.py`、`codex_runtime.py`、`.claude/skills/main-orchestrator-project-coordination/SKILL.md`、`tools.py` 都写了 `riot-log-triage` 触发规则。
- prompt 规则和代码硬规则容易漂移。

建议：

- 新增 `core/orchestrator/routing_policy.py`。
- 暴露：
  - `select_required_project_skill(project_name, task, context, artifact_roots)`
  - `project_analysis_requires_dispatch(...)`
  - `build_policy_prompt_summary()`
- prompt 只引用 `build_policy_prompt_summary()` 生成的短规则。

验收：

- 触发规则只在一个 Python 模块维护。
- prompt 和 skill 文档只描述原则，不复制完整关键词表。

## P1：正式化投递恢复服务

现状：

- `recover_reply_delivery.py` 为了不重跑 Agent，调用了 `ResultHandler._deliver_reply` 私有方法。
- `ResultHandler` 同时负责校验、修复、投递、降级、保存状态，职责偏重。

建议：

- 新增 `core/app/delivery_recovery.py`。
- 暴露正式 API：
  - `recover_failed_delivery(message_id, session_id)`
  - `deliver_existing_reply(ctx, workspace, reply, recovery_reason)`
- `recover_reply_delivery.py` 改成薄 CLI。

验收：

- 维护脚本不再调用私有方法。
- `reply_delivery_failed` 能通过服务恢复为 `completed`。
- 有测试覆盖“已分析成功、投递失败、从 artifacts 恢复 reply”。

## P1：统一分析产物命名

现状：

- 同一轮可能同时有：
  - `result.json`
  - `final_summary.json`
  - `artifacts/summary.json`
  - `.triage/.../final_decision.json`
- 职责重叠，恢复脚本需要猜路径。

建议契约：

- `result.json`：项目 Agent runtime 原始结果。
- `summary.json`：项目 Agent 结构化结论。
- `final_reply.json`：主编排最终回复 contract。
- `analysis_trace.md/json`：过程记录。

验收：

- dispatch 成功后固定写出 `summary.json`。
- ResultHandler 和恢复脚本只读取明确契约，不做多路径猜测。

## P2：处理旧 Feishu tool 残留

现状：

- `core/orchestrator/tools.py` 里仍有 `send_feishu_message` / `reply_feishu_message` 风格工具残留。
- 目前架构要求平台投递由 `ResultHandler` 负责，Agent-visible MCP 不应该有平台副作用。

建议：

- 确认这些旧工具是否仍注册。
- 未使用则删除；若保留，只允许测试或 admin 显式调用，不暴露给 Agent。

验收：

- `ORCHESTRATOR_TOOLS` 和 `PROJECT_TOOLS` 都不含平台投递工具。
- 相关测试固定工具边界。

## P2：收敛维护脚本入口

现状：

- 主编排 skill 下有多个脚本：
  - `collect_orchestration_context.py`
  - `validate_dispatch_artifacts.py`
  - `mark_stale_dispatch.py`
  - `recover_reply_delivery.py`
  - `e2e_scenario.py`
- 命令分散，参数风格不完全统一。

建议：

- 新增统一入口：
  - `orchestrator_ops.py collect`
  - `orchestrator_ops.py validate`
  - `orchestrator_ops.py mark-stale`
  - `orchestrator_ops.py recover-delivery`
  - `orchestrator_ops.py smoke-e2e`

验收：

- `SKILL.md` 只展示统一入口。
- 旧脚本保留为兼容 wrapper。

## P2：降低 broad `except Exception`

现状：

- 主编排、Codex runtime、Feishu connector 中有较多 broad exception。
- 这些 catch 让问题不容易定位，只能靠后续 audit 猜。

建议：

- 对关键链路拆分异常类型：
  - DB 写失败
  - 文件写失败
  - runtime timeout
  - MCP 启动失败
  - 飞书 API 空响应
  - 飞书 API 非成功响应
- audit detail 写入结构化 `error_type`、`stage`、`retryable`。

验收：

- 新增失败用例能准确落到 `failed_stage`。
- 不再把底层异常全部压成同一条字符串。

## 建议执行顺序

1. 先拆 `core/orchestrator/tools.py`。
2. 同步抽 dispatch test harness。
3. 抽 `routing_policy.py`，消除 prompt/代码双写。
4. 正式化 delivery recovery API。
5. 统一分析产物契约。
