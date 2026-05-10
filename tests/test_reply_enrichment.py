from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.app.context import PreparedWorkspace
from core.app.context import MessageContext
from core.app.reply_enrichment import (
    attach_declared_feishu_reply_files,
    enhance_feishu_card_code_blocks,
    enrich_reply_with_workspace_context,
    materialize_long_feishu_card_details,
)
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
    (workspace.input_dir / "project_workspace.json").write_text(
        json.dumps(
            {
                "active_project": "allspark",
                "project_order": ["allspark"],
                "projects": {
                    "allspark": {
                        "running_project": "allspark",
                        "normalized_version": "3.52.0",
                        "version_source_field": "summary_snapshot.version_normalized",
                        "version_source_value": "3.52.0",
                        "target_branch_ref": "master",
                        "current_branch": "release/3.46.x",
                        "checkout_ref": "3.52.0",
                        "worktree_path": r"D:\standard\work-agent-os\data\sessions\session-141\worktrees\allspark\ones-3.52.0",
                        "execution_commit_sha": "a7916525f8d1",
                        "execution_describe": "3.52.0",
                        "execution_version": "3.52.0",
                    }
                },
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
    assert "检出 `3.52.0`" in first["content"]
    assert "主仓库 `release/3.46.x`" in first["content"]
    assert "Worktree" in first["content"]
    assert "a7916525f8d1 / 3.52.0" in first["content"]
    assert "运行上下文" in enriched.content


def test_enrich_feishu_card_with_direct_project_workspace_context(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.input_dir / "project_workspace.json").write_text(
        json.dumps(
            {
                "active_project": "allspark",
                "projects": {
                    "allspark": {
                        "running_project": "allspark",
                        "source_path": r"D:\standard\riot\allspark",
                        "worktree_path": r"D:\standard\work-agent-os\data\sessions\session-141\worktrees\allspark\current-3.46.16",
                        "current_branch": "release/3.46.x",
                        "current_commit_sha": "5ed0956073b6",
                        "current_describe": "3.46.16-7-g5ed095607",
                        "current_version": "3.46.16",
                        "execution_version": "3.46.16",
                    }
                },
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
    assert "**当前项目：allspark**" in first
    assert r"源仓库：`D:\standard\riot\allspark`" in first
    assert "Worktree" in first
    assert "版本 `3.46.16`" in first
    assert "主仓库 `release/3.46.x`" in first


def test_enrich_feishu_card_compacts_multi_project_runtime_context(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.input_dir / "project_workspace.json").write_text(
        json.dumps(
            {
                "active_project": "riot-frontend-v3",
                "project_order": ["allspark", "riot-frontend-v3"],
                "projects": {
                    "allspark": {
                        "running_project": "allspark",
                        "source_path": r"D:\standard\riot\allspark",
                        "worktree_path": r"D:\standard\work-agent-os\data\sessions\session-164\worktrees\allspark\entry-977-master",
                        "checkout_ref": "master",
                        "execution_version": "3.52.0",
                        "execution_commit_sha": "a7916525f8d1",
                        "execution_describe": "3.52.0",
                    },
                    "riot-frontend-v3": {
                        "running_project": "riot-frontend-v3",
                        "source_path": r"D:\standard\riot\riot-frontend-v3",
                        "worktree_path": r"D:\standard\work-agent-os\data\sessions\session-164\worktrees\riot-frontend-v3\entry-992-main",
                        "checkout_ref": "main",
                        "execution_version": "3.51.0",
                        "execution_commit_sha": "03f87835ff90",
                        "execution_describe": "v3.51.0-10-g03f87835",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reply = ReplyPayload(
        type="feishu_card",
        content="前端 WebSocket 配置梳理",
        payload={"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "**结论**\n需要调整。"}]}},
    )

    enriched = enrich_reply_with_workspace_context(reply, workspace)

    first = enriched.payload["body"]["elements"][0]["content"]
    assert "**当前项目：riot-frontend-v3**" in first
    assert "版本 `3.51.0`" in first
    assert "Worktree：`D:\\...\\session-164\\worktrees\\riot-frontend-v3\\entry-992-main`" in first
    assert "**相关项目**" in first
    assert "- allspark：版本 `3.52.0`" in first
    assert "D:\\...\\session-164\\worktrees\\allspark\\entry-977-master" in first
    assert first.count("源仓库") == 1
    assert "项目目录：" not in first
    assert "Worktree 版本" not in first


def test_enrich_feishu_card_moves_runtime_context_to_front(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.input_dir / "project_workspace.json").write_text(
        json.dumps(
            {
                "active_project": "allspark",
                "projects": {
                    "allspark": {
                        "running_project": "allspark",
                        "source_path": r"D:\standard\riot\allspark",
                        "worktree_path": r"D:\standard\work-agent-os\data\sessions\session-141\worktrees\allspark\current-3.46",
                        "execution_version": "3.46",
                    }
                },
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


def test_enhance_feishu_card_code_blocks_splits_markdown_code_panel():
    reply = ReplyPayload(
        type="feishu_card",
        content="代码说明",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "先定义类型。\n\n```ts path=src/types/robot.ts\nexport interface RobotLiteFrame {\n  deviceKey: string\n}\n```\n\n再切订阅。",
                    }
                ],
            },
        },
    )

    enhanced = enhance_feishu_card_code_blocks(reply)

    contents = [item["content"] for item in enhanced.payload["body"]["elements"]]
    assert contents[0] == "先定义类型。"
    assert "代码片段：src/types/robot.ts" in contents[1]
    assert "文件 `src/types/robot.ts`" in contents[1]
    assert "语言 `ts`" in contents[1]
    assert contents[2].startswith("```ts\nexport interface RobotLiteFrame")
    assert contents[3] == "再切订阅。"


def test_enhance_feishu_card_code_blocks_keeps_mermaid_for_image_renderer():
    reply = ReplyPayload(
        type="feishu_card",
        content="流程图",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "```mermaid\nflowchart TD\nA-->B\n```"},
                ],
            },
        },
    )

    enhanced = enhance_feishu_card_code_blocks(reply)

    elements = enhanced.payload["body"]["elements"]
    assert len(elements) == 1
    assert elements[0]["content"].startswith("```mermaid")


def test_materialize_long_feishu_card_details_is_explicit_legacy_path_not_default(tmp_path):
    workspace = _workspace(tmp_path)
    long_code = "\n".join(f"line_{index} = {index}" for index in range(80))
    reply = ReplyPayload(
        type="feishu_card",
        content="长分析",
        payload={
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "派单等待分析"}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**运行上下文**\n当前项目 allspark"},
                    {"tag": "markdown", "content": "订单 14021 长时间停在派单匹配阶段。"},
                    {"tag": "markdown", "content": "**核心结论**"},
                    {"tag": "markdown", "content": "不是未入队，而是车辆-订单匹配没有正向分数。"},
                    {"tag": "markdown", "content": "**可验证时间线**"},
                    {"tag": "markdown", "content": "13:36 入队，14:44 获得正向匹配分数，14:47 完成。"},
                    {"tag": "markdown", "content": f"**代码片段：Matcher**\n```java\n{long_code}\n```"},
                    {
                        "tag": "table",
                        "columns": [
                            {"name": "time", "display_name": "时间"},
                            {"name": "event", "display_name": "事件"},
                        ],
                        "rows": [{"time": f"13:{i:02d}", "event": f"事件 {i}"} for i in range(9)],
                    },
                ],
            },
        },
    )

    enriched = materialize_long_feishu_card_details(reply, workspace, message_id=1195)

    attachments = enriched.metadata["feishu_file_attachments"]
    detail_path = tmp_path / "output" / "reply_artifacts" / "reply-details-1195.md"
    detail = detail_path.read_text(encoding="utf-8")
    elements = enriched.payload["body"]["elements"]
    raw = json.dumps(elements, ensure_ascii=False)

    assert attachments[0]["path"] == str(detail_path.resolve())
    assert attachments[0]["file_name"] == "reply-details-1195.md"
    assert "# 派单等待分析" in detail
    assert "代码片段：Matcher" in detail
    assert "line_79 = 79" in detail
    assert "| 时间 | 事件 |" in detail
    assert "line_79 = 79" not in raw
    assert "可验证时间线" in raw
    assert "13:36 入队，14:44 获得正向匹配分数，14:47 完成。" in raw
    assert "代码片段：java" in raw
    assert "80 行" in raw
    assert "完整代码见飞书 Markdown 附件。" in raw
    assert "完整细节" in raw
    assert "reply-details-1195.md" in raw
    table = next(item for item in elements if item.get("tag") == "table")
    assert len(table["rows"]) == 6


@pytest.mark.asyncio
async def test_result_handler_does_not_auto_materialize_long_card_details(tmp_path):
    workspace = _workspace(tmp_path)
    repository = SimpleNamespace(
        audit=AsyncMock(),
        update_message=AsyncMock(),
        update_session_patch=AsyncMock(),
        save_bot_reply=AsyncMock(),
    )
    channel = SimpleNamespace(deliver_reply=AsyncMock(return_value=SimpleNamespace(delivered=True, message_id="m1", thread_id="t1", root_id="r1", raw={})))
    handler = ResultHandler(repository=repository, channel_port=channel, clock=SimpleNamespace(now_iso=lambda: "2026-05-10T00:00:00"))
    long_code = "\n".join(f"line_{index} = {index}" for index in range(80))
    reply = ReplyPayload(
        type="feishu_card",
        content="长分析",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "卡片已经完整展示分析。"},
                    {"tag": "markdown", "content": f"```java\n{long_code}\n```"},
                ],
            },
        },
    )
    ctx = MessageContext(
        message={"id": 1195, "platform_message_id": "om_1195"},
        session={"id": 190},
        history=[],
    )

    await handler._deliver_reply(ctx, workspace, reply)

    delivered = channel.deliver_reply.await_args.kwargs["reply"]
    assert "feishu_file_attachments" not in delivered.metadata
    assert not (workspace.output_dir / "reply_artifacts").exists()


def test_materialize_long_feishu_card_details_keeps_short_card_inline(tmp_path):
    workspace = _workspace(tmp_path)
    reply = ReplyPayload(
        type="feishu_card",
        content="短摘要",
        payload={
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": "简短结论。"}]},
        },
    )

    enriched = materialize_long_feishu_card_details(reply, workspace, message_id=1)

    assert enriched is reply
    assert not (tmp_path / "output" / "reply_artifacts").exists()


def test_attach_declared_feishu_reply_files_adds_card_attachment_section(tmp_path):
    workspace = _workspace(tmp_path)
    for name in ("sample.docx", "sample.pdf", "sample.md"):
        (workspace.output_dir / name).write_bytes(f"fake {name}".encode("utf-8"))
    reply = ReplyPayload(
        type="feishu_card",
        content="已生成文件",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**摘要**\n已生成 Word、PDF 和 Markdown 三份材料。"},
                ],
            },
        },
        metadata={
            "attachments": [
                {
                    "path": "sample.docx",
                    "title": "Word 版本",
                    "description": "适合编辑",
                },
                {
                    "path": "sample.pdf",
                    "title": "PDF 版本",
                    "description": "适合分发",
                },
                {
                    "path": "sample.md",
                    "title": "Markdown 源稿",
                    "description": "适合继续迭代",
                },
            ]
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    attachments = enriched.metadata["feishu_file_attachments"]
    assert [item["file_name"] for item in attachments] == ["sample.docx", "sample.pdf", "sample.md"]
    assert all(item["file_type"] == "stream" for item in attachments)
    raw = json.dumps(enriched.payload["body"]["elements"], ensure_ascii=False)
    assert "已随本话题发送" in raw
    assert "Word 版本（DOCX，适合编辑）" in raw
    assert "PDF 版本（PDF，适合分发）" in raw
    assert "Markdown 源稿（MD，适合继续迭代）" in raw


def test_attach_declared_feishu_reply_files_keeps_multiple_target_documents(tmp_path):
    workspace = _workspace(tmp_path)
    for name in ("solution-a.md", "solution-b.md"):
        (workspace.output_dir / name).write_text(f"# {name}", encoding="utf-8")
    reply = ReplyPayload(
        type="feishu_card",
        content="已生成两个方案",
        payload={"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "**摘要**\n方案 A 和方案 B 已生成。"}]}},
        metadata={
            "attachments": [
                {"path": "solution-a.md", "title": "方案 A", "description": "保守方案"},
                {"path": "solution-b.md", "title": "方案 B", "description": "激进方案"},
            ]
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    attachments = enriched.metadata["feishu_file_attachments"]
    assert [item["file_name"] for item in attachments] == ["solution-a.md", "solution-b.md"]
    raw = json.dumps(enriched.payload, ensure_ascii=False)
    assert "方案 A（MD，保守方案）" in raw
    assert "方案 B（MD，激进方案）" in raw


def test_attach_declared_feishu_reply_files_finalizes_analysis_artifact_to_workspace_output(tmp_path):
    workspace = _workspace(tmp_path)
    analysis_artifact = tmp_path / ".analysis" / "message-1218" / "allspark" / "dispatch-001" / "artifacts" / "solution-a.md"
    analysis_artifact.parent.mkdir(parents=True)
    analysis_artifact.write_text("# 方案 A", encoding="utf-8")
    reply = ReplyPayload(
        type="feishu_card",
        content="已生成方案 A",
        payload={"schema": "2.0", "body": {"elements": []}},
        metadata={
            "attachments": [
                {"path": str(analysis_artifact), "title": "方案 A", "description": "最终汇总文档"},
            ]
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    attachment = enriched.metadata["feishu_file_attachments"][0]
    final_path = workspace.output_dir / "solution-a.md"
    assert Path(attachment["path"]) == final_path.resolve()
    assert final_path.read_text(encoding="utf-8") == "# 方案 A"


def test_attach_declared_feishu_reply_files_does_not_overwrite_existing_output_attachment(tmp_path):
    workspace = _workspace(tmp_path)
    existing = workspace.output_dir / "solution-a.md"
    existing.write_text("# existing", encoding="utf-8")
    analysis_artifact = tmp_path / ".analysis" / "message-1218" / "allspark" / "dispatch-001" / "artifacts" / "solution-a.md"
    analysis_artifact.parent.mkdir(parents=True)
    analysis_artifact.write_text("# generated", encoding="utf-8")
    reply = ReplyPayload(
        type="feishu_card",
        content="已生成方案 A",
        payload={"schema": "2.0", "body": {"elements": []}},
        metadata={"attachments": [{"path": str(analysis_artifact), "title": "方案 A"}]},
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    attachment_path = Path(enriched.metadata["feishu_file_attachments"][0]["path"])
    assert attachment_path.parent == workspace.output_dir.resolve()
    assert attachment_path.name.startswith("solution-a-")
    assert attachment_path.suffix == ".md"
    assert existing.read_text(encoding="utf-8") == "# existing"
    assert attachment_path.read_text(encoding="utf-8") == "# generated"


def test_attach_declared_feishu_reply_files_does_not_resend_unlisted_old_output(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.output_dir / "old-version.pptx").write_bytes(b"old ppt")
    (workspace.output_dir / "latest-version.pptx").write_bytes(b"new ppt")
    reply = ReplyPayload(
        type="feishu_card",
        content="已更新 PPT",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**摘要**\n只发送最新版本。"},
                ],
            },
        },
        metadata={
            "attachments": [
                {
                    "path": "latest-version.pptx",
                    "title": "PPT 最新版",
                    "description": "本轮更新后的版本",
                },
            ]
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    attachments = enriched.metadata["feishu_file_attachments"]
    assert [item["file_name"] for item in attachments] == ["latest-version.pptx"]
    raw = json.dumps(enriched.payload, ensure_ascii=False)
    assert "latest-version.pptx" not in raw
    assert "old-version.pptx" not in raw


def test_attach_declared_feishu_reply_files_ignores_stale_declared_local_file(tmp_path):
    workspace = _workspace(tmp_path)
    stale = workspace.output_dir / "old-version.pptx"
    stale.write_bytes(b"old ppt")
    os.utime(stale, (10, 10))
    latest = workspace.output_dir / "latest-version.pptx"
    latest.write_bytes(b"new ppt")
    reply = ReplyPayload(
        type="feishu_card",
        content="已更新 PPT",
        payload={"schema": "2.0", "body": {"elements": []}},
        metadata={
            "attachment_not_before_mtime": 1000,
            "attachments": [
                {"path": "old-version.pptx", "title": "PPT 旧版"},
                {"path": "latest-version.pptx", "title": "PPT 最新版"},
            ],
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    attachments = enriched.metadata["feishu_file_attachments"]
    assert [item["file_name"] for item in attachments] == ["latest-version.pptx"]
    raw = json.dumps(enriched.payload, ensure_ascii=False)
    assert "PPT 最新版" in raw
    assert "PPT 旧版" not in raw


def test_attach_declared_feishu_reply_files_allows_stale_file_when_explicit(tmp_path):
    workspace = _workspace(tmp_path)
    stale = workspace.output_dir / "old-version.pptx"
    stale.write_bytes(b"old ppt")
    os.utime(stale, (10, 10))
    reply = ReplyPayload(
        type="feishu_card",
        content="用户要求历史版本",
        payload={"schema": "2.0", "body": {"elements": []}},
        metadata={
            "attachment_not_before_mtime": 1000,
            "attachments": [
                {"path": "old-version.pptx", "title": "PPT 旧版", "allow_existing": True},
            ],
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    assert [item["file_name"] for item in enriched.metadata["feishu_file_attachments"]] == ["old-version.pptx"]


def test_attach_declared_feishu_reply_files_renders_url_when_provided(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.output_dir / "report.pdf").write_bytes(b"%PDF")
    reply = ReplyPayload(
        type="feishu_card",
        content="已生成 PDF",
        payload={"schema": "2.0", "body": {"elements": []}},
        metadata={
            "attachments": [
                {
                    "path": "report.pdf",
                    "title": "PDF 报告",
                    "description": "完整报告",
                    "url": "https://example.test/report.pdf",
                },
            ]
        },
    )

    enriched = attach_declared_feishu_reply_files(reply, workspace)

    assert enriched.metadata["feishu_file_attachments"][0]["url"] == "https://example.test/report.pdf"
    raw = json.dumps(enriched.payload, ensure_ascii=False)
    assert "[PDF 报告](https://example.test/report.pdf)" in raw


def test_feature_design_card_keeps_summary_and_moves_code_details_to_attachment(tmp_path):
    workspace = _workspace(tmp_path)
    design_path = workspace.output_dir / "feature-design-details.md"
    design_path.write_text(
        "# 功能设计完整细节\n\n## 代码逻辑\n\n```ts\nconst enabled = true\n```\n",
        encoding="utf-8",
    )
    reply = ReplyPayload(
        type="feishu_card",
        content="功能设计摘要",
        payload={
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "功能设计：附件化回复"}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "**摘要**\n卡片展示设计目标、用户路径和风险。"},
                    {"tag": "markdown", "content": "**设计要点**"},
                    {"tag": "markdown", "content": "- 卡片展示摘要\n- 完整细节补充详细文件地址\n- 涉及代码内容走附件"},
                    {
                        "tag": "markdown",
                        "content": "**代码逻辑**\n```ts path=src/reply.ts\nconst shouldAttach = details.length > 1600\n```",
                    },
                ],
            },
            "attachments": [
                {
                    "path": "feature-design-details.md",
                    "title": "完整设计文档",
                    "description": "包含详细流程、边界和代码逻辑",
                }
            ],
        },
    )

    materialized = materialize_long_feishu_card_details(reply, workspace, message_id=1200)
    enriched = attach_declared_feishu_reply_files(materialized, workspace)

    raw = json.dumps(enriched.payload["body"]["elements"], ensure_ascii=False)
    attachments = enriched.metadata["feishu_file_attachments"]

    assert "卡片展示设计目标、用户路径和风险" in raw
    assert "卡片展示摘要" in raw
    assert "代码片段：src/reply.ts" in raw
    assert "const shouldAttach" not in raw
    assert "完整代码见飞书 Markdown 附件" in raw
    assert "完整细节" in raw
    assert "完整设计文档（MD，包含详细流程、边界和代码逻辑）" in raw
    assert [item["file_name"] for item in attachments] == [
        "reply-details-1200.md",
        "feature-design-details.md",
    ]


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
