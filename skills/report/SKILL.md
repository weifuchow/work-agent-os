---
name: report
description: 汇总前一日工作会话，生成结构化日报并保存
agent: report
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
- `scripts/send_report.py` — 通过飞书发送日报给自己

## 日报结构

- 昨日收到的工作问题
- 已处理问题
- 待跟进问题
- 风险与阻塞
- 新增待办
- 需要本人介入
- 可沉淀知识点

## 定时触发

通过 APScheduler 配置每天 08:00 自动运行。
