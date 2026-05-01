"""Generic agent/skill result contract."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from core.ports import AgentResponse, ReplyPayload


VALID_ACTIONS = {"reply", "no_reply", "failed"}
LEGACY_ACTIONS = {"replied": "reply", "drafted": "reply", "silent": "no_reply"}


@dataclass(frozen=True)
class SkillResult:
    action: str
    reply: ReplyPayload | None = None
    session_patch: dict[str, Any] = field(default_factory=dict)
    workspace_patch: dict[str, Any] = field(default_factory=dict)
    skill_trace: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_skill_result(response: AgentResponse | dict[str, Any] | str) -> SkillResult:
    """Parse the generic contract, accepting legacy replies during migration."""

    if isinstance(response, AgentResponse):
        parsed = _parse_json_object(response.text)
        raw = dict(response.raw)
        raw.update({
            "text": response.text,
            "session_id": response.session_id,
            "runtime": response.runtime,
            "model": response.model,
            "usage": response.usage,
        })
    elif isinstance(response, dict):
        if "text" in response and not _looks_like_contract(response):
            parsed = _parse_json_object(str(response.get("text") or ""))
            raw = dict(response)
        else:
            parsed = response
            raw = dict(response)
    else:
        parsed = _parse_json_object(str(response or ""))
        raw = {"text": str(response or "")}

    if not parsed:
        text = str(raw.get("text") or "").strip()
        if text:
            recovered = _recover_malformed_contract(text)
            if recovered:
                return SkillResult(
                    action="reply",
                    reply=_reply_from_value(recovered["reply"]),
                    skill_trace=[{"skill": "contract_repair", "reason": "recovered malformed agent contract"}],
                    audit=[
                        {
                            "event_type": "agent_contract_repaired",
                            "detail": {
                                "reason": recovered["reason"],
                                "original_length": len(text),
                            },
                        }
                    ],
                    raw={**raw, "contract_repair": recovered["reason"]},
                )
            if _looks_like_contract_text(text):
                return SkillResult(
                    action="failed",
                    error_message="agent returned malformed JSON contract",
                    raw=raw,
                )
            return SkillResult(
                action="reply",
                reply=ReplyPayload(content=text),
                raw=raw,
            )
        return SkillResult(action="failed", error_message="empty agent output", raw=raw)

    parsed = _normalize_legacy_result(parsed)
    action = str(parsed.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        return SkillResult(
            action="failed",
            error_message=f"unsupported action: {action or '<empty>'}",
            raw={**raw, "parsed": parsed},
        )

    session_patch = _dict_value(parsed.get("session_patch"))
    response_session_id = str(raw.get("session_id") or "").strip()
    if response_session_id and not session_patch.get("agent_session_id"):
        session_patch["agent_session_id"] = response_session_id
    response_runtime = str(raw.get("runtime") or "").strip()
    if response_runtime and not session_patch.get("agent_runtime"):
        session_patch["agent_runtime"] = response_runtime

    return SkillResult(
        action=action,
        reply=_reply_from_value(parsed.get("reply")),
        session_patch=session_patch,
        workspace_patch=_dict_value(parsed.get("workspace_patch")),
        skill_trace=_list_of_dicts(parsed.get("skill_trace")),
        audit=_list_of_dicts(parsed.get("audit")),
        error_message=str(parsed.get("error_message") or parsed.get("error") or ""),
        raw={**raw, "parsed": parsed},
    )


def _looks_like_contract(value: dict[str, Any]) -> bool:
    action = value.get("action")
    return action in VALID_ACTIONS or action in LEGACY_ACTIONS


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    candidates = [text]
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block:
                candidates.append(block)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    end = text.rfind("}")
    while end >= 0:
        depth = 0
        start = end
        while start >= 0:
            if text[start] == "}":
                depth += 1
            elif text[start] == "{":
                depth -= 1
                if depth == 0:
                    break
            start -= 1
        if start >= 0 and depth == 0:
            try:
                parsed = json.loads(text[start : end + 1])
            except (TypeError, ValueError, json.JSONDecodeError):
                end = text.rfind("}", 0, start)
                continue
            if isinstance(parsed, dict):
                return parsed
        end = text.rfind("}", 0, max(start, 0))
    return None


def _looks_like_contract_text(text: str) -> bool:
    prefix = str(text or "").lstrip()[:2000]
    return (
        '"action"' in prefix
        and '"reply"' in prefix
        and ("feishu_card" in prefix or '"channel"' in prefix)
    )


def _recover_malformed_contract(text: str) -> dict[str, Any] | None:
    """Recover a safe reply from a truncated JSON contract.

    This deliberately does not try to reconstruct a Feishu card payload. If the
    payload is incomplete, the safe behavior is to send the already-complete
    fallback content as markdown instead of leaking raw JSON into the chat.
    """

    if not _looks_like_contract_text(text):
        return None
    content = _extract_json_string_field(text, "content")
    if not content:
        return None
    return {
        "reason": "truncated_or_invalid_json_contract",
        "reply": {
            "channel": "feishu",
            "type": "markdown",
            "content": content,
            "payload": None,
        },
    }


def _extract_json_string_field(text: str, field_name: str) -> str:
    pattern = re.compile(rf'"{re.escape(field_name)}"\s*:\s*')
    decoder = json.JSONDecoder()
    for match in pattern.finditer(text):
        start = match.end()
        try:
            value, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_legacy_result(value: dict[str, Any]) -> dict[str, Any]:
    action = str(value.get("action") or "").strip()
    if action not in LEGACY_ACTIONS:
        return value

    reply_content = value.get("reply_content")
    reply_type = "text"
    payload = None
    content = ""

    if isinstance(reply_content, dict):
        content = str(
            reply_content.get("fallback_text")
            or reply_content.get("content")
            or reply_content.get("summary")
            or reply_content.get("title")
            or ""
        )
        if reply_content.get("msg_type") == "interactive":
            reply_type = "feishu_card"
            payload = reply_content.get("card") or reply_content.get("payload") or reply_content
        elif reply_content.get("type") in {"text", "markdown", "feishu_card", "file"}:
            reply_type = str(reply_content.get("type"))
            payload = reply_content.get("payload")
    else:
        content = str(reply_content or "")

    result = {
        "action": LEGACY_ACTIONS[action],
        "reply": {
            "channel": "feishu",
            "type": reply_type,
            "content": content,
            "payload": payload,
        },
        "session_patch": {},
        "workspace_patch": {},
        "skill_trace": value.get("skill_trace") or [],
        "audit": value.get("audit") or [],
    }
    project_name = str(value.get("project_name") or "").strip()
    if project_name:
        result["session_patch"]["project"] = project_name
    return result


def _reply_from_value(value: Any) -> ReplyPayload | None:
    if value is None:
        return None
    if isinstance(value, ReplyPayload):
        return value
    if isinstance(value, dict):
        return ReplyPayload.from_dict(value)
    if isinstance(value, str):
        return ReplyPayload(content=value)
    return ReplyPayload(content=str(value))


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
