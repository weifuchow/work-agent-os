from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.connectors import message_service as message_service_mod
from core.connectors.feishu import FeishuClient, _parse_message_content
from core.pipeline import _build_agent_input, _collect_agent_image_paths
from core.orchestrator.agent_client import AgentClient
from models.db import Message


@pytest.mark.asyncio
async def test_build_agent_input_emits_image_block(tmp_path):
    image_path = tmp_path / "evidence.png"
    image_bytes = b"\x89PNG\r\n\x1a\nfake"
    image_path.write_bytes(image_bytes)

    msg = {
        "content": "[图片]",
        "message_type": "image",
        "platform_message_id": "om_test_001",
        "attachment_path": str(image_path),
        "media_info_json": json.dumps({
            "type": "image",
            "local_path": str(image_path),
            "mime_type": "image/png",
        }, ensure_ascii=False),
    }

    prompt = _build_agent_input(msg, session=None, runtime="claude", prompt_text="请分析这张图片")
    assert not isinstance(prompt, str)

    collected = []
    async for item in prompt:
        collected.append(item)

    assert len(collected) == 1
    payload = collected[0]
    assert payload["message"]["content"][0]["type"] == "text"
    assert payload["message"]["content"][1]["type"] == "image"
    assert payload["message"]["content"][1]["mimeType"] == "image/png"
    assert base64.b64decode(payload["message"]["content"][1]["data"]) == image_bytes


@pytest.mark.asyncio
async def test_build_agent_input_includes_ones_description_images(tmp_path):
    image_path = tmp_path / "desc-1.png"
    image_bytes = b"\x89PNG\r\n\x1a\nones-image"
    image_path.write_bytes(image_bytes)

    messages_json = tmp_path / "messages.json"
    messages_json.write_text(json.dumps({
        "description_images": [
            {
                "label": "description_image_01_demo",
                "path": str(image_path),
                "uuid": "demo-uuid",
                "mime": "image/png",
            }
        ],
        "attachment_downloads": [],
    }, ensure_ascii=False), encoding="utf-8")

    msg = {
        "content": "帮我分析这个 ONES",
        "message_type": "text",
        "platform_message_id": "om_test_ones_001",
        "media_info_json": json.dumps({}, ensure_ascii=False),
    }
    ones_result = {
        "paths": {
            "messages_json": str(messages_json),
        }
    }

    prompt = _build_agent_input(
        msg,
        session=None,
        runtime="claude",
        prompt_text="请先阅读正文，再参考附图补充判断",
        ones_result=ones_result,
    )
    assert not isinstance(prompt, str)

    collected = []
    async for item in prompt:
        collected.append(item)

    assert len(collected) == 1
    payload = collected[0]
    assert payload["message"]["content"][0]["type"] == "text"
    assert payload["message"]["content"][1]["type"] == "image"
    assert payload["message"]["content"][1]["mimeType"] == "image/png"
    assert base64.b64decode(payload["message"]["content"][1]["data"]) == image_bytes


def test_collect_agent_image_paths_includes_ones_description_images(tmp_path):
    image_path = tmp_path / "desc-1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nones-image")
    messages_json = tmp_path / "messages.json"
    messages_json.write_text(json.dumps({
        "description_images": [
            {
                "label": "description_image_01_demo",
                "path": str(image_path),
                "uuid": "demo-uuid",
                "mime": "image/png",
            }
        ],
        "attachment_downloads": [],
    }, ensure_ascii=False), encoding="utf-8")

    msg = {
        "content": "帮我分析这个 ONES",
        "message_type": "text",
        "platform_message_id": "om_test_ones_paths_001",
        "media_info_json": json.dumps({}, ensure_ascii=False),
    }
    ones_result = {
        "paths": {
            "messages_json": str(messages_json),
        }
    }

    image_paths = _collect_agent_image_paths(msg, ones_result)

    assert image_paths == [str(image_path.resolve())]


def test_build_codex_command_includes_image_flags(tmp_path):
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


def test_build_agent_input_keeps_file_message_as_text_prompt(tmp_path):
    file_path = tmp_path / "evidence.log"
    file_path.write_text("2026-04-15 10:23:10 ERROR order-123 failed\n", encoding="utf-8")

    msg = {
        "content": "[文件: evidence.log]",
        "message_type": "file",
        "platform_message_id": "om_test_file_001",
        "attachment_path": str(file_path),
        "media_info_json": json.dumps({
            "type": "file",
            "local_path": str(file_path),
            "mime_type": "text/plain",
            "file_name": "evidence.log",
        }, ensure_ascii=False),
    }

    prompt = _build_agent_input(msg, session=None, runtime="claude", prompt_text="请分析这条消息")
    assert isinstance(prompt, str)
    assert prompt == "请分析这条消息"


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
    )

    fake_client = SimpleNamespace(
        get_image_bytes=lambda image_key: (b"\x89PNGtest-image", "capture.png"),
        get_file_bytes=lambda file_key: None,
    )

    with patch.object(message_service_mod, "settings", SimpleNamespace(attachments_dir=tmp_path)), \
         patch("core.connectors.feishu.FeishuClient", return_value=fake_client):
        await message_service_mod._download_media_if_needed(
            fake_session,
            msg,
            {"media_info": {"type": "image", "image_key": "img_xxx"}},
        )

    attachment_path = Path(msg.attachment_path)
    assert attachment_path.exists()
    assert attachment_path.read_bytes() == b"\x89PNGtest-image"

    media_info = json.loads(msg.media_info_json)
    assert media_info["download_status"] == "downloaded"
    assert media_info["mime_type"] == "image/png"
    assert media_info["proxy_url"] == "/api/messages/7/attachment"
    assert media_info["local_path"] == str(attachment_path.resolve())

    manifest_path = attachment_path.parent.parent / "manifest.json"
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
