# CLAUDE.md

项目级上下文，供 Claude Code 在本项目中使用。

## 项目概述

Personal Work Agent OS — 本地部署的个人工作助理系统。通过飞书接收消息，由 Orchestrator Agent 自主决策处理方式（分类、路由、分析、回复），定时生成日报。

## 架构

**Agentic 架构** — 所有消息统一进入 Orchestrator Agent，模型自主决定：
- 调用哪些 skills（intake/context/analysis/reply/report）
- 是否路由到工作会话（route_to_session tool）
- 是否关联任务上下文（link_task_context tool）
- 是否派发到具体项目（dispatch_to_project tool）
- 直接回复还是走完整分析链

```
消息 → Orchestrator Agent
         ├── 闲聊 → 直接回复（可执行命令获取信息）
         ├── 工作问题 → route_to_session → intake → context → analysis → reply
         ├── 项目任务 → route_to_session → dispatch_to_project → 项目 Agent 执行
         ├── 紧急问题 → route_to_session → 快速回复
         └── 噪音 → 简短回复
```

## 技术栈

- **Python 3.11+** — 主语言
- **FastAPI** — API 服务
- **SQLModel + SQLAlchemy** — ORM，异步
- **SQLite** — 数据库（aiosqlite）
- **lark-oapi** — 飞书 SDK（WebSocket 长连接）
- **claude-agent-sdk** — Claude Code Agent SDK
- **anthropic** — Claude API 客户端
- **React + Vite + TypeScript** — 管理后台前端
- **TanStack Query** — 前端数据查询
- **Tailwind CSS** — 样式

## 目录结构

```
apps/api/          — FastAPI 应用，管理 API
apps/worker/       — feishu_worker（消息接收）、scheduler（定时任务）
apps/admin-ui/     — React 管理后台
core/              — 核心业务逻辑
  pipeline.py      — Orchestrator Agent 入口（单入口，模型自主决策）
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
.claude/agents/    — Agent 定义（唯一定义源，被 Agent SDK 加载为子 agent）
skills/            — Skill 注册（从 .claude/agents/*.md 自动发现）
data/              — 运行时数据（DB、日志、摘要、记忆）
  models.yaml      — 多模型配置
  projects.yaml    — 项目注册
tests/             — 集成测试
scripts/           — 初始化和迁移脚本
```

## 关键文件

- `core/pipeline.py` — Orchestrator Agent 入口：消息进来 → agent 自主处理
- `core/orchestrator/agent_client.py` — Agent SDK 客户端 + 所有 MCP 工具定义（12 个 tool）
- `core/orchestrator/claude_client.py` — 多 provider 模型路由（Anthropic + OpenAI），支持运行时 override
- `core/projects.py` — 项目注册、skill 合并、dispatch 支持
- `.claude/agents/*.md` — 子 Agent 定义（intake, context, analysis, reply, report）
- `core/connectors/feishu.py` — 飞书 WebSocket + 消息收发
- `core/connectors/message_service.py` — 消息入库 + 命令拦截（`/m` 模型切换）
- `models/db.py` — 所有数据库模型和枚举
- `apps/api/routers/admin.py` — 管理 API
- `tests/test_multiturn_session.py` — 多轮会话 e2e 集成测试

## MCP 工具

| 工具 | 说明 |
|------|------|
| `query_db` | 只读 SQL 查询 |
| `send_feishu_message` | 发送飞书消息 |
| `save_bot_reply` | 保存回复到 DB |
| `write_audit_log` | 写审计日志 |
| `read_memory` / `write_memory` | 长期记忆读写 |
| `update_session` | 更新会话字段 |
| `route_to_session` | 查找/创建工作会话 |
| `link_task_context` | 关联任务上下文 |
| `list_projects` | 列出注册项目 |
| `dispatch_to_project` | 派发任务到项目 Agent |

## 数据库表

- `messages` — 消息（含 pipeline_status、classified_type、session_id）
- `sessions` — 工作会话（含 task_context_id、agent_session_id）
- `task_contexts` — 任务上下文（多个 session 归属同一任务）
- `session_messages` — 会话-消息关联（role: user/assistant，sequence_no）
- `agent_runs` — Agent 执行记录（含 token 消耗、子 agent transcript）
- `audit_logs` — 审计日志（含 pipeline_agent_call/result 详情）
- `tasks` — 待办任务
- `reports` — 日报记录

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

# 运行测试
pytest tests/test_multiturn_session.py -v

# 数据库迁移
python scripts/migrate_pipeline.py
python scripts/migrate_agent_session.py
```

## 飞书命令

| 命令 | 说明 |
|------|------|
| `/m <model_id>` | 切换运行时模型（如 `/m gpt-5.4`），支持任意模型 ID |

## 设计原则

1. **Agentic 架构** — 单入口 Orchestrator，模型自主决策调用哪些 skills
2. `.claude/agents/*.md` 是子 Agent 的**唯一定义源**，`skills/` 自动发现并注册
3. 进度反馈和监控**不调用 LLM**，用 DB 查询和文件读取
4. 日报从**实际对话数据**汇总，不凭空生成
5. 所有操作写**审计日志**，pipeline 每轮记录完整 prompt 和 agent 结果
6. Session 路由在 **pipeline 代码层强制执行**（chat_id + 2h 窗口匹配），不依赖模型调 tool
7. 多轮对话通过 **chat_id + 时间窗口** 自动关联已有会话
8. 所有时间使用**本地时间**（`datetime.now()`），不用 UTC
9. **运行时模型切换** — `core/config.py` 内存 override，不修改 models.yaml；任意模型 ID 自动推断 provider
10. **多轮 resume 精简 prompt** — 有 `agent_session_id` + `project` 时，prompt 只传新消息 + session_id，不重复元数据
11. **dispatch 回写 project** — `dispatch_to_project` 成功后将 project 写回 session 表，确保后续消息能关联

## 环境变量

```
FEISHU_APP_ID           — 飞书应用 ID
FEISHU_APP_SECRET       — 飞书应用密钥
FEISHU_BOT_NAME         — 机器人名称（默认 WorkAgent）
FEISHU_REPORT_CHAT_ID   — 日报推送目标 chat_id（留空则不推送）
ANTHROPIC_API_KEY       — Claude API Key
ANTHROPIC_AUTH_TOKEN    — Claude Auth Token（替代 API Key）
ANTHROPIC_BASE_URL      — API 代理地址（可选）
OPENAI_API_KEY          — OpenAI API Key（可选，多 provider）
OPENAI_BASE_URL         — OpenAI API 代理地址（可选）
DATABASE_URL            — 数据库连接串
```
