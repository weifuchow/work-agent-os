# Structured Workflow State

在以下场景进入结构化状态流：

- 用户直接给了 GitLab issue URL
- issue 下存在多个 MR
- 需要判断多个 target branch 是否只是等价 cherry-pick
- issue / MR 文本里出现 ONES 链接
- 用户后续大概率会要求正式 review 结论、风险说明或逐分支差异摘要

## Lightweight State Contract

复杂问题建议维护一个轻量状态对象。可以保存在会话记忆里，或写到
`artifact_roots.review_dir/<topic>/00-state.json`。`artifact_roots` 来自当前
`workspace/input/artifact_roots.json`；session 级目录导航在
`artifact_roots.session_dir/session_workspace.json`，不要写到项目根目录 `.review/`。

建议字段：

```json
{
  "project": "allspark",
  "mode": "structured",
  "phase": "initialized",
  "gitlab_collection": {
    "source": "glab | http | fixture",
    "status": "pending | collected | blocked",
    "notes": []
  },
  "issue": {
    "url": "http://git.standard-robots.com/cybertron/allspark/-/issues/1078",
    "host": "http://git.standard-robots.com",
    "project_path": "cybertron/allspark",
    "iid": "1078",
    "title": "",
    "state": "",
    "labels": []
  },
  "ones_links": [],
  "mr_candidates": [],
  "mr_equivalence_groups": [],
  "branch_targets": [],
  "change_intent": {
    "type": "bugfix | feature | mixed | unknown",
    "signals": [],
    "notes": []
  },
  "call_logic": {
    "status": "pending | seeded | confirmed",
    "entry_points": [],
    "changed_modules": [],
    "notes": []
  },
  "review_dimensions": {
    "status": "pending | seeded | confirmed",
    "template": "bugfix | feature | mixed | generic",
    "normal_business_flow": "",
    "anomaly_point": "",
    "trigger_conditions": [],
    "fix_summary": "",
    "side_effect_risks": [],
    "test_validation": {
      "status": "present | partial | missing | unknown",
      "signals": []
    },
    "output_notes": []
  },
  "final_review": {
    "status": "pending | confirmed | commented | merge_ready",
    "line_comments": [],
    "merge_recommendation": "pending | blocked | merge_ready",
    "merge_reason": "",
    "published_to_mr": false,
    "published_mr": {
      "project_path": "",
      "mr_iid": ""
    },
    "published_discussion_ids": [],
    "published_at": "",
    "notes": []
  },
  "review_scope": {
    "distinct_change_groups": 0,
    "representative_mrs": [],
    "skipped_groups": []
  },
  "worktree_plan": {
    "status": "pending",
    "notes": []
  },
  "evidence_chain_status": "weak",
  "confidence": "low",
  "missing_items": [],
  "analysis_artifacts": {
    "last_issue_context_json": "",
    "last_summary_md": ""
  },
  "user_confirmation": {
    "formal_report": false
  }
}
```

## Recommended Phases

- `initialized`
- `issue_collected`
- `mr_collected`
- `call_logic_seeded`
- `call_logic_confirmed`
- `intent_confirmed`
- `dimensions_seeded`
- `ones_link_detected`
- `ones_evidence_loaded`
- `review_scoped`
- `evidence_reviewed`
- `review_confirmed`
- `line_comments_ready`
- `merge_ready`
- `awaiting_input`
- `ready_for_report`
- `completed`

## Phase Rules

- `mr_collected` 之前，不要开始逐分支代码 review。
- `call_logic_seeded` 阶段必须先把问题入口、涉及模块和代表 MR 基本对齐。
- `call_logic_confirmed` 之前，不要把调用链推断包装成确定性结论。
- `intent_confirmed` 之前，不能把变更默认包装成“问题修复”或“新功能”。
- `dimensions_seeded` 阶段至少要把正常业务链路、异常点/新增点、触发条件、副作用、测试验证几个问题立起来。
- `review_scoped` 阶段必须先完成 MR 归并，再决定代表分支。
- `review_scoped` 阶段要明确哪些组需要深度分析，哪些组只保留代表 MR。
- `review_confirmed` 只有在用户明确确认后才能进入。
- `line_comments_ready` 阶段需要能指出具体文件/行和评论内容。
- `merge_ready` 仅表示“当前没有阻塞性风险，可以合并”，不是强制马上合并。
- `ready_for_report` 仅表示“可以给 review 结论”，不代表应该立刻输出正式报告。
- `formal_report` 只有在用户明确同意后才改成 `true`。

## Human-in-the-loop Rule

- 这是多轮工作流，不要求首轮完整。
- 每轮只推动一个最关键状态前进：
  - `gitlab_collection`
  - `change_intent`
  - `call_logic`
  - `review_dimensions`
  - `mr_equivalence_groups`
  - `ones_evidence_loaded`
  - `review_scoped`
- 只有在用户确认后，才允许推进：
  - `final_review.line_comments`
  - `final_review.merge_recommendation`
- 如果当前阻塞在 `call_logic`，优先向用户追问现象入口、触发动作、关键模块名，而不是直接追要所有日志。
- 如果当前阻塞在 `change_intent`，优先确认“这是问题修复还是新功能”，不要提前给异常点结论。

## Representative Branch Rule

- 一个等价 patch 组里，默认只保留 1 条代表 MR。
- 代表 MR 最好优先选择：
  - 已 merged
  - iid 更早
  - target branch 更接近现场分支
- 其余 MR 在结论里标注为“等价 cherry-pick，未做重复深挖”。

## Interim Update Contract

每轮中间汇报保持短格式：

- `阶段`
- `GitLab 读取通道`
- `变更类型`
- `当前判断`
- `问题调用逻辑`
- `业务逻辑 / 异常点 / 副作用 / 测试验证`
- `MR 归并结果`
- `最终 review / 可否合并`
- `当前证据`
- `置信度`
- `缺口`
- `下一步`

如果还没完成代表分支 review，明确说明“暂不出正式 review 结论”。

## Final Report Gate

只有满足以下条件时，才建议请求用户确认出正式报告：

- issue 与关联 MR 已经对齐
- 多分支 patch 组已经归并
- `change_intent` 已经确认
- `review_dimensions` 至少达到 `seeded`
- ONES 链接已提取，必要时已抓取附件/评论
- 至少一个代表分支已经完成代码 review
- 证据链达到 `partial` 或 `complete`
- 如果要生成 line comments，必须有用户确认
