# CLAUDE.md

项目级上下文，供 Claude Code 在本项目中使用。

## 项目概述

Personal Work Agent OS — 本地部署的个人工作助理系统。通过飞书接收消息，由主 Agent / 项目 Agent 处理会话，支持 `claude` 与 `codex` 两种运行时，并内置 GitLab review 工作流。

## 架构

**两级 Agent 架构** — Pipeline 控制所有 IO，Agent 只输出 JSON：

```
消息 → FeishuClient → save_and_process
         ├── GLANCE 表情回应（已收到）
         ├── /m 命令 → 模型切换
         └── save → pipeline.process_message()
              ├── Session 路由（thread_id 匹配，raw SQL）
              ├── 分流：
              │    ├── 有 agent_session_id + project → 直接 resume 项目 Agent
              │    ├── 首轮短消息明确指定项目 → 直接进入项目 Agent
              │    ├── 已绑定 project 但 agent_session_id 为空 → 直接进入项目 Agent
              │    └── 否则 → orchestrator 分类 + dispatch
              ├── Pipeline 发飞书消息（从 JSON reply_content）
              ├── Pipeline 保存 bot_reply + 绑定 thread_id
              └── Pipeline 回写 agent_session_id / project 到 session
```

关键设计：
- **Agent 不发消息** — orchestrator 无 reply_to_message/save_bot_reply tool，只输出 JSON
- **Pipeline 负责所有 DB 写入** — 全部用 raw SQL（aiosqlite），无 ORM 缓存问题
- **续会话跳过 orchestrator** — 有 agent_session_id + project 时直接 resume 项目 Agent

## 技术栈

- **Python 3.11+** — 主语言
- **FastAPI** — API 服务
- **SQLModel + SQLAlchemy** — ORM，异步
- **SQLite** — 数据库（pipeline 用 aiosqlite raw SQL，其他模块用 SQLModel ORM）
- **lark-oapi** — 飞书 SDK（WebSocket 长连接）
- **claude-agent-sdk** — Claude Code Agent SDK（Claude runtime）
- **codex CLI / OpenAI runtime** — Codex runtime，支持本地 MCP 工具
- **anthropic** — Claude API 客户端（统一走 Anthropic 兼容代理）
- **openai** — OpenAI API / Codex 相关客户端
- **React + Vite + TypeScript** — 管理后台前端
- **TanStack Query** — 前端数据查询
- **Tailwind CSS** — 样式

## 目录结构

```
apps/api/          — FastAPI 应用，管理 API
apps/worker/       — feishu_worker（消息接收）、scheduler（定时任务）
apps/admin-ui/     — React 管理后台
core/              — 核心业务逻辑
  pipeline.py      — 主 Agent 入口（分类 + 回复 + 项目路由，单 agent 调用）
  projects.py      — 项目注册与发现（data/projects.yaml）
  monitor.py       — 任务进度监控
  connectors/      — 飞书接入、消息服务
  sessions/        — 路由、摘要、生命周期（辅助模块）
  orchestrator/    — Agent SDK 客户端 + MCP 工具
    agent_client.py  — Agent SDK 客户端 + 所有 MCP 工具定义
    claude_client.py — Claude API 客户端（多 provider 路由）
  reports/         — 日报生成
  memory/          — 长期记忆归档
models/db.py       — 数据库模型（SQLModel）
.claude/agents/    — Agent 定义（仅 report.md，日报生成子 agent）
skills/            — Skill 注册（从 .claude/agents/*.md 自动发现）
data/              — 运行时数据（DB、日志、摘要、记忆）
  models.yaml      — 多模型配置
  projects.yaml    — 项目注册
.review/           — GitLab issue / MR review 工作目录
.triage/           — 日志 / 现场 / 附件分析工作目录
tests/             — 集成测试
scripts/           — 初始化和迁移脚本
AGENT.MD           — 项目操作/协作速览（供人和自动化入口阅读）
```

## 关键文件

- `core/pipeline.py` — Pipeline 入口：session 路由 → agent 调用 → 飞书发送 → DB 回写（全 raw SQL）
- `core/ones_intake.py` — ONES intake：下载工单、正文/图片联合读取、`summary_snapshot.json` 持久化
- `core/orchestrator/agent_client.py` — Agent runtime 客户端 + MCP 工具（支持 claude / codex）
- `core/orchestrator/claude_client.py` — Anthropic 兼容代理路由，支持运行时 override
- `core/projects.py` — 项目注册、skill 合并、dispatch 支持
- `.claude/skills/gitlab-issue-review/` — GitLab issue/MR review 工作流
- `.claude/skills/ones/` — ONES intake workflow
- `.claude/skills/ones/scripts/ones_cli.py` — 仓库内 ONES CLI，本地 skill 优先使用
- `.claude/agents/report.md` — 日报生成子 Agent 定义（唯一子 agent）
- `core/connectors/feishu.py` — 飞书 WebSocket + 消息收发 + 表情回应 + 多模态解析（`_parse_message_content` 支持 image/file/video/audio/post/interactive/sticker）
- `core/connectors/message_service.py` — 消息入库 + 命令拦截（`/m` 模型切换）+ 收到消息立即 GLANCE 回应
- `core/monitor.py` — 任务进度监控：思考中累计时间通知（5min/10min...），仅明确失败时重试
- `models/db.py` — 所有数据库模型和枚举
- `apps/api/routers/admin.py` — 管理 API
- `tests/test_multiturn_session.py` — 多轮会话 e2e 集成测试

## MCP 工具

| 工具 | 说明 |
|------|------|
| `query_db` | 只读 SQL 查询 |
| `send_feishu_message` | 发送飞书消息（主动发送） |
| `reply_to_message` | 回复飞书消息（创建/回复话题，自动绑定 session） |
| `save_bot_reply` | 保存回复到 DB |
| `write_audit_log` | 写审计日志 |
| `read_memory` / `write_memory` | 长期记忆读写 |
| `update_session` | 更新会话字段 |
| `link_task_context` | 关联任务上下文 |
| `list_projects` | 列出注册项目 |
| `dispatch_to_project` | 派发任务到项目 Agent |

## 数据库表

- `messages` — 消息（含 pipeline_status、classified_type、session_id、thread_id）
- `sessions` — 工作会话（含 task_context_id、agent_session_id、thread_id）
- `task_contexts` — 任务上下文（多个 session 归属同一任务）
- `session_messages` — 会话-消息关联（role: user/assistant，sequence_no）
- `agent_runs` — Agent 执行记录（含 token 消耗、子 agent transcript）
- `audit_logs` — 审计日志（含 pipeline_agent_call/result 详情）
- `tasks` — 待办任务
- `reports` — 日报记录
- `app_settings` — 应用设置（含运行时模型切换状态）

## Agent SDK 与执行架构

当前存在两套 runtime：

- `claude`：`claude_agent_sdk` + 本地 Claude Code CLI
- `codex`：`codex exec --json` + 本地 MCP server

其中 `codex` 运行时会通过本地 MCP server 调用内部工具，所以内部日志里出现 `MCP tool call` 文案是正常现象；真正的问题是内部错误文本不能透传给用户，pipeline 已做脱敏拦截。

Claude runtime 调用链：

```
agent_client.py  →  claude_agent_sdk.query(prompt, options)
    →  SubprocessCLITransport  (启动 claude CLI 子进程)
        →  Claude Code CLI (实际执行，支持 --resume 恢复 session)
```

Codex runtime 调用链：

```
agent_client.py  →  codex exec --json
    →  core/orchestrator/codex_mcp_server.py
        →  本地 MCP tools
```

- `claude` / `codex` 都支持项目 session resume
- Session rollout 默认记录在 `~/.codex/sessions/` 或 `.claude/sessions/`
- GitLab review 失败过程会额外落到 `.review/`

## 常用命令

```bash
# 启动飞书 worker
python -m apps.worker.feishu_worker

# 启动 API
python -m uvicorn apps.api.main:app --port 8000

# 启动定时任务
python -m apps.worker.scheduler

# 启动前端
cd apps/admin-ui && npm run dev

# Windows 一键重启全部服务
cmd /c scripts\\restart_all_windows.bat

# 单元测试（mock agent，快速）
pytest tests/test_multiturn_session.py -v
pytest tests/test_analysis_workspace.py tests/test_gitlab_issue_review_scripts.py tests/test_gitlab_review_publish_script.py tests/test_ones_desc_local.py tests/test_service_runtime.py -q

# 核心能力集成测试（真实 Claude API，约 5 分钟）
pytest tests/test_e2e_routing.py -v -s -m e2e

# 数据库迁移
python scripts/migrate_pipeline.py
python scripts/migrate_agent_session.py
```

## 飞书命令

| 命令 | 说明 |
|------|------|
| `/m <model_id>` | 切换运行时模型（如 `/m gpt-5.4`），支持任意模型 ID |

## 核心能力测试

### 路由一致性测试（`tests/test_e2e_routing.py`）

**这是系统最核心的集成测试**，验证 Orchestrator → 项目路由 → Agent Resume 完整链路。
模型固定使用 `claude-haiku-4-5`（通过 `app_settings.current_model` 配置）。

| 测试场景 | 对话内容 | 验证指标 |
|----------|----------|----------|
| **Scenario A**（项目关联） | allspark是什么 / 数据源 / sqlserver配置 | `session.project=="allspark"` + `agent_session_id` 3轮稳定 + `claude --resume` 正确ID |
| **Scenario B**（非项目） | radiohead对比queen / 推歌 / 为什么抓人 | `session.project==""` + `agent_session_id` 只写一次不变 |

**成功标准**：
- Scenario A Turn 1：orchestrator 识别项目 → `dispatch_to_project` 工具写回 `agent_session_id` + `project`
- Scenario A Turn 2/3：pipeline 检测到 `agent_session_id+project` → 直接 `_run_project_agent(session_id=<id>)` → 等价于 `claude --resume <id>`
- 全程同一 DB session（thread_id 路由），3 次飞书回复无重复无丢失

**飞书话题模拟**：Turn 1 无 thread_id → 回复后创建话题 → Turn 2/3 携带 thread_id 在话题内追问

```bash
# 运行核心能力测试（需要 Claude API Key，约 5 分钟）
pytest tests/test_e2e_routing.py -v -s -m e2e
```

### 单元测试（`tests/test_multiturn_session.py`）

当前单元测试覆盖范围包括：
- A1/A2/A3：项目路由 agent_session_id 持久化 + resume 调用链
- B1/B2/B3：非项目路由 + 噪声消息
- C：agent_session_id 稳定性（只写一次）
- D/D2：haiku 模型默认 + 运行时切换优先级
- E：thread_id 路由到存量 session
- GitLab issue review 脚本
- GitLab review 正式评论发布脚本
- `.review` / `.triage` 工作目录分流
- 内部 MCP/tool 错误脱敏

```bash
pytest tests/test_multiturn_session.py -v
```

## 设计原则

1. **两级 Agent 架构** — 主 Agent 分类+判断项目，项目 Agent 在项目目录下执行；日报由 report 子 agent 独立生成
2. **Pipeline 控制所有 IO** — Agent 只输出 JSON，消息发送/DB 写入/session 回写全由 pipeline 代码处理
3. **全 raw SQL** — pipeline.py 不使用 ORM，避免缓存不一致
4. `.claude/agents/report.md` 是日报子 Agent 定义，`skills/` 自动发现并注册
5. 进度反馈和监控**不调用 LLM**，用 DB 查询和文件读取，最多通知 3 次
6. 日报从**实际对话数据**汇总，不凭空生成
7. 所有操作写**审计日志**，pipeline 每轮记录完整 prompt 和 agent 结果
8. Session 路由在 **pipeline 代码层强制执行**（thread_id 匹配）
9. 多轮对话通过 **飞书话题（thread_id）** 关联会话；Pipeline 自动创建话题并回写 thread_id
10. **续会话直接 resume 项目 Agent** — 有 agent_session_id + project 时跳过 orchestrator
11. **显式项目切换快速直达** — 首轮如 `allspark 项目` 这类短消息可直接进入项目 Agent
12. **已知项目补料也直达** — 即使还没有 `agent_session_id`，只要 session 已绑定项目，也不应回退到 orchestrator
13. **dispatch 回写 project + agent_session_id** — 确保后续消息能关联
14. **内部工具错误不透传** — `user cancelled MCP tool call` 等内部文本必须先脱敏
15. **收到即回应** — 消息入库后立即 GLANCE 表情回应
16. **Feishu WS 自动重连** — worker 长连接断开后自动恢复，不依赖人工重启
17. 所有时间使用**本地时间**（`datetime.now()`），不用 UTC
18. **运行时模型切换** — `core/config.py` 通过 DB `app_settings` 表持久化

## GitLab Review

- review 相关工作区默认写入 `.review/`
- 根目录 `.claude/skills/gitlab-issue-review/` 提供：
  - issue / MR 上下文抓取
  - MR 等价分组
  - `bugfix / feature / mixed` 识别
  - `format=rich` 固定输出契约
  - 用户确认后再发布正式 MR 行评论

常用脚本：

```bash
python .claude/skills/gitlab-issue-review/scripts/init_review.py --project allspark --issue-url <issue-url>
python .claude/skills/gitlab-issue-review/scripts/collect_issue_context.py --issue-url <issue-url> --state .review/<case>/00-state.json
python .claude/skills/gitlab-issue-review/scripts/publish_review_comments.py --state .review/<case>/00-state.json --mr-iid <iid> --confirmed
```

## 当前重点 Workflow Skills

当前仓库已经有两个重点工作流型 skill：

- `ones`
  - ONES 工单下载、评论与附件补齐、现场证据收集、版本线索识别、问题分析
  - 已从 `core/pipeline.py` 中抽离为独立 intake 模块 `core/ones_intake.py`
  - 配套 skill 位于 `.claude/skills/ones/`，下游优先消费 `summary_snapshot.json`
- `gitlab-issue-review`
  - GitLab issue / MR 上下文抓取、代码 review、风险判断、正式 MR 行评论发布

后续计划继续扩展更多 workflow skills，尤其包括：

- 记忆系统的深度集成
- ONES / review 结果与结构化长期记忆的联动
- 更多项目专用分析链路
- 更强的卡片化输出和确认流

## 多模态与附件

- 飞书 `image` / `file` / `post` 图片消息当前已经支持下载
- 下载后的原始文件会落到分析工作目录下：
  - `.triage/.../01-intake/attachments/`
  - `.review/.../01-intake/attachments/`
- 当前仍未完成的是“把 Agent 生成的图片主动回发到飞书”这一类输出侧增强
