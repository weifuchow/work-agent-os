#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml

from review_state import load_state, save_state


PROJECT_URL_RE = re.compile(
    r"^(?P<host>https?://[^/]+)/(?P<project_path>.+?)(?:/-/(?:issues|merge_requests)/\d+)?/?$",
    flags=re.IGNORECASE,
)

ENV_FILES = (".env", ".gitlab.env", "gitlab.env")
GLAB_CONFIG = Path.home() / ".config" / "glab-cli" / "config.yml"


class ReviewPublishError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish confirmed GitLab review line comments to an MR.",
    )
    parser.add_argument("--mr-iid", required=True, help="Target merge request iid.")
    parser.add_argument("--state", default="", help="Optional 00-state.json path for reading/writing final_review.")
    parser.add_argument(
        "--review-json",
        default="",
        help="Optional JSON file containing final_review or review payload.",
    )
    parser.add_argument("--project-url", default="", help="GitLab project URL, for example http://host/group/project.")
    parser.add_argument("--project-path", default="", help="GitLab project path, for example cybertron/allspark.")
    parser.add_argument("--gitlab-token", default="", help="GitLab token. Falls back to env or glab config.")
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="Explicit confirmation gate. Required to publish to MR.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render API requests only; do not publish to GitLab.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP timeout.")
    return parser.parse_args()


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _read_env_fallback() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in ENV_FILES:
        path = Path(name)
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
    return os.getenv(name, "").strip() or _read_env_fallback().get(name, "").strip()


def parse_project_url(project_url: str) -> tuple[str, str]:
    match = PROJECT_URL_RE.match((project_url or "").strip())
    if not match:
        return "", ""
    return match.group("host").strip(), match.group("project_path").strip()


def _read_glab_token(hostname: str) -> str:
    if not GLAB_CONFIG.exists():
        return ""
    try:
        payload = yaml.safe_load(GLAB_CONFIG.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ""
    hosts = payload.get("hosts") or {}
    candidates = [hostname, f"https://{hostname}", f"http://{hostname}"]
    for key in candidates:
        host_cfg = hosts.get(key) or {}
        for token_key in ("token", "oauth_token"):
            token = str(host_cfg.get(token_key) or "").strip()
            if token:
                return token
    return ""


def resolve_gitlab_token(*, host: str, explicit: str = "") -> str:
    if explicit.strip():
        return explicit.strip()
    for name in ("GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN", "GL_TOKEN", "GITLAB_API_TOKEN"):
        value = resolve_env(name)
        if value:
            return value
    hostname = urlparse(host).netloc or host.replace("https://", "").replace("http://", "")
    return _read_glab_token(hostname)


def load_review_payload(review_json_path: Path | None, state: dict[str, Any] | None) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if review_json_path:
        raw = json.loads(review_json_path.read_text(encoding="utf-8"))
    elif isinstance(state, dict):
        raw = dict((state or {}).get("final_review") or {})

    if "final_review" in raw and isinstance(raw.get("final_review"), dict):
        raw = dict(raw["final_review"])

    line_comments = [item for item in (raw.get("line_comments") or []) if isinstance(item, dict)]
    return {
        "status": str(raw.get("status") or "pending"),
        "line_comments": line_comments,
        "merge_recommendation": str(raw.get("merge_recommendation") or "pending"),
        "merge_reason": str(raw.get("merge_reason") or "").strip(),
        "notes": [str(item).strip() for item in (raw.get("notes") or []) if str(item).strip()],
    }


def resolve_project_target(
    *,
    state: dict[str, Any] | None,
    args: argparse.Namespace,
) -> tuple[str, str]:
    explicit_project_path = str(args.project_path or "").strip()
    explicit_project_url = str(args.project_url or "").strip()
    env_project_url = resolve_env("GITLAB_PROJECT_URL")

    host = ""
    project_path = ""

    if explicit_project_url:
        host, project_path = parse_project_url(explicit_project_url)

    if explicit_project_path:
        project_path = explicit_project_path

    if not host and isinstance(state, dict):
        host = str(((state.get("issue") or {}).get("host")) or "").strip()
    if not project_path and isinstance(state, dict):
        project_path = str(((state.get("issue") or {}).get("project_path")) or "").strip()

    if (not host or not project_path) and env_project_url:
        env_host, env_project_path = parse_project_url(env_project_url)
        host = host or env_host
        project_path = project_path or env_project_path

    if not host or not project_path:
        raise ReviewPublishError("Unable to resolve GitLab host/project path. Provide --project-url or --project-path.")
    return host, project_path


def build_comment_body(comment: dict[str, Any]) -> str:
    body = str(comment.get("body") or "").strip()
    if not body:
        raise ReviewPublishError("line comment body is empty")
    severity = str(comment.get("severity") or "").strip().lower()
    if severity and not body.lower().startswith(f"[{severity}]"):
        return f"[{severity}] {body}"
    return body


def build_discussion_payload(comment: dict[str, Any], diff_refs: dict[str, Any]) -> dict[str, str]:
    path = str(comment.get("path") or comment.get("new_path") or comment.get("old_path") or "").strip()
    if not path:
        raise ReviewPublishError("line comment missing path")

    side = str(comment.get("side") or "new").strip().lower()
    if side not in {"new", "old"}:
        raise ReviewPublishError(f"unsupported line comment side: {side}")

    line = comment.get("line")
    if line in (None, ""):
        line = comment.get("new_line") if side == "new" else comment.get("old_line")
    try:
        line_no = int(line)
    except (TypeError, ValueError) as exc:
        raise ReviewPublishError(f"invalid line comment line: {line!r}") from exc

    old_path = str(comment.get("old_path") or path).strip()
    new_path = str(comment.get("new_path") or path).strip()
    payload = {
        "body": build_comment_body(comment),
        "position[position_type]": "text",
        "position[base_sha]": str(diff_refs.get("base_sha") or ""),
        "position[start_sha]": str(diff_refs.get("start_sha") or ""),
        "position[head_sha]": str(diff_refs.get("head_sha") or ""),
        "position[old_path]": old_path,
        "position[new_path]": new_path,
    }
    if not all(payload[key] for key in ("position[base_sha]", "position[start_sha]", "position[head_sha]")):
        raise ReviewPublishError("merge request diff_refs are incomplete; cannot create line discussion")
    if side == "old":
        payload["position[old_line]"] = str(line_no)
    else:
        payload["position[new_line]"] = str(line_no)
    return payload


def build_overview_note(review: dict[str, Any]) -> str:
    recommendation = str(review.get("merge_recommendation") or "pending").strip()
    merge_reason = str(review.get("merge_reason") or "").strip()
    line_comments = review.get("line_comments") or []

    if recommendation == "pending" and not merge_reason:
        return ""

    if recommendation == "blocked":
        headline = "Review result: blocked"
        verdict = "当前存在阻塞性风险，不建议合并。"
    elif recommendation == "merge_ready":
        headline = "Review result: merge_ready"
        verdict = "当前未发现阻塞性风险，可以合并。"
    else:
        headline = "Review result"
        verdict = ""

    parts = [headline]
    if merge_reason:
        parts.append(merge_reason)
    if verdict:
        parts.append(verdict)
    if line_comments:
        parts.append(f"Line comments: {len(line_comments)}")
    return "\n\n".join(part for part in parts if part.strip())


class GitLabReviewPublisher:
    def __init__(self, *, host: str, token: str, timeout_seconds: float) -> None:
        headers = {
            "PRIVATE-TOKEN": token,
            "User-Agent": "gitlab-issue-review-publisher/1.0",
        }
        self._client = httpx.Client(
            base_url=host.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _request_json(self, method: str, path: str, *, data: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._client.request(method, path, data=data)
        if response.status_code >= 400:
            raise ReviewPublishError(
                f"{method} {path} failed: HTTP {response.status_code} {(response.text or '').strip()[:240]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ReviewPublishError(f"{method} {path} returned invalid JSON") from exc

    def get_merge_request(self, *, project_path: str, mr_iid: str) -> dict[str, Any]:
        encoded = quote(project_path, safe="")
        return self._request_json("GET", f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}")

    def create_discussion(self, *, project_path: str, mr_iid: str, data: dict[str, str]) -> dict[str, Any]:
        encoded = quote(project_path, safe="")
        return self._request_json("POST", f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}/discussions", data=data)


def publish_review(
    *,
    state_path: Path | None,
    review_payload: dict[str, Any],
    project_path: str,
    mr_iid: str,
    host: str,
    token: str,
    confirmed: bool,
    dry_run: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not confirmed:
        raise ReviewPublishError("Refusing to publish review comments without --confirmed.")
    if not token:
        raise ReviewPublishError("GitLab token is missing.")

    line_comments = [item for item in (review_payload.get("line_comments") or []) if isinstance(item, dict)]
    merge_recommendation = str(review_payload.get("merge_recommendation") or "pending").strip()
    merge_reason = str(review_payload.get("merge_reason") or "").strip()
    if not line_comments and merge_recommendation == "pending" and not merge_reason:
        raise ReviewPublishError("No final review content to publish.")

    prepared_requests: list[dict[str, Any]] = []
    discussion_ids: list[str] = []
    published_at = ""

    publisher = GitLabReviewPublisher(host=host, token=token, timeout_seconds=timeout_seconds)
    try:
        mr = publisher.get_merge_request(project_path=project_path, mr_iid=mr_iid)
        diff_refs = mr.get("diff_refs") or {}

        for comment in line_comments:
            payload = build_discussion_payload(comment, diff_refs)
            prepared_requests.append({"type": "line_comment", "payload": payload})
            if not dry_run:
                created = publisher.create_discussion(project_path=project_path, mr_iid=mr_iid, data=payload)
                discussion_ids.append(str(created.get("id") or ""))

        overview_body = build_overview_note({
            "line_comments": line_comments,
            "merge_recommendation": merge_recommendation,
            "merge_reason": merge_reason,
        })
        if overview_body:
            overview_payload = {"body": overview_body}
            prepared_requests.append({"type": "overview", "payload": overview_payload})
            if not dry_run:
                created = publisher.create_discussion(project_path=project_path, mr_iid=mr_iid, data=overview_payload)
                discussion_ids.append(str(created.get("id") or ""))

        if not dry_run:
            published_at = utc_now_iso()
            if state_path:
                state = load_state(state_path)
                final_review = dict((state.get("final_review") or {}))
                final_review.update({
                    "status": "merge_ready" if merge_recommendation == "merge_ready" else "commented",
                    "line_comments": line_comments,
                    "merge_recommendation": merge_recommendation or "pending",
                    "merge_reason": merge_reason,
                    "published_to_mr": True,
                    "published_mr": {
                        "project_path": project_path,
                        "mr_iid": str(mr_iid),
                    },
                    "published_discussion_ids": [item for item in discussion_ids if item],
                    "published_at": published_at,
                })
                state["final_review"] = final_review
                save_state(state_path, state)

        return {
            "published": not dry_run,
            "dry_run": dry_run,
            "project_path": project_path,
            "mr_iid": str(mr_iid),
            "line_comments_attempted": len(line_comments),
            "merge_recommendation": merge_recommendation or "pending",
            "discussion_ids": [item for item in discussion_ids if item],
            "published_at": published_at,
            "requests": prepared_requests,
        }
    finally:
        publisher.close()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state).resolve() if args.state else None
    state = load_state(state_path) if state_path and state_path.exists() else None
    review_payload = load_review_payload(Path(args.review_json).resolve() if args.review_json else None, state)
    host, project_path = resolve_project_target(state=state, args=args)
    token = resolve_gitlab_token(host=host, explicit=args.gitlab_token)

    try:
        result = publish_review(
            state_path=state_path,
            review_payload=review_payload,
            project_path=project_path,
            mr_iid=str(args.mr_iid),
            host=host,
            token=token,
            confirmed=bool(args.confirmed),
            dry_run=bool(args.dry_run),
            timeout_seconds=args.timeout_seconds,
        )
    except ReviewPublishError as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
