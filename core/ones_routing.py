"""ONES task routing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from typing import Any

import httpx
from loguru import logger

from core.config import settings


ONES_TASK_RE = re.compile(r"https?://[^\s]+/project/#/team/(?P<team>[^/\s]+)/task/(?P<ref>[^?\s#]+)")
PROJECT_TEXT_FIELDS = (
    "项目名称",
    "客户项目",
    "所属项目名称",
)
VERSION_FIELDS = (
    "FMS/RIoT版本",
    "FMS/RIOT版本",
    "FMS版本",
    "RIOT版本",
)
KEY_FIELDS = (
    "项目名称",
    "项目编号",
    "FMS/RIoT版本",
    "SROS版本",
    "SRC/SRTOS版本",
    "一级问题根因分类",
    "产品名称-底盘/软件",
    "现场临时处理方案",
    "进展更新",
)
PROJECT_ALIASES = {
    "allspark": ("allspark", "riot3", "riot 3", "riot3.0", "riot 3.0", "riot3调度系统"),
    "riot-standalone": ("riot-standalone", "riot2", "riot 2", "riot2.0", "riot 2.0", "单体版"),
    "fms-java": ("fms-java", "riot1", "riot 1", "riot1.0", "riot 1.0", "fms", "车队管理系统"),
    "allsparkbox": ("allsparkbox", "部署包", "安装包"),
}
PROJECT_DOMAIN_KEYWORDS = {
    "allspark": (
        "reservation",
        "mapf",
        "mrs",
        "电梯",
        "风淋门",
    ),
    "riot-standalone": (
        "brokerx",
        "fcs",
        "flyway",
        "fleet control",
        "单体版",
        "riot-standalone",
    ),
    "fms-java": (
        "车队管理",
        "fms_frontend",
        "api-base",
        "accessory",
        "dhcp",
        "rmi",
        "statistics",
        "extension",
    ),
    "allsparkbox": (
        "docker",
        "podman",
        "镜像",
        "部署",
        "安装",
        "supervisord",
        "makefile",
    ),
}
PROJECT_GENERIC_DOMAIN_KEYWORDS = {
    "allspark": (
        "调度",
        "自动充电",
        "充电任务",
        "充电桩",
        "停靠点",
        "导航",
        "快速充电",
    ),
    "riot-standalone": (
        "自动充电",
        "充电任务",
        "充电桩",
        "停靠点",
        "快速充电",
    ),
    "fms-java": (
        "自动充电",
        "充电任务",
        "充电桩",
        "停靠点",
        "快速充电",
        "电量阈值",
    ),
}
VERSION_MAJOR_TO_PROJECT = {
    2: "riot-standalone",
    3: "allspark",
    4: "fms-java",
}


@dataclass
class OnesRouteDecision:
    project_name: str
    confidence: str
    score: int
    reasons: list[str]
    task_ref: str
    task_url: str
    task_summary: str
    task_payload: dict[str, Any]

    def build_project_prompt(self, user_message: str) -> str:
        task = self.task_payload["task"]
        payload = {
            "ones_task_url": self.task_url,
            "ones_task_ref": self.task_ref,
            "title": task.get("summary"),
            "description": task.get("desc", ""),
            "issue_type": task.get("issue_type_name") or task.get("issue_type_uuid"),
            "status": task.get("status_name") or task.get("status_uuid"),
            "ones_project_name": self.task_payload.get("ones_project_name"),
            "business_project_name": self.task_payload.get("business_project_name"),
            "key_fields": self.task_payload.get("key_fields", {}),
            "route_reasons": self.reasons,
            "route_confidence": self.confidence,
        }
        return (
            f"用户直接发送了一个 ONES 工单链接，系统已将该请求预路由到项目 `{self.project_name}`。\n"
            "不要先要求用户重新解释项目，也不要先要求用户重新粘贴 ONES 链接。\n"
            "处理顺序必须是：先检查证据是否完整，再决定是否开始分析；如果缺少关键证据，就先要求最小补料，不要直接下结论。\n"
            "现场证据只能来自当前 ONES 工单、当前会话里用户提供的日志/截图/附件，以及你在仓库里能确认的代码/配置事实。\n"
            "不要把本机或仓库里其他无关历史日志当成现场证据；这类日志最多只能作为实现参考，并且必须明确区分。\n"
            "如果当前项目里有 ONES 相关 skill，可直接使用补全附件和评论。\n\n"
            f"原始用户消息:\n{user_message}\n\n"
            f"ONES 摘要:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "只有在证据完整时才开始分析。若仍缺少会改变结论的关键上下文，最后只问最少必要的问题。"
        )


def extract_ones_task_link(text: str) -> tuple[str, str, str] | None:
    match = ONES_TASK_RE.search(text or "")
    if not match:
        return None
    return match.group("team"), match.group("ref"), match.group(0)


@lru_cache(maxsize=1)
def _read_env_fallback() -> dict[str, str]:
    values: dict[str, str] = {}

    paths: list[Path] = []
    explicit = os.getenv("ONES_ENV_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    for name in (".env", ".ones.env", "ones.env"):
        paths.append(settings.project_root / name)

    seen: set[Path] = set()
    for env_path in paths:
        try:
            resolved = env_path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        for raw_line in resolved.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values.setdefault(key, value)
    return values


def _env(name: str) -> str:
    return os.getenv(name, "").strip() or _read_env_fallback().get(name, "").strip()


async def _ones_login(client: httpx.AsyncClient, host: str, email: str, password: str) -> dict[str, Any]:
    response = await client.post(
        f"{host}/api/project/auth/login",
        json={"email": email, "password": password},
    )
    response.raise_for_status()
    return response.json()


async def _ones_get(
    client: httpx.AsyncClient,
    host: str,
    team_uuid: str,
    endpoint: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await client.get(
        f"{host}/project/api/project/team/{team_uuid}/{endpoint.lstrip('/')}",
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


async def _ones_list_projects(
    client: httpx.AsyncClient,
    host: str,
    team_uuid: str,
    headers: dict[str, str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for endpoint in ("projects/recent_projects", "projects/my_project"):
        try:
            payload = await _ones_get(client, host, team_uuid, endpoint, headers)
        except Exception:
            continue
        for item in payload.get("projects", []):
            project_uuid = item.get("uuid")
            if project_uuid and project_uuid not in result:
                result[project_uuid] = str(item.get("name") or "")
    return result


def _named_fields(task: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    by_uuid = {field["uuid"]: field for field in fields}
    result: dict[str, Any] = {}
    for item in task.get("field_values", []):
        field = by_uuid.get(item.get("field_uuid"))
        if field:
            result[field.get("name", item["field_uuid"])] = _decode_field_value(item.get("value"), field)
    return result


def _field_option_map(field: dict[str, Any]) -> dict[str, str]:
    options = field.get("options") or []
    if not isinstance(options, list):
        return {}
    result: dict[str, str] = {}
    for option in options:
        uuid = str(option.get("uuid") or "").strip()
        value = str(option.get("value") or "").strip()
        if uuid and value:
            result[uuid] = value
    return result


def _decode_field_value(value: Any, field: dict[str, Any]) -> Any:
    option_map = _field_option_map(field)
    if not option_map:
        return value
    if isinstance(value, list):
        decoded = [option_map.get(str(item).strip(), item) for item in value]
        return decoded
    return option_map.get(str(value).strip(), value)


def _stringify_field_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _collect_version_project_hints(key_fields: dict[str, Any]) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for field_name in VERSION_FIELDS:
        rendered = _stringify_field_value(key_fields.get(field_name))
        major = _major_version(rendered)
        hinted_project = VERSION_MAJOR_TO_PROJECT.get(major)
        if not hinted_project:
            continue
        key = (field_name, rendered, hinted_project)
        if key in seen:
            continue
        seen.add(key)
        hints.append({
            "field": field_name,
            "value": rendered,
            "project": hinted_project,
        })
    return hints


def _major_version(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)", value.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _score_alias_hits(blob_lower: str) -> dict[str, tuple[int, list[str]]]:
    scored: dict[str, tuple[int, list[str]]] = {}
    for project_name, aliases in PROJECT_ALIASES.items():
        hits: list[str] = []
        for alias in aliases:
            alias_lower = alias.lower()
            if re.search(r"[a-z0-9]", alias_lower):
                pattern = rf"(?<![a-z0-9]){re.escape(alias_lower)}(?![a-z0-9])"
                if re.search(pattern, blob_lower):
                    hits.append(alias)
            elif alias_lower in blob_lower:
                hits.append(alias)
        if hits:
            scored[project_name] = (8, [f"命中文本别名: {', '.join(hits[:3])}"])
    return scored


def score_project_routes(
    *,
    user_message: str,
    task_summary: str,
    task_description: str,
    ones_project_name: str,
    business_project_name: str,
    key_fields: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    blob_parts = [
        user_message or "",
        task_summary or "",
        task_description or "",
        ones_project_name or "",
        business_project_name or "",
    ]
    blob = "\n".join(blob_parts)
    blob_lower = blob.lower()

    scores: dict[str, int] = {name: 0 for name in PROJECT_ALIASES}
    reasons: dict[str, list[str]] = {name: [] for name in PROJECT_ALIASES}

    for project_name, (score, reason_items) in _score_alias_hits(blob_lower).items():
        scores[project_name] += score
        reasons[project_name].extend(reason_items)

    for project_name, keywords in PROJECT_DOMAIN_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword.lower() in blob_lower]
        if hits:
            domain_score = min(len(hits) * 3, 6)
            scores[project_name] += domain_score
            reasons[project_name].append(f"命中特征领域关键词: {', '.join(hits[:4])}")

    for project_name, keywords in PROJECT_GENERIC_DOMAIN_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword.lower() in blob_lower]
        if hits:
            domain_score = min(len(hits), 2)
            scores[project_name] += domain_score
            reasons[project_name].append(f"命中通用领域关键词: {', '.join(hits[:4])}")

    for hint in _collect_version_project_hints(key_fields):
        weight = 6 if hint["field"] == "所属迭代" else 4
        scores[hint["project"]] += weight
        reasons[hint["project"]].append(
            f"{hint['field']} 命中 {hint['value']} -> {hint['project']}"
        )

    return {
        name: {"score": scores[name], "reasons": reasons[name]}
        for name in PROJECT_ALIASES
    }


def choose_project_route(
    *,
    user_message: str,
    task_summary: str,
    task_description: str,
    ones_project_name: str,
    business_project_name: str,
    key_fields: dict[str, Any],
) -> tuple[str | None, str, int, list[str]]:
    scored = score_project_routes(
        user_message=user_message,
        task_summary=task_summary,
        task_description=task_description,
        ones_project_name=ones_project_name,
        business_project_name=business_project_name,
        key_fields=key_fields,
    )
    ordered = sorted(scored.items(), key=lambda item: item[1]["score"], reverse=True)
    top_name, top_meta = ordered[0]
    second_score = ordered[1][1]["score"] if len(ordered) > 1 else 0
    top_score = top_meta["score"]
    margin = top_score - second_score
    version_hints = _collect_version_project_hints(key_fields)
    version_projects = {item["project"] for item in version_hints}

    confidence = "low"
    if top_score >= 6 and margin >= 2:
        confidence = "high"
    elif top_score >= 4 and margin >= 2:
        confidence = "medium"

    if len(version_projects) > 1 and (top_score < 14 or margin < 6):
        conflict_reason = "版本线索冲突: " + "; ".join(
            f"{item['field']}={item['value']} -> {item['project']}"
            for item in version_hints
        )
        reasons = [*top_meta["reasons"], conflict_reason]
        return None, "low", top_score, reasons

    if confidence == "low":
        return None, confidence, top_score, top_meta["reasons"]
    return top_name, confidence, top_score, top_meta["reasons"]


async def try_direct_project_route(user_message: str) -> OnesRouteDecision | None:
    extracted = extract_ones_task_link(user_message)
    if not extracted:
        return None

    team_uuid, task_ref, task_url = extracted
    host = _env("ONES_HOST").rstrip("/")
    email = _env("ONES_EMAIL")
    password = _env("ONES_PASSWORD")
    fallback_team = _env("ONES_TEAM_UUID")
    verify_ssl = _env("ONES_VERIFY_SSL").lower() == "true"

    if not host or not email or not password:
        logger.info("ONES direct routing skipped: missing ONES credentials")
        return None

    team_uuid = team_uuid or fallback_team
    if not team_uuid:
        logger.info("ONES direct routing skipped: missing team uuid")
        return None

    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:
        try:
            login_payload = await _ones_login(client, host, email, password)
            user = login_payload.get("user", {})
            headers = {
                "Ones-Auth-Token": user.get("token", ""),
                "Ones-User-Id": user.get("uuid", ""),
                "Referer": host,
                "Content-Type": "application/json",
            }
            task = await _ones_get(client, host, team_uuid, f"task/{task_ref}/info", headers)
            fields_payload = await _ones_get(client, host, team_uuid, "fields", headers)
            issue_types_payload = await _ones_get(client, host, team_uuid, "issue_types", headers)
            statuses_payload = await _ones_get(client, host, team_uuid, "task_statuses", headers)
            project_names = await _ones_list_projects(client, host, team_uuid, headers)
        except Exception as exc:
            logger.warning("ONES direct routing lookup failed: {}", exc)
            return None

    fields = fields_payload.get("fields", [])
    named = _named_fields(task, fields)
    issue_types = {item["uuid"]: item.get("name") for item in issue_types_payload.get("issue_types", [])}
    statuses = {item["uuid"]: item.get("name") for item in statuses_payload.get("task_statuses", [])}

    business_project_name = next(
        (str(named.get(field_name)).strip() for field_name in PROJECT_TEXT_FIELDS if named.get(field_name)),
        "",
    )
    ones_project_name = project_names.get(str(task.get("project_uuid") or ""), "")
    for field_name in PROJECT_TEXT_FIELDS:
        if field_name == "项目名称" and named.get(field_name):
            business_project_name = str(named[field_name]).strip()
            break
    if not business_project_name:
        business_project_name = title_project(task.get("summary")) or ""

    key_fields = {field_name: named.get(field_name) for field_name in KEY_FIELDS if named.get(field_name) not in (None, "", [])}
    project_name, confidence, score, reasons = choose_project_route(
        user_message=user_message,
        task_summary=str(task.get("summary") or ""),
        task_description=str(task.get("desc") or ""),
        ones_project_name=ones_project_name,
        business_project_name=business_project_name,
        key_fields=key_fields,
    )
    if not project_name:
        return None

    task_payload = {
        "task": {
            "uuid": task.get("uuid"),
            "number": task.get("number"),
            "summary": task.get("summary"),
            "desc": task.get("desc"),
            "status_uuid": task.get("status_uuid"),
            "status_name": statuses.get(task.get("status_uuid")),
            "issue_type_uuid": task.get("issue_type_uuid"),
            "issue_type_name": issue_types.get(task.get("issue_type_uuid")),
            "project_uuid": task.get("project_uuid"),
            "sprint_uuid": task.get("sprint_uuid"),
        },
        "ones_project_name": ones_project_name,
        "business_project_name": business_project_name,
        "key_fields": key_fields,
    }
    return OnesRouteDecision(
        project_name=project_name,
        confidence=confidence,
        score=score,
        reasons=reasons,
        task_ref=task_ref,
        task_url=task_url,
        task_summary=str(task.get("summary") or ""),
        task_payload=task_payload,
    )


def title_project(summary: str | None) -> str | None:
    if not summary:
        return None
    match = re.match(r"^【([^】]+)】", summary.strip())
    return match.group(1).strip() if match else None
