---
name: context
description: 检索并聚合最小必要上下文包（最近消息、摘要、项目知识、个人偏好）
agent: context
---

# Context — 上下文检索

为问题分析阶段准备精炼的上下文包。

## 使用方式

传入当前消息和会话 ID，Context 会：

1. 查询当前 session 最近消息
2. 读取会话摘要
3. 检索相关项目知识和个人偏好
4. 输出最小必要上下文 JSON

## 关联脚本

- `scripts/build_context.py` — 根据 session_id 构建上下文包

## 上下文分层

| 层级 | 内容 | 来源 |
|------|------|------|
| Layer 1 | 当前输入 | 直接传入 |
| Layer 2 | 最近 5-15 条消息 | query_db → messages |
| Layer 3 | 会话滚动摘要 | data/sessions/*.md |
| Layer 4 | 长期记忆 | data/memory/ |
