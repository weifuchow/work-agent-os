# AGENT.MD

项目操作速览，供人和自动化入口快速理解当前仓库的实际运行方式。

## 当前运行方式

- 主入口是 `core/pipeline.py`
- 支持 `claude` 和 `codex` 两种 runtime
- 会话命中项目后：
  - 已绑定 `agent_session_id + project` 的会话直接 resume 项目 Agent
  - 首轮短消息如 `allspark 项目` 可直接进入项目 Agent
  - 已绑定 `project` 但尚未生成 `agent_session_id` 的补料消息也直接进入项目 Agent
  - 其他情况走 orchestrator 判断后再分发

## 当前关键行为

- 内部工具错误文本已做脱敏，不应再把 `user cancelled MCP tool call` 直接发给飞书
- GitLab issue / MR review 过程默认写入 `.review/`
- 日志/现场/附件分析过程默认写入 `.triage/`
- 飞书图片、文件和 post 图片附件已支持下载到本地工作目录
- Feishu WebSocket 断线后会自动重连
- GitLab 正式 MR 行评论只有在用户明确确认后才允许发布

## 当前重点 Workflow Skills

- `ones`
  - 处理 ONES 工单下载、评论/附件补齐、版本线索与问题分析
  - 先做 intake，再输出 `summary_snapshot.json` 供后续链路直接消费
- `gitlab-issue-review`
  - 处理 GitLab issue / MR review、风险判断与正式评论发布

后续会继续扩展更多工作流能力，重点包括：

- 记忆系统集成
- review / triage 结果沉淀
- 更多项目专用 workflow skill

## 关键目录

- `core/`：pipeline、runtime、项目路由、飞书接入
- `core/ones_intake.py`：ONES 独立 intake 编排
- `.claude/skills/ones/`：ONES intake workflow
- `.claude/skills/ones/scripts/ones_cli.py`：ONES 本地 skill 脚本
- `.claude/skills/gitlab-issue-review/`：GitLab review workflow
- `.ones/`：ONES 工单本地缓存与结构化产物
- `.review/`：review 工作目录（运行时生成）
- `.triage/`：分析工作目录（运行时生成）
- `.tmp/run-logs/`：本地服务启动日志

## 启动命令

```bash
python -m apps.worker.feishu_worker
python -m uvicorn apps.api.main:app --port 8000
python -m apps.worker.scheduler
cd apps/admin-ui && npm run dev
cmd /c scripts\\restart_all_windows.bat
```

## 常用测试

```bash
pytest tests/test_multiturn_session.py -v
pytest tests/test_analysis_workspace.py tests/test_gitlab_issue_review_scripts.py tests/test_gitlab_review_publish_script.py tests/test_ones_desc_local.py tests/test_service_runtime.py -q
pytest tests/test_e2e_routing.py -v -s -m e2e
```

## GitLab Review 脚本

```bash
python .claude/skills/gitlab-issue-review/scripts/init_review.py --project allspark --issue-url <issue-url>
python .claude/skills/gitlab-issue-review/scripts/collect_issue_context.py --issue-url <issue-url> --state .review/<case>/00-state.json
python .claude/skills/gitlab-issue-review/scripts/publish_review_comments.py --state .review/<case>/00-state.json --mr-iid <iid> --confirmed
```

## 注意事项

- `agent_runs` 落库仍有一处已知问题：`NOT NULL constraint failed: agent_runs.input_tokens`
- 如果首轮消息只是切换项目，不必再走慢速项目识别链路
- ONES 补料消息如果 session 已经绑定项目，也不应再回退到 orchestrator

## 环境变量

```text
FEISHU_APP_ID           — 飞书应用 ID
FEISHU_APP_SECRET       — 飞书应用密钥
FEISHU_BOT_NAME         — 机器人名称（默认 WorkAgent）
FEISHU_REPORT_CHAT_ID   — 日报推送目标 chat_id（留空则不推送）
ANTHROPIC_API_KEY       — Claude API Key
ANTHROPIC_AUTH_TOKEN    — Claude Auth Token（替代 API Key）
ANTHROPIC_BASE_URL      — API 代理地址（可选）
OPENAI_API_KEY          — OpenAI API Key（可选，多 provider）
OPENAI_BASE_URL         — OpenAI API 代理地址（可选）
DATABASE_URL            — 数据库连接串
GITLAB_TOKEN            — GitLab Token（review / MR 评论脚本）
GITLAB_PROJECT_URL      — GitLab 项目地址
```
