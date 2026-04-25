from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
import tarfile
import subprocess
import sys
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / ".claude" / "skills" / "riot-log-triage" / "scripts"
INIT_STATE = SCRIPT_DIR / "init_state.py"
SEARCH_WORKER = SCRIPT_DIR / "search_worker.py"


def _run_script(script: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_init_state_script_creates_triage_state(tmp_path):
    result = _run_script(
        INIT_STATE,
        "--project", "allspark",
        "--topic", "订单卡死排查",
        "--base-dir", str(tmp_path / ".triage"),
        "--version", "3.0.12",
        "--problem-time", "2026-04-15 10:23:00",
        "--module", "reservation",
        "--issue-type", "order_execution",
        "--order-id", "order-123",
        "--vehicle-name", "AG0019",
        "--task-id", "Move_001",
        "--primary-question", "为什么 AG0019 在订单 order-123 上没有继续下发下一段移动",
        "--process-stage", "cross-map",
        "--artifact-status", "partial",
        "--missing-item", "订单号缺失",
    )

    state_path = Path(result["state_path"])
    assert state_path.exists()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["project"] == "allspark"
    assert state["phase"] == "initialized"
    assert state["version_info"]["value"] == "3.0.12"
    assert state["version_info"]["status"] == "known"
    assert state["artifact_completeness"]["status"] == "partial"
    assert state["primary_question"] == "为什么 AG0019 在订单 order-123 上没有继续下发下一段移动"
    assert state["current_question"] == state["primary_question"]
    assert state["time_alignment"]["problem_timezone"] == ""
    assert state["evidence_anchor"]["issue_type"] == "order_execution"
    assert state["evidence_anchor"]["order_id"] == "order-123"
    assert state["evidence_anchor"]["vehicle_name"] == "AG0019"
    assert state["evidence_anchor"]["task_id"] == "Move_001"
    assert state["evidence_anchor"]["status"] == "strong"
    assert state["incident_snapshot"]["process_stage"] == "cross-map"
    assert state["module_hypothesis"] == ["reservation"]
    assert state["missing_items"] == ["订单号缺失"]
    assert state["order_candidates"][0]["order_id"] == "order-123"
    assert (state_path.parent / "search-runs").is_dir()


def test_search_worker_summarizes_hits_and_updates_state(tmp_path):
    triage_result = _run_script(
        INIT_STATE,
        "--project", "allspark",
        "--topic", "订单卡死排查",
        "--base-dir", str(tmp_path / ".triage"),
    )
    state_path = Path(triage_result["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["phase"] = "keywords_ready"
    state["keyword_package_status"] = "ready"
    state["search_artifacts"]["keyword_package_round1"] = str(tmp_path / "keyword_package.round1.json")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_root = tmp_path / "logs"
    log_root.mkdir()

    reservation_log = log_root / "reservation.log"
    reservation_log.write_text(
        "\n".join([
            "2026-04-15 10:21:00.000 [main] INFO heartbeat ok",
            "2026-04-15 10:23:10.000 [main] ERROR ReservationConflictException order-123 detect conflict",
        ]) + "\n",
        encoding="utf-8",
    )

    notify_gz = log_root / "notify.log.gz"
    with gzip.open(notify_gz, "wt", encoding="utf-8") as handle:
        handle.write(
            "2026-04-15 10:24:00.000 [http] WARN order-123 notify retry failed in NotifyService\n"
        )

    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["order-123", "ReservationConflictException", "NotifyService"],
        "exclude_terms": ["heartbeat"],
        "target_files": ["reservation.log", "notify.log.gz"],
        "excluded_files": ["metric.log"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00",
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--state", str(state_path),
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    result_json = Path(result["result_json"])
    summary_md = Path(result["summary_md"])
    assert result_json.exists()
    assert summary_md.exists()

    payload = json.loads(result_json.read_text(encoding="utf-8"))
    assert payload["round_no"] == 1
    assert payload["hits_total"] == 2
    assert "order-123" in payload["matched_terms"]
    assert "heartbeat" not in json.dumps(payload["evidence_hits"], ensure_ascii=False)
    assert any(item["path"].endswith("reservation.log") for item in payload["top_files"])
    assert any("notify.log.gz" in item["path"] for item in payload["top_files"])

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["search_status"] == "returned"
    assert updated_state["phase"] == "evidence_reviewed"
    assert updated_state["search_artifacts"]["keyword_package_round1"] == str(tmp_path / "keyword_package.round1.json")
    assert updated_state["search_artifacts"]["last_result_json"] == str(result_json)
    assert updated_state["evidence_chain_status"] == "partial"
    assert updated_state["target_log_files"] == ["reservation.log", "notify.log.gz"]
    assert updated_state["time_alignment"]["normalized_window"]["start"] == "2026-04-15 10:20:00"
    assert updated_state["time_alignment"]["normalized_window"]["end"] == "2026-04-15 10:30:00"
    assert updated_state["delegation"]["status"] == "completed"
    assert updated_state["narrowing_round"]["current"] == 1
    assert len(updated_state["narrowing_round"]["history"]) == 1
    assert updated_state["narrowing_round"]["history"][0]["hits_total"] == 2


def test_search_worker_honors_excluded_files(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "metric.log").write_text(
        "2026-04-15 10:24:00.000 [metrics] INFO device=AG0019 metric heartbeat\n",
        encoding="utf-8",
    )
    (log_root / "bootstrap.log").write_text(
        "2026-04-15 10:24:01.000 [main] INFO vehicle AG0019 ChangeMapRequest finished\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "exclude_terms": [],
        "target_files": [],
        "excluded_files": ["metric"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00",
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 1
    assert payload["top_files"][0]["path"].endswith("bootstrap.log")


def test_search_worker_prefers_preferred_files_first(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "reservation.log").write_text(
        "2026-04-15 10:24:02.000 [main] INFO AG0019 reservation gate waiting\n",
        encoding="utf-8",
    )
    (log_root / "bootstrap.log").write_text(
        "2026-04-15 10:24:01.000 [main] INFO AG0019 state machine moved to EXECUTING\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "exclude_terms": [],
        "target_files": ["bootstrap", "reservation"],
        "preferred_files": ["bootstrap"],
        "excluded_files": [],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )
    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 2
    assert payload["evidence_hits"][0]["path"].endswith("bootstrap.log")


def test_search_worker_scans_matching_members_inside_tar_archive(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    bootstrap_plain = tmp_path / "bootstrap.log"
    bootstrap_plain.write_text(
        "2026-04-15 10:24:01.000 [main] INFO AG0019 state machine moved to EXECUTING\n",
        encoding="utf-8",
    )
    archive_path = log_root / "bundle.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(bootstrap_plain, arcname="bootstrap.log")

    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "exclude_terms": [],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "excluded_files": [],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )
    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 1
    assert payload["documents_scanned"] == 1
    assert payload["evidence_hits"][0]["path"].endswith("bundle.tar::bootstrap.log")


def test_search_worker_scans_bootstrap_rolling_gz_members_inside_tar_archive(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    gz_member_path = tmp_path / "bootstrap.log.2026-04-15-10.0.gz"
    with gzip.open(gz_member_path, "wt", encoding="utf-8") as handle:
        handle.write("2026-04-15 10:24:01.000 [main] INFO AG0019 state machine moved to EXECUTING\n")

    archive_path = log_root / "bundle.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(gz_member_path, arcname="bootstrap.log.2026-04-15-10.0.gz")

    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "anchor_terms": ["AG0019"],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "excluded_files": [],
        "require_anchor": True,
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )
    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 1
    assert payload["evidence_hits"][0]["path"].endswith("bundle.tar::bootstrap.log.2026-04-15-10.0.gz")


def test_search_worker_falls_back_to_zip_when_gz_extension_is_misleading(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    fake_gz_path = log_root / "bootstrap.log.2026-04-15-10.0.gz"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bootstrap.log", "2026-04-15 10:24:01.000 [main] INFO AG0019 state machine moved to EXECUTING\n")
    fake_gz_path.write_bytes(buffer.getvalue())

    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "anchor_terms": ["AG0019"],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "excluded_files": [],
        "require_anchor": True,
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )
    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 1
    assert payload["evidence_hits"][0]["path"].endswith("bootstrap.log.2026-04-15-10.0.gz")


def test_search_worker_requires_anchor_for_generic_terms(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "mini_trace.log").write_text(
        "\n".join([
            '{"name":"updateState","args":{"state":"HANG","taskName":"物料搬运任务"}}',
            '{"name":"updateState","args":{"state":"HANG","taskName":"AG0019 任务"}}',
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "hang"],
        "anchor_terms": ["AG0019"],
        "generic_terms": ["hang"],
        "exclude_terms": [],
        "target_files": ["mini_trace"],
        "preferred_files": ["mini_trace"],
        "excluded_files": [],
        "require_anchor": True,
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )
    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 1
    assert payload["suppressed_hits_total"] == 1
    assert payload["matched_terms"] == ["AG0019", "hang"]


def test_search_worker_injects_vehicle_anchor_from_state(tmp_path):
    triage_result = _run_script(
        INIT_STATE,
        "--project", "allspark",
        "--topic", "车辆卡住排查",
        "--base-dir", str(tmp_path / ".triage"),
        "--issue-type", "order_execution",
        "--vehicle-name", "AG0019",
    )
    state_path = Path(triage_result["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["phase"] = "keywords_ready"
    state["keyword_package_status"] = "ready"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "mini_trace.log").write_text(
        "\n".join([
            '{"name":"updateState","args":{"state":"HANG","taskName":"物料搬运任务"}}',
            '{"name":"updateState","args":{"state":"HANG","taskName":"AG0019 物料搬运任务"}}',
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "generic_terms": ["hang"],
        "include_terms": ["hang"],
        "target_files": ["mini_trace"],
        "preferred_files": ["mini_trace"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--state", str(state_path),
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 1
    assert payload["suppressed_hits_total"] == 1
    assert payload["keyword_package"]["anchor_terms"] == ["AG0019"]
    assert payload["keyword_package"]["require_anchor"] is True


def test_search_worker_default_max_hits_is_100():
    import importlib.util

    module_name = "riot_log_triage_search_worker_defaults_test"
    if str(SEARCH_WORKER.parent) not in sys.path:
        sys.path.insert(0, str(SEARCH_WORKER.parent))
    spec = importlib.util.spec_from_file_location(module_name, SEARCH_WORKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    parser_args = module.parse_args.__wrapped__ if hasattr(module.parse_args, "__wrapped__") else module.parse_args
    old_argv = sys.argv
    try:
        sys.argv = ["search_worker.py", "--search-root", "D:/tmp/logs", "--keyword-package-json", "{}"]
        args = parser_args()
    finally:
        sys.argv = old_argv

    assert args.max_hits == 100


def test_search_worker_collects_order_candidates_from_vehicle_anchor(tmp_path):
    triage_result = _run_script(
        INIT_STATE,
        "--project", "allspark",
        "--topic", "订单执行排查",
        "--base-dir", str(tmp_path / ".triage"),
        "--issue-type", "order_execution",
        "--vehicle-name", "AG0019",
    )
    state_path = Path(triage_result["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["phase"] = "keywords_ready"
    state["keyword_package_status"] = "ready"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [main] INFO AG0019|358208.Move_xxx: send request finished request:PlayMusicRequest{musicId='0'}",
            "2026-04-15 10:24:02.000 [main] INFO AG0019|358208.Move_xxx: sending request success",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "anchor_terms": ["AG0019"],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--state", str(state_path),
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["hits_total"] == 2
    assert payload["order_candidates"][0]["order_id"] == "358208"
    assert payload["order_candidates"][0]["hits"] == 2

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["order_candidates"][0]["order_id"] == "358208"


def test_search_worker_still_collects_other_order_candidates_when_primary_order_exists(tmp_path):
    triage_result = _run_script(
        INIT_STATE,
        "--project", "allspark",
        "--topic", "订单执行排查",
        "--base-dir", str(tmp_path / ".triage"),
        "--issue-type", "order_execution",
        "--vehicle-name", "AG0019",
        "--order-id", "358208",
    )
    state_path = Path(triage_result["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["phase"] = "keywords_ready"
    state["keyword_package_status"] = "ready"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [main] INFO AG0019|358208.Move_xxx: send request finished",
            "2026-04-15 10:24:02.000 [main] INFO AG0019|358209.Move_yyy: send request finished",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "anchor_terms": ["AG0019"],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--state", str(state_path),
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )
    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    order_ids = [item["order_id"] for item in payload["order_candidates"]]
    assert "358208" in order_ids
    assert "358209" in order_ids
