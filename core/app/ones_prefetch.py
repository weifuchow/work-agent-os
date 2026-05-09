"""Service-side ONES intake before the main agent run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from loguru import logger

from core.app.context import MessageContext, PreparedWorkspace
from core.config import settings


ONES_URL_RE = re.compile(r"https?://[^\s<>\]]+/task/[^\s<>\]]+", re.IGNORECASE)
ONES_NUMBER_RE = re.compile(r"(?<!\w)#(?P<number>\d{4,})(?!\w)")


@dataclass(frozen=True)
class OnesPrefetchResult:
    reference: str
    fetched: bool
    summary_snapshot_json: str = ""
    images_available: bool = False
    images_consumed: bool = False
    image_findings_count: int = 0
    task_number: str = ""
    task_uuid: str = ""
    project_context: dict[str, Any] | None = None
    error: str = ""

    def to_audit_detail(self) -> dict[str, Any]:
        detail = {
            "reference": self.reference,
            "fetched": self.fetched,
            "summary_snapshot_json": self.summary_snapshot_json,
            "images_available": self.images_available,
            "images_consumed": self.images_consumed,
            "image_findings_count": self.image_findings_count,
            "task_number": self.task_number,
            "task_uuid": self.task_uuid,
            "error": self.error,
        }
        if self.project_context:
            detail["project_context"] = {
                "running_project": self.project_context.get("running_project") or "",
                "execution_path": self.project_context.get("execution_path") or self.project_context.get("worktree_path") or "",
                "checkout_ref": self.project_context.get("checkout_ref") or "",
                "execution_commit_sha": self.project_context.get("execution_commit_sha") or "",
                "loaded_reason": self.project_context.get("loaded_reason") or "",
            }
        return detail


async def prepare_ones_intake(
    ctx: MessageContext,
    workspace: PreparedWorkspace,
    *,
    runtime: str,
) -> OnesPrefetchResult | None:
    if os.getenv("WORK_AGENT_DISABLE_ONES_PREFETCH", "").strip().lower() in {"1", "true", "yes"}:
        return None

    reference = extract_ones_reference(str(ctx.message.get("content") or ""))
    if not reference:
        return None

    existing = _existing_ready_summary(Path(workspace.artifact_roots["ones_dir"]), reference)
    if existing:
        runtime_context = _prepare_project_workspace_context(
            ctx=ctx,
            workspace=workspace,
            ones_result=_ones_result_from_existing_snapshot(reference, existing),
        )
        return _result_from_snapshot(
            reference,
            existing,
            fetched=False,
            project_context=runtime_context if isinstance(runtime_context, dict) else None,
        )

    fetched = await _fetch_ones(reference, Path(workspace.artifact_roots["ones_dir"]))
    if not fetched.get("success"):
        return OnesPrefetchResult(
            reference=reference,
            fetched=False,
            error=str(fetched.get("error") or "fetch-task failed")[:500],
        )

    ones_result = fetched.get("data") if isinstance(fetched.get("data"), dict) else fetched
    if not isinstance(ones_result, dict):
        return OnesPrefetchResult(reference=reference, fetched=False, error="fetch-task returned invalid data")

    snapshot = await _build_image_summary_snapshot(ones_result, runtime=runtime)
    if not snapshot:
        snapshot = _load_snapshot_from_result(ones_result) or {}

    runtime_context = _prepare_project_workspace_context(
        ctx=ctx,
        workspace=workspace,
        ones_result=ones_result,
    )

    return _result_from_snapshot(
        reference,
        snapshot,
        fetched=True,
        project_context=runtime_context if isinstance(runtime_context, dict) else None,
    )


def extract_ones_reference(content: str) -> str:
    text = str(content or "").strip()
    match = ONES_URL_RE.search(text)
    if match:
        return match.group(0).rstrip("。)，,)")
    number = ONES_NUMBER_RE.search(text)
    if number and ("ones" in text.lower() or "工单" in text or "【" in text):
        return f"#{number.group('number')}"
    return ""


async def _fetch_ones(reference: str, output_base_dir: Path) -> dict[str, Any]:
    script = settings.project_root / ".claude" / "skills" / "ones" / "scripts" / "ones_cli.py"
    if not script.exists():
        return {"success": False, "error": f"ONES CLI not found: {script}"}

    output_base_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(script),
        "fetch-task",
        reference,
        "--output-base-dir",
        str(output_base_dir),
        "--summary-mode",
        "deterministic",
    ]
    env = {
        **os.environ,
        "ONES_SUMMARY_MODE": "deterministic",
        "PYTHONIOENCODING": "utf-8",
    }

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=str(settings.project_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(run)
    except Exception as exc:
        logger.warning("ONES prefetch failed to run fetch-task for {}: {}", reference, exc)
        return {"success": False, "error": str(exc)}

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        logger.warning("ONES prefetch fetch-task failed for {}: {}", reference, detail[:500])
        return {"success": False, "error": detail[:1000] or f"exit code {completed.returncode}"}

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"fetch-task output is not JSON: {exc}"}
    return payload if isinstance(payload, dict) else {"success": False, "error": "fetch-task output is not an object"}


async def _build_image_summary_snapshot(ones_result: dict[str, Any], *, runtime: str) -> dict[str, Any] | None:
    module = _load_ones_cli_module()
    messages = _load_messages_from_result(ones_result)
    desc_files = messages.get("description_images") or []
    att_files = messages.get("attachment_downloads") or []
    image_paths = module.summary_image_paths(desc_files, att_files)
    if not image_paths:
        return None

    base_snapshot = module.build_summary_snapshot(ones_result, desc_files=desc_files, att_files=att_files)
    prompt = module.build_summary_subagent_prompt(ones_result, desc_files=desc_files, att_files=att_files)

    from core.orchestrator.agent_client import agent_client

    response = await agent_client.run(
        prompt=prompt,
        system_prompt=(
            "你是 ONES summary_snapshot 摘要提取器。只输出一个 JSON 对象。"
            "不要下载 ONES，不要写文件，不要发送消息，不要分析最终根因。"
        ),
        max_turns=8,
        runtime=runtime,
        skill="ones-summary",
        image_paths=image_paths,
    )
    parsed = module.extract_json_object(str(response.get("text") or ""))
    if not isinstance(parsed, dict):
        return None

    parsed.setdefault("_subagent_meta", {})
    parsed["_subagent_meta"].update({
        "runtime": runtime,
        "model": response.get("model"),
        "session_id": response.get("session_id"),
    })
    snapshot = module.merge_subagent_summary_snapshot(base_snapshot, parsed)
    _mark_core_summary_source(snapshot)
    _persist_summary_snapshot(ones_result, snapshot, module=module)
    return snapshot


def _load_ones_cli_module():
    path = settings.project_root / ".claude" / "skills" / "ones" / "scripts" / "ones_cli.py"
    spec = importlib.util.spec_from_file_location("_work_agent_ones_cli", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load ONES CLI module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ones_routing_module():
    path = settings.project_root / ".claude" / "skills" / "ones" / "scripts" / "routing.py"
    spec = importlib.util.spec_from_file_location("_work_agent_ones_routing", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load ONES routing module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _prepare_project_workspace_context(
    *,
    ctx: MessageContext,
    workspace: PreparedWorkspace,
    ones_result: dict[str, Any],
) -> dict[str, Any] | None:
    project_name = _infer_project_name(ctx=ctx, ones_result=ones_result)
    if not project_name:
        return None
    try:
        from core.app.project_workspace import prepare_project_in_workspace

        entry = prepare_project_in_workspace(
            workspace,
            project_name,
            ones_result=ones_result,
            reason="ones_intake",
            active=True,
        )
    except Exception as exc:
        logger.warning("ONES prefetch failed to prepare project workspace context: {}", exc)
        return {
            "running_project": project_name,
            "status": "failed",
            "error": str(exc)[:500],
        }
    if not entry:
        return {
            "running_project": project_name,
            "status": "unavailable",
        }
    return entry


def _infer_project_name(*, ctx: MessageContext, ones_result: dict[str, Any]) -> str:
    try:
        routing = _load_ones_routing_module()
        task = ones_result.get("task") or {}
        project = ones_result.get("project") or {}
        project_name, confidence, _score, _reasons = routing.choose_project_route(
            user_message=str(ctx.message.get("content") or ""),
            task_summary=str(task.get("summary") or ""),
            task_description=str(task.get("description_local") or task.get("description") or ""),
            ones_project_name=str(project.get("ones_project_name") or ""),
            business_project_name=str(project.get("business_project_name") or project.get("display_name") or ""),
            key_fields=ones_result.get("named_fields") if isinstance(ones_result.get("named_fields"), dict) else {},
        )
        if project_name and confidence in {"high", "medium"}:
            return str(project_name)
    except Exception as exc:
        logger.warning("ONES prefetch project inference failed: {}", exc)

    snapshot = _load_snapshot_from_result(ones_result) or {}
    version = str(snapshot.get("version_normalized") or snapshot.get("version_hint") or "").strip()
    if version.startswith("3."):
        return "allspark"
    if version.startswith("2."):
        return "riot-standalone"
    if version.startswith("4."):
        return "fms-java"
    return ""


def _load_messages_from_result(ones_result: dict[str, Any]) -> dict[str, Any]:
    path = Path(str((ones_result.get("paths") or {}).get("messages_json") or ""))
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_snapshot_from_result(ones_result: dict[str, Any]) -> dict[str, Any] | None:
    embedded = ones_result.get("summary_snapshot")
    if isinstance(embedded, dict):
        return embedded
    path = Path(str((ones_result.get("paths") or {}).get("summary_snapshot_json") or ""))
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _persist_summary_snapshot(ones_result: dict[str, Any], snapshot: dict[str, Any], *, module) -> None:
    summary_path_text = str((ones_result.get("paths") or {}).get("summary_snapshot_json") or "").strip()
    if not summary_path_text:
        return
    summary_path = Path(summary_path_text)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(module.dump(snapshot), encoding="utf-8")

    task_json_text = str((ones_result.get("paths") or {}).get("task_json") or "").strip()
    task_json_path = Path(task_json_text) if task_json_text else None
    if task_json_path and task_json_path.exists():
        try:
            task_payload = json.loads(task_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            task_payload = None
        if isinstance(task_payload, dict):
            task_payload["summary_snapshot"] = snapshot
            task_payload.setdefault("paths", {})["summary_snapshot_json"] = str(summary_path)
            task_json_path.write_text(module.dump(task_payload), encoding="utf-8")

    ones_result["summary_snapshot"] = snapshot
    ones_result.setdefault("paths", {})["summary_snapshot_json"] = str(summary_path)


def _mark_core_summary_source(snapshot: dict[str, Any]) -> None:
    source = snapshot.setdefault("source", {})
    status = source.pop("subagent_status", "success")
    source.pop("subagent", None)
    source.pop("subagent_error", None)
    source["summary_generator"] = "core-ones-intake"
    source["summary_status"] = status


def _existing_ready_summary(ones_dir: Path, reference: str) -> dict[str, Any] | None:
    if not ones_dir.exists():
        return None
    token = _reference_token(reference)
    candidates = list(ones_dir.glob(f"*{token}*/summary_snapshot.json")) if token else []
    for summary_path in candidates:
        try:
            snapshot = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(snapshot, dict) and (snapshot.get("source") or {}).get("images_consumed") is True:
            return snapshot
    return None


def _ones_result_from_existing_snapshot(reference: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    task_number = ""
    task_uuid = ""
    summary_path = ""
    task_dir: Path | None = None
    downloaded = snapshot.get("downloaded_files") or []
    if downloaded:
        first_path = Path(str((downloaded[0] or {}).get("path") or ""))
        task_dir = first_path.parent.parent if first_path.parent.name == "attachment" else first_path.parent
        if task_dir.name:
            summary_path = str(task_dir / "summary_snapshot.json")
        if "_" in task_dir.name:
            task_number, task_uuid = task_dir.name.split("_", 1)
    if not task_uuid:
        token = _reference_token(reference)
        if token and not token.isdigit():
            task_uuid = token
        elif token:
            task_number = token

    result: dict[str, Any] = {}
    task_json = task_dir / "task.json" if task_dir else None
    if task_json and task_json.exists():
        try:
            loaded = json.loads(task_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            result.update(loaded)

    task_payload = dict(result.get("task") or {}) if isinstance(result.get("task"), dict) else {}
    if task_number and not str(task_payload.get("number") or "").strip():
        task_payload["number"] = task_number
    if task_uuid and not str(task_payload.get("uuid") or "").strip():
        task_payload["uuid"] = task_uuid
    result["task"] = task_payload
    if not isinstance(result.get("project"), dict):
        result["project"] = {}
    if not isinstance(result.get("named_fields"), dict):
        result["named_fields"] = {}
    result["summary_snapshot"] = snapshot

    paths = dict(result.get("paths") or {}) if isinstance(result.get("paths"), dict) else {}
    if summary_path:
        paths["summary_snapshot_json"] = summary_path
    if task_dir:
        paths.setdefault("task_dir", str(task_dir))
    if task_json:
        paths.setdefault("task_json", str(task_json))
    result["paths"] = paths
    return result


def _reference_token(reference: str) -> str:
    match = re.search(r"/task/(?P<task>[^/?#\s]+)", reference)
    if match:
        return match.group("task")
    return reference.lstrip("#").strip()


def _result_from_snapshot(
    reference: str,
    snapshot: dict[str, Any],
    *,
    fetched: bool,
    project_context: dict[str, Any] | None = None,
) -> OnesPrefetchResult:
    source = snapshot.get("source") or {}
    downloaded = snapshot.get("downloaded_files") or []
    task_number = ""
    task_uuid = ""
    summary_path = ""
    if downloaded:
        first_path = Path(str((downloaded[0] or {}).get("path") or ""))
        task_dir = first_path.parent.parent if first_path.parent.name == "attachment" else first_path.parent
        summary_path = str(task_dir / "summary_snapshot.json")
        name = task_dir.name
        if "_" in name:
            task_number, task_uuid = name.split("_", 1)

    return OnesPrefetchResult(
        reference=reference,
        fetched=fetched,
        summary_snapshot_json=summary_path,
        images_available=bool(source.get("images_available")),
        images_consumed=bool(source.get("images_consumed")),
        image_findings_count=len(snapshot.get("image_findings") or []),
        task_number=task_number,
        task_uuid=task_uuid,
        project_context=project_context,
    )
