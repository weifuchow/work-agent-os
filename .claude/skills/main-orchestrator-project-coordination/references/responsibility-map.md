# 主编排职责地图

用于审查 work-agent-os 主编排相关职责和脚本归属。这里记录边界，不替代代码约束。

## 模块边界

- 主编排 Agent：识别项目、整理本轮上下文、初始化当前 `session-xx` 下的项目工程目录、派发项目 Agent、复核项目结果、生成最终回复。
- 项目 Agent：只在主编排指定的项目 worktree 内分析代码、日志、ONES 或附件；返回证据和结论，不做跨项目路由、不初始化其他项目目录、不生成最终飞书回复。
- `core/orchestrator/tools.py`：承载 MCP 工具实现、项目 worktree 准备、项目派发记录和派发产物落盘；不承载具体项目业务分析。
- `core/artifacts/session_init.py`：创建 `data/sessions/session-xx/workspace/input`、`.analysis`、`.orchestration` 等 session 级目录和输入快照。
- `core/app/project_workspace.py`：维护当前 session 的项目 workspace 状态；`active_project` 只能作为历史提示。
- `core/agents/runner.py`：生成主编排和项目 Agent 的系统提示与输入契约。
- `docs/`：设计和复盘材料；不是运行时 source of truth。

## 脚本归属

- 放入本 skill `scripts/`：主编排路由、项目协同、派发产物校验、session/message 复盘、E2E 场景验证等确定性脚本。
- 留在根 `scripts/`：数据库迁移、应用初始化、服务启动/重启、跨模块运维脚本。
- 放入项目或领域 skill：ONES intake、GitLab review、RIOT 日志排障等业务工作流脚本。
- 旧命令需要兼容时，根 `scripts/` 只保留薄 wrapper，真实实现放入对应 skill。

## Session 产物

- 主编排输入：`data/sessions/session-xx/workspace/input/*.json`
- 项目 worktree：`data/sessions/session-xx/worktrees/<project>/<task-version>/`
- 项目分析产物：`data/sessions/session-xx/.analysis/message-<id>/<project>/<dispatch-id>/`
- 主编排派发记录：`data/sessions/session-xx/.orchestration/message-<id>/dispatch-*.json`
- 源仓库根目录不接收项目分析产物。
