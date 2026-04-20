from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / ".claude" / "skills" / "gitlab-issue-review" / "scripts"
PUBLISH_SCRIPT = SCRIPT_DIR / "publish_review_comments.py"


def _load_publish_module():
    module_name = "gitlab_issue_review_publish_test"
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(module_name, PUBLISH_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, *args, **kwargs):
        self.requests: list[tuple[str, str, dict | None]] = []
        self._discussion_id = 0

    def request(self, method: str, path: str, data: dict | None = None):
        self.requests.append((method, path, data))
        if method == "GET" and path == "/api/v4/projects/cybertron%2Fallspark/merge_requests/201":
            return _FakeResponse({
                "iid": 201,
                "diff_refs": {
                    "base_sha": "base-sha",
                    "start_sha": "start-sha",
                    "head_sha": "head-sha",
                },
            })
        if method == "POST" and path == "/api/v4/projects/cybertron%2Fallspark/merge_requests/201/discussions":
            self._discussion_id += 1
            return _FakeResponse({"id": f"discussion-{self._discussion_id}"})
        raise AssertionError(f"Unexpected request: {method} {path} data={data}")

    def close(self):
        return None


def test_publish_review_requires_confirmation(tmp_path):
    module = _load_publish_module()

    with pytest.raises(module.ReviewPublishError, match="--confirmed"):
        module.publish_review(
            state_path=None,
            review_payload={
                "line_comments": [{"path": "src/main/java/com/demo/OrderService.java", "line": 12, "body": "demo"}],
                "merge_recommendation": "blocked",
                "merge_reason": "demo",
            },
            project_path="cybertron/allspark",
            mr_iid="201",
            host="http://git.standard-robots.com",
            token="token",
            confirmed=False,
            dry_run=True,
            timeout_seconds=5.0,
        )


def test_publish_review_posts_discussions_and_updates_state(monkeypatch, tmp_path):
    module = _load_publish_module()
    fake_client = _FakeHttpClient()
    monkeypatch.setattr(module.httpx, "Client", lambda *args, **kwargs: fake_client)

    state_path = tmp_path / "00-state.json"
    state_path.write_text(json.dumps({
        "issue": {
            "host": "http://git.standard-robots.com",
            "project_path": "cybertron/allspark",
        },
        "final_review": {},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = module.publish_review(
        state_path=state_path,
        review_payload={
            "line_comments": [
                {
                    "path": "src/main/java/com/demo/OrderService.java",
                    "line": 128,
                    "severity": "major",
                    "body": "这里仍然会在空状态下继续调用下游，建议先做空值保护。",
                }
            ],
            "merge_recommendation": "blocked",
            "merge_reason": "当前仍有空指针风险。",
        },
        project_path="cybertron/allspark",
        mr_iid="201",
        host="http://git.standard-robots.com",
        token="token",
        confirmed=True,
        dry_run=False,
        timeout_seconds=5.0,
    )

    assert result["published"] is True
    assert result["merge_recommendation"] == "blocked"
    assert result["discussion_ids"] == ["discussion-1", "discussion-2"]
    assert len(result["requests"]) == 2
    assert fake_client.requests[0][0] == "GET"
    assert fake_client.requests[1][0] == "POST"
    assert fake_client.requests[2][0] == "POST"

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["final_review"]["status"] == "commented"
    assert updated_state["final_review"]["merge_recommendation"] == "blocked"
    assert updated_state["final_review"]["published_to_mr"] is True
    assert updated_state["final_review"]["published_mr"]["mr_iid"] == "201"
    assert updated_state["final_review"]["published_discussion_ids"] == ["discussion-1", "discussion-2"]


def test_publish_review_merge_ready_without_line_comments(monkeypatch):
    module = _load_publish_module()
    fake_client = _FakeHttpClient()
    monkeypatch.setattr(module.httpx, "Client", lambda *args, **kwargs: fake_client)

    result = module.publish_review(
        state_path=None,
        review_payload={
            "line_comments": [],
            "merge_recommendation": "merge_ready",
            "merge_reason": "当前未发现阻塞性风险。",
        },
        project_path="cybertron/allspark",
        mr_iid="201",
        host="http://git.standard-robots.com",
        token="token",
        confirmed=True,
        dry_run=False,
        timeout_seconds=5.0,
    )

    assert result["published"] is True
    assert result["merge_recommendation"] == "merge_ready"
    assert result["discussion_ids"] == ["discussion-1"]
    assert len(result["requests"]) == 1
