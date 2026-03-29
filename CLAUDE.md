# CLAUDE.md

项目级上下文，供 Claude Code 在本项目中使用。

## 项目概述

Personal Work Agent OS — 本地部署的个人工作助理系统。通过飞书接收消息，由 Orchestrator Agent 自主决策处理方式（分类、路由、分析、回复），定时生成日报。

## 架构

**Agentic 架构** — 所有消息统一进入 Orchestrator Agent，模型自主决定：
- 调用哪些 skills（intake/context/analysis/reply/report）
- 是否路由到工作会话（route_to_session tool）
- 是否关联任务上下文（link_task_context tool）
- 直接回复还是走完整分析链

```
消息 → Orchestrator Agent
         ├── 闲聊 → 直接回复
         ├── 工作问题 → intake → context → analysis → reply
         ├── 紧急问题 → 快速回复
         └── 噪音 → 静默
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
  monitor.py       — 任务进度监控
  connectors/      — 飞书接入、消息服务
  sessions/        — 路由、摘要、生命周期（辅助模块）
  orchestrator/    — Agent SDK 客户端 + MCP 工具（route_to_session, link_task_context 等）
  reports/         — 日报生成
  memory/          — 长期记忆归档
models/db.py       — 数据库模型（SQLModel）
.claude/agents/    — Agent 定义（唯一定义源，被 Agent SDK 加载为子 agent）
skills/            — Skill 文档 + 辅助脚本
data/              — 运行时数据（DB、日志、摘要、记忆）
scripts/           — 初始化和迁移脚本
```

## 关键文件

- `core/pipeline.py` — Orchestrator Agent 入口：消息进来 → agent 自主处理
- `core/orchestrator/agent_client.py` — Agent SDK 客户端 + 所有 MCP 工具定义
- `.claude/agents/*.md` — 子 Agent 定义（intake, context, analysis, reply, report）
- `core/connectors/feishu.py` — 飞书 WebSocket + 消息收发
- `models/db.py` — 所有数据库模型和枚举
- `apps/api/routers/admin.py` — 管理 API

## 数据库表

- `messages` — 消息（含 pipeline_status、classified_type）
- `sessions` — 工作会话（含 task_context_id）
- `task_contexts` — 任务上下文（多个 session 归属同一任务）
- `session_messages` — 会话-消息关联（role: user/assistant）
- `agent_runs` — Agent 执行记录（含 token 消耗、子 agent transcript）
- `audit_logs` — 审计日志
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

# 数据库迁移
python scripts/migrate_pipeline.py
python scripts/migrate_agent_session.py

# 初始化数据库
python scripts/init_db.py
```

## 设计原则

1. **Agentic 架构** — 单入口 Orchestrator，模型自主决策调用哪些 skills
2. `.claude/agents/*.md` 是子 Agent 的**唯一定义源**，`skills/` 是文档和辅助脚本
3. 进度反馈和监控**不调用 LLM**，用 DB 查询和文件读取
4. 日报从**实际对话数据**汇总，不凭空生成
5. 所有操作写**审计日志**，子 agent transcript 也持久化到 DB
6. Session 路由和 TaskContext 关联通过 **MCP tool** 暴露给模型，而非硬编码

## 环境变量

```
FEISHU_APP_ID       — 飞书应用 ID
FEISHU_APP_SECRET   — 飞书应用密钥
FEISHU_BOT_NAME         — 机器人名称（默认 WorkAgent）
FEISHU_REPORT_CHAT_ID   — 日报推送目标 chat_id（留空则不推送）
ANTHROPIC_API_KEY       — Claude API Key
ANTHROPIC_BASE_URL  — API 代理地址（可选）
DATABASE_URL        — 数据库连接串
```
