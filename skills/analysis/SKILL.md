---
name: analysis
description: 对工作问题进行结构化深度分析，输出分析报告
---

# Analysis — 问题分析

对工作问题进行结构化拆解和深度分析。

## 使用方式

传入问题描述（通常带上 context agent 的输出），Analysis 会：

1. 理解问题本质
2. 拆解多个维度
3. 输出结构化分析报告

## 关联脚本

- `scripts/analyze.py` — 对指定 session 的最新消息运行分析
