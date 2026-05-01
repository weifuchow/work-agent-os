from __future__ import annotations

import json
from types import SimpleNamespace

from core.app.context import PreparedWorkspace
from core.app.context import MessageContext
from core.app.reply_enrichment import enrich_reply_with_workspace_context
from core.app.result_handler import ResultHandler
from core.ports import ReplyPayload


def _workspace(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    artifacts_dir = tmp_path / "artifacts"
    for path in (input_dir, output_dir, state_dir, artifacts_dir):
        path.mkdir(parents=True, exist_ok=True)
    return PreparedWorkspace(
        path=tmp_path,
        input_dir=input_dir,
        state_dir=state_dir,
        output_dir=output_dir,
        artifacts_dir=artifacts_dir,
        artifact_roots={},
        media_manifest={},
        skill_registry={},
    )


def test_enrich_feishu_card_with_runtime_context(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.input_dir / "project_runtime_context.json").write_text(
        json.dumps(
            {
                "running_project": "allspark",
                "normalized_version": "3.52.0",
                "version_source_field": "summary_snapshot.version_normalized",
                "version_source_value": "3.52.0",
                "target_branch_ref": "master",
                "current_branch": "release/3.46.x",
                "checkout_ref": "3.52.0",
                "execution_path": r"D:\standard\work-agent-os\data\sessions\session-141\worktrees\allspark\ones-3.52.0",
                "execution_commit_sha": "a7916525f8d1",
                "execution_describe": "3.52.0",
                "execution_version": "3.52.0",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reply = ReplyPayload(
        type="feishu_card",
        content="分析结果",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**结论**\n证据不足。"},
                ],
            },
        },
    )

    enriched = enrich_reply_with_workspace_context(reply, workspace)

    first = enriched.payload["body"]["elements"][0]
    assert first["tag"] == "markdown"
    assert "运行上下文" in first["content"]
    assert "allspark" in first["content"]
    assert r"D:\standard\riot\allspark" not in first["content"]
    assert "3.52.0" in first["content"]
    assert "目标分支：master" in first["content"]
    assert "主仓库分支：release/3.46.x" in first["content"]
    assert "Worktree" in first["content"]
    assert "Worktree 版本：3.52.0" in first["content"]
    assert "a7916525f8d1 / 3.52.0" in first["content"]
    assert "运行上下文" in enriched.content


def test_enrich_feishu_card_with_direct_project_runtime_context(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.input_dir / "project_runtime_context.json").write_text(
        json.dumps(
            {
                "running_project": "allspark",
                "project_path": r"D:\standard\riot\allspark",
                "execution_path": r"D:\standard\riot\allspark",
                "current_branch": "release/3.46.x",
                "current_commit_sha": "5ed0956073b6",
                "current_describe": "3.46.16-7-g5ed095607",
                "current_version": "3.46.16",
                "execution_version": "3.46.16",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reply = ReplyPayload(
        type="feishu_card",
        content="项目上下文",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**结论**\n需要更多信息。"},
                ],
            },
        },
    )

    enriched = enrich_reply_with_workspace_context(reply, workspace)

    first = enriched.payload["body"]["elements"][0]["content"]
    assert "- 项目：allspark" in first
    assert r"- 项目目录：`D:\standard\riot\allspark`" in first
    assert "- 版本：3.46.16" in first
    assert "- 主仓库分支：release/3.46.x" in first


def test_enrich_feishu_card_moves_runtime_context_to_front(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.input_dir / "project_runtime_context.json").write_text(
        json.dumps(
            {
                "running_project": "allspark",
                "project_path": r"D:\standard\riot\allspark",
                "execution_version": "3.46",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reply = ReplyPayload(
        type="feishu_card",
        content="已有运行上下文",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**结论**\n证据不足。"},
                    {"tag": "markdown", "content": "**运行上下文**"},
                    {"tag": "markdown", "content": "- 项目：allspark\n- 版本：旧值"},
                ],
            },
        },
    )

    enriched = enrich_reply_with_workspace_context(reply, workspace)

    elements = enriched.payload["body"]["elements"]
    assert elements[0]["tag"] == "markdown"
    assert elements[0]["content"].startswith("**运行上下文**")
    assert r"D:\standard\riot\allspark" in elements[0]["content"]
    assert elements[1]["content"].startswith("**结论**")
    assert sum("运行上下文" in item.get("content", "") for item in elements) == 1


def test_result_handler_rebuilds_card_from_structured_summary(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.output_dir / "structured_summary_870.json").write_text(
        json.dumps(
            {
                "title": "#150552 DB/内存态不一致收口",
                "summary": "高置信：页面/API 展示态与调度内存态不一致。",
                "sections": [
                    {
                        "title": "关键流程图",
                        "content": "```mermaid\nflowchart TD\nA-->B\n```",
                    }
                ],
                "fallback_text": "高置信：页面/API 展示态与调度内存态不一致。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    handler = ResultHandler(
        repository=None,
        channel_port=SimpleNamespace(),
        clock=SimpleNamespace(now_iso=lambda: "2026-04-30T10:00:00"),
    )
    ctx = MessageContext(message={"id": 870}, session={"id": 145}, history=[])
    malformed_fallback = ReplyPayload(
        type="markdown",
        content="高置信：页面/API 展示态与调度内存态不一致。",
    )

    rebuilt = handler._rebuild_card_from_structured_summary(ctx, workspace, malformed_fallback)

    assert rebuilt.type == "feishu_card"
    assert rebuilt.payload["schema"] == "2.0"
    assert rebuilt.payload["header"]["title"]["content"] == "#150552 DB/内存态不一致收口"
    assert "```mermaid" in json.dumps(rebuilt.payload, ensure_ascii=False)
