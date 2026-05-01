#!/usr/bin/env python3
import argparse
import html
from html.parser import HTMLParser
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


FIELDS = {"status": "field005", "project": "field006", "issue_type": "field007", "sprint": "field011"}
BUGLIKE = ("bug", "问题", "缺陷", "故障", "异常")
PROJECT_NAME_FIELDS = ("项目名称", "客户项目", "所属项目名称")
TASK_RE = re.compile(r"/team/(?P<team>[^/?#]+)(?:/[^?#]*)?/task/(?P<ref>[^/?#]+)")
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = re.compile(r'([A-Za-z_:][A-Za-z0-9_:\-]*)\s*=\s*"([^"]*)"')


class OnesError(RuntimeError):
    pass


def dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2)


def load_env(path):
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ.setdefault(key, value)


def load_envs(explicit):
    paths = []
    if explicit:
        paths.append(Path(explicit))
    if os.getenv("ONES_ENV_FILE"):
        paths.append(Path(os.getenv("ONES_ENV_FILE")))
    for name in (".env", ".ones.env", "ones.env"):
        paths.append(Path.cwd() / name)
    seen = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        load_env(resolved)


def iso_time(value):
    if value in (None, ""):
        return None
    if value > 10**14:
        value = value / 1_000_000
    elif value > 10**11:
        value = value / 1_000
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().isoformat()


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return value or "item"


def parse_task_ref(raw):
    raw = raw.strip()
    if raw.startswith("#"):
        return raw[1:], None
    match = TASK_RE.search(raw)
    if match:
        return match.group("ref"), match.group("team")
    return raw, None


def title_project(summary):
    match = re.match(r"^【([^】]+)】", (summary or "").strip())
    return match.group(1).strip() if match else None


class Client:
    def __init__(self, team_uuid=None):
        self.host = os.getenv("ONES_HOST", "").rstrip("/")
        self.email = os.getenv("ONES_EMAIL", "")
        self.password = os.getenv("ONES_PASSWORD", "")
        self.token = os.getenv("ONES_TOKEN", "")
        self.user_uuid = os.getenv("ONES_USER_UUID", "")
        self.team_uuid = team_uuid or os.getenv("ONES_TEAM_UUID", "")
        self.timeout = int(os.getenv("ONES_TIMEOUT", "30"))
        self.verify = os.getenv("ONES_VERIFY_SSL", "false").lower() == "true"
        if not self.host:
            raise OnesError("ONES_HOST is required.")
        self.session = requests.Session()
        self.session.verify = self.verify
        if not self.verify:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.base = f"{self.host}/project/api/project"
        if not (self.token and self.user_uuid):
            self.login()
        elif not self.team_uuid:
            raise OnesError("ONES_TEAM_UUID is required when using ONES_TOKEN/ONES_USER_UUID.")

    def login(self):
        if not (self.email and self.password):
            raise OnesError("Configure ONES_EMAIL and ONES_PASSWORD, or ONES_TOKEN and ONES_USER_UUID.")
        resp = self.session.post(
            f"{self.host}/api/project/auth/login",
            json={"email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise OnesError(f"ONES login failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self.token = data.get("user", {}).get("token", "")
        self.user_uuid = data.get("user", {}).get("uuid", "")
        if not self.team_uuid:
            teams = data.get("teams") or []
            self.team_uuid = teams[0].get("uuid", "") if teams else ""
        if not (self.token and self.user_uuid and self.team_uuid):
            raise OnesError("ONES login succeeded but auth data is incomplete.")

    def req(self, method, endpoint, data=None):
        url = f"{self.base}/team/{self.team_uuid}/{endpoint.lstrip('/')}"
        resp = self.session.request(
            method,
            url,
            json=data,
            timeout=self.timeout,
            headers={
                "Ones-Auth-Token": self.token,
                "Ones-User-Id": self.user_uuid,
                "Referer": self.host,
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            raise OnesError(f"ONES API failed: {resp.status_code} {resp.text}")
        return resp.json()

    def get_task(self, ref):
        return self.req("GET", f"task/{ref}/info")

    def get_messages(self, task_uuid):
        return self.req("GET", f"task/{task_uuid}/messages")

    def get_fields(self):
        return self.req("GET", "fields").get("fields", [])

    def get_issue_types(self):
        return self.req("GET", "issue_types").get("issue_types", [])

    def get_statuses(self):
        return self.req("GET", "task_statuses").get("task_statuses", [])

    def get_recent_projects(self):
        return self.req("GET", "projects/recent_projects").get("projects", [])

    def get_my_projects(self):
        return self.req("GET", "projects/my_project").get("projects", [])

    def get_sprints(self):
        return self.req("GET", "sprints/all").get("sprints", [])

    def peek(self, query, limit, include_subtasks):
        return self.req("POST", "filters/peek", {"query": query or None, "limit": limit, "offset": 0, "include_subtasks": include_subtasks})

    def tasks_info(self, ids):
        return self.req("POST", "tasks/info", {"ids": ids}).get("tasks", [])

    def download_attachment(self, uuid):
        meta = self.req("GET", f"res/attachment/{uuid}")
        url = meta.get("url")
        if not url:
            raise OnesError(f"Attachment {uuid} has no download url.")
        resp = self.session.get(url, timeout=self.timeout, headers={"Ones-Auth-Token": self.token, "Ones-User-Id": self.user_uuid}, verify=self.verify)
        if resp.status_code != 200:
            raise OnesError(f"Attachment download failed: {resp.status_code}")
        return resp.content


def field_map(fields):
    return {field["uuid"]: field for field in fields}


def named_fields(task, fields_by_uuid):
    result = {}
    for item in task.get("field_values", []):
        field = fields_by_uuid.get(item.get("field_uuid"))
        if field:
            result[field.get("name", item["field_uuid"])] = item.get("value")
    return result


def project_info(client, task, fields_by_uuid):
    projects = {}
    for item in client.get_recent_projects() + client.get_my_projects():
        projects[item.get("uuid")] = item.get("name")
    values = named_fields(task, fields_by_uuid)
    business = next((values.get(name) for name in PROJECT_NAME_FIELDS if values.get(name)), None)
    title = title_project(task.get("summary"))
    ones_name = projects.get(task.get("project_uuid"))
    display = business or title or ones_name
    confidence = "low"
    if display:
        confidence = "medium"
    if business and title and business == title:
        confidence = "high"
    elif business and ones_name:
        confidence = "high"
    elif title and ones_name:
        confidence = "high"
    return {
        "ones_project_uuid": task.get("project_uuid"),
        "ones_project_name": ones_name,
        "business_project_name": business,
        "title_project_name": title,
        "display_name": display,
        "confidence": confidence,
    }


def attachment_ext(mime, name=None):
    if name and Path(name).suffix:
        return Path(name).suffix
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }.get((mime or "").lower(), ".bin")


def parse_html_attrs(tag):
    attrs = {}
    for key, value in ATTR_RE.findall(tag or ""):
        attrs[key] = html.unescape(value)
    return attrs


def infer_mime(mime=None, src=None, name=None):
    candidate = (mime or "").strip().lower()
    if candidate:
        return candidate
    src = (src or "").strip()
    if src.lower().startswith("data:"):
        header = src[5:].split(",", 1)[0]
        return header.split(";", 1)[0].strip().lower()
    if name:
        suffix = Path(name).suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".pdf": "application/pdf",
        }.get(suffix, "")
    return ""


def extract_desc_image_refs(desc_rich):
    refs = []
    for index, tag in enumerate(IMG_TAG_RE.findall(desc_rich or ""), start=1):
        attrs = parse_html_attrs(tag)
        uuid = (attrs.get("data-uuid") or "").strip()
        if not uuid:
            continue
        mime = infer_mime(attrs.get("data-mime"), attrs.get("src"))
        refs.append({
            "index": index,
            "uuid": uuid,
            "mime": mime,
            "src": attrs.get("src", ""),
            "ref_id": attrs.get("data-ref-id", ""),
        })
    return refs


class DescLocalParser(HTMLParser):
    def __init__(self, image_map):
        super().__init__(convert_charrefs=True)
        self.image_map = image_map
        self.parts = []
        self._pending_break = False

    def _append_text(self, text):
        value = (text or "").replace("\xa0", " ")
        if not value.strip():
            return
        if self._pending_break and self.parts:
            self.parts.append("\n")
        self._pending_break = False
        self.parts.append(value)

    def _append_break(self):
        if self.parts and self.parts[-1] != "\n":
            self._pending_break = True

    def handle_starttag(self, tag, attrs):
        attrs_map = dict(attrs)
        if tag in {"p", "div", "figure", "figcaption", "br"}:
            self._append_break()
            return
        if tag == "img":
            self._append_break()
            uuid = (attrs_map.get("data-uuid") or "").strip()
            item = self.image_map.get(uuid, {})
            label = item.get("label") or f"image_{uuid or 'unknown'}"
            marker = f"[image:{label}"
            if item.get("path"):
                marker += f' path="{item["path"]}"'
            mime = infer_mime(item.get("mime"), attrs_map.get("src"), item.get("path"))
            if mime:
                marker += f' mime="{mime}"'
            marker += "]"
            self._append_text(marker)
            self._append_break()

    def handle_endtag(self, tag):
        if tag in {"p", "div", "figure", "figcaption"}:
            self._append_break()

    def handle_data(self, data):
        self._append_text(data)

    def render(self):
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def build_desc_local(desc, desc_rich, desc_files):
    image_map = {item.get("uuid"): item for item in desc_files if item.get("uuid")}
    parser = DescLocalParser(image_map)
    parser.feed(desc_rich or "")
    rendered = parser.render()
    if rendered:
        return rendered
    return (desc or "").strip()


def task_url(task, team_uuid):
    host = os.getenv("ONES_HOST", "").rstrip("/")
    ref = task.get("uuid") or task.get("number")
    return f"{host}/project/#/team/{team_uuid}/task/{ref}" if host and team_uuid and ref else ""


def write_report(path, task, project, issue_name, status_name, comments, desc_files, att_files, desc_local):
    lines = [
        f"# ONES Task #{task.get('number')}",
        "",
        f"- 标题: {task.get('summary')}",
        f"- UUID: {task.get('uuid')}",
        f"- 链接: {task_url(task, os.getenv('ONES_TEAM_UUID', ''))}",
        f"- 工作项类型: {issue_name or task.get('issue_type_uuid')}",
        f"- 状态: {status_name or task.get('status_uuid')}",
        f"- ONES 项目: {project.get('ones_project_name') or '未知'}",
        f"- 业务项目: {project.get('display_name') or '未知'}",
        "",
        "## 描述",
        "",
        task.get("desc") or "(空)",
        "",
        "## 本地化描述",
        "",
        desc_local or "(空)",
        "",
        "## 附件",
        "",
    ]
    for item in desc_files + att_files:
        if item.get("path"):
            lines.append(f"- {item.get('label')}: {item['path']}")
        else:
            lines.append(f"- {item.get('label')}: 下载失败 - {item.get('error')}")
    text_comments = [c for c in comments if c.get("text")]
    if text_comments:
        lines.extend(["", "## 评论", ""])
        for item in text_comments:
            lines.append(f"- [{item.get('send_time_iso') or '未知时间'}] {item.get('from') or '未知'}: {item['text']}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def fetch_task(args):
    ref, team = parse_task_ref(args.reference)
    client = Client(team or args.team_uuid)
    if team:
        client.team_uuid = team
    task = client.get_task(ref)
    fields = client.get_fields()
    fields_by_uuid = field_map(fields)
    issues = {item["uuid"]: item for item in client.get_issue_types()}
    statuses = {item["uuid"]: item for item in client.get_statuses()}
    project = project_info(client, task, fields_by_uuid)
    base = Path(args.output_base_dir).expanduser().resolve()
    task_dir = base / safe_name(f"{task.get('number')}_{task.get('uuid')}")
    attach_dir = task_dir / "attachment"
    attach_dir.mkdir(parents=True, exist_ok=True)

    msg_payload = client.get_messages(task["uuid"])
    comments, attachments = [], []
    for msg in msg_payload.get("messages", []):
        item = {"uuid": msg.get("uuid"), "type": msg.get("type"), "from": msg.get("from"), "text": msg.get("text"), "send_time": msg.get("send_time"), "send_time_iso": iso_time(msg.get("send_time"))}
        if msg.get("resource"):
            res = msg["resource"]
            item["resource"] = {"uuid": res.get("uuid"), "name": res.get("name"), "mime": res.get("mime"), "size": res.get("size")}
            attachments.append(item["resource"])
        comments.append(item)

    desc_files = []
    for item in extract_desc_image_refs(task.get("desc_rich") or ""):
        uuid = item["uuid"]
        mime = item["mime"]
        index = item["index"]
        label = f"description_image_{index:02d}_{uuid}"
        try:
            content = client.download_attachment(uuid)
            resolved_mime = infer_mime(mime, item.get("src"))
            filename = f"{safe_name(label)}{attachment_ext(resolved_mime)}"
            path = attach_dir / filename
            path.write_bytes(content)
            desc_files.append({
                "label": label,
                "path": str(path),
                "uuid": uuid,
                "mime": resolved_mime,
                "src": item.get("src", ""),
            })
        except Exception as exc:
            desc_files.append({
                "label": label,
                "error": str(exc),
                "uuid": uuid,
                "mime": infer_mime(mime, item.get("src")),
                "src": item.get("src", ""),
            })

    att_files = []
    for index, item in enumerate(attachments, start=1):
        uuid = item.get("uuid")
        label = item.get("name") or f"attachment_{index:02d}_{uuid}"
        try:
            content = client.download_attachment(uuid)
            filename = f"attachment_{index:02d}_{safe_name(uuid)}{attachment_ext(item.get('mime'), item.get('name'))}"
            path = attach_dir / filename
            path.write_bytes(content)
            att_files.append({"label": label, "path": str(path), "uuid": uuid})
        except Exception as exc:
            att_files.append({"label": label, "error": str(exc), "uuid": uuid})

    desc_local = build_desc_local(task.get("desc"), task.get("desc_rich"), desc_files)

    result = {
        "task": {
            "uuid": task.get("uuid"),
            "number": task.get("number"),
            "summary": task.get("summary"),
            "description": task.get("desc"),
            "description_local": desc_local,
            "status_uuid": task.get("status_uuid"),
            "status_name": statuses.get(task.get("status_uuid"), {}).get("name"),
            "issue_type_uuid": task.get("issue_type_uuid"),
            "issue_type_name": issues.get(task.get("issue_type_uuid"), {}).get("name"),
            "project_uuid": task.get("project_uuid"),
            "sprint_uuid": task.get("sprint_uuid"),
            "assignee_uuid": task.get("assign"),
            "create_time_iso": iso_time(task.get("create_time")),
            "update_time_iso": iso_time(task.get("server_update_stamp")),
            "url": task_url(task, client.team_uuid),
        },
        "project": project,
        "counts": {
            "comments": len(comments),
            "attachments": len(attachments),
            "downloaded_description_images": len([x for x in desc_files if x.get("path")]),
            "downloaded_message_attachments": len([x for x in att_files if x.get("path")]),
        },
        "project_inference": {"needs_confirmation": project.get("confidence") == "low"},
        "named_fields": named_fields(task, fields_by_uuid),
        "paths": {
            "task_dir": str(task_dir),
            "task_json": str(task_dir / "task.json"),
            "task_raw_json": str(task_dir / "task.raw.json"),
            "messages_json": str(task_dir / "messages.json"),
            "report_md": str(task_dir / "report.md"),
            "desc_local_md": str(task_dir / "desc_local.md"),
            "summary_snapshot_json": str(task_dir / "summary_snapshot.json"),
        },
    }
    summary_snapshot = build_summary_snapshot_bound_to_download(
        result,
        desc_files=desc_files,
        att_files=att_files,
        mode=getattr(args, "summary_mode", "deterministic"),
    )
    result["summary_snapshot"] = summary_snapshot
    raw_task = dict(task)
    raw_task["desc_local"] = desc_local
    (task_dir / "task.raw.json").write_text(dump(raw_task), encoding="utf-8")
    (task_dir / "messages.json").write_text(dump({"comments": comments, "attachments": attachments, "description_images": desc_files, "attachment_downloads": att_files}), encoding="utf-8")
    (task_dir / "summary_snapshot.json").write_text(dump(summary_snapshot), encoding="utf-8")
    (task_dir / "task.json").write_text(dump(result), encoding="utf-8")
    (task_dir / "desc_local.md").write_text(desc_local.rstrip() + "\n", encoding="utf-8")
    write_report(task_dir / "report.md", raw_task, project, result["task"]["issue_type_name"], result["task"]["status_name"], comments, desc_files, att_files, desc_local)
    return result


def build_summary_snapshot(result, desc_files, att_files):
    task = result.get("task") or {}
    named = result.get("named_fields") or {}
    description = task.get("description_local") or task.get("description") or ""
    summary = task.get("summary") or ""
    text = "\n".join([str(summary), str(description)])

    problem_time = extract_problem_time(text)
    version_fields = []
    if isinstance(named, dict):
        for key in ("FMS/RIoT版本", "FMS/RIOT版本", "RIOT版本", "FMS版本", "SROS版本", "SRC/SRTOS版本"):
            value = str(named.get(key) or "").strip()
            if value and value not in {"/", "无", "\\", "\\ "} and value not in version_fields:
                version_fields.append(value)
    version_texts = extract_version_hints(text)
    version_normalized = first_non_empty([*version_texts, *version_fields])
    downloaded_files = [
        {
            "label": str(item.get("label") or "").strip() or Path(str(item.get("path") or "")).name,
            "path": str(item.get("path") or "").strip(),
            "uuid": str(item.get("uuid") or "").strip(),
        }
        for item in [*(desc_files or []), *(att_files or [])]
        if str(item.get("path") or "").strip()
    ]
    observations = []
    if task.get("status_name"):
        observations.append(f"ONES 状态: {task.get('status_name')}")
    if task.get("issue_type_name"):
        observations.append(f"ONES 类型: {task.get('issue_type_name')}")
    if problem_time:
        observations.append(f"问题发生时间: {problem_time}")
    if version_normalized:
        observations.append(f"版本线索: {version_normalized}")

    missing_items = []
    if not problem_time:
        missing_items.append("问题发生时间")
    if not downloaded_files:
        missing_items.append("现场附件或截图")

    return {
        "status": "ready",
        "summary_text": compact_summary(summary, description),
        "problem_time": problem_time,
        "problem_time_confidence": "high" if problem_time else "low",
        "version_text": version_texts[0] if version_texts else "",
        "version_fields": version_fields,
        "version_from_images": [],
        "version_normalized": version_normalized,
        "version_evidence": [item for item, enabled in (
            ("text", bool(version_texts)),
            ("fields", bool(version_fields)),
        ) if enabled],
        "version_hint": version_normalized,
        "business_identifiers": extract_business_identifiers(text),
        "observations": observations,
        "image_findings": [],
        "missing_items": missing_items,
        "downloaded_files": downloaded_files,
        "source": {
            "text_first": True,
            "images_available": bool(summary_image_paths(desc_files, att_files)),
            "images_consumed": False,
            "runtime": "ones_cli",
        },
    }


def build_summary_snapshot_bound_to_download(result, desc_files, att_files, mode="deterministic"):
    base_snapshot = build_summary_snapshot(result, desc_files=desc_files, att_files=att_files)
    selected_mode = str(mode or os.getenv("ONES_SUMMARY_MODE") or "deterministic").strip().lower()
    if selected_mode in {"none", "off", "false"}:
        base_snapshot["source"]["subagent_status"] = "disabled"
        return base_snapshot
    if selected_mode not in {"subagent", "agent", "llm"}:
        return base_snapshot

    try:
        agent_summary = asyncio_run_summary_subagent(result, desc_files=desc_files, att_files=att_files)
    except Exception as exc:
        base_snapshot["source"]["subagent"] = "ones-summary"
        base_snapshot["source"]["subagent_status"] = "fallback"
        base_snapshot["source"]["subagent_error"] = str(exc)[:300]
        base_snapshot["source"]["images_consumed"] = False
        return base_snapshot

    if not agent_summary:
        base_snapshot["source"]["subagent"] = "ones-summary"
        base_snapshot["source"]["subagent_status"] = "fallback"
        base_snapshot["source"]["images_consumed"] = False
        return base_snapshot
    return merge_subagent_summary_snapshot(base_snapshot, agent_summary)


def asyncio_run_summary_subagent(result, desc_files, att_files):
    import asyncio

    try:
        return asyncio.run(run_summary_subagent(result, desc_files=desc_files, att_files=att_files))
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_summary_subagent(result, desc_files=desc_files, att_files=att_files))
        finally:
            loop.close()


async def run_summary_subagent(result, desc_files, att_files):
    ensure_project_root_on_path()
    from core.config import get_agent_runtime_override, settings
    from core.orchestrator.agent_client import agent_client
    from core.orchestrator.agent_runtime import DEFAULT_AGENT_RUNTIME, normalize_agent_runtime

    runtime = normalize_agent_runtime(
        os.getenv("ONES_SUMMARY_RUNTIME")
        or get_agent_runtime_override()
        or settings.default_agent_runtime
        or DEFAULT_AGENT_RUNTIME
    )
    response = await agent_client.run(
        prompt=build_summary_subagent_prompt(result, desc_files=desc_files, att_files=att_files),
        system_prompt=(
            "你是 ONES summary_snapshot 子 agent。只输出一个 JSON 对象。"
            "不要下载 ONES，不要写文件，不要发送消息，不要分析最终根因。"
        ),
        max_turns=8,
        runtime=runtime,
        skill="ones-summary",
        image_paths=summary_image_paths(desc_files, att_files),
    )
    parsed = extract_json_object(str(response.get("text") or ""))
    if not isinstance(parsed, dict):
        return None
    parsed.setdefault("_subagent_meta", {})
    parsed["_subagent_meta"].update({
        "runtime": runtime,
        "model": response.get("model"),
        "session_id": response.get("session_id"),
    })
    return parsed


def ensure_project_root_on_path():
    root = Path(__file__).resolve().parents[4]
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def build_summary_subagent_prompt(result, desc_files, att_files):
    task = result.get("task") or {}
    payload = {
        "task": {
            "number": task.get("number"),
            "uuid": task.get("uuid"),
            "summary": task.get("summary"),
            "description_local": task.get("description_local"),
            "status_name": task.get("status_name"),
            "issue_type_name": task.get("issue_type_name"),
            "url": task.get("url"),
        },
        "project": result.get("project") or {},
        "named_fields": result.get("named_fields") or {},
        "counts": result.get("counts") or {},
        "paths": result.get("paths") or {},
        "downloaded_files": [
            {
                "label": str(item.get("label") or "").strip(),
                "path": str(item.get("path") or "").strip(),
                "uuid": str(item.get("uuid") or "").strip(),
            }
            for item in [*(desc_files or []), *(att_files or [])]
        ],
    }
    return (
        "请基于以下 ONES 下载产物生成 summary_snapshot JSON。\n"
        "只输出 JSON 对象，不要 Markdown，不要解释。\n\n"
        "image_findings 必须保留截图证据的可检索实体：同一条截图事实里尽量同时写出"
        "订单号、车辆名、状态、页面/日志类型、操作结果和时间。"
        "如果任务正文已给出订单号或车辆名，而截图事实就是该订单/车辆的证据，"
        "不要把这些标识拆到其他 finding 里。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def summary_image_paths(desc_files, att_files, limit=6):
    paths = []
    for item in [*(desc_files or []), *(att_files or [])]:
        path = Path(str(item.get("path") or ""))
        if not path.exists() or not path.is_file():
            continue
        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime.startswith("image/") or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            paths.append(str(path.resolve()))
        if len(paths) >= limit:
            break
    return paths


def extract_json_object(text):
    value = str(text or "").strip()
    if not value:
        return None
    candidates = [value]
    if "```" in value:
        for block in value.split("```"):
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
    return None


def merge_subagent_summary_snapshot(base_snapshot, agent_summary):
    snapshot = dict(base_snapshot)
    for key in (
        "summary_text",
        "problem_time",
        "problem_time_confidence",
        "version_text",
        "version_normalized",
        "version_hint",
    ):
        value = str(agent_summary.get(key) or "").strip()
        if value:
            snapshot[key] = value
    for key in (
        "version_fields",
        "version_from_images",
        "version_evidence",
        "business_identifiers",
        "observations",
        "image_findings",
        "missing_items",
    ):
        values = [
            str(item).strip()
            for item in (agent_summary.get(key) or [])
            if str(item).strip()
        ]
        if values:
            snapshot[key] = values
    if not snapshot.get("version_hint"):
        snapshot["version_hint"] = snapshot.get("version_normalized") or ""
    meta = agent_summary.get("_subagent_meta") if isinstance(agent_summary.get("_subagent_meta"), dict) else {}
    snapshot.setdefault("source", {})
    images_available = bool(snapshot.get("source", {}).get("images_available"))
    snapshot["source"].update({
        "text_first": True,
        "images_available": images_available,
        "images_consumed": bool(snapshot.get("version_from_images") or snapshot.get("image_findings")),
        "runtime": str(meta.get("runtime") or "subagent"),
        "model": meta.get("model"),
        "agent_session_id": meta.get("session_id"),
        "subagent": "ones-summary",
        "subagent_status": "success",
    })
    return snapshot


def compact_summary(summary, description, limit=900):
    parts = [str(summary or "").strip(), str(description or "").strip()]
    text = "\n\n".join(item for item in parts if item)
    return text[: limit - 3].rstrip() + "..." if len(text) > limit else text


def first_non_empty(values):
    for value in values:
        text = str(value or "").strip()
        if text and text not in {"/", "无", "\\"}:
            return text
    return ""


def extract_problem_time(text):
    value = str(text or "")
    patterns = [
        r"(?:问题发生时间|发生问题时间|故障时间|问题时间|发生时间)[:：\s]*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}[^\n，,；;]*)",
        r"([0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)",
        r"([0-9]{1,2}月[0-9]{1,2}日\s*[0-9]{1,2}[:：][0-9]{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if match:
            return match.group(1).strip()
    return ""


def extract_version_hints(text):
    value = str(text or "")
    results = []
    for pattern in (
        r"\b(?:RIOT|FMS|SROS|SRC|SRTOS)[^\n，,；;]{0,12}(?:v)?\d+(?:\.\d+){1,4}[^\n，,；;]*",
        r"\b\d+(?:\.\d+){2,4}\b",
    ):
        for match in re.finditer(pattern, value, flags=re.I):
            item = match.group(0).strip()
            if item and item not in results:
                results.append(item)
    return results[:8]


def extract_business_identifiers(text):
    value = str(text or "")
    results = []
    for pattern in (
        r"(?:订单|订单号|任务号|任务id|task id|order id)[^\n，,；;:：]{0,8}[:：#]?\s*([A-Za-z0-9_-]{4,})",
        r"(?:车|车辆|机器人|小车|AGV)[^\n，,；;:：]{0,8}[:：#]?\s*([A-Za-z0-9_-]{2,})",
    ):
        for match in re.finditer(pattern, value, flags=re.I):
            item = match.group(0).strip()
            if item and item not in results:
                results.append(item)
    return results[:12]


def resolve_project(client, raw):
    projects = client.get_recent_projects() + client.get_my_projects()
    by_uuid = {item.get("uuid"): item for item in projects if item.get("uuid")}
    if raw in by_uuid:
        item = by_uuid[raw]
        return item["uuid"], item.get("name")
    exact = [item for item in projects if item.get("name") == raw]
    if len(exact) == 1:
        return exact[0]["uuid"], exact[0].get("name")
    if len(exact) > 1:
        raise OnesError(f"Project name is ambiguous: {[item['name'] for item in exact]}")
    fuzzy = [item for item in projects if raw.lower() in item.get("name", "").lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]["uuid"], fuzzy[0].get("name")
    if len(fuzzy) > 1:
        raise OnesError(f"Project name is ambiguous: {[item['name'] for item in fuzzy[:10]]}")
    raise OnesError(f"Project '{raw}' was not found in recent or my projects.")


def select_current_sprint(sprints):
    if not sprints:
        return None, []
    now = int(time.time())
    running = [item for item in sprints if item.get("start_time") and item.get("end_time") and item["start_time"] <= now <= item["end_time"]]
    if len(running) == 1:
        return running[0], []
    if len(running) > 1:
        return None, sorted(running, key=lambda item: item.get("start_time") or 0, reverse=True)
    future = [item for item in sprints if item.get("end_time") and item["end_time"] >= now]
    if len(future) == 1:
        return future[0], []
    if len(future) > 1:
        return None, sorted(future, key=lambda item: item.get("start_time") or 0)
    return None, []


def match_issue_types(client, mode, issue_type):
    items = client.get_issue_types()
    by_uuid = {item["uuid"]: item for item in items}
    if issue_type:
        if issue_type in by_uuid:
            item = by_uuid[issue_type]
            return [item["uuid"]], [item["name"]], "explicit"
        exact = [item for item in items if item.get("name") == issue_type]
        if len(exact) == 1:
            return [exact[0]["uuid"]], [exact[0]["name"]], "explicit"
        fuzzy = [item for item in items if issue_type.lower() in item.get("name", "").lower()]
        if len(fuzzy) == 1:
            return [fuzzy[0]["uuid"]], [fuzzy[0]["name"]], "explicit"
        raise OnesError(f"Issue type '{issue_type}' was not found or is ambiguous.")
    if mode == "all":
        return [], [], "all"
    matched = [item for item in items if any(word.lower() in item.get("name", "").lower() for word in BUGLIKE)]
    if not matched:
        return [], [], "all"
    return [item["uuid"] for item in matched], [item["name"] for item in matched], "buglike"


def open_statuses(client):
    return [item["uuid"] for item in client.get_statuses() if item.get("category") != "done" and item.get("uuid")]


def current_iteration_issues(args):
    task_ref, team = (None, None)
    if args.from_task:
        task_ref, team = parse_task_ref(args.from_task)
    client = Client(team or args.team_uuid)
    if team:
        client.team_uuid = team

    project_uuid, project_name, task_project = None, None, None
    if task_ref:
        task = client.get_task(task_ref)
        task_project = project_info(client, task, field_map(client.get_fields()))
        project_uuid = task.get("project_uuid")
        project_name = task_project.get("ones_project_name")
    if args.project:
        project_uuid, project_name = resolve_project(client, args.project.strip())
    if not project_uuid:
        raise OnesError("Provide --project or --from-task so the project can be resolved.")

    sprints = [item for item in client.get_sprints() if item.get("project_uuid") == project_uuid]
    sprint, candidates = select_current_sprint(sprints)
    if candidates:
        return {
            "project_uuid": project_uuid,
            "project_name": project_name,
            "ambiguous": True,
            "candidate_sprints": [{"uuid": item.get("uuid"), "title": item.get("title"), "start_time": item.get("start_time"), "end_time": item.get("end_time")} for item in candidates],
            "message": "More than one sprint looks current. Ask the user to confirm the sprint.",
        }
    if not sprint:
        return {
            "project_uuid": project_uuid,
            "project_name": project_name,
            "current_sprint": None,
            "total": 0,
            "examples": [],
            "message": "No sprint was found for the resolved project.",
            "task_project": task_project,
        }

    issue_uuids, issue_names, mode = match_issue_types(client, args.mode, args.issue_type)
    must = [{"in": {f"field_values.{FIELDS['project']}": [project_uuid]}}, {"in": {f"field_values.{FIELDS['sprint']}": [sprint['uuid']]}}]
    if issue_uuids:
        must.append({"in": {f"field_values.{FIELDS['issue_type']}": issue_uuids}})
    status_uuids = [] if args.include_done else open_statuses(client)
    if status_uuids:
        must.append({"in": {f"field_values.{FIELDS['status']}": status_uuids}})

    peek = client.peek({"must": must}, args.limit, args.include_subtasks)
    ids = [entry["uuid"] for group in peek.get("groups", []) for entry in group.get("entries", [])]
    tasks = client.tasks_info(ids)
    statuses = {item["uuid"]: item for item in client.get_statuses()}
    issues = {item["uuid"]: item for item in client.get_issue_types()}
    examples = []
    for task in tasks:
        examples.append({
            "uuid": task.get("uuid"),
            "number": task.get("number"),
            "summary": task.get("summary"),
            "status_uuid": task.get("status_uuid"),
            "status_name": statuses.get(task.get("status_uuid"), {}).get("name"),
            "issue_type_uuid": task.get("issue_type_uuid"),
            "issue_type_name": issues.get(task.get("issue_type_uuid"), {}).get("name"),
            "assignee_uuid": task.get("assign"),
            "url": task_url(task, client.team_uuid),
        })
    return {
        "project_uuid": project_uuid,
        "project_name": project_name,
        "task_project": task_project,
        "current_sprint": {"uuid": sprint.get("uuid"), "title": sprint.get("title"), "start_time": sprint.get("start_time"), "end_time": sprint.get("end_time"), "start_time_iso": iso_time(sprint.get("start_time")), "end_time_iso": iso_time(sprint.get("end_time"))},
        "filters": {"mode": mode, "issue_type_uuids": issue_uuids, "issue_type_names": issue_names, "include_done": args.include_done, "include_subtasks": args.include_subtasks, "status_uuids": status_uuids},
        "total": peek.get("total", 0),
        "returned_examples": len(examples),
        "examples": examples,
    }


def parser():
    root = argparse.ArgumentParser(description="ONES skill helper CLI")
    root.add_argument("--env-file")
    root.add_argument("--team-uuid")
    subs = root.add_subparsers(dest="command", required=True)

    fetch = subs.add_parser("fetch-task")
    fetch.add_argument("reference")
    fetch.add_argument("--output-base-dir", default=".ones")
    fetch.add_argument(
        "--summary-mode",
        choices=("subagent", "deterministic", "none"),
        default=os.getenv("ONES_SUMMARY_MODE", "deterministic"),
        help="How to generate summary_snapshot.json after download. Default: deterministic.",
    )
    fetch.set_defaults(handler=fetch_task)

    count = subs.add_parser("current-iteration-issues")
    count.add_argument("--project")
    count.add_argument("--from-task")
    count.add_argument("--mode", choices=("buglike", "all"), default="buglike")
    count.add_argument("--issue-type")
    count.add_argument("--include-done", action="store_true")
    count.add_argument("--include-subtasks", action="store_true")
    count.add_argument("--limit", type=int, default=10)
    count.set_defaults(handler=current_iteration_issues)
    return root


def main():
    args = parser().parse_args()
    load_envs(args.env_file)
    try:
        result = args.handler(args)
        print(dump({"success": True, "data": result}))
        return 0
    except OnesError as exc:
        print(dump({"success": False, "error": str(exc)}))
        return 1
    except requests.RequestException as exc:
        print(dump({"success": False, "error": f"Network error: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
