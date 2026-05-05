# AGENT.MD

供人和自动化入口快速理解 `work-agent-os` 当前运行方式的操作速览。

## 先看这些

- 主入口：`core/pipeline.py`
- 主 use case：`core/app/message_processor.py`
- runtime adapter：`core/orchestrator/agent_client.py`
- tools 和项目 dispatch：`core/orchestrator/tools.py`
- 项目/worktree runtime context：`core/projects.py`
- 管理 API：`apps/api/routers/admin.py`
- 前端：`apps/admin-ui/`

Agent 只返回 contract。core 负责发送回复、更新 session、记录 audit 和保存 bot message。

## Workspace 规则

所有运行产物都应使用这里准备好的 session artifact roots：

```text
data/sessions/session-<id>/workspace/input/artifact_roots.json
```

重要 root：

```text
.ones       -> session-scoped ONES 产物
.triage     -> session-scoped 日志排障产物
.review     -> session-scoped review 产物
worktrees   -> session-scoped 项目 worktrees
uploads     -> 飞书下载媒体
attachments -> 解压/整理后的附件
scratch     -> 临时文件
```

不要在仓库根目录新建运行时 `.ones`、`.triage`、`.review`、`.session` 目录。

## Project Worktree

需要版本对齐的项目工作，使用 `prepare_project_runtime_context()`，并传入 session worktree root：

```text
data/sessions/session-<id>/worktrees/<project>/<task-version>
```

`project_runtime_context.json` 是主项目或活跃项目的兼容文件，保持单项目对象。多项目支持应新增每项目 runtime 文件和多项目索引，不要破坏这个旧结构。

## Subagent Dispatch

需要项目源码上下文时，使用 `dispatch_to_project`。

正确责任划分：

```text
主 Agent 判断项目
core 准备 runtime/worktree
项目 subagent 在 execution_path 中运行
主 Agent 汇总结果
```

不要让项目 subagent 自己猜或手动切换 worktree。

## 常用命令

```bash
python scripts/init_db.py
python -m apps.worker.feishu_worker
python -m uvicorn apps.api.main:app --port 8000
python -m apps.worker.scheduler

cd apps/admin-ui
npm install
npm run dev

cmd /c scripts\restart_all_windows.bat
```

## 测试

```bash
pytest tests/baseline -q
pytest tests/test_ones_routing.py tests/test_direct_project_context.py tests/test_dispatch_to_project.py -q
pytest tests/test_reply_enrichment.py tests/test_service_runtime.py tests/test_multimodal_attachments.py -q
pytest tests/test_gitlab_issue_review_scripts.py tests/test_gitlab_review_publish_script.py -q
pytest tests/test_riot_log_triage_scripts.py tests/test_riot_log_triage_replay_case.py -q
pytest tests/test_e2e_routing.py -v -s -m e2e
```

前端：

```bash
cd apps/admin-ui
npm run build
npm run lint
```

## Runtime Skills

- `ones`：ONES 工单 intake 和 summary snapshot。
- `riot-log-triage`：RIOT/FMS/allspark 现场和日志排障。
- `gitlab-issue-review`：GitLab issue/MR review workflow。
- `feishu_card_builder`：结构化摘要和飞书卡片渲染。
- `daily-report`：日报生成。

## Guardrails

- 飞书消息只能由 channel adapter 发送。
- bot reply 只能由 repository/result handler 保存。
- GitLab MR 评论必须在用户明确确认后发布。
- 内部 MCP/tool 错误必须脱敏后才能作为用户可见回复。
- 大日志和中间证据写到 artifact 文件，不要塞进聊天输出。
