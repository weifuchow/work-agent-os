# work-agent-os 重构方案：单一职责版

本方案替代原先“先保行为、逐步搬代码”的方案。

当前项目还处在早期阶段，应该用一次清晰的大重构建立边界，而不是继续围绕
`core/pipeline.py` 的私有函数做补丁式迁移。

核心判断：

> **只保证基准流程测试通过。除基准流程合同以外，内部模块、私有函数、旧测试、旧 prompt、旧目录细节都可以替换。**

---

## 1. 重构目标

### 1.1 最终目标

`core` 只负责通用工作流：

1. 读取消息
2. 绑定或创建 session
3. 暂存媒体和附件
4. 创建 workspace
5. 调用 agent，并提供可用 skill registry
6. 解析通用结果
7. 把 skill 产出的回复 payload 投递到 channel adapter
8. 写 DB 和 audit

`skill` 负责业务判断：

1. ONES 工单如何理解
2. RIOT 日志排障需要哪些证据
3. 车号、订单号、时间窗口、日志 DSL、关键词如何生成
4. 某个项目应该怎样分析
5. 应该问用户补什么材料
6. 回复内容应该如何组织
7. 回复应该用文本、Markdown、飞书卡片还是附件
8. 飞书卡片标题、颜色、字段、按钮、布局模板如何生成
9. 自己什么时候应该被触发，以及触发后要不要继续调用其他 skill

### 1.2 明确不要什么

重构后不接受这些形态：

- `core/pipeline.py` 里出现 RIOT / ONES evidence / 车号 / 订单号 / 日志窗口 / DSL 等业务规则
- `core` 里出现“这个项目特殊处理一下”的分支
- pipeline 拼业务 prompt
- pipeline 解释 skill 的业务字段
- pipeline / core 根据业务内容选择具体 skill
- pipeline / core 构建飞书业务卡片模板
- 测试 patch `core.pipeline._xxx`
- 为了兼容旧白盒测试保留无价值 wrapper
- 一个函数同时做读库、路由、业务判断、调用 agent、发飞书、写 audit

### 1.3 唯一必须稳定的合同

稳定合同只有这些：

1. `process_message(message_id: int) -> None`
2. `reprocess_message(message_id: int) -> None`
3. 一条可处理的消息进入后，最终进入 completed 或 failed
4. agent 通过 skill 产出回复时，channel port 收到一次待投递 payload
5. session 能通过 thread 继续
6. 媒体能被暂存并作为 artifact 交给 skill
7. 处理过程有最小 audit，失败可定位
8. 基准流程测试全绿

除此以外都不是硬约束。

可以改：

- 文件结构
- 私有函数名
- 旧测试结构
- prompt 内容
- `.triage/` / `.review/` 内部布局
- audit detail 字段
- 中间 artifact 名称
- agent 返回格式，只要经过新 contract
- DB 内部读写实现，只要基准流程需要的 observable state 能成立

---

## 2. 架构原则

### 2.1 单一职责

每层只做一件事。

| 层 | 职责 | 禁止 |
|---|---|---|
| `apps/*` | HTTP / worker 入口，调用 public API | 写业务流程 |
| `core/pipeline.py` | public facade，保持外部函数签名 | 放业务逻辑 |
| `core/app/*` | 消息处理 use case 编排 | 写 RIOT/ONES/项目规则 |
| `core/ports.py` | 外部依赖接口 | 具体实现 |
| `core/deps.py` | 依赖装配 | 业务判断 |
| `core/repositories.py` 或 `core/repositories/*` | DB 读写 | 业务决策 |
| `core/sessions/*` | session 路由、锁、状态补丁 | 项目特例 |
| `core/artifacts/*` | workspace、媒体暂存、manifest | 判断证据是否充分 |
| `core/agents/*` | agent runtime 调用、结果解析 | 拼业务 prompt |
| `core/connectors/*` | Feishu / ONES API 协议适配、payload 投递 | 构建模板 / 解释业务含义 |
| `.claude/skills/*` | 业务规则、分析策略、业务 prompt、展示模板、领域输出 | 直接写 DB / 直接发飞书 |

### 2.2 Core 和 Skill 的边界

Core 只给 skill 准备事实，不解释事实。

Core 可以知道：

- 这是一条消息
- 有 text / image / file / post
- 这是某个 chat / thread / sender
- 附件已经落在哪个路径
- 当前 session 有哪些历史摘要
- 当前 workspace 在哪个目录
- 当前启用了哪些 skill，以及每个 skill 的 trigger 描述在哪里
- skill 返回的 reply payload 要投递到哪个 channel

Core 不可以知道：

- RIOT3 和 RIOT2 的时间偏移怎么处理
- 哪些日志文件算有效证据
- 缺订单号时应该如何追问
- ONES 图片是否暗示了别的项目
- 某项目的排障步骤
- 日志搜索关键词优先级
- “订单取消成功但是未解绑小车”这种业务现象怎么解释
- 飞书卡片应该如何排版
- 某类业务回复应该用什么卡片颜色、标题、按钮
- 这条消息应该调用哪个业务 skill
- 某个 skill 是否比另一个 skill 更适合当前业务问题

### 2.3 Skill 和 Core 的边界

Skill 只产生业务结论和展示 payload，不做平台副作用。

Skill 可以：

- 读 workspace 输入文件
- 读项目代码和日志
- 调用本 skill 自己的脚本
- 使用本 skill 自带的 schema、模板、fixture、配置
- 写 `state.json`
- 写 search request / search result / evidence summary
- 返回结构化业务结果
- 返回文本、Markdown、飞书卡片 JSON、附件等展示 payload

Skill 不可以：

- 直接更新 `messages` / `sessions`
- 直接调用 Feishu
- 直接修改 pipeline 状态
- 依赖 pipeline 私有函数

### 2.4 Channel Presentation

飞书模板本质上是展示策略，不是 pipeline 能力。

因此：

- pipeline 不生成飞书卡片
- pipeline 不选择卡片模板
- pipeline 不决定标题、颜色、按钮、字段顺序
- Feishu connector 只负责把 payload 按 Feishu API 发出去
- 文本转卡片、结构化回复转卡片、流程图卡片、补充材料卡片都归 skill / reply-presentation skill

推荐做法：

1. 业务 skill 直接返回 `reply.type = "text" | "markdown" | "feishu_card" | "file"`。
2. 如果业务 skill 只返回领域结构，由回复 / presentation skill 二次渲染成 Feishu payload。
3. Core 只做 schema 校验、长度限制、安全兜底和投递。

注意：不要在第一轮重构就强行新增 `presentation-feishu` 大类。先把卡片渲染从
`core` 移出去，放到现有回复能力或一个最小 presentation skill 中。只有多个业务
skill 开始共享复杂卡片模板时，才把它独立成 `presentation-feishu`。

兜底规则也必须保持通用：

- payload 缺失：标记 failed 或发通用错误文本
- payload 太大：按 channel 限制截断或转附件
- channel 不支持某种类型：降级为纯文本

这些兜底不能包含业务语义。

### 2.5 Skill-First 调度

能 skill 化的能力必须彻底 skill 化。

Core 不做业务路由，不根据关键词选择业务 skill，不维护业务 if/else。Core 只做三件事：

1. 把消息、session、媒体、workspace 事实写清楚。
2. 把可用 skill registry 交给 agent。
3. 执行 agent / skill 返回的通用结果。

模型负责决定：

- 当前消息是否需要调用 skill
- 调用哪个 skill
- 是否需要多个 skill 串联
- 是否需要先调用 router skill 再调用业务 skill
- 是否需要调用 reply-presentation skill 渲染最终回复

每个 skill 必须在 `SKILL.md` 顶部写清楚触发规则：

```markdown
## Trigger

Use this skill when:
- 用户提供 ONES 工单链接，需要读取工单事实
- 用户描述现场问题且需要判断证据是否完整

Do not use this skill when:
- 用户只是闲聊
- 用户只是在问通用系统使用方式

When uncertain:
- 先读取 workspace/input/message.json
- 如果仍不确定，调用 router skill 或返回一个最小澄清问题
```

触发规则的要求：

- 精准：避免“所有工作问题都用我”
- 可组合：允许一个 skill 调用另一个 skill
- 可替换：换 skill 或改 trigger 不需要改 core
- 可测试：每个重要 trigger 都有 skill contract 测试
- 无副作用：trigger 判断不能直接写 DB、发 Feishu

如果某个判断能通过改 `SKILL.md` trigger 解决，就不允许写进 core。

### 2.6 Skill Package

Skill 是一个可执行能力包，不只是一个 prompt 文件。

一个完整 skill 可以包含：

```
.claude/skills/<skill-name>/
├── SKILL.md
├── scripts/
├── schemas/
├── templates/
├── fixtures/
└── README.md
```

skill 根目录规则：

- 业务 skill 的 canonical root 是 `.claude/skills/`
- 顶层 `skills/` 只作为 Python 包、registry/runtime helper 或兼容层
- 不新增一套和 `.claude/skills/` 并行的业务 skill 根目录
- 已存在的 `.claude/skills/ones` 沿用这个名字，不为了“intake”再重命名成
  `ones-intake`

各目录职责：

| 路径 | 职责 |
|---|---|
| `SKILL.md` | 触发规则、使用步骤、输出要求 |
| `scripts/` | 确定性处理逻辑，例如解析、搜索、转换、渲染 |
| `schemas/` | skill 输入输出 JSON schema |
| `templates/` | prompt 片段、Feishu card 模板、Markdown 模板 |
| `fixtures/` | skill contract 测试样本 |

脚本归属规则：

- 业务脚本放在对应 skill 的 `scripts/` 下
- 展示脚本放在回复 / presentation skill 的 `scripts/` 下
- 平台协议脚本才允许放在 connector
- 通用无业务含义的文件操作才允许放在 core

脚本调用规则：

- 优先由 agent 按 `SKILL.md` 指令调用脚本
- core 不硬编码某个 skill script 的路径
- core 不理解脚本参数的业务含义
- 脚本输入来自 workspace/input 或显式 CLI 参数
- 脚本输出写入 workspace/output、workspace/state 或 stdout JSON
- 脚本不得直接写 DB、调用 Feishu、修改 pipeline 状态

适合放进 skill script 的内容：

- ONES 工单解析和归一化
- RIOT 日志关键词生成
- 日志搜索 DSL 生成
- 搜索结果 rerank
- evidence summary 生成
- Feishu card payload 渲染
- Mermaid / 图表渲染策略
- 项目专属诊断辅助脚本

不适合放进 core 的判断，如果需要确定性，就写成 skill script。

---

## 3. 目标结构

目标不是把 `pipeline.py` 拆成一堆同样耦合的小文件，而是按职责重组。

```
core/
├── pipeline.py                     # public facade: process_message / reprocess_message
├── deps.py                         # dependency assembly
├── ports.py                        # ChannelPort / AgentPort / FilePort / ClockPort
│
├── app/
│   ├── message_processor.py         # process_message use case
│   ├── context.py                   # MessageContext, no business fields
│   ├── contract.py                  # Agent/skill generic result schema
│   └── result_handler.py            # apply generic result
│
├── repositories.py                  # messages/sessions/agent_runs/audit_logs; split only if too large
│
├── sessions/
│   └── service.py                   # generic thread/chat routing, lock, session patch
│
├── artifacts/
│   ├── workspace.py                 # create/resolve workspace and write inputs
│   ├── media.py                     # parse/download/copy media
│   └── manifest.py                  # media and workspace manifests
│
├── agents/
│   └── runner.py                    # call agent runtime with skill registry and parse result
│
└── connectors/
    ├── feishu.py                    # API protocol and payload delivery only, no templates
    └── ones.py                      # optional protocol client only, no evidence rules

.claude/skills/
├── ones/
│   ├── SKILL.md
│   ├── scripts/
│   ├── schemas/
│   └── fixtures/
├── riot-log-triage/
│   ├── SKILL.md
│   ├── scripts/
│   ├── schemas/
│   └── fixtures/
├── gitlab-issue-review/
│   └── ...
└── reply-presentation/              # optional; only split out when shared rendering exists
    └── ...
```

行数约束：

- 所有 `core/**/*.py` 文件必须 ≤ 800 行
- 目标文件优先 ≤ 500 行
- 超过 500 行必须能说明为什么不能再拆
- 超过 800 行直接不合格
- `repositories.py`、`connectors/feishu.py`、`agents/runner.py` 先允许单文件承载；
  当某个文件接近 500 行或职责明显分叉时，再拆成子包

---

## 4. Skill Contract

Core 和 skill 只通过 workspace artifact + 结构化结果通信。

### 4.1 Workspace 输入

Core 在调用 agent 前写入标准输入目录。

```
workspace/
├── input/
│   ├── message.json
│   ├── session.json
│   ├── history.json
│   ├── media_manifest.json
│   ├── project_context.json
│   └── skill_registry.json
├── state/
│   └── state.json
├── output/
└── artifacts/
```

`message.json` 只描述平台事实：

```json
{
  "id": 123,
  "platform": "feishu",
  "platform_message_id": "om_xxx",
  "chat_id": "oc_xxx",
  "thread_id": "omt_xxx",
  "sender_id": "ou_xxx",
  "message_type": "text",
  "content": "用户原文",
  "received_at": "2026-04-30T10:00:00"
}
```

`media_manifest.json` 只描述文件事实：

```json
{
  "items": [
    {
      "kind": "image",
      "source": "feishu",
      "local_path": "workspace/artifacts/media/1.png",
      "mime_type": "image/png",
      "size_bytes": 12345
    }
  ]
}
```

Core 不在调用 agent 前主动按业务内容下载 ONES 或其他外部证据。ONES 工单下载、
正文/图片解析、证据摘要等由 `.claude/skills/ones` 通过脚本或 agent 工具完成，
产物写入 `workspace/output`、`workspace/state` 或 `workspace/artifacts`。

`skill_registry.json` 只描述可用能力和触发摘要，不替模型做选择：

```json
{
  "skills": [
    {
      "name": "ones",
      "path": ".claude/skills/ones/SKILL.md",
      "trigger_summary": "用于读取和理解 ONES 工单事实",
      "scripts_dir": ".claude/skills/ones/scripts",
      "schemas_dir": ".claude/skills/ones/schemas"
    },
    {
      "name": "riot-log-triage",
      "path": ".claude/skills/riot-log-triage/SKILL.md",
      "trigger_summary": "用于 RIOT 日志排障和证据分析",
      "scripts_dir": ".claude/skills/riot-log-triage/scripts",
      "schemas_dir": ".claude/skills/riot-log-triage/schemas"
    }
  ]
}
```

`scripts_dir` / `schemas_dir` 只是 registry metadata，供 agent 或 skill runner 读取。
Core 可以把这些路径写入 workspace 输入，但不能直接拼接某个脚本路径、构造业务参数或调用
skill script。

### 4.2 Agent 输入

Core 给 agent 的 prompt 必须是通用指令。

允许：

```text
请处理这条工作消息。
workspace: <path>
请读取 workspace/input 下的事实文件。
请根据 skill_registry.json 和各 SKILL.md 的 Trigger 自行决定是否调用 skill。
请按指定 JSON schema 输出结果。
```

禁止：

```text
这是 RIOT 日志排障。你需要检查车号、订单号、发生时间……
如果缺日志就回复……
RIOT3 日志时区是……
```

这些内容必须在对应 skill 的 `SKILL.md` 或脚本里。

### 4.3 Skill 输出

Skill / agent 返回统一结构。

```json
{
  "action": "reply",
  "reply": {
    "channel": "feishu",
    "type": "text",
    "content": "回复给用户的内容",
    "payload": null
  },
  "session_patch": {
    "project": "allspark",
    "agent_session_id": "sdk-session-id",
    "agent_runtime": "codex"
  },
  "workspace_patch": {
    "state_path": "workspace/state/state.json"
  },
  "skill_trace": [
    {
      "skill": "ones",
      "reason": "message contains ONES task link"
    }
  ],
  "audit": [
    {
      "event_type": "skill_decision",
      "detail": {
        "skill": "riot-log-triage"
      }
    }
  ]
}
```

Core 只理解通用字段：

- `action`
- `reply`
- `session_patch`
- `workspace_patch`
- `skill_trace`
- `audit`

Core 只记录 `skill_trace`，不根据它做业务分支。Core 不解释 skill 私有字段。Skill 私有字段只能进入 workspace artifact 或 audit detail。

`reply` 是待投递 payload，不是 core 里的模板输入。

允许的 `reply.type`：

| type | 含义 | Core 行为 |
|---|---|---|
| `text` | 纯文本 | 原样交给 channel port |
| `markdown` | 通用 Markdown | 原样交给 channel port，由 adapter 做协议包装 |
| `feishu_card` | 已完成的飞书卡片 JSON | 只校验基本 JSON 和大小，然后交给 ChannelPort |
| `file` | 本地文件或 artifact | 交给 channel port 上传/发送 |

如果需要复杂飞书卡片，由 skill 直接输出：

```json
{
  "action": "reply",
  "reply": {
    "channel": "feishu",
    "type": "feishu_card",
    "content": "纯文本 fallback",
    "payload": {
      "schema": "2.0",
      "header": {},
      "body": {}
    }
  }
}
```

Core 不知道这个卡片为什么这么设计，只知道它是一个要发给飞书的 payload。

### 4.4 Action 枚举

| action | Core 行为 |
|---|---|
| `reply` | 把 `reply` payload 交给 channel port，保存 bot reply，标记 completed |
| `no_reply` | 不发送回复，标记 completed |
| `failed` | 标记 failed，写 audit |

不要把业务意图膨胀成 action。

- 需要补材料：仍然使用 `action = "reply"`，由 `reply.intent = "needs_input"` 表达
- 项目绑定、运行时切换、agent session 续接：通过 `session_patch` 表达
- workspace 状态更新：通过 `workspace_patch` 表达

Core 只根据 action 判断是否投递、是否完成、是否失败；不解释 `reply.intent` 的业务含义。

### 4.5 最小 Audit 合同

Audit 的目标是定位失败和串起一次处理链路，不是记录业务推理。

Core 必须记录这些通用事件：

| event_type | 触发时机 | 必要 detail 字段 | 禁止 |
|---|---|---|---|
| `message_processing_started` | 消息进入处理、状态改为 processing 前后 | `message_id`、`session_id`、`attempt` | 写业务分类结论 |
| `workspace_prepared` | workspace 和 input artifact 写完 | `message_id`、`session_id`、`workspace_path` | 写 ONES/RIOT 证据判断 |
| `agent_run_started` | 调用 AgentPort 前 | `message_id`、`session_id`、`workspace_path`、`agent_runtime` | 写指定业务 skill 的硬编码选择原因 |
| `agent_run_completed` | AgentPort 返回可解析结果 | `message_id`、`session_id`、`action`、`skill_trace` | 解释 skill 私有字段 |
| `agent_run_failed` | AgentPort 抛错或返回 `action=failed` | `message_id`、`session_id`、`error_type`、`error_message` | 吞掉原始错误 |
| `reply_delivery_started` | 调用 ChannelPort 前 | `message_id`、`session_id`、`reply_type`、`channel` | 展开完整卡片 payload |
| `reply_delivery_failed` | ChannelPort 发送失败 | `message_id`、`session_id`、`error_type`、`error_message` | 改写为业务失败 |
| `message_completed` | 消息最终 completed | `message_id`、`session_id`、`action` | 写模板细节 |
| `message_failed` | 消息最终 failed | `message_id`、`session_id`、`failed_stage`、`error_message` | 丢失 failed stage |

`message_processing_started` 发生在 session resolve 前时，`session_id` 可以为 `null`，
但后续事件必须带上已解析的 `session_id`。

Audit detail 只允许记录通用 trace 和错误定位字段。业务 evidence、搜索结果、
ONES 摘要、RIOT 状态机细节应该写入 workspace artifact，再由 audit 记录 artifact
路径或摘要哈希。

---

## 5. 测试设计与基准流程

重构期间把 `tests/baseline/` 当作不可破坏的 core 合同，把
`tests/scenarios/feishu/` 当作产品入口验收集。

旧的 pipeline 私有函数测试不再是合同。它们可以迁移、删除、重写。

### 5.1 测试边界

测试只 mock ports：

- `ChannelPort`
- `AgentPort`
- `FilePort` 中真实外部命令部分
- `ClockPort`

测试不 patch：

- `core.pipeline._xxx`
- `core.app._xxx`
- 任何私有函数

### 5.2 必须通过的基准用例

```
tests/baseline/
├── test_text_reply_flow.py
├── test_session_continuation.py
├── test_project_binding_flow.py
├── test_media_artifact_flow.py
├── test_needs_input_reply_flow.py
├── test_no_reply_flow.py
├── test_agent_failure_flow.py
├── test_feishu_delivery_failure.py
├── test_reprocess_message.py
└── test_session_locking.py
```

具体合同：

1. **text reply**  
   用户文本消息进入，fake agent 返回 `action=reply`，ChannelPort 收到一次 payload，message completed，bot reply 入库。

2. **session continuation**  
   同一 thread 的第二条消息复用同一个 session，并把上一轮 agent session id 传给 AgentPort。

3. **project binding**  
   fake agent 自行选择 skill 后返回 `session_patch.project`，core 保存 session project；后续消息只把该 project 作为事实交给 agent，不基于它选择业务 skill。

4. **media artifact**  
   图片或文件消息进入，core 下载/复制到 workspace，`media_manifest.json` 写入本地路径，AgentPort 收到 workspace。

5. **needs input**  
   fake agent 返回 `action=reply`、`reply.intent=needs_input` 和补充材料 payload，core 只转发 payload，不解释缺什么。

6. **no reply**  
   fake agent 返回 `action=no_reply`，core 不发飞书，message completed。

7. **agent failure**  
   AgentPort 抛错或返回 `action=failed`，message failed，audit 可定位。

8. **channel delivery failure**  
   AgentPort 成功但 ChannelPort 发送失败，message failed，错误入 audit。

基准测试不检查飞书卡片模板细节。模板是否正确由 `tests/skills/test_reply_presentation_contract.py` 负责。

9. **reprocess**  
   `reprocess_message` 重置消息状态并重新走完整流程。

10. **session locking**  
    同一 session 并发两条消息时串行调用 AgentPort。

### 5.3 不再保留的测试形态

这些测试要删除或改写：

- 断言 `_run_orchestrator` 被调用几次
- 断言 `_build_riot_log_triage_prompt_context` 输出
- patch `_fetch_ones_task_artifacts`
- 直接访问 pipeline 私有函数
- 手写一大段 CREATE TABLE 复制 schema
- 为旧函数名保留兼容断言

Skill 业务测试应该放到 skill 自己的测试里。

示例：

```
tests/skills/
├── test_ones_contract.py
├── test_riot_log_triage_contract.py
├── test_router_contract.py
├── test_skill_trigger_contract.py
├── test_skill_scripts_contract.py
└── test_reply_presentation_contract.py
```

这些测试验证 skill contract，不验证 pipeline 内部实现。

Skill script 测试直接调用 `.claude/skills/<name>/scripts/*`，断言 stdout JSON、workspace artifact 和 exit code。不要通过 pipeline 间接测脚本。

### 5.4 飞书场景测试集

除了 `tests/baseline/` 的通用合同，还必须建设一组表格驱动的飞书场景测试。

这组测试不是用来约束 core 私有实现，而是用来保证真实用户入口不会退化：

- 闲聊不会误进 ONES / RIOT / 项目分析链路
- ONES 链接能被 skill 识别、下载、摘要，并把关键字段传给后续分析
- 项目流程咨询能走项目知识 / 代码分析，不被当成现场事故
- 项目问题能进入对应业务 skill，但 core 不参与选择
- 附件、截图、日志文件能进入 workspace，并被 agent / skill 消费
- 最终回复必须能被断言：优先用 Markdown 表格或 Feishu card table 表达关键结论、依据、下一步

建议目录：

```
tests/scenarios/feishu/
├── test_casual_chat.py
├── test_ones_intake_flow.py
├── test_project_process_consulting.py
├── test_project_issue_flow.py
├── test_attachment_screenshot_analysis.py
└── fixtures/
    ├── feishu_messages.yaml
    ├── ones_150552_order_cancel_unbind.yaml
    ├── ones_150295_ag0019_elevator_stuck.yaml
    ├── screenshot_message.json
    └── attachment_message.json
```

测试入口仍然只能是 public API：

1. 用 builder 构造模拟飞书入库消息，包括 `chat_id`、`thread_id`、`sender_id`、
   `message_type`、`content`、`raw_event`、附件资源 id。
2. 调用 `process_message(message_id)`。
3. fake `AgentPort` 读取 workspace 输入事实，按 fixture 返回结构化 `SkillResult`。
4. 断言 DB 状态、workspace artifact、fake agent call、fake channel payload。
5. 不 patch `core.pipeline._xxx`，不依赖旧 prompt 文本。

场景矩阵：

表里的“预期 skill trace”由 fake `AgentPort` 或 skill contract 产出。场景测试只断言
trace 被记录、workspace 被正确交给 agent、回复被正确投递；不要求 core 根据输入内容
选择 skill。

| case id | 场景 | 模拟飞书输入 | 预期 skill trace | 预期回复形态 | 关键断言 |
|---|---|---|---|---|---|
| `chat_001` | 闲聊 | `早上好，今天辛苦了` / `谢谢` | 空或通用 assistant | `reply.type=text` 或 `markdown` | 不绑定 project；不出现 `ones` / `riot-log-triage`；不创建业务 artifact；message completed |
| `ones_150552` | ONES 工单 intake | `#150552 【3.52.0】【订单】：订单取消成功，但是订单未与小车解绑（现象，小车挂着订单，同时在执行其他订单） https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF` | `ones`，必要时后续 `riot-log-triage` | Markdown 表格或 Feishu table，列出“识别事实 / 证据状态 / 下一步” | task no=`150552`；task uuid=`NbJXtiyGP7R4vYnF`；版本=`3.52.0`；领域=`订单`；命中“取消成功”“未与小车解绑”“小车挂着订单”“同时执行其他订单”；证据不足时必须是补料/低置信，不直接给根因 |
| `ones_150295` | ONES + RIOT3 现场问题 | `#150295 【玉晶光电(厦门)有限公司厦门集美工厂项目】RIot3.46.23小车AG0019乘梯时在电梯里面不出来 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/H5KnYfup2tU5dOZu` | `ones` -> `riot-log-triage` | Markdown 表格或 Feishu table，列出“项目 / 车辆 / 版本 / 现象 / 缺口” | task no=`150295`；task uuid=`H5KnYfup2tU5dOZu`；项目名包含“玉晶光电(厦门)有限公司厦门集美工厂项目”；版本归一为 `RIOT3 3.46.23`；车辆=`AG0019`；现象命中“乘梯”“电梯里面不出来”；不得把单个现象包装成高置信根因 |
| `project_consult_001` | 项目流程咨询 | `RIOT3 订单取消流程里，取消成功后车辆任务和订单解绑应该在哪一步发生？` | 项目知识 / 代码分析 skill，或 `riot-log-triage` 的流程咨询模式 | 表格列出“流程步骤 / 状态变化 / 代码入口或待确认点” | 不需要 ONES；不要求用户补工单；不按现场事故直接要日志；如果无法定位项目，最多问一个确认问题 |
| `project_issue_001` | 直接项目问题 | `3.52.0 订单取消成功但小车还挂着订单，同时又在跑其他订单，帮我分析` | `riot-log-triage` | 表格列出“已知事实 / 缺失证据 / 首轮搜索建议” | 命中版本、订单取消、车辆未解绑、并行执行其他订单；缺车号/订单号/时间时回复 `reply.intent=needs_input` 或低置信下一步 |
| `media_001` | 截图分析 | 图片消息，文本 `看下截图里 AG0019 为什么卡在电梯里` | 由 agent 根据 registry 决定，常见为 `riot-log-triage` | 表格列出“截图可见事实 / 不能从截图确认的事实 / 需要的日志” | `media_manifest.json` 有 image local path、mime、size；AgentPort 收到 workspace；回复必须引用截图事实，不能只泛泛要求重新上传 |
| `media_002` | 附件 + 文本 | 文件/zip/log 附件，文本 `这份日志里订单取消后车辆还挂着订单，帮我看` | `riot-log-triage` | 表格列出“附件 / 初步判断 / 下一步检索” | 附件被 stage 到 workspace；manifest 保留原文件名和本地路径；core 不解析日志业务含义；日志检索计划来自 skill |

回复表格断言规则：

- Markdown 回复必须至少包含一个表格，且表头包含 `事实`、`依据`、`下一步`、`缺口`
  中的两个以上字段。
- Feishu card 回复如果使用 table 元素，测试只断言通用结构和关键文本，不断言模板颜色、
  icon、按钮顺序。
- 补料类回复仍然是 `action=reply`，但应带 `reply.intent=needs_input`。
- 低置信分析必须明确“缺什么证据”，不能输出确定根因。

场景测试分层：

- `tests/scenarios/feishu/*`：验证飞书消息进入系统后的端到端可观测行为。
- `tests/skills/test_ones_contract.py`：验证 ONES intake 对 `#150552`、`#150295`
  这类文本和链接的字段抽取、附件产物、summary schema。
- `tests/skills/test_riot_log_triage_contract.py`：验证 RIOT 订单/车辆/电梯类问题的
  trigger、缺口判断、搜索计划、低置信表达。
- `tests/skills/test_reply_presentation_contract.py`：验证 Markdown 表格 / Feishu
  table 渲染，不通过 pipeline 间接测试模板。

### 5.5 飞书场景 Fixture Schema

`tests/scenarios/feishu/fixtures/*.yaml` 使用统一 schema。测试代码只读取这些字段，
不把业务判断写死在测试函数里。

```yaml
case_id: ones_150552
title: "ONES 150552 order cancel unbind"
xfail:
  enabled: true
  phase_until: 2
  owner_contract: "skill"   # core | skill | presentation | unknown
  reason: "ones skill contract not implemented yet"
  remove_when: "tests/skills/test_ones_contract.py::test_extracts_ones_150552 passes"

input_message:
  platform: feishu
  chat_id: oc_demo
  thread_id: ""
  sender_id: ou_demo
  message_type: text        # text | post | image | file
  content: "#150552 ..."
  raw_event:
    message_id: om_demo
    create_time: "2026-04-30T10:00:00+08:00"
  attachments: []

agent_result:
  action: reply
  reply:
    channel: feishu
    type: markdown
    intent: needs_input
    content: |
      | 事实 | 依据 | 缺口 | 下一步 |
      |---|---|---|---|
      | ... | ... | ... | ... |
  session_patch: {}
  workspace_patch: {}
  skill_trace:
    - skill: ones
      reason: "message contains ONES task link"

expected:
  message_status: completed
  session:
    project: null
    should_bind_project: false
  agent_call:
    workspace_input_contains:
      message.json:
        content_contains:
          - "#150552"
          - "NbJXtiyGP7R4vYnF"
      skill_registry.json:
        skill_names:
          - ones
          - riot-log-triage
  channel_payload:
    type: markdown
    intent: needs_input
    contains:
      - "150552"
      - "取消成功"
      - "未与小车解绑"
    table_headers_any:
      - "事实"
      - "依据"
      - "缺口"
      - "下一步"
  workspace_files:
    must_exist:
      - input/message.json
      - input/session.json
      - input/skill_registry.json
    media_manifest_items: 0
  audit_events:
    contains:
      - message_processing_started
      - workspace_prepared
      - agent_run_started
      - agent_run_completed
      - reply_delivery_started
      - message_completed
    not_contains:
      - message_failed
```

字段约束：

- `input_message` 必须像飞书真实消息一样包含平台消息 id、chat/thread/sender、消息类型和原始事件。
- `agent_result` 是 fake AgentPort 的返回值；它可以包含业务 skill trace，但测试不得让 core
  根据输入内容选择 skill。
- `expected.channel_payload.contains` 只断言关键文本，不断言整段回复完全一致。
- `expected.audit_events` 只断言通用事件链，不断言业务 audit detail。
- 附件/截图场景必须在 `input_message.attachments` 里声明资源 id、文件名、mime、
  fixture path，并在 `workspace_files.media_manifest_items` 里断言数量。

附件 fixture 示例：

```yaml
input_message:
  message_type: image
  content: "看下截图里 AG0019 为什么卡在电梯里"
  attachments:
    - resource_id: img_demo_001
      file_name: ag0019_elevator.png
      mime_type: image/png
      fixture_path: tests/fixtures/media/ag0019_elevator.png
expected:
  workspace_files:
    media_manifest_items: 1
    media_contains:
      - file_name: ag0019_elevator.png
        mime_type: image/png
```

### 5.6 Xfail 退出规则

允许用 xfail 托住重构过程，但 xfail 必须有明确退出阶段。

| 测试类型 | Phase 0 | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|---|
| `tests/baseline/*` | 可 xfail，必须说明当前旧实现缺口 | 逐步转绿，新增 contract 用例不得新增 xfail | 必须全绿 | 必须全绿 |
| `tests/scenarios/feishu/chat_001` | 可 xfail | 应转绿 | 必须全绿 | 必须全绿 |
| `tests/scenarios/feishu/media_*` | 可 xfail | 可 xfail，缺口只能是 media staging | 应转绿 | 必须全绿 |
| `tests/scenarios/feishu/ones_*` | 可 xfail | 可 xfail，缺口只能是 skill contract | 可 xfail，必须标明 skill/presentation 缺口 | 必须全绿 |
| `tests/scenarios/feishu/project_*` | 可 xfail | 可 xfail，缺口只能是 skill contract | 可 xfail，必须标明 skill/presentation 缺口 | 必须全绿 |
| `tests/skills/*` | 可 xfail | 可 xfail | 应逐步转绿 | 必须全绿 |

每个 xfail 必须写：

- `phase_until`：最晚在哪个 Phase 结束前转绿
- `owner_contract`：`core` / `skill` / `presentation`
- `reason`：缺的具体合同，不允许写“待实现”这类空泛原因
- `remove_when`：哪条测试或哪项 contract 完成后移除 xfail

禁止：

- baseline 在 Phase 2 后继续 xfail
- 飞书场景在 Phase 3 后继续 xfail
- 用 xfail 掩盖 flaky、超时或外部网络依赖
- 因为旧私有函数不存在而 xfail，新测试不得依赖它们

---

## 6. 执行计划

这是一次边界重建，不做“最小可发布版”。每一步都服务于最终结构。

### Phase 0：冻结基准流程

交付物：

- `tests/baseline/` 10 个测试
- `tests/scenarios/feishu/` 场景测试骨架和 fixtures
- `tests/fakes/` fake ports
- `tests/builders/` message / skill result builders
- `SQLModel.metadata.create_all` 建真实测试 schema，不再手写 schema

规则：

- 只允许从 public API 进入：`process_message` / `reprocess_message`
- 只允许断言 DB observable state、ChannelPort fake、AgentPort fake calls、workspace artifact
- 不新增任何 pipeline 私有函数测试

完成条件：

- 当前旧实现下，能让基准测试尽量跑通
- 跑不通的用例允许标记为 expected failure，但必须说明缺口
- 飞书场景测试允许先标记 expected failure，但每个 xfail 必须写清楚缺的是 core contract、
  skill contract 还是 presentation contract
- 所有 xfail 都必须符合 `5.6 Xfail 退出规则`
- Phase 1 开始后逐步消灭 baseline xfail；Phase 2 完成前 baseline 必须全绿

### Phase 1：建立 ports 和 contract

交付物：

- `core/ports.py`
- `core/deps.py`
- `core/app/contract.py`
- `core/app/context.py`
- fake ports 接入 baseline tests

要收口的外部依赖：

- Feishu client
- Agent runtime
- 文件下载
- clock

不收进 core port 的内容：

- ONES 工单下载、证据理解、附件补齐：由 `.claude/skills/ones` 负责
- RIOT search worker、日志 DSL、rerank：由 `.claude/skills/riot-log-triage` 负责
- 任意业务脚本调用：由 agent 按 `SKILL.md` 或 skill contract 调用

完成条件：

- core 业务流程不再直接 import `FeishuClient`
- core 业务流程不再直接 import `agent_client`
- core 业务流程不直接调用业务脚本或 `subprocess.run`
- baseline tests 只 patch ports

### Phase 2：重写 message processor

交付物：

- `core/pipeline.py` 变成薄 facade
- `core/app/message_processor.py` 接管流程
- `MessageContext` 只包含通用字段
- `core/repositories.py` 收口 DB 读写
- `core/artifacts/*` 收口 workspace、媒体暂存和 manifest
- `core/agents/runner.py` 收口 agent 调用和通用结果解析
- 删除 `_process_message_locked` 原 god function

新的主流程：

```python
async def process(message_id: int) -> None:
    msg = await messages.get(message_id)
    if should_skip(msg):
        return

    await messages.mark_processing(message_id)
    session = await sessions.resolve_for_message(msg)

    async with session_locks.for_session(session.id):
        ctx = await build_context(msg, session)
        workspace = await artifacts.prepare_workspace(ctx)
        await artifacts.write_inputs(workspace, ctx)
        await artifacts.write_skill_registry(workspace)

        result = await agents.run_with_skills(workspace, ctx)
        parsed = parse_skill_result(result)

        await apply_result(ctx, workspace, parsed)
```

完成条件：

- baseline tests 全绿
- `pipeline.py` 不超过 150 行
- `message_processor.py` 不超过 500 行
- `core/app/*` 无业务词汇
- `core/app/*` 不根据消息内容选择业务 skill

### Phase 3：业务下沉 skill

交付物：

- ONES evidence 判断移出 core
- RIOT triage state 语义移出 core
- 日志 search window / keyword / DSL 移出 core
- 项目特定 prompt 移出 core
- 飞书卡片模板 / presentation 规则移出 core
- skill 选择规则移出 core，进入各 `SKILL.md` Trigger
- 业务确定性脚本移出 core，进入对应 skill 的 `scripts/`
- 旧 `_ones_*` wrapper 删除
- 旧 `_build_riot_log_triage_prompt_context` 删除

业务归属：

| 业务点 | 新归属 |
|---|---|
| ONES 链接内容理解 | `.claude/skills/ones` |
| ONES 证据完整性 | `.claude/skills/ones` |
| 工单图片语义 | `.claude/skills/ones` |
| RIOT state schema | `.claude/skills/riot-log-triage` |
| 车号/订单号/时间提取 | `.claude/skills/riot-log-triage` |
| 日志搜索关键词 | `.claude/skills/riot-log-triage` |
| 日志 DSL | `.claude/skills/riot-log-triage` |
| 搜索/rerank/summary 脚本 | 对应业务 skill 的 `scripts/` |
| 项目路由推断 | 模型根据 trigger 调用 router skill 或项目 skill |
| 项目分析步骤 | project skill / `SKILL.md` |
| 飞书卡片模板 | 回复 / presentation skill；需要共享模板时再独立 |
| Mermaid / 图表渲染策略和脚本 | 回复 / presentation skill 或业务 skill |

Core 保留：

- ONES API 协议 client 可以存在，但 `process_message` 主流程不能按链接主动调用它
- workspace artifact 写入
- 调用 agent 并提供 skill registry
- 应用通用结果
- channel payload 投递

完成条件：

- 排除协议 adapter 后，`rg -n "RIOT|riot|车号|订单号|日志搜索|DSL|evidence|ones_check|search_window|卡片模板|presentation|mermaid" core` 没有业务实现命中
- 允许 `connectors/ones` 出现 ONES API 协议词
- 允许 `connectors/feishu` 出现 Feishu API 协议词，但不能出现业务模板构建
- baseline tests 全绿
- skill contract tests 全绿
- trigger contract tests 全绿
- skill script contract tests 全绿
- `tests/scenarios/feishu/` 全绿，尤其是闲聊、`#150552`、`#150295`、项目咨询、
  项目问题、附件截图分析场景

### Phase 4：收敛文件规模和清理旧结构

交付物：

- 如果单文件接近 500 行或职责明显分叉，再拆：
  - `repositories.py` -> `repositories/messages.py` / `sessions.py` / `audit_logs.py`
  - `connectors/feishu.py` -> `connectors/feishu/client.py` / `delivery.py` / `media.py`
  - `agents/runner.py` -> `agents/gateway.py` / `runtime.py` / `parser.py`
- 删除或迁移旧白盒测试
- 删除无价值 wrapper
- 删除 pipeline 里的 dead code
- 删除 legacy 目录
- 写架构守护检查。守护检查是 review gate：允许协议 adapter、registry metadata、
  测试 fixture 名称等白名单命中，但每个白名单都必须有明确说明，不能用来放行业务实现

守护检查：

```powershell
pytest tests/baseline
pytest tests/skills
pytest tests/scenarios/feishu
rg -n "core\.pipeline\._|pipeline_mod\._|pipeline\._" tests
rg -n "RIOT|riot|车号|订单号|日志搜索|DSL|ones_check|search_window|卡片模板|presentation|mermaid" core -g "!core/connectors/ones*"
rg -n "if .*ones|if .*riot|if .*project|select_.*skill|choose_.*skill|route_.*skill" core -g "!core/connectors/ones*"
rg -n "\.claude[/\\\\]skills[/\\\\].*scripts|riot-log-triage[/\\\\]scripts|ones[/\\\\]scripts|reply-presentation[/\\\\]scripts" core
```

完成条件：

- baseline tests 全绿
- skill contract tests 全绿
- trigger contract tests 全绿
- skill script contract tests 全绿
- 飞书场景测试全绿
- 旧 pipeline 私有函数测试为 0
- `core/pipeline.py` ≤ 150 行
- `core/**/*.py` 全部 ≤ 800 行
- DB SQL 不散落在 message processor
- Feishu payload 只投递，不在 core 构建业务模板
- 媒体下载和 manifest 不散落在 message processor

---

## 7. 验收标准

### 7.1 架构验收

- `pipeline.py` 只暴露 public API 和少量委托
- `message_processor.py` 只编排流程
- `core` 不包含领域业务规则
- `core` 不构建飞书业务卡片或 presentation 模板
- `skill` 不直接发 Feishu、不直接写 DB
- 所有外部依赖都通过 ports
- 所有业务判断都可通过替换 skill 改变
- 所有业务 skill 选择都由模型根据 trigger 完成
- 所有展示策略都可通过替换回复 / presentation skill 改变
- 所有业务确定性逻辑都可通过替换 skill scripts 改变

### 7.2 测试验收

- `tests/baseline/` 全绿
- `tests/skills/` 全绿
- `tests/scenarios/feishu/` 全绿
- 没有超过 `phase_until` 的 xfail
- 没有测试 patch pipeline 私有函数
- 没有测试依赖旧 prompt 文本
- 没有测试为旧模块名保驾

### 7.3 可替换性验收

下面任意替换时，baseline tests 不需要改：

- 换 Feishu client 实现
- 换 agent runtime
- 换 `.claude/skills/ones` 的 intake 实现或脚本
- 换 RIOT triage skill
- 换项目路由 skill
- 换回复 / presentation skill
- 改任意 skill trigger 规则
- 换任意 skill script
- 换 workspace 内部目录布局
- 换 Feishu 卡片渲染策略

如果替换这些东西会导致 baseline tests 需要改，说明职责边界还没切干净。

---

## 8. 红线

1. 不为了旧私有函数测试保留旧函数
2. 不把业务 prompt 放回 core
3. 不把飞书业务卡片模板放回 core
4. 不在 core 里解释 skill 私有字段
5. 不在 core 里按业务内容选择 skill
6. 不在 core 里硬编码 skill script 路径或参数
7. 不在 skill 里直接操作 DB / Feishu
8. 不把 adapter、repository、business rule 写进同一个函数
9. 不新增和 `.claude/skills/` 并行的业务 skill 根目录
10. 不新增 thin wrapper
11. 不接受超过 800 行的 Python 文件
12. baseline tests 不绿，不继续合并后续清理

---

## 9. 起步动作

立即做 Phase 0。

顺序：

1. 新建 `tests/baseline/`
2. 新建 `tests/scenarios/feishu/fixtures/`，落地闲聊、`#150552`、`#150295`、
   项目咨询、项目问题、附件截图消息 fixture
3. 新建 fake ports 和 builders
4. 写 10 个基准流程测试
5. 写飞书场景测试骨架，暂时允许 expected failure，但必须标注缺口归属
6. 新建 `core/ports.py` 和 `core/deps.py`
7. 新建 `core/app/contract.py`、`core/repositories.py`、`core/artifacts/`
8. 让现有 pipeline 先通过 ports 运行
9. baseline 稳住后，直接重写 `core/app/message_processor.py`

从这一刻开始，判断代码是否合格只看两件事：

1. 职责是否单一
2. 基准流程测试是否通过

旧实现细节不再作为约束。
