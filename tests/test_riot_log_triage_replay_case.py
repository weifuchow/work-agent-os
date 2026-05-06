from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_CASE = REPO_ROOT / ".claude" / "skills" / "riot-log-triage" / "scripts" / "replay_case.py"


def _load_replay_case():
    module_name = "riot_log_triage_replay_case_test"
    if str(REPLAY_CASE.parent) not in sys.path:
        sys.path.insert(0, str(REPLAY_CASE.parent))
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, REPLAY_CASE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_case_fixture(case_path: Path) -> None:
    _write_json(case_path, {
        "case_id": "ones-150295-ag0019-elevator-stuck",
        "title": "AG0019 乘梯后卡在电梯内",
        "source": {
            "type": "ones",
            "task_number": 150295,
            "task_uuid": "H5KnYfup2tU5dOZu",
        },
        "input": {
            "project": "allspark",
            "version": "3.46.23",
            "problem_time_local": "2026-04-21 18:34:00",
            "problem_timezone": "UTC+8",
            "vehicle_name": "AG0019",
            "summary": "小车乘梯切图后一直不出来，十多分钟后才开始移动。",
            "prompt_without_hints": "RIot3.46.23 小车 AG0019 在 2026-04-21 18:34 乘梯从 4 楼去往 5 楼，切换地图后一直在电梯里面不出来，十多分钟后才开始移动。请分析日志原因。"
        },
        "artifacts": {
            "attachments": [
                {
                    "name": "allspark-log_20260423095934840.tar",
                    "kind": "log_bundle"
                },
                {
                    "name": "capture.mp4",
                    "kind": "video"
                }
            ]
        },
        "expected_progression": {
            "issue_type": "order_execution",
            "target_process_stage": "elevator-cross-map-next-move-gate"
        },
        "expected_convergence": {},
        "acceptance": ["收敛到乘梯/切图后后续移动未下发"],
        "anti_patterns": ["直接把没有规划路径当主因"]
    })


def _build_frozen_ones_task(ones_root: Path) -> Path:
    task_dir = ones_root / "150295_H5KnYfup2tU5dOZu"
    attachment_dir = task_dir / "attachment"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    log_bundle_path = attachment_dir / "attachment_01_S1BBfrPY.tar"
    video_path = attachment_dir / "attachment_02_PZUxLBsK.mp4"
    log_bundle_path.write_bytes(b"fake-tar")
    video_path.write_bytes(b"fake-mp4")

    _write_json(task_dir / "task.json", {
        "task": {
            "number": 150295,
            "uuid": "H5KnYfup2tU5dOZu",
            "summary": "AG0019 乘梯后卡在电梯内",
        },
        "project": {
            "display_name": "玉晶光电(厦门)有限公司厦门集美工厂项目",
        },
        "named_fields": {
            "FMS/RIoT版本": "3.46.23",
        },
        "paths": {
            "task_dir": str(task_dir),
            "task_json": str(task_dir / "task.json"),
            "messages_json": str(task_dir / "messages.json"),
        },
    })
    _write_json(task_dir / "messages.json", {
        "attachment_downloads": [
            {
                "label": "allspark-log_20260423095934840.tar",
                "path": str(log_bundle_path),
                "uuid": "S1BBfrPY",
            },
            {
                "label": "capture.mp4",
                "path": str(video_path),
                "uuid": "PZUxLBsK",
            },
        ]
    })
    return task_dir


def test_build_replay_bundle_uses_frozen_ones_and_generates_seed_inputs(tmp_path, monkeypatch):
    module = _load_replay_case()
    case_path = tmp_path / "case.json"
    ones_root = tmp_path / ".ones"
    _build_case_fixture(case_path)
    task_dir = _build_frozen_ones_task(ones_root)

    runtime_context = module.projects_mod.ProjectRuntimeContext(
        running_project="allspark",
        project_path=tmp_path / "repo" / "allspark",
        execution_path=tmp_path / "repo" / "allspark",
        business_project_name="玉晶光电(厦门)有限公司厦门集美工厂项目",
        current_branch="master",
        current_version="3.51.0",
        normalized_version="3.46.23",
        checkout_ref="3.46.23",
        recommended_worktree=tmp_path / ".worktrees" / "allspark" / "150295-3.46.23",
        notes=["stub"],
    )
    monkeypatch.setattr(module.projects_mod, "resolve_project_runtime_context", lambda *args, **kwargs: runtime_context)

    manifest = module.build_replay_bundle(
        case_path=case_path,
        base_dir=tmp_path / ".triage" / "replay-cases",
        ones_root=ones_root,
        prepare_worktree=False,
        force=True,
    )

    assert manifest["ones"]["task_dir"] == str(task_dir)
    assert manifest["log_bundle"]["path"].endswith("attachment_01_S1BBfrPY.tar")
    assert manifest["search_root"] == str((task_dir / "attachment").resolve())
    assert manifest["runtime_context"]["checkout_ref"] == "3.46.23"
    assert manifest["normalized_window"]["problem_time_log"] == "2026-04-21 10:34:00"
    assert manifest["normalized_window"]["start"] == "2026-04-21 10:04:00"
    assert manifest["normalized_window"]["end"] == "2026-04-21 11:04:00"

    state_path = Path(manifest["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["project"] == "allspark"
    assert state["primary_question"].startswith("RIot3.46.23 小车 AG0019")
    assert state["current_question"] == "问题时间点车辆处于什么状态、卡在哪个流程门禁"
    assert state["time_alignment"]["normalized_problem_time"] == "2026-04-21 10:34:00"
    assert state["project_workspace"]["projects"]["allspark"]["checkout_ref"] == "3.46.23"
    assert state["missing_items"] == ["order_id"]

    keyword_package = json.loads(Path(manifest["keyword_package_round1"]).read_text(encoding="utf-8"))
    assert keyword_package["anchor_terms"] == ["AG0019"]
    assert "ChangeMapRequest" in keyword_package["gate_terms"]
    assert "hang" in keyword_package["generic_terms"]
    assert keyword_package["require_anchor"] is True
    assert keyword_package["preferred_files"][0] == "bootstrap"
    assert "bootstrap" in keyword_package["target_files"]
    assert "reservation" in keyword_package["target_files"]
    assert "mini_trace" in keyword_package["target_files"]
    assert "notify" in keyword_package["target_files"]
    assert keyword_package["excluded_files"] == ["metric"]
    assert any("metric" in reason for reason in manifest["log_routing"]["reasons"])
    assert any("电梯" in reason or "切换地图" in reason for reason in manifest["log_routing"]["reasons"])


def test_build_replay_bundle_can_use_prepare_worktree_branch(tmp_path, monkeypatch):
    module = _load_replay_case()
    case_path = tmp_path / "case.json"
    ones_root = tmp_path / ".ones"
    _build_case_fixture(case_path)
    _build_frozen_ones_task(ones_root)

    prepared_context = module.projects_mod.ProjectRuntimeContext(
        running_project="allspark",
        project_path=tmp_path / "repo" / "allspark",
        execution_path=tmp_path / ".worktrees" / "allspark" / "150295-3.46.23",
        current_branch="master",
        current_version="3.51.0",
        normalized_version="3.46.23",
        checkout_ref="3.46.23",
        recommended_worktree=tmp_path / ".worktrees" / "allspark" / "150295-3.46.23",
        notes=["prepared"],
    )
    monkeypatch.setattr(module.projects_mod, "prepare_project_runtime_context", lambda *args, **kwargs: prepared_context)

    manifest = module.build_replay_bundle(
        case_path=case_path,
        base_dir=tmp_path / ".triage" / "replay-cases",
        ones_root=ones_root,
        prepare_worktree=True,
        force=True,
    )

    assert manifest["runtime_context"]["execution_path"] == str(prepared_context.execution_path)
    assert manifest["runtime_context"]["recommended_worktree"] == str(prepared_context.recommended_worktree)


def test_infer_log_routing_routes_deadlock_to_project_specific_files():
    module = _load_replay_case()

    riot3_case = {
        "title": "AG0019 死锁后解锁失败",
        "input": {
            "project": "allspark",
            "vehicle_name": "AG0019",
            "summary": "车辆死锁后解锁失败，需要看解锁和重规划。",
            "prompt_without_hints": "AG0019 死锁后解锁失败，请分析。",
        },
        "expected_progression": {"issue_type": "order_execution"},
    }
    riot2_case = {
        "title": "RIOT2 死锁解除异常",
        "input": {
            "project": "riot-standalone",
            "vehicle_name": "AG0019",
            "summary": "车辆发生交通死锁，之后一直无法解锁。",
            "prompt_without_hints": "车辆发生交通死锁，之后一直无法解锁，请分析。",
        },
        "expected_progression": {"issue_type": "order_execution"},
    }
    riot1_case = {
        "title": "RIOT1 车辆解锁失败",
        "input": {
            "project": "fms-java",
            "vehicle_name": "AG0019",
            "summary": "车辆死锁后一直处于 deadlock 状态，人工解锁失败。",
            "prompt_without_hints": "车辆死锁后一直处于 deadlock 状态，人工解锁失败，请分析。",
        },
        "expected_progression": {"issue_type": "order_execution"},
    }

    riot3_routing = module.infer_log_routing(riot3_case)
    assert "mapf" in riot3_routing["target_files"]
    assert riot3_routing["preferred_files"][0] == "bootstrap"
    assert riot3_routing["excluded_files"] == ["metric"]
    assert riot3_routing["anchor_terms"] == ["AG0019"]

    riot2_routing = module.infer_log_routing(riot2_case)
    assert riot2_routing["preferred_files"][0] == "bootstrap"
    assert "DeadLockDetector" in riot2_routing["target_files"]
    assert "TrafficSubSystem" in riot2_routing["target_files"]
    assert "ShapeTrafficManager" in riot2_routing["target_files"]
    assert "WorldRoute" in riot2_routing["target_files"]

    riot1_routing = module.infer_log_routing(riot1_case)
    assert "fms" in riot1_routing["target_files"]
    assert "fms-monitor" in riot1_routing["target_files"]
