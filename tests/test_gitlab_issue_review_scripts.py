from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / ".claude" / "skills" / "gitlab-issue-review" / "scripts"
INIT_REVIEW = SCRIPT_DIR / "init_review.py"
COLLECT_CONTEXT = SCRIPT_DIR / "collect_issue_context.py"


def _run_script(script: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _load_collect_module():
    module_name = "gitlab_issue_review_collect_test"
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(module_name, COLLECT_CONTEXT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_init_review_script_creates_review_state(tmp_path):
    result = _run_script(
        INIT_REVIEW,
        "--project", "allspark",
        "--issue-url", "http://git.standard-robots.com/cybertron/allspark/-/issues/1078",
        "--topic", "issue-1078 review",
        "--base-dir", str(tmp_path / ".review"),
        "--missing-item", "关联 MR 待确认",
    )

    state_path = Path(result["state_path"])
    assert state_path.exists()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["project"] == "allspark"
    assert state["phase"] == "initialized"
    assert state["issue"]["project_path"] == "cybertron/allspark"
    assert state["issue"]["iid"] == "1078"
    assert state["missing_items"] == ["关联 MR 待确认"]
    assert (state_path.parent / "artifacts").is_dir()


def test_collect_issue_context_groups_equivalent_merge_requests(tmp_path):
    init_result = _run_script(
        INIT_REVIEW,
        "--project", "allspark",
        "--issue-url", "http://git.standard-robots.com/cybertron/allspark/-/issues/1078",
        "--topic", "issue-1078 review",
        "--base-dir", str(tmp_path / ".review"),
    )
    state_path = Path(init_result["state_path"])

    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({
        "issue": {
            "iid": 1078,
            "title": "订单状态修复 review",
            "state": "opened",
            "web_url": "http://git.standard-robots.com/cybertron/allspark/-/issues/1078",
            "labels": ["bug", "review"],
            "description": (
                "现场问题来自 ONES: "
                "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/AAA111BBB222"
            ),
        },
        "issue_notes": [
            {"body": "关联修复 !201 和 !205"},
            {"body": "release/3.1.x 还需要单独补丁 !207"},
        ],
        "merge_requests": [
            {
                "detail": {
                    "iid": 201,
                    "title": "fix: order state",
                    "state": "merged",
                    "web_url": "http://git.standard-robots.com/cybertron/allspark/-/merge_requests/201",
                    "source_branch": "fix/order-state",
                    "target_branch": "release/3.0.x",
                    "description": "主修复分支",
                },
                "changes": [
                    {
                        "old_path": "src/main/java/com/demo/OrderService.java",
                        "new_path": "src/main/java/com/demo/OrderService.java",
                        "new_file": False,
                        "renamed_file": False,
                        "deleted_file": False,
                        "diff": "@@ -10,2 +10,2 @@\n-        return oldState;\n+        return fixedState;\n",
                    }
                ],
                "notes": [],
            },
            {
                "detail": {
                    "iid": 205,
                    "title": "cherry-pick: order state",
                    "state": "merged",
                    "web_url": "http://git.standard-robots.com/cybertron/allspark/-/merge_requests/205",
                    "source_branch": "cherry-pick/order-state",
                    "target_branch": "master",
                    "description": "同步到 master",
                },
                "changes": [
                    {
                        "old_path": "src/main/java/com/demo/OrderService.java",
                        "new_path": "src/main/java/com/demo/OrderService.java",
                        "new_file": False,
                        "renamed_file": False,
                        "deleted_file": False,
                        "diff": "@@ -100,2 +100,2 @@\n-        return oldState;\n+        return fixedState;\n",
                    }
                ],
                "notes": [
                    {
                        "body": (
                            "对应 ONES "
                            "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/AAA111BBB222"
                        )
                    }
                ],
            },
            {
                "detail": {
                    "iid": 207,
                    "title": "release/3.1.x extra guard",
                    "state": "opened",
                    "web_url": "http://git.standard-robots.com/cybertron/allspark/-/merge_requests/207",
                    "source_branch": "fix/order-state-31",
                    "target_branch": "release/3.1.x",
                    "description": "3.1 需要额外兼容",
                },
                "changes": [
                    {
                        "old_path": "src/main/java/com/demo/OrderService.java",
                        "new_path": "src/main/java/com/demo/OrderService.java",
                        "new_file": False,
                        "renamed_file": False,
                        "deleted_file": False,
                        "diff": "@@ -10,2 +10,3 @@\n-        return oldState;\n+        audit();\n+        return fixedState;\n",
                    }
                ],
                "notes": [],
            },
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        COLLECT_CONTEXT,
        "--issue-url", "http://git.standard-robots.com/cybertron/allspark/-/issues/1078",
        "--state", str(state_path),
        "--fixture-json", str(fixture_path),
    )

    context_path = Path(result["issue_context_json"])
    summary_path = Path(result["issue_summary_md"])
    assert context_path.exists()
    assert summary_path.exists()

    payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert payload["issue"]["key"] == "cybertron/allspark#1078"
    assert payload["change_intent"]["type"] == "bugfix"
    assert len(payload["merge_requests"]) == 3
    assert len(payload["merge_request_groups"]) == 2
    assert payload["merge_request_groups"][0]["equivalence_reason"] == "unique_change"
    duplicate_group = next(
        item for item in payload["merge_request_groups"]
        if item["equivalence_reason"] != "unique_change"
    )
    assert duplicate_group["skip_deep_review"] is True
    assert duplicate_group["representative"]["key"] == "cybertron/allspark!201"
    assert sorted(payload["ones_links"]) == [
        "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/AAA111BBB222"
    ]
    assert payload["review_dimensions"]["template"] == "bugfix"
    assert payload["review_dimensions"]["test_validation"]["status"] == "missing"
    assert any("测试验证" in note for note in payload["review_dimensions"]["output_notes"])
    assert payload["reply_contract"]["format"] == "rich"
    assert "变更类型" in payload["reply_contract"]["section_order"]
    assert any(column["key"] == "mr" for column in payload["reply_contract"]["table_columns"])
    assert payload["final_review"]["status"] == "pending"
    assert payload["final_review"]["merge_recommendation"] == "pending"
    assert payload["final_review"]["line_comments"] == []
    assert any(item["action"] == "skip_duplicate_group" for item in payload["review_targets"])
    assert "distinct_change_groups" in result

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["gitlab_collection"]["status"] == "collected"
    assert updated_state["phase"] == "dimensions_seeded"
    assert updated_state["review_scope"]["distinct_change_groups"] == 2
    assert updated_state["evidence_chain_status"] == "partial"
    assert updated_state["confidence"] == "medium"
    assert updated_state["change_intent"]["type"] == "bugfix"
    assert updated_state["review_dimensions"]["template"] == "bugfix"
    assert updated_state["reply_contract"]["format"] == "rich"
    assert updated_state["final_review"]["status"] == "pending"
    assert updated_state["final_review"]["merge_recommendation"] == "pending"
    assert "问题调用逻辑确认" in updated_state["missing_items"]
    assert "ONES 工单正文/附件/评论" in updated_state["missing_items"]
    assert "测试验证证据" in updated_state["missing_items"]
    assert updated_state["call_logic"]["status"] == "seeded"
    assert "src" in json.dumps(updated_state["call_logic"], ensure_ascii=False)


def test_collect_from_glab_uses_glab_api(monkeypatch):
    module = _load_collect_module()
    issue_ref = module.parse_issue_url(
        "http://git.standard-robots.com/cybertron/allspark/-/issues/1078"
    )
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        endpoint = command[-1]
        payload = None
        if endpoint == "api/v4/projects/cybertron%2Fallspark/issues/1078":
            payload = {
                "iid": 1078,
                "title": "订单状态修复 review",
                "state": "opened",
                "web_url": "http://git.standard-robots.com/cybertron/allspark/-/issues/1078",
                "description": "issue desc",
                "labels": ["bug"],
            }
        elif endpoint == "api/v4/projects/cybertron%2Fallspark/issues/1078/notes?page=1&per_page=100":
            payload = [{"body": "关联 !201"}]
        elif endpoint == "api/v4/projects/cybertron%2Fallspark/issues/1078/related_merge_requests":
            payload = [
                {
                    "iid": 201,
                    "web_url": "http://git.standard-robots.com/cybertron/allspark/-/merge_requests/201",
                }
            ]
        elif endpoint == "api/v4/projects/cybertron%2Fallspark/issues/1078/closed_by":
            payload = []
        elif endpoint == "api/v4/projects/cybertron%2Fallspark/merge_requests/201":
            payload = {
                "iid": 201,
                "title": "fix: order state",
                "state": "merged",
                "web_url": "http://git.standard-robots.com/cybertron/allspark/-/merge_requests/201",
                "source_branch": "fix/order-state",
                "target_branch": "release/3.0.x",
                "description": "主修复分支",
            }
        elif endpoint == "api/v4/projects/cybertron%2Fallspark/merge_requests/201/changes":
            payload = {
                "changes": [
                    {
                        "old_path": "src/main/java/com/demo/OrderService.java",
                        "new_path": "src/main/java/com/demo/OrderService.java",
                        "new_file": False,
                        "renamed_file": False,
                        "deleted_file": False,
                        "diff": "@@ -10,2 +10,2 @@\n-        return oldState;\n+        return fixedState;\n",
                    }
                ]
            }
        elif endpoint == "api/v4/projects/cybertron%2Fallspark/merge_requests/201/notes?page=1&per_page=100":
            payload = []
        else:
            raise AssertionError(f"Unexpected glab endpoint: {endpoint}")

        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    monkeypatch.setattr(module, "has_glab", lambda glab_bin: True)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    issue, issue_notes, bundles, warnings = module.collect_from_glab(
        SimpleNamespace(
            glab_bin="glab",
            gitlab_token="",
            per_page=100,
            max_issue_notes=100,
            max_mr_notes=20,
        ),
        issue_ref,
    )

    assert issue["title"] == "订单状态修复 review"
    assert len(issue_notes) == 1
    assert len(bundles) == 1
    assert warnings == []
    assert commands[0][:4] == ["glab", "api", "--hostname", "git.standard-robots.com"]
    assert any("related_merge_requests" in command[-1] for command in commands)
    assert any("merge_requests/201/changes" in command[-1] for command in commands)
