# Personal Work Agent OS

Local-first 个人工作助理系统。接入飞书，由 Orchestrator Agent 自主决策处理每条消息 — 分类、路由、分析、回复。依托 Claude Code Agent SDK，模型驱动的 Agentic 架构。

## 系统流程

```mermaid
flowchart TB
    subgraph Input["消息入口"]
        Feishu["飞书 WebSocket"]
    end

    subgraph Persist["消息持久化"]
        Save["入库去重<br/>(message_service)"]
    end

    subgraph Orchestrator["Orchestrator Agent<br/>模型自主决策"]
        direction TB
        Judge{"判断消息类型"}

        Judge -->|"闲聊/问候"| DirectReply["直接回复<br/>send_feishu_message"]
        Judge -->|"噪音"| Silent["静默记录<br/>write_audit_log"]
        Judge -->|"工作问题"| WorkFlow
        Judge -->|"紧急问题"| UrgentFlow["快速回复<br/>跳过深度分析"]

        subgraph WorkFlow["工作消息处理链"]
            direction TB
            Route["route_to_session<br/>查找/创建会话"]
            Link["link_task_context<br/>关联任务上下文"]
            Route --> Link
        end

        subgraph Skills["按需调用 Skills (子 Agent)"]
            direction LR
            Intake["intake<br/>结构化分类"]
            Context["context<br/>上下文检索"]
            Analysis["analysis<br/>深度分析"]
            Reply["reply<br/>生成回复+策略"]
        end

        WorkFlow --> Skills
        Intake --> Context --> Analysis --> Reply
    end

    subgraph Output["输出"]
        Auto["auto → 飞书自动发送"]
        Draft["draft → 草稿待确认"]
        SilentOut["silent → 仅记录"]
    end

    subgraph DB["数据持久化"]
        Messages[(messages)]
        Sessions[(sessions)]
        TaskCtx[(task_contexts)]
        AgentRuns[(agent_runs<br/>含子agent transcript)]
        AuditLogs[(audit_logs)]
    end

    subgraph Scheduled["定时任务"]
        Monitor["进度监控<br/>每2min · 无LLM"]
        Lifecycle["会话生命周期<br/>每1h · 无LLM"]
        Memory["记忆归档<br/>每6h · 1次LLM"]
        DailyReport["日报生成<br/>每天8:00"]
    end

    subgraph AdminUI["管理后台 (React)"]
        TaskView["任务与会话<br/>按TaskContext分组折叠"]
        MemoryView["记忆管理<br/>查看/编辑记忆文件"]
        ConvView["对话/消息/审计"]
        Playground["模型测试"]
    end

    Feishu --> Save --> Orchestrator
    Reply --> Output
    UrgentFlow --> Output
    DirectReply --> Output

    Orchestrator --> DB
    Scheduled --> DB
    AdminUI --> DB

    style Orchestrator fill:#e8f4fd,stroke:#2196F3,stroke-width:2px
    style Skills fill:#fff3e0,stroke:#FF9800,stroke-width:1px
    style WorkFlow fill:#f3e5f5,stroke:#9C27B0,stroke-width:1px
```

## 核心能力

| 能力 | 实现方式 | 说明 |
|------|----------|------|
| 消息接入 | 飞书 WebSocket 长连接 | 私聊 + @机器人 |
| 自主决策 | Orchestrator Agent | 模型判断每条消息的处理路径 |
| 消息分类 | intake skill (子agent) | work/chat/noise/urgent/task_request |
| 上下文检索 | context skill (子agent) | 聚合最近消息+摘要+项目知识 |
| 深度分析 | analysis skill (子agent) | 结构化拆解工作问题 |
| 智能回复 | reply skill (子agent) | auto/draft/silent 三种策略 |
| 会话路由 | route_to_session MCP tool | 按 chat_id+项目+时间窗口匹配 |
| 任务关联 | link_task_context MCP tool | 模型判断会话归属哪个任务 |
| 子agent追踪 | SubagentStop hook | 子agent对话记录持久化到DB |
| 会话摘要 | 达阈值自动生成 | Markdown 格式 |
| 日报 | report skill | 从DB汇总，LLM润色 |
| 长期记忆 | memory consolidator | 定期归档会话知识 |
| 记忆管理 | 管理后台 Memory 页 | 查看/编辑 data/memory/ 文件 |
| Token追踪 | agent_runs 表 | 按agent/按天统计消耗 |

## 架构

```
.claude/agents/          子 Agent 定义（唯一定义源）
├── intake.md            消息分类
├── context.md           上下文检索
├── analysis.md          深度分析
├── reply.md             回复生成
└── report.md            日报生成

apps/
├── api/                 FastAPI 服务 + 管理 API
├── worker/
│   ├── feishu_worker    飞书 WebSocket 长连接
│   └── scheduler        定时任务（监控/日报/记忆）
└── admin-ui/            React + Vite 管理后台

core/
├── pipeline.py          Orchestrator Agent 入口（单入口，模型自主决策）
├── monitor.py           任务进度监控（纯 DB 查询）
├── connectors/
│   ├── feishu.py        飞书 SDK 封装
│   └── message_service  消息入库 + 触发 pipeline
├── sessions/
│   ├── router.py        会话路由（辅助模块）
│   ├── summary.py       会话摘要
│   └── lifecycle.py     生命周期管理
├── orchestrator/
│   ├── agent_client.py  Agent SDK 客户端 + MCP 工具
│   └── claude_client.py Claude API 客户端
├── reports/
│   └── daily.py         日报生成
└── memory/
    └── consolidator.py  长期记忆归档

skills/                  Skill 文档 + 辅助脚本
models/db.py             数据库模型
data/                    运行时数据（DB、摘要、记忆、日报）
scripts/                 初始化和迁移脚本
```

## 设计原则

1. **Agentic 单入口** — Orchestrator Agent 处理所有消息，模型自主决定调用哪些 skills
2. **依托 Runtime** — Claude Code Agent SDK 做执行器，子 agent 可以读文件、执行命令、查 DB
3. **文件是记忆，DB 是状态** — 摘要/日报/知识落文件，消息/路由/审计落 DB
4. **LLM 按需调用** — 定时任务中只有日报和记忆归档各调 1 次 LLM，进度监控纯 DB
5. **子 agent 可追溯** — SubagentStop hook 持久化 transcript，管理后台可查

## 快速开始

```bash
# 1. 安装依赖
pip install -e .

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 FEISHU_APP_ID, FEISHU_APP_SECRET, ANTHROPIC_API_KEY

# 3. 初始化数据库
python scripts/init_db.py
python scripts/migrate_task_context.py

# 4. 启动服务
python -m apps.worker.feishu_worker  # 飞书消息接收
python -m apps.worker.scheduler      # 定时任务
python -m uvicorn apps.api.main:app --port 8000  # API 服务

# 5. 启动管理后台（开发模式）
cd apps/admin-ui && npm install && npm run dev
```

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/conversations` | 对话记录（问答配对） |
| `GET /api/conversations/{chat_id}/history` | 聊天历史 |
| `GET /api/messages` | 原始消息列表 |
| `GET /api/sessions` | 工作会话列表 |
| `GET /api/sessions/{id}` | 会话详情 + 摘要 |
| `GET /api/task-contexts` | 任务上下文列表（含关联会话） |
| `GET /api/task-contexts/{id}` | 任务详情 |
| `GET /api/memory/files` | 记忆文件列表 |
| `GET/PUT/DELETE /api/memory/files/{path}` | 记忆文件 CRUD |
| `GET /api/token-usage` | Token 消耗统计 |
| `GET /api/pipeline/stats` | 管线状态统计 |
| `GET /api/agent/sessions/{sid}/transcript` | Agent 会话 transcript |
| `POST /api/messages/{id}/reprocess` | 重新处理消息 |
| `POST /api/reports/daily` | 手动生成日报 |
| `POST /api/memory/consolidate` | 手动触发记忆归档 |

## 定时任务

| 任务 | 频率 | LLM 调用 |
|------|------|---------|
| 进度监控 | 每 2 分钟 | 否 |
| 会话生命周期 | 每 1 小时 | 否 |
| 记忆归档 | 每 6 小时 | 1 次/批 |
| 日报 | 每天 8:00 | 1 次 |
