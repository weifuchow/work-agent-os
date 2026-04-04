# CLAUDE.md

项目级上下文，供 Claude Code 在本项目中使用。

## 项目概述

Personal Work Agent OS — 本地部署的个人工作助理系统。通过飞书接收消息，由主 Agent 直接分类、回复或派发到项目 Agent 执行，定时生成日报。

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
- **claude-agent-sdk** — Claude Code Agent SDK（依赖本地 Claude Code CLI 作为执行器）
- **anthropic** — Claude API 客户端（统一走 Anthropic 兼容代理）
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
tests/             — 集成测试
scripts/           — 初始化和迁移脚本
```

## 关键文件

- `core/pipeline.py` — Pipeline 入口：session 路由 → agent 调用 → 飞书发送 → DB 回写（全 raw SQL）
- `core/orchestrator/agent_client.py` — Agent SDK 客户端 + MCP 工具（orchestrator 仅 query_db/read_memory/dispatch_to_project）
- `core/orchestrator/claude_client.py` — Anthropic 兼容代理路由，支持运行时 override
- `core/projects.py` — 项目注册、skill 合并、dispatch 支持
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

`claude_agent_sdk` 是 Python 包装层，**实际执行器是本地 Claude Code CLI**。调用链：

```
agent_client.py  →  claude_agent_sdk.query(prompt, options)
    →  SubprocessCLITransport  (启动 claude CLI 子进程)
        →  Claude Code CLI (实际执行，支持 --resume 恢复 session)
```

- 所有 Agent session（飞书对话、项目 dispatch）都是 Claude Code 原生 session
- `opts.resume = session_id` 等同于 `claude --resume <session_id>`
- 飞书对话可通过 `claude --resume <agent_session_id>` 在终端直接恢复，继续工作
- Session 数据存储在 `.claude/sessions/`，可通过 SDK 的 `get_session_messages()` 读取完整 transcript

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

# 单元测试（mock agent，快速）
pytest tests/test_multiturn_session.py -v

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

10 个 mock 测试，验证 pipeline 逻辑（无 API 调用，秒级完成）：
- A1/A2/A3：项目路由 agent_session_id 持久化 + resume 调用链
- B1/B2/B3：非项目路由 + 噪声消息
- C：agent_session_id 稳定性（只写一次）
- D/D2：haiku 模型默认 + 运行时切换优先级
- E：thread_id 路由到存量 session

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
11. **dispatch 回写 project + agent_session_id** — 确保后续消息能关联
12. **收到即回应** — 消息入库后立即 GLANCE 表情回应
13. 所有时间使用**本地时间**（`datetime.now()`），不用 UTC
14. **运行时模型切换** — `core/config.py` 通过 DB `app_settings` 表持久化

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
