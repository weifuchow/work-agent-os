#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from review_state import load_state, save_state


ISSUE_URL_RE = re.compile(
    r"^(?P<host>https?://[^/]+)/(?P<project_path>.+?)/-/issues/(?P<iid>\d+)(?:[/?#].*)?$",
    flags=re.IGNORECASE,
)
MR_URL_RE = re.compile(
    r"(?P<url>(?P<host>https?://[^/]+)/(?P<project_path>.+?)/-/merge_requests/(?P<iid>\d+)(?:[/?#][^\s)]*)?)",
    flags=re.IGNORECASE,
)
MR_INLINE_RE = re.compile(r"(?<![\w/])!(?P<iid>\d+)\b")
ONES_URL_RE = re.compile(r"https?://[^\s)]+/project/#/team/[^/\s]+/task/[^?\s#)]+", flags=re.IGNORECASE)
HUNK_HEADER_RE = re.compile(r"^@@ [^@]*@@ ?")
BUGFIX_HINTS = ("fix", "bug", "defect", "hotfix", "修复", "问题", "异常", "故障", "报错", "回归")
FEATURE_HINTS = ("feat", "feature", "story", "新功能", "新增", "支持", "增强", "能力")
BUGFIX_LABEL_HINTS = ("bug", "defect", "hotfix", "incident", "regression", "问题", "缺陷", "异常")
FEATURE_LABEL_HINTS = ("feature", "enhancement", "story", "需求", "新功能")
TEST_TEXT_HINTS = ("test", "tests", "pytest", "unit test", "integration test", "自测", "测试", "验证", "回归测试")


class GitLabApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitLabRef:
    host: str
    project_path: str
    iid: str
    web_url: str

    @property
    def encoded_project(self) -> str:
        return quote(self.project_path, safe="")

    @property
    def key(self) -> str:
        return f"{self.project_path}!{self.iid}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect GitLab issue/MR context and group equivalent branch patches.",
    )
    parser.add_argument("--issue-url", required=True, help="GitLab issue URL.")
    parser.add_argument("--state", default="", help="Optional path to 00-state.json to update.")
    parser.add_argument("--output-dir", default="", help="Directory for issue_context.json and issue_summary.md.")
    parser.add_argument("--fixture-json", default="", help="Optional fixture JSON path for offline replay/testing.")
    parser.add_argument(
        "--gitlab-source",
        default="auto",
        choices=["auto", "glab", "http", "fixture"],
        help="GitLab collection source. Default: auto (prefer glab, then HTTP API).",
    )
    parser.add_argument("--glab-bin", default="glab", help="glab executable name or absolute path.")
    parser.add_argument("--gitlab-token", default="", help="Optional GitLab token. Falls back to env vars.")
    parser.add_argument(
        "--verify-ssl",
        default="auto",
        choices=["auto", "true", "false"],
        help="Whether to verify SSL for GitLab HTTP requests.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP timeout for GitLab API calls.")
    parser.add_argument("--per-page", type=int, default=100, help="Pagination size for notes/API list calls.")
    parser.add_argument("--max-issue-notes", type=int, default=100, help="Maximum issue notes to scan.")
    parser.add_argument("--max-mr-notes", type=int, default=60, help="Maximum notes to scan per MR.")
    return parser.parse_args()


def utc_now_label() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def parse_issue_url(issue_url: str) -> GitLabRef:
    match = ISSUE_URL_RE.match((issue_url or "").strip())
    if not match:
        raise GitLabApiError(f"Unsupported GitLab issue URL: {issue_url}")
    clean_url = (issue_url or "").strip()
    return GitLabRef(
        host=match.group("host").strip(),
        project_path=match.group("project_path").strip(),
        iid=match.group("iid").strip(),
        web_url=clean_url,
    )


def extract_ones_links(*texts: str) -> list[str]:
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in ONES_URL_RE.findall(text):
            found.add(match.strip())
    return sorted(found)


def compact_text(text: str, max_length: int = 280) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 1].rstrip() + "…"


def count_diff_stats(diff_text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def normalize_diff_text(diff_text: str, *, loose: bool) -> str:
    lines: list[str] = []
    for raw_line in (diff_text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip("\r")
        if line.startswith("index "):
            continue
        if loose and line.startswith("@@"):
            context = HUNK_HEADER_RE.sub("", line).strip()
            line = "@@" if not context else f"@@ {context}"
        lines.append(line)
    return "\n".join(lines).strip()


def fingerprint_changes(changes: list[dict[str, Any]]) -> dict[str, str]:
    strict_parts: list[str] = []
    loose_parts: list[str] = []
    for change in sorted(
        changes,
        key=lambda item: (
            str(item.get("new_path") or item.get("old_path") or ""),
            str(item.get("old_path") or ""),
            str(item.get("new_path") or ""),
        ),
    ):
        flags = "|".join(
            [
                f"new={int(bool(change.get('new_file')))}",
                f"rename={int(bool(change.get('renamed_file')))}",
                f"delete={int(bool(change.get('deleted_file')))}",
            ]
        )
        identity = "|".join(
            [
                str(change.get("old_path") or ""),
                str(change.get("new_path") or ""),
                flags,
            ]
        )
        strict_parts.append(identity + "\n" + normalize_diff_text(str(change.get("diff") or ""), loose=False))
        loose_parts.append(identity + "\n" + normalize_diff_text(str(change.get("diff") or ""), loose=True))
    strict_hash = hashlib.sha256("\n====\n".join(strict_parts).encode("utf-8")).hexdigest()
    loose_hash = hashlib.sha256("\n====\n".join(loose_parts).encode("utf-8")).hexdigest()
    return {"strict": strict_hash, "loose": loose_hash}


def _read_note_bodies(items: list[dict[str, Any]], *, max_items: int) -> tuple[list[str], list[str]]:
    bodies: list[str] = []
    excerpts: list[str] = []
    for note in items[:max_items]:
        body = str(note.get("body") or "").strip()
        if not body:
            continue
        bodies.append(body)
        excerpts.append(compact_text(body, max_length=220))
    return bodies, excerpts


def extract_merge_request_refs(text: str, *, default_host: str, default_project_path: str) -> list[GitLabRef]:
    refs: dict[tuple[str, str, str], GitLabRef] = {}
    for match in MR_URL_RE.finditer(text or ""):
        ref = GitLabRef(
            host=match.group("host").strip(),
            project_path=match.group("project_path").strip(),
            iid=match.group("iid").strip(),
            web_url=match.group("url").strip(),
        )
        refs[(ref.host.lower(), ref.project_path, ref.iid)] = ref

    for match in MR_INLINE_RE.finditer(text or ""):
        iid = match.group("iid").strip()
        ref = GitLabRef(
            host=default_host,
            project_path=default_project_path,
            iid=iid,
            web_url=f"{default_host}/{default_project_path}/-/merge_requests/{iid}",
        )
        refs[(ref.host.lower(), ref.project_path, ref.iid)] = ref
    return list(refs.values())


@lru_cache(maxsize=1)
def _env_fallback() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (Path(".env"), Path(".gitlab.env"), Path("gitlab.env")):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("'\""))
    return values


def resolve_env(name: str) -> str:
    return os.getenv(name, "").strip() or _env_fallback().get(name, "").strip()


def resolve_gitlab_token(explicit: str = "") -> str:
    if explicit.strip():
        return explicit.strip()
    for name in ("GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN", "GL_TOKEN", "GITLAB_API_TOKEN"):
        value = resolve_env(name)
        if value:
            return value
    return ""


def resolve_verify_ssl(raw_value: str, issue_ref: GitLabRef) -> bool:
    normalized = str(raw_value or "auto").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    env_value = resolve_env("GITLAB_VERIFY_SSL").lower()
    if env_value in {"true", "false"}:
        return env_value == "true"
    return issue_ref.host.startswith("https://")


class GitLabClient:
    def __init__(self, *, issue_ref: GitLabRef, token: str, verify_ssl: bool, timeout_seconds: float) -> None:
        headers = {
            "Accept": "application/json",
            "User-Agent": "gitlab-issue-review-skill/1.0",
        }
        if token:
            headers["PRIVATE-TOKEN"] = token
        self._client = httpx.Client(
            base_url=issue_ref.host.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=True,
            verify=verify_ssl,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, *, params: dict[str, Any] | None = None, allow_statuses: set[int] | None = None) -> Any:
        response = self._client.get(path, params=params)
        allow_statuses = allow_statuses or set()
        if response.status_code in allow_statuses:
            return None
        if response.status_code >= 400:
            message = compact_text(response.text, max_length=240)
            raise GitLabApiError(f"GET {path} failed: HTTP {response.status_code} {message}")
        try:
            return response.json()
        except ValueError as exc:
            raise GitLabApiError(f"GET {path} returned invalid JSON") from exc

    def get_paginated(
        self,
        path: str,
        *,
        per_page: int,
        max_items: int,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while len(items) < max_items:
            payload = self.get_json(path, params={"page": page, "per_page": per_page})
            if not isinstance(payload, list) or not payload:
                break
            for item in payload:
                if isinstance(item, dict):
                    items.append(item)
                if len(items) >= max_items:
                    break
            if len(payload) < per_page:
                break
            page += 1
        return items


def glab_hostname(issue_ref: GitLabRef) -> str:
    parsed = urlparse(issue_ref.host)
    return parsed.netloc or parsed.path or issue_ref.host.replace("https://", "").replace("http://", "")


def has_glab(glab_bin: str) -> bool:
    return bool(shutil.which(glab_bin))


def _glab_error_mentions_status(message: str, status: int) -> bool:
    lowered = (message or "").lower()
    return f"http {status}" in lowered or f"{status} " in lowered or lowered.endswith(str(status))


class GlabClient:
    def __init__(self, *, issue_ref: GitLabRef, glab_bin: str, token: str) -> None:
        self._glab_bin = glab_bin
        self._token = token
        self._hostname = glab_hostname(issue_ref)

    def _run_json(self, path: str, *, params: dict[str, Any] | None = None, allow_statuses: set[int] | None = None) -> Any:
        endpoint = path.lstrip("/")
        if params:
            endpoint += "?" + urlencode(params, doseq=True)
        env = os.environ.copy()
        if self._token:
            env.setdefault("GITLAB_TOKEN", self._token)
            env.setdefault("GITLAB_PRIVATE_TOKEN", self._token)
        result = subprocess.run(
            [self._glab_bin, "api", "--hostname", self._hostname, endpoint],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
        )
        allow_statuses = allow_statuses or set()
        if result.returncode != 0:
            message = compact_text(result.stderr or result.stdout, max_length=240)
            if any(_glab_error_mentions_status(message, status) for status in allow_statuses):
                return None
            raise GitLabApiError(f"glab api {endpoint} failed: {message}")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise GitLabApiError(f"glab api {endpoint} returned invalid JSON") from exc

    def get_json(self, path: str, *, params: dict[str, Any] | None = None, allow_statuses: set[int] | None = None) -> Any:
        return self._run_json(path, params=params, allow_statuses=allow_statuses)

    def get_paginated(
        self,
        path: str,
        *,
        per_page: int,
        max_items: int,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while len(items) < max_items:
            payload = self.get_json(path, params={"page": page, "per_page": per_page})
            if not isinstance(payload, list) or not payload:
                break
            for item in payload:
                if isinstance(item, dict):
                    items.append(item)
                if len(items) >= max_items:
                    break
            if len(payload) < per_page:
                break
            page += 1
        return items


def discover_issue_merge_requests(
    client: GitLabClient,
    issue_ref: GitLabRef,
    issue: dict[str, Any],
    issue_notes: list[dict[str, Any]],
) -> tuple[list[GitLabRef], list[str]]:
    candidates: dict[tuple[str, str, str], GitLabRef] = {}
    warnings: list[str] = []
    endpoints = (
        f"/api/v4/projects/{issue_ref.encoded_project}/issues/{issue_ref.iid}/related_merge_requests",
        f"/api/v4/projects/{issue_ref.encoded_project}/issues/{issue_ref.iid}/closed_by",
    )
    for path in endpoints:
        try:
            payload = client.get_json(path, allow_statuses={404})
        except GitLabApiError as exc:
            warnings.append(str(exc))
            continue
        if not payload:
            continue
        items = payload if isinstance(payload, list) else payload.get("merge_requests") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            web_url = str(item.get("web_url") or "").strip()
            try:
                ref = parse_issue_url(web_url.replace("/-/merge_requests/", "/-/issues/", 1))
                ref = GitLabRef(
                    host=ref.host,
                    project_path=ref.project_path,
                    iid=str(item.get("iid") or "").strip() or web_url.rsplit("/", 1)[-1],
                    web_url=web_url or f"{issue_ref.host}/{issue_ref.project_path}/-/merge_requests/{item.get('iid')}",
                )
            except GitLabApiError:
                iid = str(item.get("iid") or "").strip()
                if not iid:
                    continue
                ref = GitLabRef(
                    host=issue_ref.host,
                    project_path=issue_ref.project_path,
                    iid=iid,
                    web_url=web_url or f"{issue_ref.host}/{issue_ref.project_path}/-/merge_requests/{iid}",
                )
            candidates[(ref.host.lower(), ref.project_path, ref.iid)] = ref

    text_chunks = [str(issue.get("description") or "")]
    text_chunks.extend(str(note.get("body") or "") for note in issue_notes)
    for chunk in text_chunks:
        for ref in extract_merge_request_refs(chunk, default_host=issue_ref.host, default_project_path=issue_ref.project_path):
            candidates[(ref.host.lower(), ref.project_path, ref.iid)] = ref

    return sorted(candidates.values(), key=lambda item: (item.project_path, int(item.iid))), warnings


def fetch_mr_bundle(
    client: GitLabClient,
    ref: GitLabRef,
    *,
    per_page: int,
    max_mr_notes: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    detail_path = f"/api/v4/projects/{ref.encoded_project}/merge_requests/{ref.iid}"
    detail = client.get_json(detail_path)
    changes_payload = client.get_json(detail_path + "/changes", allow_statuses={404})
    changes = []
    if isinstance(changes_payload, dict):
        changes = [item for item in (changes_payload.get("changes") or []) if isinstance(item, dict)]
    if not changes:
        diff_payload = client.get_json(detail_path + "/diffs", allow_statuses={404})
        if isinstance(diff_payload, list):
            changes = [item for item in diff_payload if isinstance(item, dict)]
        elif isinstance(diff_payload, dict):
            changes = [item for item in (diff_payload.get("diffs") or []) if isinstance(item, dict)]
    notes = client.get_paginated(detail_path + "/notes", per_page=per_page, max_items=max_mr_notes)
    return detail, changes, notes


def normalize_merge_request(
    ref: GitLabRef,
    detail: dict[str, Any],
    changes: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    issue_texts = [
        str(detail.get("description") or ""),
        *(str(note.get("body") or "") for note in notes),
    ]
    ones_links = extract_ones_links(*issue_texts)
    note_bodies, note_excerpts = _read_note_bodies(notes, max_items=5)
    changed_paths: list[str] = []
    additions = 0
    deletions = 0
    for change in changes:
        path = str(change.get("new_path") or change.get("old_path") or "").strip()
        if path:
            changed_paths.append(path)
        diff_additions, diff_deletions = count_diff_stats(str(change.get("diff") or ""))
        additions += diff_additions
        deletions += diff_deletions
    fingerprints = fingerprint_changes(changes)
    return {
        "key": ref.key,
        "url": str(detail.get("web_url") or ref.web_url),
        "project_path": ref.project_path,
        "iid": str(detail.get("iid") or ref.iid),
        "title": str(detail.get("title") or "").strip(),
        "state": str(detail.get("state") or "").strip(),
        "draft": bool(detail.get("draft") or detail.get("work_in_progress")),
        "source_branch": str(detail.get("source_branch") or "").strip(),
        "target_branch": str(detail.get("target_branch") or "").strip(),
        "merge_status": str(detail.get("detailed_merge_status") or detail.get("merge_status") or "").strip(),
        "merged_at": str(detail.get("merged_at") or "").strip(),
        "labels": [str(item).strip() for item in (detail.get("labels") or []) if str(item).strip()],
        "description_excerpt": compact_text(str(detail.get("description") or ""), max_length=420),
        "note_excerpts": note_excerpts,
        "ones_links": ones_links,
        "changes_count": len(changes),
        "changed_paths": sorted(set(changed_paths)),
        "additions": additions,
        "deletions": deletions,
        "patch_fingerprint": fingerprints,
        "note_count": len(note_bodies),
    }


def choose_representative_mr(items: list[dict[str, Any]]) -> dict[str, Any]:
    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        state_rank = {"merged": 0, "opened": 1, "closed": 2}.get(str(item.get("state") or "").lower(), 3)
        try:
            iid = int(str(item.get("iid") or "0"))
        except ValueError:
            iid = 0
        return state_rank, iid

    return sorted(items, key=sort_key)[0]


def build_equivalence_groups(mrs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mr in mrs:
        key = str(((mr.get("patch_fingerprint") or {}).get("loose")) or mr.get("key") or "")
        grouped[key].append(mr)

    result: list[dict[str, Any]] = []
    for group_key, items in grouped.items():
        representative = choose_representative_mr(items)
        strict_keys = {
            str(((item.get("patch_fingerprint") or {}).get("strict")) or "")
            for item in items
        }
        if len(items) == 1:
            reason = "unique_change"
        elif len(strict_keys) == 1:
            reason = "identical_diff"
        else:
            reason = "same_patch_different_context"
        target_branches = sorted({str(item.get("target_branch") or "") for item in items if str(item.get("target_branch") or "")})
        result.append({
            "equivalence_key": group_key,
            "equivalence_reason": reason,
            "skip_deep_review": len(items) > 1,
            "representative": {
                "key": representative["key"],
                "iid": representative["iid"],
                "url": representative["url"],
                "target_branch": representative["target_branch"],
            },
            "target_branches": target_branches,
            "mrs": [
                {
                    "key": item["key"],
                    "iid": item["iid"],
                    "url": item["url"],
                    "target_branch": item["target_branch"],
                    "state": item["state"],
                }
                for item in sorted(items, key=lambda value: int(str(value.get("iid") or "0")))
            ],
            "changed_paths": representative["changed_paths"],
        })

    return sorted(result, key=lambda item: (item["equivalence_reason"] != "unique_change", int(str(item["representative"]["iid"] or "0"))))


def build_issue_payload(
    issue_ref: GitLabRef,
    issue: dict[str, Any],
    issue_notes: list[dict[str, Any]],
) -> dict[str, Any]:
    note_bodies, note_excerpts = _read_note_bodies(issue_notes, max_items=6)
    ones_links = extract_ones_links(str(issue.get("description") or ""), *note_bodies)
    return {
        "key": f"{issue_ref.project_path}#{issue_ref.iid}",
        "url": str(issue.get("web_url") or issue_ref.web_url),
        "host": issue_ref.host,
        "project_path": issue_ref.project_path,
        "iid": issue_ref.iid,
        "title": str(issue.get("title") or issue.get("summary") or "").strip(),
        "state": str(issue.get("state") or "").strip(),
        "labels": [str(item).strip() for item in (issue.get("labels") or []) if str(item).strip()],
        "description_excerpt": compact_text(str(issue.get("description") or ""), max_length=600),
        "note_excerpts": note_excerpts,
        "note_count": len(note_bodies),
        "ones_links": ones_links,
    }


def build_review_targets(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for group in groups:
        representative = group["representative"]
        targets.append({
            "group_key": group["equivalence_key"],
            "action": "skip_duplicate_group" if group["skip_deep_review"] else "review_this_group",
            "reason": group["equivalence_reason"],
            "representative_mr": representative["key"],
            "representative_url": representative["url"],
            "target_branch": representative["target_branch"],
            "all_target_branches": group["target_branches"],
        })
    return targets


def summarize_changed_modules(groups: list[dict[str, Any]]) -> list[str]:
    modules: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group.get("changed_paths") or []:
            parts = [part for part in str(path).split("/") if part]
            if not parts:
                continue
            if parts[0] in {"apps", "packages", "plugins", "frontend"} and len(parts) >= 2:
                token = "/".join(parts[:2])
            else:
                token = parts[0]
            if token not in seen:
                seen.add(token)
                modules.append(token)
    return modules[:8]


def infer_change_intent(issue: dict[str, Any], merge_requests: list[dict[str, Any]]) -> dict[str, Any]:
    bug_score = 0
    feature_score = 0
    signals: list[str] = []
    notes: list[str] = []

    issue_texts = [
        str(issue.get("title") or ""),
        str(issue.get("description_excerpt") or ""),
    ]
    mr_texts = [
        str(item.get("title") or "")
        for item in merge_requests
    ]
    labels = [str(label).strip().lower() for label in (issue.get("labels") or []) if str(label).strip()]
    labels.extend(
        str(label).strip().lower()
        for item in merge_requests
        for label in (item.get("labels") or [])
        if str(label).strip()
    )

    for text in [*issue_texts, *mr_texts]:
        lowered = text.lower()
        for token in BUGFIX_HINTS:
            if token in lowered:
                bug_score += 1
                signals.append(f"bugfix:{token}")
                break
        for token in FEATURE_HINTS:
            if token in lowered:
                feature_score += 1
                signals.append(f"feature:{token}")
                break

    for label in labels:
        if any(token == label or token in label for token in BUGFIX_LABEL_HINTS):
            bug_score += 2
            signals.append(f"bugfix-label:{label}")
        if any(token == label or token in label for token in FEATURE_LABEL_HINTS):
            feature_score += 2
            signals.append(f"feature-label:{label}")

    if bug_score > 0 and feature_score == 0:
        change_type = "bugfix"
        notes.append("当前更像问题修复，输出时必须回答正常业务链路、异常点、触发条件、修复方式、副作用和测试验证。")
    elif feature_score > 0 and bug_score == 0:
        change_type = "feature"
        notes.append("当前更像新功能，输出时要注明“本次以新增能力为主，不以异常修复为主”。")
    elif bug_score > 0 and feature_score > 0:
        change_type = "mixed"
        notes.append("当前同时带有问题修复和功能增强信号，输出时要显式拆开写。")
    else:
        change_type = "unknown"
        notes.append("当前还无法稳定判断是问题修复还是新功能，后续需要结合 issue 现象和 MR 意图确认。")

    return {
        "type": change_type,
        "signals": sorted(set(signals)),
        "notes": notes,
    }


def detect_test_validation(merge_requests: list[dict[str, Any]]) -> dict[str, Any]:
    signals: list[str] = []
    test_file_hits: list[str] = []
    text_hits: list[str] = []

    for mr in merge_requests:
        for path in mr.get("changed_paths") or []:
            lowered = str(path).lower()
            name = Path(str(path)).name
            if (
                "/src/test/" in lowered
                or lowered.startswith("tests/")
                or lowered.startswith("test/")
                or name.endswith("Test.java")
                or name.endswith("_test.py")
                or name.endswith(".spec.ts")
                or name.endswith(".spec.tsx")
                or name.endswith(".test.ts")
                or name.endswith(".test.tsx")
            ):
                test_file_hits.append(str(path))

        text_parts = [
            str(mr.get("description_excerpt") or ""),
            *(str(note or "") for note in (mr.get("note_excerpts") or [])),
        ]
        lowered_text = " ".join(text_parts).lower()
        for token in TEST_TEXT_HINTS:
            if token in lowered_text:
                text_hits.append(token)

    if test_file_hits:
        status = "present"
        signals.extend(f"test-file:{path}" for path in sorted(set(test_file_hits))[:6])
    elif text_hits:
        status = "partial"
        signals.extend(f"test-note:{token}" for token in sorted(set(text_hits)))
    else:
        status = "missing"
        signals.append("no-test-signal-detected")

    return {
        "status": status,
        "signals": signals,
    }


def build_call_logic_seed(
    issue: dict[str, Any],
    groups: list[dict[str, Any]],
    merge_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    representatives = {group["representative"]["key"] for group in groups if group.get("representative")}
    representative_titles = [
        mr["title"]
        for mr in merge_requests
        if mr.get("key") in representatives and str(mr.get("title") or "").strip()
    ]
    entry_points = []
    issue_title = str(issue.get("title") or "").strip()
    if issue_title:
        entry_points.append(issue_title)
    for title in representative_titles:
        if title not in entry_points:
            entry_points.append(title)
    changed_modules = summarize_changed_modules(groups)
    notes: list[str] = []
    if changed_modules:
        notes.append("先根据代表 MR 的 changed_modules 反推入口接口、任务链路或 service 调用，再决定是否继续逐分支 review。")
    if entry_points:
        notes.append("如果 issue 标题和 MR 标题对不上，优先回到 issue 现象和 ONES 描述校对问题调用逻辑。")
    status = "seeded" if (entry_points or changed_modules) else "pending"
    return {
        "status": status,
        "entry_points": entry_points[:5],
        "changed_modules": changed_modules,
        "notes": notes,
    }


def build_review_dimensions_seed(
    *,
    issue: dict[str, Any],
    merge_requests: list[dict[str, Any]],
    call_logic: dict[str, Any],
    change_intent: dict[str, Any],
) -> dict[str, Any]:
    test_validation = detect_test_validation(merge_requests)
    entry_point_hint = ", ".join(call_logic.get("entry_points") or []) or "待确认问题入口"
    module_hint = ", ".join(call_logic.get("changed_modules") or []) or "待确认改动模块"
    change_type = str(change_intent.get("type") or "unknown")
    output_notes: list[str] = []
    side_effect_risks: list[str] = []
    trigger_conditions: list[str] = []

    if call_logic.get("status") == "seeded":
        trigger_conditions.append("先结合 issue 现象与代表 MR，确认问题在什么输入、状态或时序下触发。")
    if module_hint != "待确认改动模块":
        side_effect_risks.append(f"重点评估 {module_hint} 相关链路是否被回归影响。")
    if test_validation["status"] != "present":
        side_effect_risks.append("当前看不到明确测试改动，副作用风险需要人工额外确认。")

    if change_type == "bugfix":
        template = "bugfix"
        anomaly_point = "待确认异常落点，优先回答“正常业务逻辑在哪一步偏离了预期”。"
        fix_summary = "待结合代表 MR 梳理修复动作、修复边界和未覆盖场景。"
        output_notes.extend([
            "输出时必须显式写出：正常业务调用逻辑、异常点、触发条件、修复方式、副作用评估、测试验证。",
            "如果测试信号不足，要直接写“当前缺少与修复对应的测试验证”。",
        ])
    elif change_type == "feature":
        template = "feature"
        anomaly_point = "本次更像新功能，不要强行写异常点；输出时注明“本次以新增能力为主，不以异常修复为主”。"
        fix_summary = "待结合代表 MR 说明新增能力、触发条件、兼容策略和未覆盖边界。"
        output_notes.extend([
            "输出时必须显式写出：现有业务调用逻辑、新增能力、何时生效、潜在副作用、测试验证。",
            "如果同时顺手修了旧问题，要单独分栏写，不要混在功能描述里。",
        ])
    elif change_type == "mixed":
        template = "mixed"
        anomaly_point = "当前同时存在问题修复和功能增强信号，要拆成两部分分别回答。"
        fix_summary = "待分别说明 bugfix 修复点和 feature 增量能力。"
        output_notes.extend([
            "输出时要拆成“问题修复部分”和“新功能部分”，不要混成一个结论。",
            "两部分都要分别回答副作用和测试验证。",
        ])
    else:
        template = "generic"
        anomaly_point = "当前还没确认这是问题修复还是新功能，先回到 issue 现象、MR 意图和代表改动确认。"
        fix_summary = "待确认变更意图后再展开说明。"
        output_notes.extend([
            "输出时先说明当前是 `change_intent=unknown`，不要过早套用 bugfix 或 feature 模板。",
        ])

    status = "seeded" if (call_logic.get("status") != "pending" or change_type != "unknown") else "pending"
    return {
        "status": status,
        "template": template,
        "normal_business_flow": f"待结合 {entry_point_hint} 还原正常业务调用逻辑，并确认它应如何流经 {module_hint}。",
        "anomaly_point": anomaly_point,
        "trigger_conditions": trigger_conditions,
        "fix_summary": fix_summary,
        "side_effect_risks": side_effect_risks,
        "test_validation": test_validation,
        "output_notes": output_notes,
    }


def build_reply_contract(
    *,
    issue: dict[str, Any],
    change_intent: dict[str, Any],
    review_dimensions: dict[str, Any],
) -> dict[str, Any]:
    change_type = str(change_intent.get("type") or "unknown")
    issue_title = str(issue.get("title") or "").strip()
    title_hint = issue_title or "GitLab Issue Review"
    base_columns = [
        {"key": "mr", "label": "MR", "type": "text"},
        {"key": "branch", "label": "Target Branch", "type": "text"},
        {"key": "group", "label": "Change Group", "type": "text"},
        {"key": "deep_review", "label": "Deep Review", "type": "text"},
    ]

    if change_type == "bugfix":
        section_order = [
            "变更类型",
            "当前判断",
            "问题调用逻辑",
            "正常业务调用逻辑",
            "异常点",
            "触发条件",
            "修复方式",
            "MR 归并结果",
            "副作用评估",
            "测试验证",
            "关键证据",
            "风险或缺口",
            "下一步",
        ]
        summary_rule = "一句话说明这是问题修复、当前主结论和置信度。"
        notes = [
            "最终回复优先输出 format=rich，不要先输出纯文本。",
            "sections 顺序尽量固定，不要随意删掉“副作用评估”或“测试验证”。",
        ]
    elif change_type == "feature":
        section_order = [
            "变更类型",
            "当前判断",
            "问题调用逻辑",
            "正常业务调用逻辑",
            "新增能力",
            "生效条件",
            "实现方式",
            "MR 归并结果",
            "副作用评估",
            "测试验证",
            "关键证据",
            "风险或缺口",
            "下一步",
        ]
        summary_rule = "一句话说明这是新功能、主要新增能力和当前置信度，并注明不是以异常修复为主。"
        notes = [
            "输出里必须明确写“本次以新功能为主，不以异常修复为主”。",
            "若存在兼容影响，要放在“副作用评估”而不是“关键证据”。",
        ]
    elif change_type == "mixed":
        section_order = [
            "变更类型",
            "当前判断",
            "问题调用逻辑",
            "问题修复部分",
            "新功能部分",
            "MR 归并结果",
            "副作用评估",
            "测试验证",
            "关键证据",
            "风险或缺口",
            "下一步",
        ]
        summary_rule = "一句话说明这是混合变更，分别点出 bugfix 和 feature 的主结论。"
        notes = [
            "问题修复部分和新功能部分必须拆开写。",
            "副作用和测试验证需要同时覆盖两部分，不能只写一边。",
        ]
    else:
        section_order = [
            "变更类型",
            "当前判断",
            "问题调用逻辑",
            "MR 归并结果",
            "关键证据",
            "风险或缺口",
            "下一步",
        ]
        summary_rule = "一句话说明当前还未确认是问题修复还是新功能，并给出当前最稳的阶段性判断。"
        notes = [
            "当前先保持 format=rich，但要明确 `change_intent=unknown`。",
            "不要提前虚构“异常点”或“新增能力”。",
        ]

    if review_dimensions.get("test_validation", {}).get("status") != "present":
        notes.append("fallback_text 里要明确当前测试验证是否缺失。")

    return {
        "format": "rich",
        "title_hint": title_hint[:80],
        "summary_rule": summary_rule,
        "section_order": section_order,
        "table_columns": base_columns,
        "fallback_required": True,
        "notes": notes,
    }


def build_final_review_seed(*, review_dimensions: dict[str, Any]) -> dict[str, Any]:
    notes = [
        "只有在用户明确确认后，才生成最终 review 行评论。",
        "如果最终没有发现阻塞性风险，可直接给出 merge_ready 结论。",
    ]
    if review_dimensions.get("test_validation", {}).get("status") != "present":
        notes.append("当前测试验证证据不足，即使后续没有明显代码风险，也要在最终结论里标出来。")
    return {
        "status": "pending",
        "line_comments": [],
        "merge_recommendation": "pending",
        "merge_reason": "",
        "published_to_mr": False,
        "published_mr": {
            "project_path": "",
            "mr_iid": "",
        },
        "published_discussion_ids": [],
        "published_at": "",
        "notes": notes,
    }


def make_output_dir(args: argparse.Namespace, state_path: Path | None) -> Path:
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    elif state_path:
        output_dir = state_path.parent / "artifacts" / utc_now_label()
    else:
        output_dir = Path(".review") / "issue-review-artifacts" / utc_now_label()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def load_fixture(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    issue = payload.get("issue") or {}
    issue_notes = [item for item in (payload.get("issue_notes") or []) if isinstance(item, dict)]
    mr_bundles: list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for item in payload.get("merge_requests") or []:
        if not isinstance(item, dict):
            continue
        detail = item.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        web_url = str(detail.get("web_url") or "").strip()
        match = MR_URL_RE.match(web_url)
        if match:
            ref = GitLabRef(
                host=match.group("host").strip(),
                project_path=match.group("project_path").strip(),
                iid=match.group("iid").strip(),
                web_url=web_url,
            )
        else:
            project_path = str(item.get("project_path") or "").strip()
            iid = str(detail.get("iid") or "").strip()
            if not project_path or not iid:
                continue
            host = str(item.get("host") or "").strip()
            ref = GitLabRef(
                host=host,
                project_path=project_path,
                iid=iid,
                web_url=web_url or f"{host}/{project_path}/-/merge_requests/{iid}",
            )
        changes = [change for change in (item.get("changes") or []) if isinstance(change, dict)]
        notes = [note for note in (item.get("notes") or []) if isinstance(note, dict)]
        mr_bundles.append((ref, detail, changes, notes))
    return issue, issue_notes, mr_bundles


def write_markdown_summary(output_path: Path, payload: dict[str, Any]) -> None:
    issue = payload["issue"]
    call_logic = payload["call_logic"]
    change_intent = payload["change_intent"]
    review_dimensions = payload["review_dimensions"]
    reply_contract = payload["reply_contract"]
    final_review = payload["final_review"]
    lines = [
        "# GitLab Issue Review Context",
        "",
        f"- Issue: `{issue['key']}`",
        f"- URL: `{issue['url']}`",
        f"- Collection source: `{payload['collection_source']}`",
        f"- Change intent: `{change_intent['type']}`",
        f"- MR count: {len(payload['merge_requests'])}",
        f"- Distinct change groups: {len(payload['merge_request_groups'])}",
        f"- ONES links: {len(payload['ones_links'])}",
        "",
        "## Issue",
        "",
        f"- Title: {issue['title'] or '(empty)'}",
        f"- State: {issue['state'] or '(unknown)'}",
        f"- Labels: {', '.join(issue['labels']) if issue['labels'] else 'none'}",
        f"- Description excerpt: {issue['description_excerpt'] or '(empty)'}",
        "",
        "## Merge Requests",
        "",
    ]
    if payload["merge_requests"]:
        for mr in payload["merge_requests"]:
            lines.append(
                f"- `{mr['key']}` -> `{mr['target_branch'] or '(unknown)'}` "
                f"[{mr['state'] or 'unknown'}], files={mr['changes_count']}, +{mr['additions']}/-{mr['deletions']}"
            )
    else:
        lines.append("- No related merge requests found")

    lines.extend(["", "## Change Groups", ""])
    if payload["merge_request_groups"]:
        for group in payload["merge_request_groups"]:
            representative = group["representative"]
            lines.append(
                f"- `{representative['key']}` as representative, reason={group['equivalence_reason']}, "
                f"skip_deep_review={'yes' if group['skip_deep_review'] else 'no'}, "
                f"branches={', '.join(group['target_branches']) if group['target_branches'] else 'none'}"
            )
    else:
        lines.append("- No change groups")

    lines.extend(["", "## ONES Links", ""])
    if payload["ones_links"]:
        for link in payload["ones_links"]:
            lines.append(f"- `{link}`")
    else:
        lines.append("- None")

    lines.extend(["", "## Call Logic Seed", ""])
    lines.append(f"- Status: {call_logic['status']}")
    lines.append(f"- Entry points: {', '.join(call_logic['entry_points']) if call_logic['entry_points'] else 'none'}")
    lines.append(f"- Changed modules: {', '.join(call_logic['changed_modules']) if call_logic['changed_modules'] else 'none'}")
    for note in call_logic["notes"]:
        lines.append(f"- {note}")

    lines.extend(["", "## Review Dimensions", ""])
    lines.append(f"- Template: {review_dimensions['template']}")
    lines.append(f"- Normal business flow: {review_dimensions['normal_business_flow']}")
    lines.append(f"- Anomaly point: {review_dimensions['anomaly_point']}")
    lines.append(f"- Trigger conditions: {', '.join(review_dimensions['trigger_conditions']) if review_dimensions['trigger_conditions'] else 'none'}")
    lines.append(f"- Fix summary: {review_dimensions['fix_summary']}")
    lines.append(f"- Side effect risks: {', '.join(review_dimensions['side_effect_risks']) if review_dimensions['side_effect_risks'] else 'none'}")
    lines.append(f"- Test validation: {review_dimensions['test_validation']['status']}")
    for signal in review_dimensions["test_validation"]["signals"]:
        lines.append(f"- Test signal: {signal}")
    for note in review_dimensions["output_notes"]:
        lines.append(f"- Output note: {note}")

    lines.extend(["", "## Reply Contract", ""])
    lines.append(f"- Format: {reply_contract['format']}")
    lines.append(f"- Title hint: {reply_contract['title_hint']}")
    lines.append(f"- Summary rule: {reply_contract['summary_rule']}")
    lines.append(f"- Section order: {', '.join(reply_contract['section_order']) if reply_contract['section_order'] else 'none'}")
    lines.append(
        f"- Table columns: {', '.join(column['label'] for column in reply_contract['table_columns']) if reply_contract['table_columns'] else 'none'}"
    )
    for note in reply_contract["notes"]:
        lines.append(f"- Reply note: {note}")

    lines.extend(["", "## Final Review Gate", ""])
    lines.append(f"- Status: {final_review['status']}")
    lines.append(f"- Merge recommendation: {final_review['merge_recommendation']}")
    lines.append(f"- Line comments: {len(final_review['line_comments'])}")
    for note in final_review["notes"]:
        lines.append(f"- Final review note: {note}")

    lines.extend(["", "## Review Targets", ""])
    if payload["review_targets"]:
        for item in payload["review_targets"]:
            lines.append(
                f"- {item['representative_mr']} -> action={item['action']}, "
                f"branch={item['target_branch'] or '(unknown)'}, reason={item['reason']}"
            )
    else:
        lines.append("- No review targets")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def update_state_after_collection(
    state_path: Path,
    *,
    payload: dict[str, Any],
    result_json: Path,
    summary_md: Path,
) -> None:
    state = load_state(state_path)
    issue = payload["issue"]
    groups = payload["merge_request_groups"]
    review_targets = payload["review_targets"]
    ones_links = payload["ones_links"]
    change_intent = payload["change_intent"]
    call_logic = payload["call_logic"]
    review_dimensions = payload["review_dimensions"]
    reply_contract = payload["reply_contract"]
    final_review = payload["final_review"]

    missing_items: list[str] = []
    if not payload["merge_requests"]:
        missing_items.append("关联 MR 或修复分支")
    if change_intent["type"] == "unknown":
        missing_items.append("变更意图确认（问题修复 / 新功能）")
    if call_logic["status"] != "confirmed" and payload["merge_requests"]:
        missing_items.append("问题调用逻辑确认")
    if ones_links:
        missing_items.append("ONES 工单正文/附件/评论")
    if review_dimensions["test_validation"]["status"] != "present":
        missing_items.append("测试验证证据")

    state["gitlab_collection"] = {
        "source": payload["collection_source"],
        "status": "collected",
        "notes": list(payload["warnings"] or []),
    }
    state["issue"] = {
        "url": issue["url"],
        "host": issue["host"],
        "project_path": issue["project_path"],
        "iid": issue["iid"],
        "title": issue["title"],
        "state": issue["state"],
        "labels": issue["labels"],
    }
    state["ones_links"] = ones_links
    state["mr_candidates"] = [
        {
            "key": item["key"],
            "iid": item["iid"],
            "url": item["url"],
            "target_branch": item["target_branch"],
            "state": item["state"],
            "patch_fingerprint": item["patch_fingerprint"],
        }
        for item in payload["merge_requests"]
    ]
    state["mr_equivalence_groups"] = groups
    state["branch_targets"] = review_targets
    state["change_intent"] = change_intent
    state["call_logic"] = call_logic
    state["review_dimensions"] = review_dimensions
    state["reply_contract"] = reply_contract
    state["final_review"] = final_review
    state["review_scope"] = {
        "distinct_change_groups": len(groups),
        "representative_mrs": [item["representative"]["key"] for item in groups],
        "skipped_groups": [item["equivalence_key"] for item in groups if item["skip_deep_review"]],
    }
    state["analysis_artifacts"] = {
        "last_issue_context_json": str(result_json),
        "last_summary_md": str(summary_md),
    }
    state["missing_items"] = missing_items
    state["evidence_chain_status"] = "partial" if payload["merge_requests"] else "weak"
    state["confidence"] = "medium" if payload["merge_requests"] else "low"
    if not payload["merge_requests"]:
        state["phase"] = "issue_collected"
    elif review_dimensions["status"] == "seeded":
        state["phase"] = "dimensions_seeded"
    elif change_intent["type"] != "unknown":
        state["phase"] = "intent_confirmed"
    elif call_logic["status"] == "seeded":
        state["phase"] = "call_logic_seeded"
    elif groups:
        state["phase"] = "review_scoped"
    elif payload["merge_requests"]:
        state["phase"] = "mr_collected"
    save_state(state_path, state)


def collect_from_api(args: argparse.Namespace, issue_ref: GitLabRef) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]], list[str]]:
    token = resolve_gitlab_token(args.gitlab_token)
    verify_ssl = resolve_verify_ssl(args.verify_ssl, issue_ref)
    client = GitLabClient(
        issue_ref=issue_ref,
        token=token,
        verify_ssl=verify_ssl,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        issue_path = f"/api/v4/projects/{issue_ref.encoded_project}/issues/{issue_ref.iid}"
        issue = client.get_json(issue_path)
        issue_notes = client.get_paginated(
            issue_path + "/notes",
            per_page=args.per_page,
            max_items=args.max_issue_notes,
        )
        mr_refs, warnings = discover_issue_merge_requests(client, issue_ref, issue, issue_notes)
        bundles: list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
        for ref in mr_refs:
            detail, changes, notes = fetch_mr_bundle(
                client,
                ref,
                per_page=args.per_page,
                max_mr_notes=args.max_mr_notes,
            )
            bundles.append((ref, detail, changes, notes))
        return issue, issue_notes, bundles, warnings
    finally:
        client.close()


def collect_from_glab(args: argparse.Namespace, issue_ref: GitLabRef) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]], list[str]]:
    if not has_glab(args.glab_bin):
        raise GitLabApiError(f"glab executable not found: {args.glab_bin}")
    client = GlabClient(
        issue_ref=issue_ref,
        glab_bin=args.glab_bin,
        token=resolve_gitlab_token(args.gitlab_token),
    )
    issue_path = f"/api/v4/projects/{issue_ref.encoded_project}/issues/{issue_ref.iid}"
    issue = client.get_json(issue_path)
    issue_notes = client.get_paginated(
        issue_path + "/notes",
        per_page=args.per_page,
        max_items=args.max_issue_notes,
    )
    mr_refs, warnings = discover_issue_merge_requests(client, issue_ref, issue, issue_notes)
    bundles: list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for ref in mr_refs:
        detail, changes, notes = fetch_mr_bundle(
            client,
            ref,
            per_page=args.per_page,
            max_mr_notes=args.max_mr_notes,
        )
        bundles.append((ref, detail, changes, notes))
    return issue, issue_notes, bundles, warnings


def resolve_collection_source(args: argparse.Namespace) -> str:
    requested = str(args.gitlab_source or "auto").strip().lower()
    if args.fixture_json:
        return "fixture"
    if requested == "fixture":
        return "fixture"
    if requested == "glab":
        return "glab"
    if requested == "http":
        return "http"
    return "glab" if has_glab(args.glab_bin) else "http"


def build_payload(
    issue_ref: GitLabRef,
    issue: dict[str, Any],
    issue_notes: list[dict[str, Any]],
    mr_bundles: list[tuple[GitLabRef, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]],
    warnings: list[str],
    *,
    collection_source: str,
) -> dict[str, Any]:
    normalized_issue = build_issue_payload(issue_ref, issue, issue_notes)
    normalized_mrs = [
        normalize_merge_request(ref, detail, changes, notes)
        for ref, detail, changes, notes in mr_bundles
    ]
    groups = build_equivalence_groups(normalized_mrs)
    ones_links = sorted(
        {
            *normalized_issue["ones_links"],
            *(link for mr in normalized_mrs for link in mr["ones_links"]),
        }
    )
    review_targets = build_review_targets(groups)
    call_logic = build_call_logic_seed(normalized_issue, groups, normalized_mrs)
    change_intent = infer_change_intent(normalized_issue, normalized_mrs)
    review_dimensions = build_review_dimensions_seed(
        issue=normalized_issue,
        merge_requests=normalized_mrs,
        call_logic=call_logic,
        change_intent=change_intent,
    )
    reply_contract = build_reply_contract(
        issue=normalized_issue,
        change_intent=change_intent,
        review_dimensions=review_dimensions,
    )
    final_review = build_final_review_seed(review_dimensions=review_dimensions)
    return {
        "collection_source": collection_source,
        "issue": normalized_issue,
        "merge_requests": normalized_mrs,
        "merge_request_groups": groups,
        "review_targets": review_targets,
        "ones_links": ones_links,
        "change_intent": change_intent,
        "call_logic": call_logic,
        "review_dimensions": review_dimensions,
        "reply_contract": reply_contract,
        "final_review": final_review,
        "warnings": warnings,
    }


def main() -> int:
    args = parse_args()
    try:
        issue_ref = parse_issue_url(args.issue_url)
    except GitLabApiError as exc:
        raise SystemExit(str(exc)) from exc
    state_path = Path(args.state).resolve() if args.state else None
    output_dir = make_output_dir(args, state_path)
    collection_source = resolve_collection_source(args)

    try:
        if collection_source == "fixture":
            issue, issue_notes, mr_bundles = load_fixture(Path(args.fixture_json).resolve())
            warnings: list[str] = []
        elif collection_source == "glab":
            issue, issue_notes, mr_bundles, warnings = collect_from_glab(args, issue_ref)
        else:
            issue, issue_notes, mr_bundles, warnings = collect_from_api(args, issue_ref)
    except GitLabApiError as exc:
        source_hint = ""
        if collection_source == "glab":
            source_hint = " Install/configure glab or rerun with --gitlab-source http / --fixture-json."
        elif not resolve_gitlab_token(args.gitlab_token):
            source_hint = " Provide --gitlab-token or set GITLAB_TOKEN / GITLAB_PRIVATE_TOKEN."
        raise SystemExit(f"{exc}{source_hint}") from exc

    payload = build_payload(
        issue_ref,
        issue,
        issue_notes,
        mr_bundles,
        warnings,
        collection_source=collection_source,
    )

    result_json = output_dir / "issue_context.json"
    summary_md = output_dir / "issue_summary.md"
    result_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown_summary(summary_md, payload)

    if state_path:
        update_state_after_collection(
            state_path,
            payload=payload,
            result_json=result_json,
            summary_md=summary_md,
        )

    response = {
        "output_dir": str(output_dir),
        "issue_context_json": str(result_json),
        "issue_summary_md": str(summary_md),
        "collection_source": payload["collection_source"],
        "issue_key": payload["issue"]["key"],
        "merge_request_count": len(payload["merge_requests"]),
        "distinct_change_groups": len(payload["merge_request_groups"]),
        "ones_links": payload["ones_links"],
        "change_intent": payload["change_intent"],
        "call_logic": payload["call_logic"],
        "review_dimensions": payload["review_dimensions"],
        "reply_contract": payload["reply_contract"],
        "final_review": payload["final_review"],
        "review_targets": payload["review_targets"],
        "warnings": payload["warnings"],
    }
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
