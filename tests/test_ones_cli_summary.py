from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ONES_SCRIPTS = ROOT / ".claude" / "skills" / "ones" / "scripts"
if str(ONES_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ONES_SCRIPTS))

from ones_cli import build_summary_snapshot, merge_subagent_summary_snapshot, parser  # noqa: E402
from core.app.context import MessageContext, PreparedWorkspace  # noqa: E402
from core.app.ones_prefetch import (  # noqa: E402
    _build_image_summary_snapshot,
    _mark_core_summary_source,
    extract_ones_reference,
    prepare_ones_intake,
)


def test_build_summary_snapshot_is_ready_and_contains_core_paths(tmp_path):
    task_dir = tmp_path / "150552_NbJXtiyGP7R4vYnF"
    image_path = task_dir / "attachment" / "desc_01.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = {
        "task": {
            "number": 150552,
            "summary": "订单取消成功，但是订单未与小车解绑",
            "description_local": "问题发生时间: 2026-04-29 10:57\n版本: 3.52.0\n小车挂着订单，同时在执行其他订单",
            "status_name": "处理中",
            "issue_type_name": "缺陷",
        },
        "named_fields": {"FMS/RIoT版本": "3.52.0"},
        "paths": {"task_dir": str(task_dir)},
    }

    snapshot = build_summary_snapshot(
        result,
        desc_files=[{"label": "desc_01", "path": str(image_path)}],
        att_files=[],
    )

    assert snapshot["status"] == "ready"
    assert "订单取消成功" in snapshot["summary_text"]
    assert snapshot["problem_time"] == "2026-04-29 10:57"
    assert snapshot["version_normalized"]
    assert snapshot["downloaded_files"][0]["path"].endswith("desc_01.png")
    assert snapshot["source"]["runtime"] == "ones_cli"
    assert snapshot["source"]["images_available"] is True
    assert snapshot["source"]["images_consumed"] is False


def test_merge_subagent_summary_snapshot_marks_success_and_preserves_files():
    base = {
        "status": "ready",
        "summary_text": "原始摘要",
        "problem_time": "",
        "version_fields": [],
        "version_from_images": [],
        "version_evidence": [],
        "business_identifiers": [],
        "observations": [],
        "image_findings": [],
        "missing_items": ["问题发生时间"],
        "downloaded_files": [{"label": "desc", "path": "desc.png", "uuid": "u1"}],
        "source": {"runtime": "ones_cli"},
    }
    agent = {
        "summary_text": "子 agent 摘要",
        "problem_time": "2026-04-29 10:57",
        "version_normalized": "3.52.0",
        "observations": ["订单取消后未解绑小车"],
        "missing_items": ["日志包"],
        "_subagent_meta": {
            "runtime": "codex",
            "model": "gpt-5.5",
            "session_id": "agent-session-1",
        },
    }

    merged = merge_subagent_summary_snapshot(base, agent)

    assert merged["summary_text"] == "子 agent 摘要"
    assert merged["problem_time"] == "2026-04-29 10:57"
    assert merged["downloaded_files"] == base["downloaded_files"]
    assert merged["source"]["subagent"] == "ones-summary"
    assert merged["source"]["subagent_status"] == "success"
    assert merged["source"]["runtime"] == "codex"


def test_fetch_task_default_summary_mode_is_deterministic(monkeypatch):
    monkeypatch.delenv("ONES_SUMMARY_MODE", raising=False)

    args = parser().parse_args(["fetch-task", "#150552"])

    assert args.summary_mode == "deterministic"


def test_core_ones_prefetch_extracts_reference_and_renames_summary_source():
    text = (
        "#150552 【3.52.0】【订单】：订单取消成功，但是订单未与小车解绑\n"
        "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"
    )
    snapshot = {
        "source": {
            "runtime": "codex",
            "images_consumed": True,
            "subagent": "ones-summary",
            "subagent_status": "success",
            "subagent_error": "[WinError 5] 拒绝访问。",
        }
    }

    _mark_core_summary_source(snapshot)

    assert extract_ones_reference(text).endswith("/task/NbJXtiyGP7R4vYnF")
    assert snapshot["source"]["summary_generator"] == "core-ones-intake"
    assert snapshot["source"]["summary_status"] == "success"
    assert "subagent" not in snapshot["source"]
    assert "subagent_error" not in snapshot["source"]


@pytest.mark.asyncio
async def test_prepare_ones_intake_refreshes_project_context_from_cached_summary(monkeypatch, tmp_path):
    session_dir = tmp_path / "session-183"
    workspace_dir = session_dir / "workspace"
    input_dir = workspace_dir / "input"
    ones_dir = session_dir / ".ones"
    task_dir = ones_dir / "150812_NbJXtiyGTbyYeF7Z"
    attachment_dir = task_dir / "attachment"
    for path in (input_dir, workspace_dir / "state", workspace_dir / "output", workspace_dir / "artifacts", attachment_dir):
        path.mkdir(parents=True, exist_ok=True)
    image_path = attachment_dir / "description_image_01.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    summary_path = task_dir / "summary_snapshot.json"
    task_path = task_dir / "task.json"
    summary_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "version_normalized": "3.52.0",
                "version_text": "3.52.0；RIOT 分支版本 origin/zwf/deadend_optimise",
                "business_identifiers": ["origin/zwf/deadend_optimise"],
                "downloaded_files": [{"label": "desc", "path": str(image_path)}],
                "source": {"images_consumed": True, "images_available": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task_path.write_text(
        json.dumps(
            {
                "project": {
                    "ones_project_name": "软件部-基础迭代组",
                    "display_name": "3.52.0",
                },
                "named_fields": {"软件版本": "3.50"},
                "task": {"number": 150812, "uuid": "NbJXtiyGTbyYeF7Z"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_roots = {
        "session_dir": str(session_dir),
        "workspace_dir": str(workspace_dir),
        "ones_dir": str(ones_dir),
        "worktrees_dir": str(session_dir / "worktrees"),
    }
    (input_dir / "project_context.json").write_text(
        json.dumps({"artifact_roots": artifact_roots}, ensure_ascii=False),
        encoding="utf-8",
    )
    workspace = PreparedWorkspace(
        path=workspace_dir,
        input_dir=input_dir,
        state_dir=workspace_dir / "state",
        output_dir=workspace_dir / "output",
        artifacts_dir=workspace_dir / "artifacts",
        artifact_roots=artifact_roots,
        media_manifest={},
        skill_registry={},
    )
    ctx = MessageContext(
        message={
            "id": 1133,
            "content": "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGTbyYeF7Z",
        },
        session={"id": 183},
        history=[],
    )
    captured = {}

    def fake_prepare_project_in_workspace(workspace_arg, project_name, *, ones_result=None, **kwargs):
        captured["project_name"] = project_name
        captured["ones_result"] = ones_result
        return {
            "running_project": project_name,
            "checkout_ref": "origin/zwf/deadend_optimise",
            "loaded_reason": kwargs.get("reason"),
        }

    monkeypatch.setattr("core.app.project_workspace.prepare_project_in_workspace", fake_prepare_project_in_workspace)
    monkeypatch.setattr(
        "core.app.ones_prefetch._infer_project_name",
        lambda *, ctx, ones_result: "allspark",
    )

    result = await prepare_ones_intake(ctx, workspace, runtime="codex")

    assert result is not None
    assert result.fetched is False
    assert result.project_context["checkout_ref"] == "origin/zwf/deadend_optimise"
    assert captured["project_name"] == "allspark"
    assert captured["ones_result"]["project"]["ones_project_name"] == "软件部-基础迭代组"
    assert captured["ones_result"]["summary_snapshot"]["version_normalized"] == "3.52.0"
    assert captured["ones_result"]["task"]["uuid"] == "NbJXtiyGTbyYeF7Z"
    assert captured["ones_result"]["paths"]["summary_snapshot_json"] == str(summary_path)


@pytest.mark.asyncio
async def test_core_ones_image_summary_persists_image_findings(monkeypatch, tmp_path):
    task_dir = tmp_path / "150552_NbJXtiyGP7R4vYnF"
    attachment_dir = task_dir / "attachment"
    attachment_dir.mkdir(parents=True)
    image_path = attachment_dir / "description_image_01.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    messages_path = task_dir / "messages.json"
    summary_path = task_dir / "summary_snapshot.json"
    task_path = task_dir / "task.json"
    messages_path.write_text(
        json.dumps(
            {
                "description_images": [
                    {
                        "label": "description_image_01.png",
                        "path": str(image_path),
                        "uuid": "image-1",
                    }
                ],
                "attachment_downloads": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task_path.write_text(
        json.dumps(
            {
                "task": {"number": 150552, "uuid": "NbJXtiyGP7R4vYnF"},
                "paths": {"summary_snapshot_json": str(summary_path)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ones_result = {
        "task": {
            "number": 150552,
            "uuid": "NbJXtiyGP7R4vYnF",
            "summary": "订单取消成功，但是订单未与小车解绑",
            "description_local": "版本 3.52.0，订单 1777271159592，车辆 方格-10008",
            "status_name": "处理中",
            "issue_type_name": "缺陷",
        },
        "named_fields": {"FMS/RIoT版本": "3.52.0"},
        "paths": {
            "task_dir": str(task_dir),
            "messages_json": str(messages_path),
            "summary_snapshot_json": str(summary_path),
            "task_json": str(task_path),
        },
    }

    class FakeAgentClient:
        def __init__(self) -> None:
            self.calls = []

        async def run(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "text": json.dumps(
                    {
                        "summary_text": "截图显示订单取消成功但车辆仍挂单",
                        "problem_time": "2026-04-29 09:11:47",
                        "version_normalized": "3.52.0",
                        "business_identifiers": [
                            "订单ID: 1777271159592",
                            "车辆: 方格-10008",
                        ],
                        "observations": [
                            "订单 1777271159592 当前状态显示为执行中",
                        ],
                        "image_findings": [
                            "操作记录显示 2026-04-29 09:11:47 对订单 1777271159592 执行取消订单结果为成功",
                            "场景监控页面显示车辆 方格-10008 仍绑定订单 1777271159592 且状态执行中",
                        ],
                        "missing_items": ["缺少后端解绑日志"],
                    },
                    ensure_ascii=False,
                ),
                "model": "fake-vision",
                "session_id": "ones-summary-session",
            }

    fake = FakeAgentClient()
    monkeypatch.setattr("core.orchestrator.agent_client.agent_client", fake)

    snapshot = await _build_image_summary_snapshot(ones_result, runtime="codex")

    assert snapshot is not None
    assert fake.calls
    assert fake.calls[0]["runtime"] == "codex"
    assert fake.calls[0]["skill"] == "ones-summary"
    assert fake.calls[0]["image_paths"] == [str(image_path.resolve())]
    assert snapshot["source"]["summary_generator"] == "core-ones-intake"
    assert snapshot["source"]["summary_status"] == "success"
    assert snapshot["source"]["images_available"] is True
    assert snapshot["source"]["images_consumed"] is True
    assert snapshot["source"]["runtime"] == "codex"
    assert "subagent" not in snapshot["source"]
    assert "subagent_error" not in snapshot["source"]

    findings = "\n".join(snapshot["image_findings"])
    assert "取消订单" in findings
    assert "1777271159592" in findings
    assert "方格-10008" in findings
    assert "执行中" in findings

    persisted_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    persisted_task = json.loads(task_path.read_text(encoding="utf-8"))
    assert persisted_summary["image_findings"] == snapshot["image_findings"]
    assert persisted_task["summary_snapshot"]["image_findings"] == snapshot["image_findings"]
    assert persisted_task["paths"]["summary_snapshot_json"] == str(summary_path)
