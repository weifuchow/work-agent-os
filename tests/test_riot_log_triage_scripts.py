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
    assert (state_path.parent / "01-intake" / "messages").is_dir()
    assert (state_path.parent / "01-intake" / "attachments").is_dir()
    assert (state_path.parent / "02-process").is_dir()
    assert (state_path.parent / "search-runs").is_dir()
    assert Path(result["workflow_dirs"]["process_dir"]) == state_path.parent / "02-process"


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
    assert payload["dsl_query_file"]
    dsl_path = Path(payload["dsl_query_file"])
    assert dsl_path.exists()
    assert dsl_path.name == "query.round1.dsl.txt"
    assert '"order-123"' in dsl_path.read_text(encoding="utf-8")
    assert "order-123" in payload["matched_terms"]
    assert "heartbeat" not in json.dumps(payload["evidence_hits"], ensure_ascii=False)
    assert any(item["path"].endswith("reservation.log") for item in payload["top_files"])
    assert any("notify.log.gz" in item["path"] for item in payload["top_files"])

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["search_status"] == "returned"
    assert updated_state["phase"] == "evidence_reviewed"
    assert updated_state["search_artifacts"]["keyword_package_round1"] == str(tmp_path / "keyword_package.round1.json")
    assert updated_state["search_artifacts"]["last_result_json"] == str(result_json)
    assert updated_state["search_artifacts"]["dsl_round1"] == str(dsl_path)
    assert updated_state["evidence_chain_status"] == "partial"
    assert updated_state["target_log_files"] == ["reservation.log", "notify.log.gz"]
    assert updated_state["time_alignment"]["normalized_window"]["start"] == "2026-04-15 10:20:00"
    assert updated_state["time_alignment"]["normalized_window"]["end"] == "2026-04-15 10:30:00"
    assert updated_state["delegation"]["status"] == "completed"
    assert updated_state["narrowing_round"]["current"] == 1
    assert len(updated_state["narrowing_round"]["history"]) == 1
    assert updated_state["narrowing_round"]["history"][0]["hits_total"] == 2
    assert updated_state["narrowing_round"]["history"][0]["dsl_query_file"] == str(dsl_path)


def test_search_worker_materializes_dsl_and_filters_placeholder_terms(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "2026-04-28 10:24:01.000 [main] INFO 88962 AMR-41-1200E-316549 MoveRequest waiting\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.round1.json"
    package_path.write_text(json.dumps({
        "include_terms": ["88962", "??????", "AMR-41-1200E-316549", "agv??", "MoveRequest"],
        "anchor_terms": ["88962", "????"],
        "gate_terms": ["????", "MoveRequest"],
        "generic_terms": ["ERROR", "??"],
        "hypotheses": ["?????????????????", "是否车辆提前上报完成？"],
        "target_files": ["bootstrap"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    normalized_package = payload["keyword_package"]
    assert payload["hits_total"] == 1
    assert payload["dsl_query_file"].endswith("query.round1.dsl.txt")
    assert "????" not in json.dumps(normalized_package, ensure_ascii=False)
    assert "agv??" not in json.dumps(normalized_package, ensure_ascii=False)
    assert normalized_package["include_terms"] == ["88962", "AMR-41-1200E-316549", "MoveRequest"]
    assert normalized_package["hypotheses"] == ["是否车辆提前上报完成？"]

    dsl_text = Path(payload["dsl_query_file"]).read_text(encoding="utf-8")
    assert "round: 1" in dsl_text
    assert "anchors:" in dsl_text
    assert "gates:" in dsl_text
    assert '"88962"' in dsl_text
    assert '"MoveRequest"' in dsl_text
    assert "????" not in dsl_text

    persisted_package = json.loads(package_path.read_text(encoding="utf-8"))
    assert persisted_package["_normalized_by_search_worker"] is True
    assert "????" not in json.dumps(persisted_package, ensure_ascii=False)


def test_search_worker_regenerates_mojibake_dsl_query(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "2026-04-28 10:24:01.000 [main] INFO 1777271159592 方格-10008 CMD_ORDER_CANCEL\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.round2.json"
    package_path.write_text(json.dumps({
        "round_no": 2,
        "focus_question": "为什么订单取消后仍绑定车辆",
        "anchor_terms": ["1777271159592", "方格-10008"],
        "anchor_match_mode": "prefer",
        "require_anchor": False,
        "gate_terms": ["CMD_ORDER_CANCEL", "绑定"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-28 10:00:00",
            "end": "2026-04-28 11:00:00",
            "source_timezone": "UTC+0",
            "display_timezone": "UTC+8",
        },
        "dsl_query": "round: 2\nquestion: ????? 1777271159592 ??-10008",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    dsl_text = Path(payload["dsl_query_file"]).read_text(encoding="utf-8")
    persisted_package = json.loads(package_path.read_text(encoding="utf-8"))

    assert payload["hits_total"] == 1
    assert "????" not in dsl_text
    assert "??-10008" not in dsl_text
    assert "round: 2" in dsl_text
    assert "mode: verification/wide-recall" in dsl_text
    assert '"1777271159592" OR "方格-10008"' in dsl_text
    assert "CMD_ORDER_CANCEL" in dsl_text
    assert persisted_package["dsl_query"] == dsl_text.strip()


def test_search_worker_keeps_state_output_under_search_runs(tmp_path):
    triage_result = _run_script(
        INIT_STATE,
        "--project", "allspark",
        "--topic", "订单输出目录",
        "--base-dir", str(tmp_path / ".triage"),
    )
    state_path = Path(triage_result["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["phase"] = "keywords_ready"
    state["keyword_package_status"] = "ready"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "2026-04-28 10:24:01.000 [main] INFO AG0019 MoveRequest waiting\n",
        encoding="utf-8",
    )
    package_path = state_path.parent / "keyword_package.round1.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "MoveRequest"],
        "target_files": ["bootstrap"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    wrong_output_dir = state_path.parent / "search-round1"

    result = _run_script(
        SEARCH_WORKER,
        "--state", str(state_path),
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--output-dir", str(wrong_output_dir),
        "--max-hits", "10",
    )

    result_json = Path(result["result_json"])
    assert result_json.parent == state_path.parent / "search-runs" / "search-round1"
    assert result_json.exists()
    assert not wrong_output_dir.exists()


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


def test_search_worker_scores_file_priority_before_output_order(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "notify.log").write_text(
        "2026-04-15 10:24:00.000 [http] INFO AG0019 callback retry\n",
        encoding="utf-8",
    )
    (log_root / "bootstrap.log").write_text(
        "2026-04-15 10:24:01.000 [main] INFO AG0019 callback state gate\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "callback"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["callback"],
        "target_files": ["notify", "bootstrap"],
        "preferred_files": ["notify"],
        "file_priorities": [
            {"pattern": "bootstrap", "score": 80},
            {"pattern": "notify", "score": 10},
        ],
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
    assert payload["hits_total"] == 2
    assert payload["evidence_hits"][0]["path"].endswith("bootstrap.log")
    assert payload["evidence_hits"][0]["score"] > payload["evidence_hits"][1]["score"]


def test_search_worker_keeps_chronological_timeline_separate_from_score_order(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "notify.log").write_text(
        "2026-04-15 10:24:00.000 [http] INFO AG0019 callback retry\n",
        encoding="utf-8",
    )
    (log_root / "bootstrap.log").write_text(
        "2026-04-15 10:25:00.000 [main] INFO AG0019 callback state gate\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "callback"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["callback"],
        "target_files": ["notify", "bootstrap"],
        "file_priorities": [
            {"pattern": "bootstrap", "score": 80},
            {"pattern": "notify", "score": 10},
        ],
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
    assert payload["evidence_hits"][0]["path"].endswith("bootstrap.log")
    assert payload["timeline_hits"][0]["path"].endswith("notify.log")


def test_search_worker_reads_numbered_rolling_log_files(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log.2026-04-21-10.0").write_text(
        "2026-04-21 10:34:00.000 [main] INFO AG0019 ChangeMapRequest sent\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "ChangeMapRequest"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["ChangeMapRequest"],
        "target_files": ["bootstrap.log.2026-04-21-10.0"],
        "preferred_files": ["bootstrap"],
        "require_anchor": True,
        "time_window": {
            "start": "2026-04-21 10:28:00",
            "end": "2026-04-21 10:50:30"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["documents_scanned"] == 1
    assert payload["hits_total"] == 1
    assert payload["evidence_hits"][0]["path"].endswith("bootstrap.log.2026-04-21-10.0")


def test_search_worker_requires_gate_when_gate_terms_exist(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "mini_trace.log").write_text(
        "\n".join([
            "2026-04-21 10:34:00.000 [main] INFO 358208 state PROCESSING",
            "2026-04-21 10:34:01.000 [main] INFO 358208 ChangeMapRequest sent",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["358208", "ChangeMapRequest"],
        "anchor_terms": ["358208"],
        "gate_terms": ["ChangeMapRequest"],
        "target_files": ["mini_trace"],
        "preferred_files": ["mini_trace"],
        "require_anchor": True,
        "time_window": {
            "start": "2026-04-21 10:28:00",
            "end": "2026-04-21 10:50:30"
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
    assert "ChangeMapRequest" in payload["evidence_hits"][0]["matched_terms"]


def test_search_worker_scores_core_and_exception_terms(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-21 10:34:00.000 [main] INFO AG0019 CrossMapManager state gate",
            "2026-04-21 10:34:01.000 [main] ERROR AG0019 AGV_CHANGE_MAP_TIME_OUT_ERROR in CrossMapManager",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "anchor_terms": ["AG0019"],
        "core_terms": ["CrossMapManager"],
        "exception_terms": ["AGV_CHANGE_MAP_TIME_OUT_ERROR"],
        "term_priorities": [
            {"term": "CrossMapManager", "score": 18, "category": "core"},
            {"term": "AGV_CHANGE_MAP_TIME_OUT_ERROR", "score": 30, "category": "exception"},
        ],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "require_anchor": True,
        "time_window": {
            "start": "2026-04-21 10:28:00",
            "end": "2026-04-21 10:50:30"
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
    assert payload["evidence_hits"][0]["score"] > payload["evidence_hits"][1]["score"]
    assert "AGV_CHANGE_MAP_TIME_OUT_ERROR" in payload["evidence_hits"][0]["matched_exception_terms"]
    assert any("term_priority:AGV_CHANGE_MAP_TIME_OUT_ERROR+30" in item for item in payload["evidence_hits"][0]["score_reasons"])


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


def test_search_worker_anchor_prefer_mode_keeps_gate_only_seed_hits(tmp_path):
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
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [main] INFO ChangeMapRequest finished",
            "2026-04-15 10:24:02.000 [main] INFO AG0019 heartbeat",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["ChangeMapRequest"],
        "gate_terms": ["ChangeMapRequest"],
        "target_files": ["bootstrap"],
        "preferred_files": ["bootstrap"],
        "anchor_match_mode": "prefer",
        "require_anchor": False,
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
    assert payload["hits_total"] == 2
    assert payload["suppressed_hits_total"] == 0
    assert payload["keyword_package"]["anchor_terms"] == ["AG0019"]
    assert payload["keyword_package"]["anchor_match_mode"] == "prefer"
    assert payload["keyword_package"]["require_anchor"] is False


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


def test_search_worker_reports_empty_root_diagnostics(tmp_path):
    log_root = tmp_path / "empty_logs"
    log_root.mkdir()
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "target_files": ["bootstrap"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["documents_scanned"] == 0
    assert payload["root_diagnostics"]["root_file_count"] == 0
    assert payload["search_warnings"]
    summary = Path(result["summary_md"]).read_text(encoding="utf-8")
    assert "Search Warnings" in summary


def test_search_worker_skips_corrupt_archive_and_scans_valid_logs(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bad.tar").write_bytes(b"not a tar")
    (log_root / "bootstrap.log").write_text(
        "2026-04-15 10:24:01.000 [main] INFO AG0019 ChangeMapRequest sent\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "ChangeMapRequest"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["ChangeMapRequest"],
        "target_files": ["bootstrap"],
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
    assert payload["evidence_hits"][0]["path"].endswith("bootstrap.log")


def test_search_worker_reports_unsupported_zst_archive(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bundle.tar.zst").write_bytes(b"\x28\xb5\x2f\xfdplaceholder")
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019"],
        "target_files": ["bootstrap"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "10",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["root_diagnostics"]["unsupported_archive_count"] == 1
    assert any(".zst" in warning for warning in payload["search_warnings"])


def test_search_worker_truncates_many_hits_by_score_for_rerank(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    lines = []
    for index in range(10):
        lines.append(f"2026-04-15 10:24:{index:02d}.000 [main] INFO AG0019 LowTerm event {index}")
    for index in range(10):
        lines.append(f"2026-04-15 10:25:{index:02d}.000 [main] INFO AG0019 MidTerm event {index}")
    for index in range(10):
        lines.append(f"2026-04-15 10:26:{index:02d}.000 [main] INFO AG0019 HighTerm event {index}")
    (log_root / "bootstrap.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "LowTerm", "MidTerm", "HighTerm"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["LowTerm", "MidTerm", "HighTerm"],
        "target_files": ["bootstrap"],
        "term_priorities": [
            {"term": "LowTerm", "score": 1, "category": "gate"},
            {"term": "MidTerm", "score": 10, "category": "gate"},
            {"term": "HighTerm", "score": 30, "category": "gate"}
        ],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
        "--max-hits", "5",
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    scores = [hit["score"] for hit in payload["evidence_hits"]]
    assert payload["returned_hits_total"] == 5
    assert payload["accepted_hits_total"] == 30
    assert payload["hits_truncated"] is True
    assert payload["needs_rerank"] is True
    assert scores == sorted(scores, reverse=True)
    assert all("HighTerm" in hit["matched_terms"] for hit in payload["evidence_hits"])


def test_search_worker_merges_same_vehicle_logger_line_template_hits(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    lines = []
    for index in range(1, 21):
        lines.append(
            "2026-04-15 10:24:{0:02d}.000 [commonTaskExecutor-{0}] INFO  "
            "c.s.a.a.d.k.m.TrafficApplyCallBackImpl line:141 - "
            "AG0019|358208.Move_abc: appliedSteps successful, "
            "sending checkPointRequest success, curCheckPointNo is {0}".format(index)
        )
    lines.append(
        "2026-04-15 10:25:00.000 [change-map-thread--1] INFO  "
        "c.s.a.a.d.k.v.m.CrossMapManager line:135 - "
        "AG0019: 切换地图成功,车辆执行状态不符合：IDLE pre：358208，now:358208"
    )
    (log_root / "bootstrap.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "CheckPointRequest", "TrafficApplyCallBackImpl", "CrossMapManager", "车辆执行状态不符合"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["CheckPointRequest", "车辆执行状态不符合"],
        "core_terms": ["TrafficApplyCallBackImpl", "CrossMapManager"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["returned_hits_limit"] == 100
    assert payload["accepted_hits_total"] == 21
    assert payload["hits_total"] == 2
    assert payload["template_groups_total"] == 1
    assert payload["template_merged_hits_total"] == 19

    merged_hit = next(
        hit for hit in payload["evidence_hits"]
        if (hit.get("template_identity") or {}).get("class_name", "").endswith("TrafficApplyCallBackImpl")
    )
    assert merged_hit["template_identity"]["vehicle"] == "AG0019"
    assert merged_hit["template_identity"]["task_key"] == "358208"
    assert merged_hit["template_identity"]["source_line"] == "141"
    assert merged_hit["template_merge"]["hit_count"] == 20
    assert merged_hit["merged_summary"].startswith("模板合并: AG0019 task:358208")
    assert any("checkpoint_no: 1 -> 20" in fact for fact in merged_hit["template_merge"]["change_facts"])

    summary = Path(result["summary_md"]).read_text(encoding="utf-8")
    assert "Template Groups" in summary
    assert "Merged summary: 模板合并: AG0019 task:358208" in summary
    assert "Template merged hits: `20`" in summary
    assert "Template time range: `2026-04-15 10:24:01.000` ~ `2026-04-15 10:24:20.000`" in summary
    assert "Template change facts:" in summary
    assert "checkpoint_no: 1 -> 20" in summary


def test_search_worker_does_not_merge_same_template_across_tasks(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    lines = []
    for task_id in ("358208", "358209"):
        for index in range(1, 6):
            lines.append(
                "2026-04-15 10:24:{0}{1}.000 [commonTaskExecutor-{1}] INFO  "
                "c.s.a.a.d.k.m.TrafficApplyCallBackImpl line:141 - "
                "AG0019|{0}.Move_abc: appliedSteps successful, "
                "sending checkPointRequest success, curCheckPointNo is {1}".format(task_id, index)
            )
    (log_root / "bootstrap.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "CheckPointRequest", "TrafficApplyCallBackImpl"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["CheckPointRequest"],
        "core_terms": ["TrafficApplyCallBackImpl"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    task_keys = sorted(
        (hit.get("template_identity") or {}).get("task_key")
        for hit in payload["evidence_hits"]
        if hit.get("template_identity")
    )
    hit_counts = sorted(
        (hit.get("template_merge") or {}).get("hit_count")
        for hit in payload["evidence_hits"]
        if hit.get("template_merge")
    )
    assert payload["accepted_hits_total"] == 10
    assert payload["hits_total"] == 2
    assert task_keys == ["358208", "358209"]
    assert hit_counts == [5, 5]
    assert payload["template_merged_hits_total"] == 8


def test_search_worker_merges_template_without_task_key_as_none_scope(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:1",
            "2026-04-15 10:24:02.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:2",
            "2026-04-15 10:24:03.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:3",
            "2026-04-15 10:24:04.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:4",
            "2026-04-15 10:24:05.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:5",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "电梯", "ElevatorResourceDevice"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["电梯"],
        "core_terms": ["ElevatorResourceDevice"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["accepted_hits_total"] == 5
    assert payload["hits_total"] == 1
    assert payload["template_merge_min_hits"] == 5
    assert payload["template_groups_total"] == 1
    merged_hit = payload["evidence_hits"][0]
    assert merged_hit["template_identity"]["task_key"] == ""
    assert merged_hit["template_identity"]["task_scope"] == "none"
    assert merged_hit["template_identity"]["template_variant"]
    assert merged_hit["template_merge"]["hit_count"] == 5
    assert "task:none" in merged_hit["merged_summary"]
    assert any("station: 1 -> 5" in fact for fact in merged_hit["template_merge"]["change_facts"])


def test_search_worker_does_not_merge_template_below_min_hit_threshold(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:1",
            "2026-04-15 10:24:02.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:2",
            "2026-04-15 10:24:03.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:3",
            "2026-04-15 10:24:04.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: deviceName:AG0019 apply success resource:AreaResource station:4",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "电梯", "ElevatorResourceDevice"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["电梯"],
        "core_terms": ["ElevatorResourceDevice"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["accepted_hits_total"] == 4
    assert payload["hits_total"] == 4
    assert payload["template_merge_min_hits"] == 5
    assert payload["template_groups_total"] == 0
    assert payload["template_merged_hits_total"] == 0
    assert all(not hit.get("merged_summary") for hit in payload["evidence_hits"])


def test_search_worker_does_not_merge_template_without_vehicle_anchor(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: apply success resource:AreaResource station:1",
            "2026-04-15 10:24:02.000 [main] INFO  c.s.a.a.d.k.p.d.h.e.ElevatorResourceDevice line:127 - 2号电梯: apply success resource:AreaResource station:2",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["电梯", "ElevatorResourceDevice"],
        "gate_terms": ["电梯"],
        "core_terms": ["ElevatorResourceDevice"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["accepted_hits_total"] == 2
    assert payload["hits_total"] == 2
    assert payload["template_groups_total"] == 0
    assert all("template_identity" not in hit for hit in payload["evidence_hits"])


def test_search_worker_keeps_no_task_state_variants_separate(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "bootstrap.log").write_text(
        "\n".join([
            "2026-04-15 10:24:01.000 [event-bus-record-1] INFO  c.s.a.a.d.l.ChargingStateListener line:41 - charging state VEHICLE_STOP_CHARGING change event {\"vehicle\":{\"name\":\"AG0019\"}}",
            "2026-04-15 10:24:02.000 [event-bus-record-1] INFO  c.s.a.a.d.l.ChargingStateListener line:41 - charging state VEHICLE_CHARGING change event {\"vehicle\":{\"name\":\"AG0019\"}}",
        ]) + "\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "keyword_package.json"
    package_path.write_text(json.dumps({
        "include_terms": ["AG0019", "charging state", "ChargingStateListener"],
        "anchor_terms": ["AG0019"],
        "gate_terms": ["charging state"],
        "core_terms": ["ChargingStateListener"],
        "target_files": ["bootstrap"],
        "time_window": {
            "start": "2026-04-15 10:20:00",
            "end": "2026-04-15 10:30:00"
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = _run_script(
        SEARCH_WORKER,
        "--search-root", str(log_root),
        "--keyword-package-file", str(package_path),
    )

    payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
    assert payload["accepted_hits_total"] == 2
    assert payload["hits_total"] == 2
    assert payload["template_groups_total"] == 0
    variants = sorted(
        (hit.get("template_identity") or {}).get("template_variant")
        for hit in payload["evidence_hits"]
        if hit.get("template_identity")
    )
    assert variants == [
        "charging state VEHICLE_CHARGING change event",
        "charging state VEHICLE_STOP_CHARGING change event",
    ]
