# Rich Output Contract

GitLab issue review 的默认结果应使用 `format=rich`，除非用户明确要求流程、步骤或时序图。

## Default Card Shape

```json
{
  "format": "rich",
  "title": "Issue Review: <issue title>",
  "summary": "一句话结论，包含变更类型和置信度",
  "sections": [
    {"title": "变更类型", "content": "bugfix / feature / mixed / unknown"},
    {"title": "当前判断", "content": ""},
    {"title": "问题调用逻辑", "content": ""},
    {"title": "正常业务调用逻辑", "content": ""},
    {"title": "异常点 / 新增点", "content": ""},
    {"title": "触发条件", "content": ""},
    {"title": "修复方式 / 实现方式", "content": ""},
    {"title": "MR 归并结果", "content": ""},
    {"title": "副作用评估", "content": ""},
    {"title": "测试验证", "content": ""},
    {"title": "关键证据", "content": ""},
    {"title": "风险或缺口", "content": ""},
    {"title": "下一步", "content": ""}
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
  "line_comments": [
    {
      "path": "src/main/java/com/demo/OrderService.java",
      "line": 128,
      "severity": "major",
      "body": "这里仍然会在空状态下继续调用下游，建议先做空值保护。"
    }
  ],
  "merge_recommendation": "blocked | merge_ready",
  "fallback_text": "纯文本兜底，必须覆盖结论、缺口和下一步"
}
```

## Intent Variants

### bugfix

- `summary`：
  - 写明“这是问题修复”
- `异常点 / 新增点`：
  - 写异常点
- `测试验证`：
  - 若没有测试改动或明确验证记录，直接写“当前未看到对应测试验证”

### feature

- `summary`：
  - 写明“本次以新功能为主，不以异常修复为主”
- `异常点 / 新增点`：
  - 写新增点
- `副作用评估`：
  - 优先评估兼容影响、默认行为变化、开关/配置影响

### mixed

- 可以将：
  - `异常点 / 新增点`
  - `修复方式 / 实现方式`
  改写成：
  - `问题修复部分`
  - `新功能部分`
- 但 `副作用评估` 和 `测试验证` 仍然必须保留

### unknown

- `变更类型` 必须显式写 `unknown`
- 不要虚构异常点或新增点
- `当前判断` 和 `下一步` 应明确说明当前还缺哪一类确认

## Table Guidance

`table.rows` 推荐至少包含：

- `mr`
- `branch`
- `group`
- `deep_review`

其中：

- `group` 可写 `unique_change` / `identical_diff` / `same_patch_different_context`
- `deep_review` 可写 `yes` / `skip-duplicate`

## Fallback Rule

`fallback_text` 不能只复制 `summary`，至少要包含：

- 当前主结论
- 当前缺口
- 下一步动作

## Final Review Rule

- 只有在用户明确确认后，才输出 `line_comments`
- `line_comments` 应尽量包含：
  - `path`
  - `line`
  - `severity`
  - `body`
- 如果没有发现阻塞性问题：
  - `line_comments` 可以为空数组
  - `merge_recommendation` 应写 `merge_ready`
- 如果发现阻塞性问题：
  - `merge_recommendation` 应写 `blocked`
  - `fallback_text` 里必须明确写“当前不建议合并”
