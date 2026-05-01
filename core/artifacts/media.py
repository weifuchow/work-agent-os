"""Media staging for workspace artifacts."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import shutil
from typing import Any

from core.ports import FilePort


class MediaStager:
    def __init__(self, file_port: FilePort) -> None:
        self.file_port = file_port

    async def stage(
        self,
        msg: dict[str, Any],
        artifacts_dir: Path,
        *,
        uploads_dir: Path | None = None,
    ) -> dict[str, Any]:
        media_dir = uploads_dir or artifacts_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        file_scope = _message_file_scope(msg) if uploads_dir else ""
        items: list[dict[str, Any]] = []

        for index, descriptor in enumerate(_media_descriptors(msg), start=1):
            item = await self._stage_one(msg, descriptor, media_dir, index, file_scope=file_scope)
            if item:
                items.append(item)

        return {"items": items}

    async def _stage_one(
        self,
        msg: dict[str, Any],
        descriptor: dict[str, Any],
        media_dir: Path,
        index: int,
        *,
        file_scope: str = "",
    ) -> dict[str, Any] | None:
        source_path = _first_existing_path(
            descriptor.get("local_path"),
            descriptor.get("fixture_path"),
            msg.get("attachment_path"),
        )
        file_name = _safe_file_name(
            str(descriptor.get("file_name") or (source_path.name if source_path else "")),
            fallback=f"media-{index}{_guess_suffix(descriptor)}",
        )
        if file_scope:
            file_name = _safe_file_name(f"{file_scope}_{file_name}", fallback=file_name)
        target = media_dir / file_name

        mime_type = str(descriptor.get("mime_type") or descriptor.get("mime") or "")
        size_bytes = 0
        status = "staged"

        if source_path:
            if source_path.resolve() != target.resolve():
                shutil.copy2(source_path, target)
            size_bytes = target.stat().st_size
            mime_type = mime_type or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        else:
            downloaded = await self.file_port.download_message_media(
                source_message=msg,
                media_info=descriptor,
            )
            if downloaded is None:
                status = "missing"
            else:
                if downloaded.file_name:
                    downloaded_name = _safe_file_name(downloaded.file_name, fallback=target.name)
                    if file_scope:
                        downloaded_name = _safe_file_name(
                            f"{file_scope}_{downloaded_name}",
                            fallback=target.name,
                        )
                    target = media_dir / downloaded_name
                target.write_bytes(downloaded.data)
                size_bytes = len(downloaded.data)
                mime_type = (
                    downloaded.mime_type
                    or mime_type
                    or mimetypes.guess_type(target.name)[0]
                    or "application/octet-stream"
                )

        return {
            "kind": str(descriptor.get("type") or descriptor.get("kind") or msg.get("message_type") or "file"),
            "source": str(msg.get("platform") or "feishu"),
            "resource_id": str(
                descriptor.get("resource_id")
                or descriptor.get("image_key")
                or descriptor.get("file_key")
                or descriptor.get("video_key")
                or ""
            ),
            "file_name": target.name,
            "local_path": str(target.resolve()) if target.exists() else "",
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "status": status,
        }


def _media_descriptors(msg: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []

    media_info = _json_dict(msg.get("media_info_json"))
    if media_info:
        media_type = str(media_info.get("type") or msg.get("message_type") or "")
        if media_type == "post":
            for image_key in media_info.get("image_keys") or []:
                descriptors.append({"type": "image", "image_key": image_key})
        elif media_type not in {"text", "interactive", "sticker", "share_chat", "share_user"}:
            descriptors.append(media_info)

    raw_payload = _json_dict(msg.get("raw_payload"))
    attachments = raw_payload.get("attachments") if isinstance(raw_payload.get("attachments"), list) else []
    for item in attachments:
        if isinstance(item, dict):
            descriptors.append(dict(item))

    message_type = str(msg.get("message_type") or "")
    if not descriptors and message_type not in {"", "text"}:
        descriptors.append({
            "type": message_type,
            "local_path": msg.get("attachment_path") or "",
        })

    return descriptors


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_existing_path(*values: Any) -> Path | None:
    for value in values:
        if not value:
            continue
        path = Path(str(value))
        if path.exists() and path.is_file():
            return path
    return None


def _safe_file_name(name: str, *, fallback: str) -> str:
    candidate = name.strip().replace("\\", "_").replace("/", "_")
    candidate = "".join(ch for ch in candidate if ch not in '<>:"|?*').strip(" .")
    return candidate or fallback


def _message_file_scope(msg: dict[str, Any]) -> str:
    value = str(msg.get("id") or msg.get("platform_message_id") or "message").strip()
    return _safe_file_name(f"message-{value}", fallback="message")


def _guess_suffix(descriptor: dict[str, Any]) -> str:
    file_name = str(descriptor.get("file_name") or "")
    suffix = Path(file_name).suffix
    if suffix:
        return suffix
    media_type = str(descriptor.get("type") or descriptor.get("kind") or "")
    if media_type == "image":
        return ".png"
    if media_type == "audio":
        return ".audio"
    if media_type == "video":
        return ".video"
    return ".bin"
