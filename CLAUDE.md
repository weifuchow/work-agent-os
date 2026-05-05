# CLAUDE.md

供 Claude Code 和项目 subagent 使用的仓库上下文。

## 项目是什么

`work-agent-os` 是本地优先的飞书工作助理系统。它接收飞书消息，写入 SQLite，准备会话级 workspace，调用 Agent 处理，再由 core adapter 把结果应用到飞书和数据库。

核心规则：

> Agent 负责判断并输出 contract，core 负责所有平台副作用。

## 当前架构

```text
Feishu event
  -> core.connectors.message_service.save_and_process
  -> core.pipeline.process_message
  -> core.app.message_processor.MessageProcessor
  -> core.artifacts.workspace.WorkspacePreparer
  -> optional ONES prefetch / direct project context
  -> core.agents.runner.DefaultAgentPort
  -> core.orchestrator.agent_client
  -> core.app.result_handler.ResultHandler
  -> Feishu + DB
```

系统支持两种 runtime：

- `claude`：Claude Agent SDK / Claude Code CLI
- `codex`：Codex CLI + 本地 MCP server

## 重要边界

- 不要添加让 Agent 直接发送飞书消息的工具。
- 不要让 Agent 直接保存 bot reply 或直接修改 session。
- DB 写入、飞书发送、审计日志、session patch 必须留在 core。
- 运行产物必须写入 `workspace/input/artifact_roots.json` 指定的 session artifact roots。
- 不要把分析产物写到仓库根目录 `.ones`、`.triage`、`.review`、`.session` 或 `data/attachments`。

## Session Workspace

`WorkspacePreparer` 每轮写入：

```text
data/sessions/session-<id>/workspace/
  input/
    message.json
    session.json
    history.json
    media_manifest.json
    artifact_roots.json
    project_context.json
    skill_registry.json
  state/state.json
  output/
  artifacts/
```

session 级根目录：

```text
data/sessions/session-<id>/.ones
data/sessions/session-<id>/.triage
data/sessions/session-<id>/.review
data/sessions/session-<id>/worktrees
data/sessions/session-<id>/uploads
data/sessions/session-<id>/attachments
data/sessions/session-<id>/scratch
```

排查 Agent 行为时，先读 `project_context.json` 和 `artifact_roots.json`，再读 message/session/history/media 输入。

## Project Runtime 和 Worktree

`core/projects.py` 负责项目注册和 git runtime context：

- `ProjectRuntimeContext`
- `resolve_project_runtime_context()`
- `prepare_project_runtime_context()`
- `build_project_runtime_prompt_block()`

对于 ONES 相关工作，runtime context 会提取版本线索，选择匹配 tag/branch，创建或复用 detached worktree，并记录：

- 注册项目目录。
- 选择的 checkout ref。
- 实际执行目录 `execution_path`。
- 主仓和执行目录的 commit 元信息。
- 分支、tag、worktree 决策说明。

当前兼容文件：

```text
workspace/input/project_runtime_context.json
workspace/output/project_runtime_context.json
```

它们描述一个主项目或活跃项目。不要在没有兼容层的情况下把这个对象改成多项目数组。多项目能力应新增索引和每项目 runtime 文件，同时保留该文件作为活跃项目视图。

## 项目 Dispatch

orchestrator 暴露：

- `query_db`
- `list_projects`
- `dispatch_to_project`

`dispatch_to_project` 是项目 subagent 边界。主 Agent 判断项目，core 准备 runtime/worktree，subagent 以该项目 `execution_path` 作为 cwd 运行。

跨项目请求应按这个流程处理：

1. 判断下一个具体问题属于哪个项目。
2. 确保该项目 runtime context 已存在。
3. 把一个边界清晰的任务 dispatch 给该项目 subagent。
4. 主 Agent 汇总返回结果。

不要依赖项目 subagent 自己猜应该进入哪个 worktree。

## Workflow Skills

workflow skill 由 `core/skill_registry.py` 从 `.claude/skills/*/SKILL.md` 发现。

优先通过 skill trigger 描述做业务分流，不要把业务路由硬编码到 core。

当前重点本地 skill：

- `ones`：ONES 工单 intake、summary snapshot、图片、评论和附件。
- `riot-log-triage`：RIOT/FMS/allspark 日志、现场问题、state、DSL 和 search runs。
- `gitlab-issue-review`：GitLab issue/MR review 状态和确认后发布。
- `feishu_card_builder`：结构化摘要到飞书卡片 contract。
- `daily-report`：日报生成。

## 关键文件

- `core/pipeline.py`：公共消息处理 facade。
- `core/app/message_processor.py`：主 use case 编排。
- `core/app/ones_prefetch.py`：服务侧 ONES fetch 和项目 runtime 准备。
- `core/app/project_context.py`：直接项目提及检测和 runtime 持久化。
- `core/app/result_handler.py`：回复发送、卡片重建、修复、上下文增强。
- `core/app/reply_enrichment.py`：给飞书卡片追加 runtime context。
- `core/artifacts/workspace.py`：session workspace 和 artifact root map。
- `core/connectors/message_service.py`：消息保存、媒体下载、`/m` 命令。
- `core/connectors/feishu.py`：飞书 client 和消息解析。
- `core/orchestrator/tools.py`：MCP tools 和 `dispatch_to_project`。
- `core/orchestrator/agent_client.py`：Claude/Codex runtime adapter。
- `core/orchestrator/codex_mcp_server.py`：Codex 使用的本地 MCP server。
- `core/projects.py`：项目注册、skill 合并、worktree 准备。
- `models/db.py`：SQLModel schema。
- `data/projects.yaml`：注册项目路由。
- `data/models.yaml`：模型配置。

## 常用命令

```bash
pip install -e .
python scripts/init_db.py

python -m apps.worker.feishu_worker
python -m uvicorn apps.api.main:app --port 8000
python -m apps.worker.scheduler

cd apps/admin-ui
npm install
npm run dev

cmd /c scripts\restart_all_windows.bat
```

## 测试

```bash
pytest tests/baseline -q
pytest tests/test_ones_routing.py tests/test_direct_project_context.py tests/test_dispatch_to_project.py -q
pytest tests/test_reply_enrichment.py tests/test_service_runtime.py tests/test_multimodal_attachments.py -q
pytest tests/test_gitlab_issue_review_scripts.py tests/test_gitlab_review_publish_script.py -q
pytest tests/test_riot_log_triage_scripts.py tests/test_riot_log_triage_replay_case.py -q
pytest tests/test_e2e_routing.py -v -s -m e2e
```

前端：

```bash
cd apps/admin-ui
npm run build
npm run lint
```

## 开发规则

- 优先通过 core use case 和 port 改行为，不要把平台副作用塞进 skill。
- skill 输出应小而结构化；大日志和中间证据写入 session artifact 目录。
- 保持 `project_runtime_context.json` 对单项目消费者的兼容。
- 增加多项目 runtime 支持时，用索引和每项目 context 文件；主 Agent 负责路由，core 负责 worktree，subagent 负责执行。
- 测试优先从 public facade、tool handler 或脚本 CLI 进入。
- 飞书卡片回复由 `ResultHandler` 校验和修复，不要绕过它直接发送 payload。
