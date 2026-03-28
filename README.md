# Personal Work Agent OS

一个常驻在备用机上的、**local-first** 的个人工作助手系统。  
系统接入飞书等工作平台，将外部 IM 消息转化为内部“工作会话（work session）”，基于多 Agent 和强 Runtime（Claude Code / Codex SDK）对工作问题进行识别、分析、沉淀和汇总，形成面向个人的持续工作分身。

---

## 1. 项目目标

本项目的目标不是做一个泛聊天机器人，而是做一个：

- 面向**个人工作流**的 AI 助手
- 可常驻运行在公司备用机上的本地服务
- 能接入飞书等工作平台
- 能识别工作消息与闲聊
- 能把单一 IM 窗口拆分成多个内部工作会话
- 能对工作问题进行分析、归档、沉淀
- 能在次日早上 8:00 自动输出日报
- 能持续学习我的工作风格、表达方式和处理边界

---

## 2. 核心设计思想

### 2.1 IM 会话 != 工作会话

飞书私聊窗口天然是“按人聚合”的，但 AI 助手应该是“按任务/主题聚合”的。

因此系统必须将：

- **IM 窗口**：飞书中的单一聊天窗口
- **Work Session**：系统内部定义的一个工作主题 / 问题 / 任务生命周期

彻底解耦。

同一个同事在同一个私聊窗口里，今天可以产生多个独立的工作会话，例如：

- 项目 A 进度咨询
- 线上报错分析
- 明日会议材料准备

系统内部需要分别管理它们，而不是把整段私聊历史直接塞给模型。

---

### 2.2 文件是记忆，数据库是状态

借鉴 OpenClaw 的 memory 哲学，但不直接照搬其通用助手模式。

本系统采用双轨存储：

#### 文件系统（面向沉淀）
用于保存：

- 工作会话摘要
- 分析结果
- 日报
- 项目知识
- 个人风格画像
- 长期记忆

#### 数据库（面向运行态）
用于保存：

- 原始消息事件
- 会话状态
- 候选归属关系
- 待办
- 审计日志
- 任务执行记录
- 调度状态

即：

- **Markdown / 文件** = Memory / Knowledge / Audit-friendly artifacts
- **SQLite / Postgres** = Runtime State / Routing / Queue / Query

---

### 2.3 主控在自己系统，Agent Runtime 只是执行器

不要把整个系统交给一个“大一统 Agent”。

正确做法：

- 我自己的主服务负责：
  - 接入平台
  - 做权限控制
  - 做消息路由
  - 做状态存储
  - 做任务调度
  - 做审计与人工接管

- Claude Code / Codex SDK 负责：
  - 理解复杂上下文
  - 执行分析任务
  - 调用工具
  - 生成回复草稿
  - 输出结构化结果

即：

**我的系统是 Orchestrator**  
**Claude Code / Codex SDK 是 Agent Runtime**

---

### 2.4 个性化优先于通用性

本项目不是第一天就做成“谁都能用”的通用产品。

优先目标是：

> 让系统越来越像“我自己的工作副驾”

个性化重点包括：

- 我的表达风格
- 我的项目术语
- 我的优先级判断方式
- 我的回复边界
- 我的风险偏好
- 我的日报结构
- 我的升级/转人工规则

---

## 3. 典型使用场景

### 3.1 飞书私聊 / @我
当有人私聊我或群里 @我 时：

1. 接收消息事件
2. 判断是否属于工作问题
3. 如果是工作问题，则路由到某个 work session
4. 结合上下文进行分析
5. 输出：
   - 自动回复
   - 回复草稿
   - 仅记录不回复

---

### 3.2 工作问题分析
例如：

- “项目 A 现在进度怎么样？”
- “这个问题可能是什么原因？”
- “这个方案为什么这么设计？”
- “这个日志报错怎么看？”

系统应当输出结构化分析，而不是只给自然语言回答。

建议固定输出结构：

- 问题摘要
- 背景信息
- 已知事实
- 初步判断
- 可能原因
- 建议动作
- 不确定点
- 置信度

---

### 3.3 次日 8:00 自动日报
每天早上自动汇总前一日所有“被判定为工作问题”的会话，生成日报：

- 昨日工作咨询
- 已处理问题
- 未闭环问题
- 风险点
- 新增待办
- 需要本人介入事项
- 可沉淀知识点

---

## 4. 产品边界

### 4.1 第一版要做的
- 飞书消息接入
- 工作/闲聊分类
- Work Session 拆分与归属
- 问题摘要与分析
- 次日 8:00 日报
- 本地沉淀与审计
- 人工接管

### 4.2 第一版先不做的
- 自动替我做高风险承诺
- 自动替我拍板技术方案
- 自动修改公司系统数据
- 自动给多人发送正式通知
- 一开始接很多平台
- 一开始做太重的多 Agent 并行执行

---

## 5. 总体架构

```text
                        ┌──────────────────────────────┐
                        │        Feishu Connector      │
                        │   私聊 / @ / 指定群消息接入    │
                        └──────────────┬───────────────┘
                                       │
                                       ▼
                        ┌──────────────────────────────┐
                        │      Message Classifier      │
                        │ 工作 / 闲聊 / 紧急 / 噪音判断 │
                        └──────────────┬───────────────┘
                                       │
                                       ▼
                        ┌──────────────────────────────┐
                        │        Session Router        │
                        │ 新建 / 归属 / 恢复 WorkSession│
                        └──────────────┬───────────────┘
                                       │
                     ┌─────────────────┴─────────────────┐
                     ▼                                   ▼
        ┌──────────────────────────┐         ┌──────────────────────────┐
        │   Context & Memory Mgr   │         │    Agent Orchestrator    │
        │ 文件记忆 / DB 状态 / 摘要 │         │ 调度 Claude/Codex Runtime│
        └──────────────────────────┘         └──────────────┬───────────┘
                                                            │
                     ┌──────────────────────────────────────┴──────────────────────────────────────┐
                     ▼                                                                             ▼
        ┌──────────────────────────┐                                                 ┌──────────────────────────┐
        │       Reply Engine       │                                                 │  Daily Report Engine     │
        │ 自动回复 / 草稿 / 不回复   │                                                 │ 次日 8 点汇总日报         │
        └──────────────────────────┘                                                 └──────────────────────────┘
```

---

## 6. 核心模块

### 6.1 Platform Connector
负责接入外部平台。

当前目标：
- 飞书私聊
- 飞书群 @我
- 指定群监听

职责：
- 事件签名校验
- 幂等去重
- 平台用户映射
- 消息回调接入
- 消息发送接口封装

---

### 6.2 Message Classifier
负责第一层判断。

输入：
- 原始消息
- 发送人
- 场景（私聊 / 群聊）
- 最近少量上下文

输出：
- `noise`
- `chat`
- `work_question`
- `urgent_issue`
- `task_request`

需要额外判断：
- 是否值得处理
- 是否需要立即响应
- 是否需要人工确认
- 是否只做记录

---

### 6.3 Session Router
本系统的关键模块。

职责：
- 将新消息归属到已有 work session
- 或新建一个 work session
- 或忽略
- 或挂起待判断

关键原则：

- 单一 IM 聊天窗口中可以存在多个 work session
- work session 维护独立摘要、状态、优先级和生命周期
- 不把整个私聊历史作为模型上下文

建议状态：

- `open`
- `waiting`
- `paused`
- `closed`
- `archived`

可扩展能力：
- 父会话 / 子会话
- 会话自动冻结
- 长期 inactive 会话归档
- 重新激活旧会话

---

### 6.4 Context & Memory Manager
负责上下文检索与长期沉淀。

上下文分层：

#### Layer 1：当前输入
用户当前这条消息

#### Layer 2：短期上下文
当前 session 最近 5~15 条消息

#### Layer 3：会话摘要
当前 session 的滚动 summary，包括：

- 问题是什么
- 当前已知事实
- 已分析结论
- 未解决点
- 建议动作

#### Layer 4：长期记忆
稳定信息，例如：

- 项目背景
- 流程知识
- 关键联系人
- 我的偏好
- 过去的类似案例

原则：
- 优先喂摘要，不喂全量历史
- 超长会话定期压缩
- Memory 可回溯、可人工修订

---

### 6.5 Agent Orchestrator
整个系统的主控核心。

职责：
- 编排多 Agent 流程
- 调用 Claude Code / Codex SDK
- 统一输入输出 schema
- 记录执行日志
- 控制 token 预算
- 控制权限边界
- 控制失败重试
- 支持人工接管

建议采用：
- 状态机
- 技能编排
- 工具调用受控
- 文件产物为真相输出

而不是让 Agent 自由发挥到底。

---

### 6.6 Reply Engine
负责生成对外回复。

回复模式分级：

#### 模式 A：自动回复
适用于低风险、信息明确的问题。

#### 模式 B：生成草稿待确认
适用于中高风险问题。

#### 模式 C：不回复，仅记录
适用于：
- 噪音
- 闲聊
- 暂不适合回应的问题

回复内容必须区分：
- 事实
- 推断
- 建议
- 不确定项

---

### 6.7 Daily Report Engine
负责次日 8:00 报告。

输入：
- 前一日所有有效 work session
- 会话摘要
- 待办状态
- 风险标签

输出结构建议：

- 昨日收到的工作问题
- 已处理问题
- 待跟进问题
- 风险与阻塞
- 新增待办
- 需要本人介入
- 可沉淀知识点

输出方式：
- 发给自己（飞书）
- 保存到本地 Markdown

---

## 7. 个性化系统

### 7.1 为什么必须做个性化
通用 AI 助手回答“像一个 AI”；
本项目希望输出更像“我自己会怎么处理”。

因此需要长期沉淀：

- 我的回复习惯
- 我的表达边界
- 我的判断风格
- 我的项目背景
- 我的组织关系理解
- 我的工作关注重点

---

### 7.2 个性化维度

#### 1）风格偏好
例如：
- 简洁型
- 分析型
- 推进型
- 安抚型
- 拒绝型

#### 2）边界偏好
例如：
- 不能替我承诺排期
- 不能替我确认上线
- 不能替我拍板方案
- 根因分析默认只出草稿

#### 3）工作画像
例如：
- 我常负责哪些项目
- 哪些人优先级更高
- 哪些群更重要
- 哪些关键词代表紧急
- 哪类问题必须人工确认

#### 4）日报偏好
例如：
- 我喜欢什么结构
- 要不要显式列风险
- 待办是否分优先级
- 是否按项目分组

---

## 8. 多 Agent 设计

第一版不建议拆太细，建议保留以下 Agent：

### 8.1 Intake Agent
职责：
- 判断是不是工作消息
- 提取主题 / 项目 / 优先级 / 紧急性
- 给 Session Router 提供候选信息

---

### 8.2 Context Agent
职责：
- 检索相关上下文
- 聚合最近消息、历史摘要、项目知识、个人偏好
- 输出最小必要上下文包

---

### 8.3 Analysis Agent
职责：
- 做问题拆解
- 输出结构化分析
- 提炼风险 / 阻塞 / 建议动作

---

### 8.4 Reply Agent
职责：
- 根据 Analysis 结果生成回复
- 根据策略判断自动发 / 草稿 / 不发

---

### 8.5 Report Agent
职责：
- 生成每日汇总
- 提炼重点、待办、风险、可沉淀内容

---

## 9. 与 OpenClaw 的关系

### 9.1 相似点
- local-first
- 文件驱动记忆
- 长期记忆沉淀
- 持续存在的 Agent 形态

### 9.2 不同点
本项目不是通用个人助手，而是面向个人工作的 Agent OS，重点补充：

- 企业 IM 接入
- Work Session 建模
- 文件 + 结构化状态双轨
- 日报闭环
- 强 Runtime 编排
- 高度个性化工作画像

一句话概括：

> 借鉴 OpenClaw 的 memory 哲学，但重做 workflow 内核。

---

## 10. 数据模型（初稿）

### 10.1 messages
```sql
id
platform              -- feishu
platform_message_id
chat_id
sender_id
sender_name
message_type
content
sent_at
received_at
raw_payload
classified_type       -- noise/chat/work_question/urgent_issue/task_request
session_id            -- nullable
created_at
```

### 10.2 sessions
```sql
id
session_key
source_platform
source_chat_id
owner_user_id
title
topic
project
priority
status                -- open/waiting/paused/closed/archived
parent_session_id     -- nullable
summary_path
last_active_at
message_count
risk_level
needs_manual_review   -- boolean
created_at
updated_at
```

### 10.3 session_messages
```sql
id
session_id
message_id
role                  -- user/assistant/system
sequence_no
created_at
```

### 10.4 tasks
```sql
id
session_id
title
description
status                -- open/doing/done/cancelled
priority
assignee
source
created_at
updated_at
```

### 10.5 reports
```sql
id
report_date
report_type           -- daily
content_path
status                -- generated/sent/failed
generated_at
sent_at
```

### 10.6 agent_runs
```sql
id
session_id
agent_name
runtime_type          -- codex/claude_code/internal
input_path
output_path
status
started_at
ended_at
error_message
```

### 10.7 audit_logs
```sql
id
event_type
target_type
target_id
detail
operator
created_at
```

---

## 11. 目录结构建议

```text
personal-work-agent/
├── README.md
├── apps/
│   ├── api/                     # FastAPI / web service
│   ├── worker/                  # 异步任务 / 调度
│   └── admin-ui/                # 可选：本地管理页面
├── core/
│   ├── connectors/              # 飞书等平台接入
│   ├── classifier/              # 消息分类
│   ├── sessions/                # 会话路由
│   ├── memory/                  # 记忆与摘要管理
│   ├── orchestrator/            # agent 编排
│   ├── reply/                   # 回复引擎
│   ├── reports/                 # 日报引擎
│   └── personalization/         # 个性化策略
├── skills/
│   ├── intake/
│   ├── context/
│   ├── analysis/
│   ├── reply/
│   └── report/
├── data/
│   ├── db/
│   │   └── app.sqlite
│   ├── memory/
│   │   ├── daily/
│   │   ├── projects/
│   │   ├── profile/
│   │   └── people/
│   ├── sessions/
│   ├── reports/
│   └── audit/
├── scripts/
│   ├── dev/
│   ├── migrate/
│   └── ops/
├── docs/
│   ├── architecture.md
│   ├── session-model.md
│   ├── personalization.md
│   └── feishu-integration.md
└── tests/
```

---

## 12. 关键流程

### 12.1 新消息处理流程
```text
接收飞书消息
  -> 幂等去重
  -> 消息分类（工作 / 闲聊 / 噪音）
  -> 若为工作消息，检索候选 session
  -> 判定归属已有 session 或新建 session
  -> 更新会话摘要
  -> 调度分析 Agent
  -> 产出回复策略（自动回复 / 草稿 / 不回复）
  -> 写审计日志
```

---

### 12.2 会话归属流程
```text
新消息进入
  -> 从当前用户最近 open session 里检索候选
  -> 基于主题/关键词/项目/时间窗口进行匹配
  -> 输出 attach_existing / create_new / pending
  -> 若 attach_existing，则挂到已有 session
  -> 若 create_new，则创建新的 work session
  -> 若 pending，则先记录，等待进一步判断
```

---

### 12.3 日报生成流程
```text
每天 08:00 定时触发
  -> 拉取前一日所有有效 work session
  -> 读取摘要 / 待办 / 风险标签
  -> 生成日报 markdown
  -> 保存到 data/reports/
  -> 发给自己（飞书）
  -> 写发送结果与审计日志
```

---

## 13. 上下文管理策略

### 13.1 禁止做法
- 不直接把单个 IM 窗口的全部历史消息塞给模型
- 不把所有项目知识一次性喂给模型
- 不让上下文无限累积

### 13.2 推荐做法
每次模型调用只使用：

- 当前消息
- 当前 session 摘要
- 最近少量消息
- 必要的长期知识片段
- 必要的个性化配置

### 13.3 摘要压缩策略
当某个 session 的消息数或 token 数超过阈值时：

- 生成增量摘要
- 更新 summary 文件
- 只保留少量最近原始消息
- 历史原始消息只作为审计保留，不默认喂模型

---

## 14. 权限与风险控制

### 14.1 高风险事项默认不自动执行
例如：
- 承诺排期
- 拍板方案
- 确认上线
- 对外正式通知
- 涉及责任归因的回复

### 14.2 回复权限分级
- **L1**：自动回复低风险问题
- **L2**：生成草稿，需本人确认
- **L3**：仅记录，不自动回复

### 14.3 审计要求
所有自动动作都必须记录：
- 输入
- 输出
- 调用的 Agent
- 调用的 Runtime
- 是否自动发送
- 是否人工确认

---

## 15. MVP 版本范围

### 15.1 MVP 目标
做出一个可长期运行的第一版，验证两个核心价值：

1. 工作消息分类是否准确
2. 日报汇总是否有价值

### 15.2 MVP 功能
- 飞书私聊 / @消息接入
- 工作 / 闲聊分类
- Work Session 拆分
- 会话摘要
- 基础分析输出
- 次日 8:00 日报
- 本地文件沉淀
- SQLite 状态存储

### 15.3 MVP 暂不做
- 自动处理高风险问题
- 太复杂的多 Agent 并行
- 太重的 UI
- 多平台接入
- 自动修改外部系统

---

## 16. 技术栈建议

### 16.1 后端
- Python 3.11+
- FastAPI
- SQLAlchemy / SQLModel
- SQLite（MVP）/ PostgreSQL（后续）
- APScheduler / Cron
- Pydantic

### 16.2 Agent Runtime
- Claude Code
- Codex SDK
- 可扩展其他 Runtime

### 16.3 存储
- 本地文件系统
- SQLite
- 可选：向量检索（后续）

### 16.4 部署
- 常驻运行在公司备用机
- systemd / supervisor / docker compose
- 不依赖云端编排服务

---

## 17. 非功能需求

### 稳定性
- 服务异常自动重启
- 消息处理失败可重试
- 日报生成失败可补偿

### 可追溯性
- 输入输出可回放
- 关键步骤有日志
- 关键产物落文件

### 可维护性
- Prompt / Skill / 策略可版本化
- 模块可替换
- Runtime 可插拔

### 可人工接管
- 支持随时暂停自动回复
- 支持手工修正分类
- 支持手工修正 session
- 支持手工编辑日报

---

## 18. 第一阶段开发计划

### Phase 0：项目骨架
- 初始化仓库
- 建立目录结构
- 建立配置系统
- 建立 SQLite 表结构
- 建立基础日志系统

### Phase 1：飞书接入
- 接 webhook
- 事件校验
- 入库与幂等处理
- 回发消息能力

### Phase 2：消息分类
- 规则 + LLM 双层判断
- 区分工作 / 闲聊 / 噪音
- 建立最小可用分类链路

### Phase 3：Session Router
- 候选 session 检索
- attach_existing / create_new
- session 生命周期管理
- 摘要文件更新

### Phase 4：分析与回复
- Analysis Agent
- Reply Agent
- 自动回复 / 草稿 / 不回复三模式

### Phase 5：日报
- 定时任务
- 拉取前一日 session
- 生成日报 markdown
- 飞书发送给自己

### Phase 6：个性化
- profile memory
- 风格模板
- 边界规则
- 风险偏好

---

## 19. 后续演进方向

- 支持更多平台（邮件、Slack、企业微信等）
- 接入项目文档和知识库
- 待办联动
- 面向项目维度的周报 / 月报
- 更细粒度的“我的工作画像”
- 历史行为学习
- 统一的本地管理界面
- Agent 执行过程可视化

---

## 20. 一句话总结

> 这个项目不是做一个“会聊天的机器人”，而是做一个能够接住工作消息、拆分工作主题、辅助分析问题、沉淀个人知识、并形成日报闭环的 Personal Work Agent OS。

