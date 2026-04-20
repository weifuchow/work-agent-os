---
name: gitlab-issue-review
description: 分析 GitLab issue review 场景里的 issue、关联 merge request、ONES 工单链接、附件线索与多分支修复差异。Use when Codex needs to 根据一个 GitLab issue 地址做 review、优先通过 glab 插件读取 GitLab 上下文、先明确问题调用逻辑、归并关联 MR、判断哪些分支只是等价 cherry-pick、决定是否需要逐分支下钻、补抓 ONES 工单和附件，或在多轮 human-in-the-loop 会话中持续推进 issue review。
---

# GitLab Issue Review

对 GitLab issue 类问题执行“先 glab 取上下文、再明确问题调用逻辑、再 MR 归并、再 ONES 补证、最后代码 review”的工作流。先把 issue、notes、关联 MR 和 ONES 链接拉齐，再确认当前问题到底落在哪条调用链和入口模块；只有调用逻辑和补丁分组都基本清楚后，才进入代表分支分析。

## Use GitLab Through glab First

- GitLab 数据入口默认走 `glab` 插件或 `glab api`，不要先手写 HTTP 请求。
- `glab` 已配置时，token 可来自 `~/.config/glab-cli/config.yml`；本地脚本也允许从 `.env` 读取 `GITLAB_TOKEN` 和 `GITLAB_PROJECT_URL` 作为兜底。
- 只有在下面几种情况才退化：
  - 当前环境没有 `glab`
  - `glab` 已配置但拿不到该 issue/MR 权限
  - 用户提供的是离线导出的 JSON/截图/文本
- 如果 `glab` 不可用，要明确说明“当前缺的是 GitLab 读取通道”，不要假装上下文已经完整。
- 本 skill 自带的 `collect_issue_context.py` 也是 `glab` 优先；HTTP API 只作为脚本退路，不是默认工作面。

## Identify the Review Surface

- 用户给的是 GitLab issue URL 时，先从 URL 识别：
  - GitLab host
  - 项目路径
  - issue iid
  - 仓库名与可能的运行项目名
- 项目未明确时，不要直接假设仓库名一定等于注册项目名。
  - 先尝试从 issue URL 的 repo 名、issue 描述、MR target branch、ONES 字段推断。
  - 仍不稳时，只问 1 个确认问题，例如“这是要按 `allspark` 项目代码来 review，还是别的仓库镜像分支？”
- 如果 issue 或 MR 文本里出现 ONES 链接，优先把 ONES 当作现场证据来源之一，而不是附属信息。

## Choose the Workflow Mode

- 简单问题走 `会话态`：
  - 只有 1 个 MR
  - 没有 ONES 链接
  - 没有多分支 cherry-pick
  - 用户只要快速判断“这个 issue 对应改动是否合理”
- 复杂问题走 `结构化状态流`：
  - issue 下存在多个 MR / 多个 target branch
  - issue / MR / notes 里有 ONES 链接
  - 需要判断多个 MR 是否等价
  - 需要按不同分支建立证据链
  - 用户后续可能要求最终 review 结论、风险点或分支差异说明
- 进入复杂模式时，读取 [references/workflow-state.md](references/workflow-state.md)。
- 如果怀疑多个 MR 只是 cherry-pick 复制，读取 [references/branch-equivalence.md](references/branch-equivalence.md)。
- 如果准备收口为正式 review 卡片，读取 [references/output-contract.md](references/output-contract.md)。

## Lock the Review Dimensions

无论是问题修复还是新功能，输出都不能只停留在“改了哪些文件”。至少要回答下面这些维度：

- `正常业务调用逻辑`
- `异常点 / 新增点`
- `什么情况下产生 / 生效`
- `怎么修复 / 怎么实现`
- `会不会有副作用`
- `有没有测试验证`

如果当前还没法把这几个维度回答完整，先输出阶段性结论，并标明还缺哪一项。

## Use the Bundled Scripts

复杂模式优先复用两个脚本：

- 初始化 issue review 状态目录：

```bash
python .claude/skills/gitlab-issue-review/scripts/init_review.py \
  --project allspark \
  --issue-url http://git.standard-robots.com/cybertron/allspark/-/issues/1078 \
  --topic "issue-1078 review"
```

- 抓取 issue / MR 上下文并归并等价改动组：

```bash
python .claude/skills/gitlab-issue-review/scripts/collect_issue_context.py \
  --issue-url http://git.standard-robots.com/cybertron/allspark/-/issues/1078 \
  --state .review/issue-1078-review/00-state.json
```

- 用户明确确认后，发布正式 MR 行评论：

```bash
python .claude/skills/gitlab-issue-review/scripts/publish_review_comments.py \
  --state .review/issue-1078-review/00-state.json \
  --mr-iid 201 \
  --review-json .review/issue-1078-review/final-review.json \
  --confirmed
```

`collect_issue_context.py` 默认会优先走 `glab`：

- 先取 issue 基本信息和 issue notes
- 再尝试发现关联 MR
- 对每个 MR 拉取 changes，并生成严格/宽松两种 patch fingerprint
- 同时产出一个 `call_logic` seed，帮助后续多轮确认问题调用逻辑
- 同时产出：
  - `change_intent`：当前更像 `bugfix` / `feature` / `mixed` / `unknown`
  - `review_dimensions`：正常链路、异常点/新增点、触发条件、副作用、测试验证的 seed
- 产出：
  - `issue_context.json`
  - `issue_summary.md`
- `publish_review_comments.py` 只认“最终 review 输入”，默认拒绝未确认发布：
  - 没有 `--confirmed` 就直接拒绝
  - 只会读取 `final_review` 或外部 `review-json`
  - 发布成功后会把 discussion id 和 MR 信息回写到 state

如果当前环境没有 `glab`，可用 `--gitlab-source http` 退化到 HTTP API；如果连 API 也拿不到，再用 `--fixture-json` 回放已导出的 issue/MR JSON。不要因为抓取失败就伪造上下文。

## Run the Workflow

### 1. Normalize the issue before reading code

- 先确认 issue URL 本身是否有效，不要一上来就搜索业务代码。
- 先建立一个最小上下文：
  - issue 标题
  - issue 描述
  - issue notes 数量
  - 关联 MR 数量
  - 是否存在 ONES 链接
- 如果 GitLab 页面当前不可匿名访问，先尝试 `glab`；还不行时，再退到 HTTP/API 或离线导出；仍失败时，明确说明缺失项并要求最小补料。

### 2. Clarify the problem call logic first

- 在真正 review 代码前，先回答这 3 个问题：
  - 用户报的现象是从哪个入口触发的
  - 问题预期落在哪条调用链或任务链路
  - 当前 MR 改动最可能打到哪个模块
- 调用逻辑的证据来源优先级：
  - issue 标题/描述/notes
  - MR 标题/描述/改动文件
  - ONES 描述、评论、附件
  - 仓库代码中的真实入口类和 service 链路
- 如果当前只能猜模块，先把它标成 `call_logic=seeded`，不要包装成已确认。
- 第一轮多半只需要把调用逻辑收敛到 1 到 2 条候选链路，而不是直接给最终根因。

### 3. Discover merge requests before branching analysis

- 先找 issue 的关联 MR，不要根据用户口头说法直接猜分支。
- MR 来源按优先级：
  - GitLab issue 关联 MR API
  - issue `closed_by`
  - issue 描述和 notes 里的 MR URL / `!iid`
- 如果没有找到 MR，先把它当成阻塞项，而不是假装已经能做代码 review。

### 4. Extract ONES links and load evidence

- 从 issue 描述、issue notes、MR 描述、MR notes 中提取 ONES URL。
- 如果提取到了 ONES 链接：
  - 立即使用 `ones` skill 抓取工单、评论、描述图片和附件
  - 不要要求用户重新贴 ONES 链接
  - 把 ONES 描述/附件当作现场证据，把仓库代码当作实现证据
- 如果 issue review 依赖 ONES 里的时间、版本、日志或截图，而这些还没抓下来，不要提前下最终结论。

### 5. Group equivalent merge requests first

- 对每个 MR 计算：
  - 改动文件列表
  - additions / deletions
  - strict patch fingerprint
  - loose patch fingerprint
- 先按 patch fingerprint 归组，再决定 review 面：
  - `identical_diff`：严格指纹相同，通常可视为等价 cherry-pick
  - `same_patch_different_context`：宽松指纹相同，多半是同一补丁跨分支落地
  - `unique_change`：需要独立 review
- 如果一个组里的 MR 只是等价 cherry-pick，默认只抽一条代表 MR 下钻。
- 只有 issue / ONES 明确提到某个分支有特有现场现象时，才恢复逐分支 review。

### 6. Decide which branches actually need code review

- 每个“唯一改动组”只保留 1 条代表 MR 和 1 个代表 target branch。
- 如果多个 target branch 对应同一改动组：
  - 默认只 review 代表 MR
  - 结论里明确写“其余分支为等价 cherry-pick，未做重复分析”
- 如果存在多个不同改动组：
  - 每个组都要单独 review
  - 不要把不同分支的风险混成一个结论

### 7. Prepare the project workspace safely

- 优先使用项目注册信息与现有 worktree 机制，不要直接污染主工作区。
- 如果 ONES 已提供版本线索，优先沿用项目运行上下文里的 `recommended_worktree`。
- 如果 review 完全由 GitLab MR target branch 决定，而不是 ONES 版本决定：
  - 为代表 branch 新建或复用 detached worktree
  - worktree 目录建议放在 `.worktrees/<project>/issue-<iid>-<branch-slug>`
- review 时明确区分：
  - issue / ONES 现场证据
  - MR 改动事实
  - 当前仓库实现推断

### 8. Review the representative changes

- review 的第一目标不是“解释所有代码”，而是回答：
  - 这个修复到底改了什么
  - 它覆盖了哪个 issue 现象和哪条调用逻辑
  - 是否遗漏了其它分支差异
  - 是否存在行为回归或隐含风险
- 优先看：
  - 入口类 / service / domain 改动
  - 配置、schema、开关、常量差异
  - 只在某个分支存在的补丁
  - 与 ONES 现场附件冲突的实现点

### 8.1 Distinguish bugfix vs feature explicitly

- 如果 `change_intent=bugfix`，输出时必须回答：
  - 正常业务调用逻辑
  - 异常点在哪里
  - 什么情况下产生
  - 怎么修复
  - 修复有没有副作用
  - 有没有相应测试验证
- 如果 `change_intent=feature`，输出时必须回答：
  - 原有业务逻辑是什么
  - 新增能力加在什么位置
  - 什么情况下生效
  - 实现方式是什么
  - 会不会影响原有链路
  - 有没有相应测试验证
  - 并注明“本次以新功能为主，不以异常修复为主”
- 如果 `change_intent=mixed`：
  - 把“问题修复部分”和“新功能部分”拆开写
  - 不要把副作用和测试验证混成一句话
- 如果 `change_intent=unknown`：
  - 明确说明当前还没确认变更意图
  - 先回到 issue 现象、MR 标题、labels 和代表改动继续确认

### 9. Drive the multi-turn loop

- 这是一个多轮完成的 review，不要试图在首轮把所有结论一次性讲完。
- 每轮优先做一件事：
  - 补 GitLab 上下文
  - 确认问题调用逻辑
  - 归并 MR 改动组
  - 拉 ONES 证据
  - 下钻代表分支代码
- 每轮只追 1 个最阻塞的补料点，最多给 3 个补料方向，不要让用户一次补一大堆东西。
- 如果用户刚补了 issue 现象或截图，不要重复要求他再解释一遍；先用新材料更新 `call_logic` 判断。

### 10. Keep the evidence chain explicit

- 结论里明确区分：
  - “GitLab issue / MR 已经证明”
  - “ONES 附件或评论支持”
  - “代码实现支持”
  - “基于多分支对比推断”
- 如果多个 MR 只是等价 patch，不要重复写同一段分析。
- 如果两个分支的改动不等价，必须明确指出差异文件或差异点，而不是笼统说“实现不同”。

### 11. Ask only for blocking follow-up

只有在下面这些缺口会改变结论时才追问：

- `glab` / GitLab 读取通道不可用
- 找不到关联 MR
- 问题调用逻辑仍然有两个以上互斥候选
- ONES 链接已发现但附件未抓取
- 多个分支 patch 不等价，但缺少其中一个代表分支代码
- issue 现象依赖版本/配置/日志，而当前证据没有

好的追问模式：

`目前我已经把 issue 关联到 3 个 MR，其中 2 个是等价 cherry-pick，只剩 release/3.1.x 这一组需要单独 review。要把结论补全，还缺它对应的 MR 详情或可访问 token。`

### 12. Return the result

对工作类问题，优先输出 `format=rich` 或 `format=flow` 的结构化 JSON。

默认包含：

- `summary`：一句话结论 + 当前置信度
- `sections`：
  - `变更类型`
  - `当前判断`
  - `问题调用逻辑`
  - `正常业务调用逻辑`
  - `异常点 / 新增点`
  - `触发条件`
  - `修复方式 / 实现方式`
  - `MR 归并结果`
  - `副作用评估`
  - `测试验证`
  - `关键证据`
  - `风险或缺口`
  - `下一步`
- 可选 `table`：
  - `MR / target branch / 改动组 / 是否需要深度分析`
- `fallback_text`：纯文本兜底

如果当前还没完成 representative branch review，就输出“阶段性 review 结果 + 缺口”，不要假装已经完成最终评审。

### 12.1 Prefer a fixed `format=rich` card contract

除非用户明确要求流程图/时序，否则 review 结果默认用 `format=rich`，而且尽量遵守固定结构：

```json
{
  "format": "rich",
  "title": "Issue Review: <issue title>",
  "summary": "一句话结论，包含变更类型和当前置信度",
  "sections": [
    {"title": "变更类型", "content": "bugfix / feature / mixed / unknown"},
    {"title": "当前判断", "content": "当前最稳的阶段性结论"},
    {"title": "问题调用逻辑", "content": "入口、链路、涉及模块"},
    {"title": "正常业务调用逻辑", "content": "正常情况下这条链路应该怎么走"},
    {"title": "异常点 / 新增点", "content": "问题修复写异常点；新功能写新增点"},
    {"title": "触发条件", "content": "什么情况下产生 / 生效"},
    {"title": "修复方式 / 实现方式", "content": "本次到底怎么改"},
    {"title": "MR 归并结果", "content": "哪些分支是代表 MR，哪些只是等价 cherry-pick"},
    {"title": "副作用评估", "content": "可能影响哪些原链路和边界场景"},
    {"title": "测试验证", "content": "新增测试、自测说明，或明确缺失"},
    {"title": "关键证据", "content": "issue / ONES / 代码 / MR 的关键证据"},
    {"title": "风险或缺口", "content": "仍缺什么"},
    {"title": "下一步", "content": "下一轮应该做什么"}
  ],
  "table": {
    "columns": [
      {"key": "mr", "label": "MR", "type": "text"},
      {"key": "branch", "label": "Target Branch", "type": "text"},
      {"key": "group", "label": "Change Group", "type": "text"},
      {"key": "deep_review", "label": "Deep Review", "type": "text"}
    ],
    "rows": []
  },
  "fallback_text": "纯文本兜底，必须包含结论、缺口和下一步"
}
```

如果脚本产物里已有 `reply_contract`，优先沿用它的 `section_order` 和 `table_columns`。

### 12.2 Use intent-specific section variants

- `bugfix`：
  - `异常点 / 新增点` 这一节写异常点
  - `summary` 里要写“这是问题修复”
- `feature`：
  - `异常点 / 新增点` 这一节写新增点
  - `summary` 里要写“本次以新功能为主，不以异常修复为主”
- `mixed`：
  - 可把 `异常点 / 新增点` 改成 `问题修复部分` 和 `新功能部分` 两节
- `unknown`：
  - 保留 `变更类型`，明确写 `unknown`
  - 不要虚构异常点或新增点

### 12.3 Generate line comments only after user confirmation

- 在用户还没有明确确认前：
  - 只输出阶段性 review 结论、风险点、缺口和下一步
  - 不要直接生成最终的 code review 行评论
- 只有当用户明确表示：
  - `可以出 review 结论`
  - `给我 review comments`
  - `可以生成行评论`
  - `确认，开始正式 review`
  才进入最终 review 阶段
- 最终 review 阶段需要额外回答两件事：
  - `line comments`：指出具体代码位置、具体问题、为什么有问题、建议怎么改
  - `merge recommendation`：当前是否可以合并
- 如果存在明确风险：
  - 输出 `line_comments`
  - `merge_recommendation=blocked`
  - 明确写“当前不建议合并”
- 如果没有发现实质风险：
  - 可以不强行凑评论
  - 直接写 `merge_recommendation=merge_ready`
  - 明确写“当前未发现阻塞性风险，可以合并”

## Apply the Confidence Rubric

- `高置信`：
  - issue、MR、ONES、代表分支代码都已经对齐
  - 多分支等价关系明确
  - 风险点能落到具体类、方法或配置
- `中置信`：
  - issue / MR 已对齐
  - 等价组已归并
  - 还缺 ONES 附件、某个分支细节或现场日志中的 1 到 2 个关键事实
- `低置信`：
  - 关联 MR 不完整
  - GitLab/ONES 无法访问
  - 分支差异还没被验证

## Keep the Review Honest

- 等价 cherry-pick 只说明“补丁形态相同”，不等于“现场表现一定相同”。
- 如果 issue 明确描述某个分支单独复现，即使 patch 相同，也要把这个冲突写出来。
- 如果 review 结果更像配置、数据、部署或版本漂移问题，要直接说明，不要强行归因到 MR 本身。
- 如果没有测试改动，也没有明确自测/验证证据，不要写成“已验证”，只能写“当前未看到对应测试验证”。
- 涉及上线、数据修复、补丁回滚或不可逆操作时，要明确标注“需要人工确认后执行”。

## Use Example Triggers

- “帮我 review 这个 issue：http://git.standard-robots.com/cybertron/allspark/-/issues/1078”
- “这个 GitLab issue 下面挂了 4 个 MR，帮我看哪些只是 cherry-pick。”
- “issue 里还有 ONES 链接和附件，先把上下文拉齐再分析风险。”
- “同一个修复打到了多个 release 分支，帮我判断是否需要逐分支 review。”
