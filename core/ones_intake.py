from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import settings


_LOG_ARTIFACT_SUFFIXES = (
    ".log",
    ".txt",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
)
_CONFIG_EVIDENCE_KEYWORDS = (
    "配置",
    "config",
    "server.ssl",
    "jetty",
    "nginx",
    "端口",
    "证书",
    "ssl",
    "开关",
    "参数",
    "env",
    "yaml",
    "yml",
    "properties",
)
_INCIDENT_TIME_KEYWORDS = (
    "发生问题时间",
    "问题时间",
    "故障时间",
    "发生时间",
    "时间点",
    "问题出现时间",
)
_IDENTIFIER_KEYWORDS = (
    "订单",
    "任务号",
    "任务id",
    "车辆",
    "车号",
    "车体",
    "设备",
    "机器人",
    "request id",
    "request_id",
    "trace id",
    "trace_id",
    "号车",
    "序列号",
    "sn",
)


def _truncate_text(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
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
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def ones_link_detected(msg: dict) -> bool:
    content = str(msg.get("content") or "").strip()
    if not content or "ones." not in content.lower():
        return False
    try:
        from core.ones_routing import extract_ones_task_link
    except Exception:
        return False
    return bool(extract_ones_task_link(content))


def extract_ones_link(msg: dict) -> str:
    content = str(msg.get("content") or "").strip()
    if not content:
        return ""
    try:
        from core.ones_routing import extract_ones_task_link
    except Exception:
        return ""
    extracted = extract_ones_task_link(content)
    return extracted[2] if extracted else ""


def find_existing_ones_task_artifacts(task_ref: str) -> dict[str, Any] | None:
    ones_root = settings.project_root / ".ones"
    if not task_ref or not ones_root.exists():
        return None
    for task_dir in sorted(ones_root.glob(f"*_{task_ref}")):
        task_json_path = task_dir / "task.json"
        if not task_json_path.exists():
            continue
        try:
            payload = json.loads(task_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        return payload if isinstance(payload, dict) else None
    return None


def ones_summary_snapshot_path(ones_result: dict[str, Any] | None) -> Path | None:
    if not isinstance(ones_result, dict):
        return None
    paths = ones_result.get("paths") or {}
    explicit = str(paths.get("summary_snapshot_json") or "").strip()
    if explicit:
        return Path(explicit)
    task_dir = str(paths.get("task_dir") or "").strip()
    if task_dir:
        return Path(task_dir) / "summary_snapshot.json"
    return None


def load_ones_summary_snapshot(ones_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(ones_result, dict):
        return None
    embedded = ones_result.get("summary_snapshot")
    if isinstance(embedded, dict) and embedded:
        return embedded
    snapshot_path = ones_summary_snapshot_path(ones_result)
    if not snapshot_path or not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def collect_ones_downloaded_files(ones_result: dict[str, Any]) -> list[dict[str, str]]:
    snapshot = load_ones_summary_snapshot(ones_result)
    if isinstance(snapshot, dict):
        files = [
            {
                "label": str(item.get("label") or "").strip() or Path(str(item.get("path") or "")).name,
                "path": str(item.get("path") or "").strip(),
                "uuid": str(item.get("uuid") or "").strip(),
            }
            for item in (snapshot.get("downloaded_files") or [])
            if str(item.get("path") or "").strip()
        ]
        if files:
            return files

    paths = (ones_result.get("paths") or {}) if isinstance(ones_result, dict) else {}
    messages_json_path = str(paths.get("messages_json") or "").strip()
    if not messages_json_path:
        return []
    try:
        payload = json.loads(Path(messages_json_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return []

    files: list[dict[str, str]] = []
    for item in payload.get("description_images", []) or []:
        path = str(item.get("path") or "").strip()
        if path:
            files.append({
                "label": str(item.get("label") or "").strip() or Path(path).name,
                "path": path,
                "uuid": str(item.get("uuid") or "").strip(),
            })
    for item in payload.get("attachment_downloads", []) or []:
        path = str(item.get("path") or "").strip()
        if path:
            files.append({
                "label": str(item.get("label") or "").strip() or Path(path).name,
                "path": path,
                "uuid": str(item.get("uuid") or "").strip(),
            })
    return files


def collect_ones_multimodal_image_paths(
    ones_result: dict[str, Any] | None,
    limit: int = 4,
) -> list[str]:
    if not isinstance(ones_result, dict):
        return []

    snapshot = load_ones_summary_snapshot(ones_result)
    if isinstance(snapshot, dict) and snapshot.get("status") == "ready":
        return []

    paths = (ones_result.get("paths") or {}) if isinstance(ones_result, dict) else {}
    messages_json_path = str(paths.get("messages_json") or "").strip()
    if not messages_json_path:
        return []

    try:
        payload = json.loads(Path(messages_json_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return []

    image_candidates: list[str] = []
    for item in payload.get("description_images", []) or []:
        path = str(item.get("path") or "").strip()
        if path:
            image_candidates.append(path)

    for item in payload.get("attachment_downloads", []) or []:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        mime_type = str(item.get("mime") or "").strip() or (mimetypes.guess_type(path)[0] or "")
        if mime_type.startswith("image/"):
            image_candidates.append(path)

    result: list[str] = []
    seen: set[str] = set()
    for raw_path in image_candidates:
        path = Path(raw_path)
        if not path.exists():
            continue
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
        if len(result) >= limit:
            break
    return result


def ones_cli_script_path() -> Path:
    local_script = settings.project_root / ".claude" / "skills" / "ones" / "scripts" / "ones_cli.py"
    if local_script.exists():
        return local_script
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / "ones" / "scripts" / "ones_cli.py"


async def fetch_ones_task_artifacts(msg: dict) -> dict[str, Any] | None:
    task_url = extract_ones_link(msg)
    if not task_url:
        return None

    try:
        from core.ones_routing import extract_ones_task_link
    except Exception:
        return None

    extracted = extract_ones_task_link(task_url)
    if not extracted:
        return None
    _, task_ref, _ = extracted

    existing = find_existing_ones_task_artifacts(task_ref)
    if existing:
        return existing

    script_path = ones_cli_script_path()
    if not script_path.exists():
        logger.warning("ONES intake: ONES CLI not found at {}", script_path)
        return None

    def _run_fetch() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script_path), "fetch-task", task_url],
            cwd=str(settings.project_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(_run_fetch)
    except Exception as e:
        logger.warning("ONES intake: fetch-task failed for {}: {}", task_ref, e)
        return None

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        logger.warning(
            "ONES intake: fetch-task non-zero for {}: rc={} stdout={} stderr={}",
            task_ref,
            completed.returncode,
            _truncate_text(stdout, limit=400),
            _truncate_text(stderr, limit=400),
        )
        return None

    try:
        payload = json.loads(completed.stdout or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("ONES intake: fetch-task output not valid JSON for {}", task_ref)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        logger.warning("ONES intake: fetch-task reported failure for {}: {}", task_ref, payload.get("error"))
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def build_ones_artifact_context(ones_result: dict[str, Any] | None) -> str:
    if not ones_result:
        return ""
    task = ones_result.get("task") or {}
    project = ones_result.get("project") or {}
    counts = ones_result.get("counts") or {}
    paths = ones_result.get("paths") or {}
    downloaded = collect_ones_downloaded_files(ones_result)
    summary_snapshot = load_ones_summary_snapshot(ones_result) or {}
    payload = {
        "task": {
            "number": task.get("number"),
            "uuid": task.get("uuid"),
            "summary": task.get("summary"),
            "status_name": task.get("status_name"),
            "issue_type_name": task.get("issue_type_name"),
            "url": task.get("url"),
        },
        "project": {
            "display_name": project.get("display_name"),
            "business_project_name": project.get("business_project_name"),
            "confidence": project.get("confidence"),
        },
        "counts": counts,
        "paths": {
            "task_dir": paths.get("task_dir", ""),
            "task_json": paths.get("task_json", ""),
            "messages_json": paths.get("messages_json", ""),
            "report_md": paths.get("report_md", ""),
            "summary_snapshot_json": paths.get("summary_snapshot_json", ""),
        },
        "summary_snapshot": summary_snapshot,
        "downloaded_files": downloaded[:12],
    }
    return "\n\n[ONES 下载产物]\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _stringify_field_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    if value is None:
        return ""
    return str(value)


def collect_ones_text_fragments(ones_result: dict[str, Any] | None, msg: dict | None = None) -> list[str]:
    fragments: list[str] = []
    if msg:
        content = str(msg.get("content") or "").strip()
        if content:
            fragments.append(content)

    if not isinstance(ones_result, dict):
        return fragments

    snapshot = load_ones_summary_snapshot(ones_result)
    if isinstance(snapshot, dict):
        summary_text = str(snapshot.get("summary_text") or "").strip()
        if summary_text:
            fragments.append(summary_text)
        problem_time = str(snapshot.get("problem_time") or "").strip()
        if problem_time:
            fragments.append(f"问题发生时间: {problem_time}")
        version_hint = str(snapshot.get("version_normalized") or snapshot.get("version_hint") or "").strip()
        if version_hint:
            fragments.append(f"版本线索: {version_hint}")
        for item in snapshot.get("version_fields") or []:
            rendered = str(item).strip()
            if rendered:
                fragments.append(f"字段版本: {rendered}")
        for item in snapshot.get("version_from_images") or []:
            rendered = str(item).strip()
            if rendered:
                fragments.append(f"图片版本: {rendered}")
        for item in snapshot.get("business_identifiers") or []:
            rendered = str(item).strip()
            if rendered:
                fragments.append(rendered)
        for item in snapshot.get("observations") or []:
            rendered = str(item).strip()
            if rendered:
                fragments.append(rendered)
        for item in snapshot.get("image_findings") or []:
            rendered = str(item).strip()
            if rendered:
                fragments.append(rendered)

    task = ones_result.get("task") or {}
    for key in ("summary", "description_local", "description", "desc_local", "desc"):
        value = str(task.get(key) or "").strip()
        if value:
            fragments.append(value)

    named_fields = ones_result.get("named_fields") or {}
    if isinstance(named_fields, dict):
        for key, value in named_fields.items():
            rendered = _stringify_field_value(value).strip()
            if rendered:
                fragments.append(f"{key}: {rendered}")

    project = ones_result.get("project") or {}
    for key in ("display_name", "business_project_name", "ones_project_name"):
        value = str(project.get(key) or "").strip()
        if value:
            fragments.append(value)
    return fragments


def _contains_problem_time(text: str) -> bool:
    lowered = text.lower()
    if any(keyword in text for keyword in _INCIDENT_TIME_KEYWORDS):
        return True
    patterns = (
        r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:[日号\sT]+)?\d{1,2}:\d{2}(?::\d{2})?",
        r"\d{1,2}[-/.月]\d{1,2}(?:[日号\sT]+)?\d{1,2}:\d{2}(?::\d{2})?",
    )
    return any(re.search(pattern, text) for pattern in patterns) or "timezone" in lowered or "时区" in text


def _extract_problem_time_value(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    patterns = (
        r"(20\d{2}年\d{1,2}月\d{1,2}[日号]?(?:上午|下午|晚上|中午)?\d{1,2}:\d{2}(?::\d{2})?(?:左右)?)",
        r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]+|\s*)(?:上午|下午|晚上|中午)?\d{1,2}:\d{2}(?::\d{2})?(?:左右)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return match.group(1).strip()
    return ""


def _extract_version_hints_from_text(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    patterns = (
        r"\b(?:RIOT|Riot|FMS|RIOT2\.0|RIOT3\.0)[\s:/-]*([0-9]+(?:\.[0-9A-Za-z_-]+){0,5})\b",
        r"\b([0-9]+(?:\.[0-9A-Za-z_-]+){1,6})\b",
    )
    values: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, raw, flags=re.IGNORECASE):
            candidate = (match.group(1) or "").strip()
            if candidate and candidate not in values:
                values.append(candidate)
    return values


def _contains_business_identifier(text: str) -> bool:
    lowered = text.lower()
    if any(keyword in lowered for keyword in _IDENTIFIER_KEYWORDS):
        return True
    patterns = (
        r"\b(order|task|vehicle|device|request|trace)[-_ ]?[a-z0-9]{2,}\b",
        r"\d+号车",
        r"[A-Z]{2,}-\d{2,}",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _needs_config_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in _CONFIG_EVIDENCE_KEYWORDS)


def _looks_like_log_artifact(label: str, path: str) -> bool:
    merged = " ".join(part for part in (label, Path(path).name) if part).lower()
    if any(merged.endswith(suffix) for suffix in _LOG_ARTIFACT_SUFFIXES):
        return True
    return any(token in merged for token in ("log", "trace", "stack", "dump", "stderr", "stdout"))


def _looks_like_config_artifact(label: str, path: str) -> bool:
    merged = " ".join(part for part in (label, Path(path).name) if part).lower()
    return any(token in merged for token in ("config", "配置", "yaml", "yml", "properties", "toml", "ini", "env"))


def iter_analysis_workspace_files(analysis_dir: Path | None) -> list[Path]:
    if not analysis_dir:
        return []
    attachments_root = analysis_dir / "01-intake" / "attachments"
    if not attachments_root.exists():
        return []
    return [path for path in attachments_root.rglob("*") if path.is_file()]


def evaluate_ones_artifact_completeness(
    msg: dict,
    ones_result: dict[str, Any] | None,
    analysis_dir: Path | None = None,
) -> dict[str, Any]:
    fragments = collect_ones_text_fragments(ones_result, msg=msg)
    issue_fragments = collect_ones_text_fragments(ones_result, msg=None)
    issue_text = "\n".join(fragment for fragment in (issue_fragments or fragments) if fragment).strip()

    downloaded_files = collect_ones_downloaded_files(ones_result or {})
    workspace_files = iter_analysis_workspace_files(analysis_dir)
    file_records = downloaded_files + [
        {"label": path.name, "path": str(path), "uuid": ""}
        for path in workspace_files
    ]

    has_description = len(issue_text) >= 40
    has_problem_time = _contains_problem_time(issue_text)
    has_log_artifact = any(
        _looks_like_log_artifact(str(item.get("label") or ""), str(item.get("path") or ""))
        for item in file_records
    ) or any(token in issue_text.lower() for token in ("exception", "stack trace", "stacktrace", "报错", "错误码"))
    has_identifier = _contains_business_identifier(issue_text)
    needs_config = _needs_config_evidence(issue_text)
    has_config_evidence = any(
        _looks_like_config_artifact(str(item.get("label") or ""), str(item.get("path") or ""))
        for item in file_records
    ) or any(token in issue_text.lower() for token in ("server.ssl", "jetty", "nginx", "配置", "参数"))

    requirements = [
        {"key": "description", "label": "问题描述", "present": has_description},
        {"key": "problem_time", "label": "问题发生时间", "present": has_problem_time},
        {"key": "related_logs", "label": "相关日志/异常堆栈", "present": has_log_artifact},
        {"key": "business_identifier", "label": "订单/车辆/设备等关键信息", "present": has_identifier},
    ]
    if needs_config:
        requirements.append({"key": "config_evidence", "label": "相关配置截图/配置片段", "present": has_config_evidence})

    missing_items = [item["label"] for item in requirements if not item["present"]]
    status = "complete" if not missing_items else "partial"

    evidence_sources: list[str] = []
    if ones_result:
        evidence_sources.append("ONES 工单描述")
    if downloaded_files:
        evidence_sources.append(f"ONES 附件 {len(downloaded_files)} 个")
    if workspace_files:
        evidence_sources.append(f"会话附件 {len(workspace_files)} 个")

    notes = []
    if missing_items:
        notes.append("当前证据还不能稳定闭合 ONES 问题的证据链。")
    notes.append("现场证据只认当前 ONES 工单和当前会话内提供的材料，本地历史日志只能作为实现参考。")

    return {
        "status": status,
        "requirements": requirements,
        "missing_items": missing_items,
        "needs_config_evidence": needs_config,
        "problem_time_detected": has_problem_time,
        "has_related_logs": has_log_artifact,
        "has_business_identifier": has_identifier,
        "evidence_sources": evidence_sources,
        "file_records": file_records[:20],
        "notes": notes,
    }


def _build_ones_summary_prompt(ones_result: dict[str, Any], msg: dict | None = None) -> str:
    task = (ones_result or {}).get("task") or {}
    named_fields = (ones_result or {}).get("named_fields") or {}
    project = (ones_result or {}).get("project") or {}
    lines = [
        "请先阅读正文，再参考附图，输出一个纯 JSON 对象，提取 ONES 工单的关键摘要信息。",
        "不要调用任何工具，不要输出代码块，不要解释，只输出 JSON。",
        "JSON 字段固定为：summary_text, problem_time, problem_time_confidence, version_text, version_fields, version_from_images, version_normalized, version_evidence, business_identifiers, observations, image_findings, missing_items。",
        "",
        f"标题: {task.get('summary') or ''}",
        f"描述正文: {task.get('description_local') or task.get('description') or task.get('desc_local') or task.get('desc') or ''}",
        f"业务项目: {project.get('display_name') or ''}",
    ]
    if isinstance(named_fields, dict) and named_fields:
        lines.append(f"字段信息: {json.dumps(named_fields, ensure_ascii=False)}")
    if msg:
        content = str(msg.get("content") or "").strip()
        if content:
            lines.append(f"用户消息: {content}")
    lines.append("")
    lines.append("要求：")
    lines.append("1. summary_text 用 1-4 句中文总结问题、现象、已知规律和已做排查。")
    lines.append("2. problem_time 尽量抽取原文里的明确时间。抽不到就填空字符串。")
    lines.append("3. version_text 填正文里直接能看到的版本描述；version_fields 填字段里能看到的版本描述数组；version_from_images 填从图片补到的版本描述数组。")
    lines.append("4. version_normalized 只保留一个后续最适合消费的版本值。")
    lines.append("5. version_evidence 说明版本最终来自 text / fields / images 的哪些来源。")
    lines.append("6. business_identifiers 放订单号/车辆号/设备号/站点编号等关键信息。")
    lines.append("7. observations 放关键观察结论。")
    lines.append("8. image_findings 只写从图片里额外补到的关键信息。")
    lines.append("9. missing_items 只保留真正还缺的关键证据。")
    return "\n".join(lines)


def merge_ones_summary_snapshot(
    *,
    llm_summary: dict[str, Any] | None,
    ones_result: dict[str, Any],
    msg: dict | None = None,
) -> dict[str, Any]:
    task = (ones_result or {}).get("task") or {}
    summary_text = str((llm_summary or {}).get("summary_text") or "").strip()
    problem_time = str((llm_summary or {}).get("problem_time") or "").strip() or _extract_problem_time_value(
        str(task.get("description_local") or task.get("description") or "")
    )
    version_text = str((llm_summary or {}).get("version_text") or "").strip()
    version_fields = [
        str(item).strip()
        for item in ((llm_summary or {}).get("version_fields") or [])
        if str(item).strip()
    ]
    version_from_images = [
        str(item).strip()
        for item in ((llm_summary or {}).get("version_from_images") or [])
        if str(item).strip()
    ]
    version_evidence = [
        str(item).strip()
        for item in ((llm_summary or {}).get("version_evidence") or [])
        if str(item).strip()
    ]

    named_fields = (ones_result or {}).get("named_fields") or {}
    if isinstance(named_fields, dict):
        for key in ("FMS/RIoT版本", "FMS/RIOT版本", "RIOT版本", "FMS版本", "SROS版本", "SRC/SRTOS版本"):
            value = str(named_fields.get(key) or "").strip()
            if value and value not in {"/", "无", "\\", "\\ "} and value not in version_fields:
                version_fields.append(value)

    if not version_text:
        text_candidates = _extract_version_hints_from_text(
            str(task.get("description_local") or task.get("description") or "")
        )
        if text_candidates:
            version_text = text_candidates[0]

    version_normalized = str((llm_summary or {}).get("version_normalized") or "").strip()
    if not version_normalized:
        for candidate in [version_text, *version_fields, *version_from_images]:
            if candidate and candidate not in {"/", "无", "\\"}:
                version_normalized = candidate
                break

    business_identifiers = [str(item).strip() for item in ((llm_summary or {}).get("business_identifiers") or []) if str(item).strip()]
    observations = [str(item).strip() for item in ((llm_summary or {}).get("observations") or []) if str(item).strip()]
    image_findings = [str(item).strip() for item in ((llm_summary or {}).get("image_findings") or []) if str(item).strip()]
    missing_items = [str(item).strip() for item in ((llm_summary or {}).get("missing_items") or []) if str(item).strip()]

    if not summary_text:
        summary_text = str(task.get("description_local") or task.get("description") or "").strip()
    if problem_time and "问题发生时间" not in summary_text:
        observations = [f"问题发生时间: {problem_time}", *observations]
    if version_normalized and not any("版本" in item for item in observations):
        observations = [f"版本线索: {version_normalized}", *observations]
    if version_text and "text" not in version_evidence:
        version_evidence.append("text")
    if version_fields and "fields" not in version_evidence:
        version_evidence.append("fields")
    if version_from_images and "images" not in version_evidence:
        version_evidence.append("images")

    downloaded_files = collect_ones_downloaded_files(ones_result or {})
    snapshot = {
        "status": "ready",
        "summary_text": summary_text,
        "problem_time": problem_time,
        "problem_time_confidence": str((llm_summary or {}).get("problem_time_confidence") or ("high" if problem_time else "low")).strip(),
        "version_text": version_text,
        "version_fields": version_fields,
        "version_from_images": version_from_images,
        "version_normalized": version_normalized,
        "version_evidence": version_evidence,
        "version_hint": version_normalized,
        "business_identifiers": business_identifiers,
        "observations": observations,
        "image_findings": image_findings,
        "missing_items": missing_items,
        "downloaded_files": downloaded_files,
        "source": {
            "text_first": True,
            "images_consumed": bool(collect_ones_multimodal_image_paths(ones_result)),
            "runtime": "",
        },
    }
    return snapshot


async def extract_ones_summary_snapshot(
    *,
    msg: dict,
    ones_result: dict[str, Any],
    runtime: str,
    run_agent,
    build_agent_input,
    collect_ones_multimodal_image_paths,
) -> dict[str, Any] | None:
    prompt = _build_ones_summary_prompt(ones_result, msg=msg)
    image_paths = collect_ones_multimodal_image_paths(ones_result)

    if runtime == "claude":
        llm_input = build_agent_input(
            {"content": "", "media_info_json": json.dumps({}, ensure_ascii=False)},
            session=None,
            runtime="claude",
            prompt_text=prompt,
            ones_result=ones_result,
        )
        result = await run_agent(
            prompt=llm_input,
            system_prompt="你是 ONES 工单摘要提取器。只输出纯 JSON 对象，不要解释，不要调用工具。",
            max_turns=6,
            runtime="claude",
        )
    else:
        result = await run_agent(
            prompt=prompt,
            system_prompt="你是 ONES 工单摘要提取器。只输出纯 JSON 对象，不要解释，不要调用工具。",
            max_turns=6,
            runtime=runtime,
            image_paths=image_paths,
        )

    parsed = _extract_json_object(str(result.get("text") or ""))
    if not isinstance(parsed, dict):
        return None
    snapshot = merge_ones_summary_snapshot(llm_summary=parsed, ones_result=ones_result, msg=msg)
    snapshot["source"]["runtime"] = runtime
    return snapshot


def persist_ones_summary_snapshot(ones_result: dict[str, Any], snapshot: dict[str, Any]) -> None:
    snapshot_path = ones_summary_snapshot_path(ones_result)
    if not snapshot_path:
        return
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    task_json_path = Path(str(((ones_result.get("paths") or {}).get("task_json")) or "").strip()) if isinstance(ones_result, dict) else None
    if task_json_path and task_json_path.exists():
        try:
            payload = json.loads(task_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            payload["summary_snapshot"] = snapshot
            payload.setdefault("paths", {})["summary_snapshot_json"] = str(snapshot_path)
            task_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ones_result["summary_snapshot"] = snapshot
    ones_result.setdefault("paths", {})["summary_snapshot_json"] = str(snapshot_path)


async def ensure_ones_summary_snapshot(
    *,
    msg: dict,
    ones_result: dict[str, Any] | None,
    runtime: str,
    run_agent,
    build_agent_input,
    collect_ones_multimodal_image_paths,
) -> dict[str, Any] | None:
    if not isinstance(ones_result, dict):
        return None
    existing = load_ones_summary_snapshot(ones_result)
    if isinstance(existing, dict) and existing.get("status") == "ready":
        return existing
    snapshot = await extract_ones_summary_snapshot(
        msg=msg,
        ones_result=ones_result,
        runtime=runtime,
        run_agent=run_agent,
        build_agent_input=build_agent_input,
        collect_ones_multimodal_image_paths=collect_ones_multimodal_image_paths,
    )
    if snapshot:
        persist_ones_summary_snapshot(ones_result, snapshot)
    return snapshot


def _ones_requirement_row(item: str) -> tuple[str, str]:
    mapping = {
        "问题描述": ("避免只靠标题或猜测分析", "ONES 现象描述、复现步骤、现场表现"),
        "问题发生时间": ("需要校准日志时间窗和时区", "精确到分钟的故障时间，最好带时区"),
        "相关日志/异常堆栈": ("证据链不能只靠本地历史日志", "原始日志包、故障时间窗日志、异常堆栈"),
        "订单/车辆/设备等关键信息": ("需要把现象和业务对象绑定", "订单号、任务号、车辆号、设备号、序列号"),
        "相关配置截图/配置片段": ("配置类问题必须有配置证据", "配置截图、配置文件片段、环境参数"),
    }
    return mapping.get(item, ("需要补齐关键证据", "请提供能直接支撑结论的原始材料"))


def build_ones_missing_info_reply(check: dict[str, Any], project_name: str = "") -> dict[str, Any]:
    received_lines = check.get("evidence_sources") or ["仅收到 ONES 链接或简要描述"]
    fallback_lines = [
        "这类 ONES 问题要先补齐最小证据链，再开始分析，避免把本地历史日志误当成现场证据。",
    ]
    if project_name:
        fallback_lines.insert(0, f"已识别项目为 {project_name}。")

    sections = [
        {"title": "已收到", "content": "\n".join(f"- {line}" for line in received_lines)},
    ]

    missing_items = check.get("missing_items") or []
    if missing_items:
        rows = []
        missing_lines = []
        for item in missing_items:
            why, accepted = _ones_requirement_row(item)
            rows.append({"item": item, "why": why, "accepted": accepted})
            missing_lines.append(f"- {item}：{accepted}")
        sections.append({"title": "请优先补充", "content": "\n".join(missing_lines)})
    else:
        rows = []

    note_lines = [f"- {line}" for line in (check.get("notes") or []) if str(line).strip()]
    if note_lines:
        sections.append({"title": "说明", "content": "\n".join(note_lines)})
        fallback_lines.append("说明：")
        fallback_lines.extend(line.removeprefix("- ") for line in note_lines)

    if missing_items:
        fallback_lines.append("请优先补充：")
        fallback_lines.extend(line.removeprefix("- ") for line in missing_lines)

    return {
        "format": "rich",
        "card_variant": "supplement",
        "title": "补充排障材料",
        "summary": f"已识别项目为 {project_name}。这类 ONES 问题要先补齐最小证据链，再开始分析。" if project_name else "这类 ONES 问题要先补齐最小证据链，再开始分析。",
        "sections": sections,
        "table": {
            "columns": [
                {"key": "item", "label": "需要补充", "type": "text"},
                {"key": "why", "label": "为什么需要", "type": "text"},
                {"key": "accepted", "label": "可接受材料", "type": "text"},
            ],
            "rows": rows,
        },
        "fallback_text": "\n".join(fallback_lines),
    }


def build_ones_evidence_guard_context(check: dict[str, Any] | None) -> str:
    if not check:
        return ""
    payload = {
        "status": check.get("status"),
        "requirements": check.get("requirements", []),
        "missing_items": check.get("missing_items", []),
        "evidence_sources": check.get("evidence_sources", []),
        "rules": [
            "现场证据只能来自当前 ONES 工单、当前会话补料和仓库内能确认的代码/配置事实。",
            "不要把本机或仓库里其他无关历史日志当成现场证据；若引用，只能标注为实现参考。",
            "如果缺失的事实会改变结论，先要求最小补料，不要直接下根因。",
        ],
    }
    return "\n\n[ONES 证据约束]\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def persist_ones_context_to_state(
    triage_dir: Path,
    *,
    ones_result: dict[str, Any] | None,
    check: dict[str, Any] | None,
    project_name: str = "",
) -> None:
    try:
        state_path = triage_dir / "00-state.json"
        if not state_path.exists():
            return
        state = json.loads(state_path.read_text(encoding="utf-8"))
        paths = (ones_result or {}).get("paths") or {}
        task = (ones_result or {}).get("task") or {}
        state["ones_context"] = {
            "task_ref": str(task.get("uuid") or task.get("number") or "").strip(),
            "task_json": str(paths.get("task_json") or ""),
            "messages_json": str(paths.get("messages_json") or ""),
            "report_md": str(paths.get("report_md") or ""),
            "summary_snapshot_json": str(paths.get("summary_snapshot_json") or ""),
            "missing_items": (check or {}).get("missing_items", []),
            "evidence_sources": (check or {}).get("evidence_sources", []),
        }
        state["project"] = project_name or state.get("project", "")
        if project_name:
            try:
                from core.projects import resolve_project_runtime_context

                runtime_context = resolve_project_runtime_context(project_name, ones_result=ones_result)
                if runtime_context:
                    state["project_runtime"] = runtime_context.to_payload()
            except Exception as e:
                logger.warning("ONES intake: failed to persist project runtime context for {}: {}", project_name, e)
        state["updated_at"] = datetime.now().isoformat()
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("ONES intake: failed to persist ONES context: {}", e)


def load_ones_artifacts_from_state(triage_dir: Path | None) -> dict[str, Any] | None:
    if not triage_dir:
        return None
    state_path = triage_dir / "00-state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    ones_context = state.get("ones_context") or {}
    task_json = Path(str(ones_context.get("task_json") or "").strip()) if ones_context.get("task_json") else None
    if task_json and task_json.exists():
        try:
            payload = json.loads(task_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            return payload
    task_ref = str(ones_context.get("task_ref") or "").strip()
    if task_ref:
        return find_existing_ones_task_artifacts(task_ref)
    return None
