---
name: report
description: 汇总前一日工作会话，生成结构化日报并保存
---

# Report — 日报生成

每日自动汇总前一日所有工作会话，生成结构化日报。

## 使用方式

直接调用（无需传入参数），Report 会：

1. 查询前一日所有有效 work session
2. 读取摘要、待办、风险标签
3. 生成日报 Markdown
4. 保存到 `data/reports/daily/{date}.md`

## 关联脚本

- `scripts/generate_daily.py` — 手动触发日报生成

## 定时触发

通过 APScheduler 配置每天 08:00 自动运行。
