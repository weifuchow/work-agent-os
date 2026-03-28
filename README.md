# Personal Work Agent OS

Local-first 个人工作助理系统。接入飞书，自动分类消息、路由会话、分析问题、生成回复和日报。依托 Claude Code Agent Runtime，只在上层构建业务逻辑。

## 当前状态

**Phase 1-5 已完成** — 系统可端到端运行：

```
飞书消息 → 入库 → 分类 → 路由 → 分析 → 回复 → 飞书
                                              ↓
                              定时：进度监控 / 日报 / 记忆归档
```

### 已实现功能

| 模块 | 状态 | 说明 |
|------|------|------|
| 飞书接入 | ✅ | WebSocket 长连接，私聊 + @机器人 |
| 消息分类 | ✅ | Claude API 分类：work/chat/noise/urgent |
| 会话路由 | ✅ | 按 chat_id + 项目 + 时间窗口匹配 |
| 问题分析 | ✅ | Agent SDK 结构化分析 |
| 智能回复 | ✅ | auto/draft/silent 三种策略 |
| 闲聊回复 | ✅ | Agent SDK（可执行命令、读文件） |
| 进度反馈 | ✅ | 飞书实时通知处理进度 |
| 会话摘要 | ✅ | 达阈值后自动生成 Markdown 摘要 |
| 会话生命周期 | ✅ | 24h→waiting，7d→archived |
| 任务监控 | ✅ | 每 2min 检查卡住任务，自动重试 |
| 日报 | ✅ | 从 DB 汇总对话+会话+token，LLM 润色 |
| 长期记忆 | ✅ | 定期归档会话知识到 data/memory/ |
| 对话历史 | ✅ | 问答配对存储，API 可查 |
| Token 追踪 | ✅ | 按 agent/按天统计消耗 |
| 管理后台 | ✅ | React UI：仪表盘/对话/消息/会话/审计 |

## 架构

```
apps/
├── api/                 FastAPI 服务 + 管理 API（22 个端点）
├── worker/
│   ├── feishu_worker    飞书 WebSocket 长连接
│   └── scheduler        定时任务（监控/日报/记忆）
└── admin-ui/            React + Vite 管理后台

core/
├── pipeline.py          消息处理管线主流程
├── monitor.py           任务进度监控（纯 DB 查询，不调 LLM）
├── connectors/
│   ├── feishu.py        飞书 SDK 封装
│   └── message_service  消息入库 + 触发管线
├── sessions/
│   ├── router.py        会话路由
│   ├── summary.py       会话摘要
│   └── lifecycle.py     生命周期管理
├── orchestrator/
│   ├── agent_client.py  Claude Agent SDK 客户端
│   └── claude_client.py Claude API 客户端
├── reports/
│   └── daily.py         日报生成
└── memory/
    └── consolidator.py  长期记忆归档
```

## 核心设计原则

1. **稳定性 > 功能** — 进度反馈用 DB 轮询不调 LLM，卡住自动重试
2. **依托 Runtime** — Claude Code Agent SDK 做执行器，不重复造轮子
3. **文件是记忆，DB 是状态** — 摘要/日报/知识落文件，消息/路由/审计落 DB
4. **LLM 按需调用** — 定时任务中只有日报和记忆归档各调 1 次 LLM

## 快速开始

```bash
# 1. 安装依赖
pip install -e .

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 FEISHU_APP_ID, FEISHU_APP_SECRET, ANTHROPIC_API_KEY

# 3. 初始化数据库
python scripts/init_db.py

# 4. 启动服务
python -m apps.worker.feishu_worker  # 飞书消息接收
python -m apps.worker.scheduler      # 定时任务
python -m uvicorn apps.api.main:app --port 8000  # API 服务

# 5. 启动管理后台（开发模式）
cd apps/admin-ui && npm install && npm run dev
```

飞书配置详见 [docs/feishu-setup.md](docs/feishu-setup.md)。

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/conversations` | 对话记录（问答配对） |
| `GET /api/conversations/{chat_id}/history` | 聊天历史 |
| `GET /api/messages` | 原始消息列表 |
| `GET /api/sessions` | 工作会话列表 |
| `GET /api/sessions/{id}` | 会话详情 + 摘要 |
| `GET /api/token-usage` | Token 消耗统计 |
| `GET /api/pipeline/stats` | 管线状态统计 |
| `POST /api/messages/{id}/reprocess` | 重新处理消息 |
| `POST /api/pipeline/process-pending` | 批量处理待分类消息 |
| `POST /api/reports/daily` | 手动生成日报 |
| `POST /api/memory/consolidate` | 手动触发记忆归档 |

## 定时任务

| 任务 | 频率 | LLM 调用 |
|------|------|---------|
| 进度监控 | 每 2 分钟 | 否 |
| 会话生命周期 | 每 1 小时 | 否 |
| 记忆归档 | 每 6 小时 | 1 次/批 |
| 日报 | 每天 8:00 | 1 次 |

## 下一阶段

见 [docs/next-phase.md](docs/next-phase.md)。
