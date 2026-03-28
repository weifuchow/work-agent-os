# CLAUDE.md

项目级上下文，供 Claude Code 在本项目中使用。

## 项目概述

Personal Work Agent OS — 本地部署的个人工作助理系统。通过飞书接收消息，自动分类、路由到工作会话、分析并回复，定时生成日报。

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
  pipeline.py      — 消息处理管线
  monitor.py       — 任务进度监控
  connectors/      — 飞书接入、消息服务
  sessions/        — 路由、摘要、生命周期
  orchestrator/    — Agent SDK / Claude API 客户端
  reports/         — 日报生成
  memory/          — 长期记忆归档
models/db.py       — 数据库模型（SQLModel）
skills/            — Agent 技能定义
data/              — 运行时数据（DB、日志、摘要、记忆）
scripts/           — 初始化和迁移脚本
```

## 关键文件

- `core/pipeline.py` — 消息处理的完整链路：classify → route → analyze → reply
- `core/connectors/feishu.py` — 飞书 WebSocket + 消息收发
- `core/sessions/router.py` — 会话路由匹配逻辑
- `core/orchestrator/agent_client.py` — Agent SDK 客户端 + MCP 工具定义
- `models/db.py` — 所有数据库模型和枚举
- `apps/api/routers/admin.py` — 管理 API（22 个端点）

## 数据库表

- `messages` — 消息（含 pipeline_status、classified_type）
- `sessions` — 工作会话
- `session_messages` — 会话-消息关联（role: user/assistant）
- `agent_runs` — Agent 执行记录（含 token 消耗）
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

# 初始化数据库
python scripts/init_db.py
```

## 设计原则

1. 进度反馈和监控**不调用 LLM**，用 DB 查询和文件读取
2. 日报从**实际对话数据**汇总，不凭空生成
3. 回复通过 **Agent SDK**（有工具执行能力），不是纯文本 LLM
4. 所有操作写**审计日志**，可追溯
5. 闲聊也回复（Agent SDK），工作消息走完整管线

## 环境变量

```
FEISHU_APP_ID       — 飞书应用 ID
FEISHU_APP_SECRET   — 飞书应用密钥
FEISHU_BOT_NAME     — 机器人名称（默认 WorkAgent）
ANTHROPIC_API_KEY   — Claude API Key
ANTHROPIC_BASE_URL  — API 代理地址（可选）
DATABASE_URL        — 数据库连接串
```
