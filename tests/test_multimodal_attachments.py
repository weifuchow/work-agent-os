from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.connectors import message_service as message_service_mod
from core.connectors.feishu import (
    FeishuClient,
    _feishu_reply_body,
    _normalize_mermaid_flowchart_labels,
    _parse_message_content,
    _replace_mermaid_blocks_with_images,
)
from core.orchestrator.agent_client import AgentClient
from core.orchestrator import codex_runtime
from core.ports import ReplyPayload
from models.db import Message


def test_build_codex_command_includes_image_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_MCP_TOOL_TIMEOUT_SECONDS", "777")
    image_path = tmp_path / "desc-1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nones-image")
    client = AgentClient()

    cmd = client._build_codex_command(
        cwd=str(tmp_path),
        model="gpt-5.4",
        scope="project",
        image_paths=[str(image_path.resolve())],
    )

    assert "--image" in cmd
    assert str(image_path.resolve()) in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--full-auto" not in cmd
    assert "mcp_servers.work_agent.startup_timeout_sec=30" in cmd
    assert "mcp_servers.work_agent.tool_timeout_sec=777" in cmd


@pytest.mark.asyncio
async def test_run_codex_kills_process_when_cancelled(monkeypatch):
    async def fake_kill_tree(*_args, **_kwargs):
        return False

    monkeypatch.setattr(codex_runtime, "_kill_windows_process_tree", fake_kill_tree)

    class FakeStdin:
        def write(self, _data):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = None
            self.stderr = None
            self.returncode = None
            self.killed = False
            self.waited = False

        async def communicate(self, _data):
            raise asyncio.CancelledError()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    proc = FakeProcess()
    client = AgentClient()

    monkeypatch.setattr(client, "_select_model", lambda model=None: "gpt-test")
    monkeypatch.setattr(client, "_build_codex_command", lambda **_kwargs: ["codex", "exec", "-"])

    async def fake_start(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(client, "_start_codex_process", fake_start)
    monkeypatch.setattr(codex_runtime, "_codex_exec_timeout_seconds", lambda _turns: 60)

    with pytest.raises(asyncio.CancelledError):
        await client._run_codex("hello", max_turns=1)

    assert proc.killed is True
    assert proc.waited is True


def test_resolve_codex_cli_uses_cmd_wrapper_when_requested(monkeypatch, tmp_path):
    vendor_exe = tmp_path / "vendor" / "codex.exe"
    vendor_exe.parent.mkdir()
    vendor_exe.write_text("", encoding="utf-8")
    wrapper_cmd = tmp_path / "codex.cmd"
    wrapper_cmd.write_text("@echo off\n", encoding="utf-8")
    ps1 = tmp_path / "codex.ps1"
    ps1.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        return {
            "codex": str(ps1),
            "codex.cmd": str(wrapper_cmd),
            "codex.exe": None,
        }.get(name)

    monkeypatch.setattr(codex_runtime, "_NPM_CODEX_EXE", vendor_exe)
    monkeypatch.setattr(codex_runtime.os, "name", "nt")
    monkeypatch.setattr(codex_runtime.shutil, "which", fake_which)

    assert codex_runtime._resolve_codex_cli_path(prefer_wrapper=False) == str(wrapper_cmd)
    assert codex_runtime._resolve_codex_cli_path(prefer_wrapper=True) == str(wrapper_cmd)


def test_should_retry_codex_start_for_windows_access_denied(monkeypatch, tmp_path):
    vendor_exe = str(tmp_path / "vendor" / "codex.exe")
    wrapper_cmd = str(tmp_path / "codex.cmd")
    exc = OSError("access denied")
    exc.winerror = 5

    monkeypatch.setattr(codex_runtime.os, "name", "nt")

    assert codex_runtime._should_retry_codex_start(exc, [vendor_exe], [wrapper_cmd])
    assert not codex_runtime._should_retry_codex_start(exc, [vendor_exe], [vendor_exe])
    assert codex_runtime._should_retry_codex_start_via_shell(exc, [wrapper_cmd])
    assert str(wrapper_cmd) in codex_runtime._windows_shell_command([wrapper_cmd, "exec", "-"])


class _FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.commit_count = 0

    def add(self, item) -> None:
        self.added.append(item)

    async def commit(self) -> None:
        self.commit_count += 1


@pytest.mark.asyncio
async def test_download_media_if_needed_persists_image_attachment(tmp_path):
    fake_session = _FakeSession()
    msg = Message(
        id=7,
        platform="feishu",
        platform_message_id="om_attach_001",
        chat_id="oc_x",
        sender_id="ou_x",
        sender_name="tester",
        message_type="image",
        content="[图片]",
        raw_payload="",
        session_id=12,
    )

    fake_client = SimpleNamespace(
        get_image_bytes=lambda image_key: (b"\x89PNGtest-image", "capture.png"),
        get_file_bytes=lambda file_key: None,
    )

    with patch.object(message_service_mod, "settings", SimpleNamespace(sessions_dir=tmp_path)), \
         patch("core.connectors.feishu.FeishuClient", return_value=fake_client):
        await message_service_mod._download_media_if_needed(
            fake_session,
            msg,
            {"media_info": {"type": "image", "image_key": "img_xxx"}},
        )

    attachment_path = Path(msg.attachment_path)
    assert attachment_path.exists()
    assert attachment_path.parent == tmp_path / "session-12" / "uploads"
    assert attachment_path.name.startswith("message-7_")
    assert attachment_path.read_bytes() == b"\x89PNGtest-image"

    media_info = json.loads(msg.media_info_json)
    assert media_info["download_status"] == "downloaded"
    assert media_info["mime_type"] == "image/png"
    assert media_info["proxy_url"] == "/api/messages/7/attachment"
    assert media_info["local_path"] == str(attachment_path.resolve())

    manifest_path = attachment_path.parent / "manifest_message-7.json"
    assert manifest_path.exists()
    assert fake_session.commit_count == 1


def test_parse_post_message_extracts_text_and_image_keys():
    content_raw = json.dumps({
        "zh_cn": {
            "title": "",
            "content": [[
                {"tag": "img", "image_key": "img_v3_001"},
                {"tag": "text", "text": "这个图片内容是什么"},
            ]],
        }
    }, ensure_ascii=False)

    content, media_info = _parse_message_content("post", content_raw)

    assert content == "这个图片内容是什么"
    assert media_info["type"] == "post"
    assert media_info["has_image"] is True
    assert media_info["image_keys"] == ["img_v3_001"]


def test_feishu_card_reply_body_strips_agent_metadata():
    reply = ReplyPayload(
        type="feishu_card",
        content="fallback",
        payload={
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "Triage"}},
            "body": {"elements": [{"tag": "markdown", "content": "结论"}]},
            "structured_summary": {"title": "Triage"},
            "artifact_path": "workspace/output/reply_contract.json",
        },
    )

    msg_type, content = _feishu_reply_body(reply)
    card = json.loads(content)

    assert msg_type == "interactive"
    assert card["schema"] == "2.0"
    assert card["body"]["elements"][0]["content"] == "结论"
    assert "structured_summary" not in card
    assert "artifact_path" not in card


def test_feishu_card_reply_body_uses_text_fallback_for_invalid_card():
    reply = ReplyPayload(type="feishu_card", content="fallback", payload={"structured_summary": {}})

    assert _feishu_reply_body(reply) == ("text", "fallback")


def test_feishu_card_reply_body_uses_safe_text_for_empty_invalid_card():
    reply = ReplyPayload(type="feishu_card", content="", payload={"structured_summary": {}})

    assert _feishu_reply_body(reply) == ("text", "回复卡片格式异常，已改用文本兜底。")


def test_feishu_card_reply_body_never_sends_raw_contract_json_as_text():
    broken = (
        '{"action":"reply","reply":{"channel":"feishu","type":"feishu_card",'
        '"content":"安全摘要：卡片格式异常时应发送兜底摘要。","payload":{"schema":"2.0"'
    )
    reply = ReplyPayload(type="text", content=broken)

    assert _feishu_reply_body(reply) == ("text", "安全摘要：卡片格式异常时应发送兜底摘要。")


def test_mermaid_markdown_block_is_replaced_with_uploaded_image(monkeypatch, tmp_path):
    image = tmp_path / "flow.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("core.connectors.feishu._render_mermaid_png", lambda source: image)

    client = SimpleNamespace(upload_image=lambda path: "img_v3_flow")
    payload = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "**关键流程图**\n```mermaid\nflowchart TD\nA-->B\n```",
                }
            ]
        },
    }

    card = _replace_mermaid_blocks_with_images(payload, client=client)

    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "markdown"
    assert "关键流程图" in elements[0]["content"]
    assert "```mermaid" not in json.dumps(elements, ensure_ascii=False)
    assert elements[1]["tag"] == "img"
    assert elements[1]["img_key"] == "img_v3_flow"


def test_direct_mermaid_card_element_is_replaced_with_uploaded_image(monkeypatch, tmp_path):
    image = tmp_path / "direct-flow.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("core.connectors.feishu._render_mermaid_png", lambda source: image)

    client = SimpleNamespace(upload_image=lambda path: "img_v3_direct_flow")
    payload = {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": "前置说明"},
                {
                    "tag": "mermaid",
                    "title": "直接流程图",
                    "source": "flowchart TD\nA-->B",
                },
            ]
        },
    }

    card = _replace_mermaid_blocks_with_images(payload, client=client)
    raw = json.dumps(card, ensure_ascii=False)

    assert '"tag": "mermaid"' not in raw
    assert card["body"]["elements"][1]["tag"] == "img"
    assert card["body"]["elements"][1]["img_key"] == "img_v3_direct_flow"
    assert card["body"]["elements"][1]["alt"]["content"] == "直接流程图"


def test_direct_mermaid_card_element_falls_back_when_render_fails(monkeypatch):
    monkeypatch.setattr("core.connectors.feishu._render_mermaid_png", lambda source: None)

    client = SimpleNamespace(upload_image=lambda path: "never")
    payload = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [
                                {
                                    "tag": "mermaid",
                                    "title": "失败流程图",
                                    "content": "```mermaid\nflowchart TD\nA-->B\n```",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }

    card = _replace_mermaid_blocks_with_images(payload, client=client)
    raw = json.dumps(card, ensure_ascii=False)
    fallback = card["body"]["elements"][0]["columns"][0]["elements"][0]

    assert '"tag": "mermaid"' not in raw
    assert "```mermaid" not in raw
    assert fallback["tag"] == "markdown"
    assert "失败流程图" in fallback["content"]
    assert "渲染失败" in fallback["content"]


def test_feishu_reply_body_never_sends_direct_mermaid_tag(monkeypatch, tmp_path):
    image = tmp_path / "reply-flow.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("core.connectors.feishu._render_mermaid_png", lambda source: image)

    reply = ReplyPayload(
        type="feishu_card",
        content="fallback",
        payload={
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "mermaid",
                        "source": "flowchart TD\nA-->B",
                    }
                ]
            },
        },
    )
    client = SimpleNamespace(upload_image=lambda path: "img_v3_reply_flow")

    msg_type, content = _feishu_reply_body(reply, client=client)
    card = json.loads(content)
    raw = json.dumps(card, ensure_ascii=False)

    assert msg_type == "interactive"
    assert '"tag": "mermaid"' not in raw
    assert card["body"]["elements"][0]["tag"] == "img"


def test_normalize_mermaid_flowchart_labels_quotes_problematic_text():
    source = "\n".join([
        "flowchart TD",
        "  N --> O[actuator存在 -> nextStage(Cancel)]",
        "  O --> P[FSM currentState=FAILED]",
    ])

    normalized = _normalize_mermaid_flowchart_labels(source)

    assert 'O["actuator存在 -> nextStage(Cancel)"]' in normalized
    assert 'P["FSM currentState=FAILED"]' in normalized


def test_get_file_bytes_falls_back_to_message_resource():
    client = FeishuClient.__new__(FeishuClient)

    primary_response = SimpleNamespace(code=999, msg="failed", file=None)
    file_obj = SimpleNamespace(
        seek=lambda offset: None,
        read=lambda: b"fallback-data",
    )
    fallback_response = SimpleNamespace(code=0, file=file_obj, file_name="evidence.log")

    client._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                file=SimpleNamespace(get=lambda request: primary_response),
                message_resource=SimpleNamespace(get=lambda request: fallback_response),
            )
        )
    )

    data = client.get_file_bytes("file_key_x", message_id="om_xxx", resource_type="file")
    assert data == (b"fallback-data", "evidence.log")


def test_get_image_bytes_falls_back_to_message_resource():
    client = FeishuClient.__new__(FeishuClient)

    primary_response = SimpleNamespace(code=234001, msg="Invalid request param.", file=None)
    file_obj = SimpleNamespace(
        seek=lambda offset: None,
        read=lambda: b"fallback-image",
    )
    fallback_response = SimpleNamespace(code=0, file=file_obj, file_name="capture.png")

    client._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                image=SimpleNamespace(get=lambda request: primary_response),
                message_resource=SimpleNamespace(get=lambda request: fallback_response),
            )
        )
    )

    data = client.get_image_bytes("img_v3_x", message_id="om_img_xxx", resource_type="image")
    assert data == (b"fallback-image", "capture.png")
