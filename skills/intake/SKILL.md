---
name: intake
description: 对消息进行分类，判断工作/闲聊/噪音，提取主题和优先级
---

# Intake — 消息分类

对输入的消息进行第一层判断，输出结构化分类结果。

## 使用方式

传入一条或多条消息内容，Intake 会：

1. 判断消息类型（work_question / urgent_issue / task_request / chat / noise）
2. 提取主题、项目、优先级
3. 判断是否需要立即响应、是否需要人工审核

## 关联脚本

- `scripts/classify.py` — 批量分类入口，从数据库读取未分类消息并调用 intake agent
