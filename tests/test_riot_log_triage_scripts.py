from __future__ import annotations

import gzip
import json
from pathlib import Path
import subprocess
import sys


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
    assert state["module_hypothesis"] == ["reservation"]
    assert state["missing_items"] == ["订单号缺失"]
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
    assert payload["hits_total"] == 2
    assert "order-123" in payload["matched_terms"]
    assert "heartbeat" not in json.dumps(payload["evidence_hits"], ensure_ascii=False)
    assert any(item["path"].endswith("reservation.log") for item in payload["top_files"])
    assert any("notify.log.gz" in item["path"] for item in payload["top_files"])

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["search_status"] == "returned"
    assert updated_state["phase"] == "search_delegated"
    assert updated_state["search_artifacts"]["last_result_json"] == str(result_json)
    assert updated_state["evidence_chain_status"] == "partial"
