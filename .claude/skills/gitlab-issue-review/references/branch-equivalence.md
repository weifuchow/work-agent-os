# Branch Equivalence Heuristics

多分支 review 的第一步不是“每个 MR 都读一遍”，而是先判断它们是不是同一补丁的重复落地。

## Equivalence Levels

- `identical_diff`
  - 改动文件集合一致
  - 规范化后的 diff 文本完全一致
  - 通常可视为同一个补丁的直接 cherry-pick
- `same_patch_different_context`
  - 改动文件集合一致
  - 去掉 hunk 坐标和 `index` 行后，diff 指纹一致
  - 常见于不同 release 分支上下文行号不同，但补丁内容相同
- `unique_change`
  - 任一文件集、diff 内容或改动路径存在真实差异
  - 需要独立 review

## Default Review Policy

- `identical_diff`：
  - 默认只 review 1 个代表 MR
  - 结论里说明其余分支是等价 cherry-pick
- `same_patch_different_context`：
  - 默认仍只抽 1 个代表 MR
  - 但要检查 issue / ONES 是否提到某个分支单独异常
- `unique_change`：
  - 每个组都要独立 review

## When to Break the Equivalence Shortcut

即使 patch 指纹一致，遇到下面情况也不要直接跳过：

- issue 明确说“只有某个 release 分支有问题”
- ONES 附件或评论指出某个分支部署形态不同
- MR 描述或 notes 有额外手工操作、配置步骤、SQL 变更
- target branch 对应的代码基线差异可能影响补丁行为

## Reporting Rule

结论里要明确写出下面两种话术之一，不要模糊：

- `!101、!108、!112 归为同一补丁组，默认按 !101 代表分析，其余视为等价 cherry-pick。`
- `!101 与 !108 的目标文件相同，但 diff 指纹不同，需要分别 review。`
