from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ANALYZER = REPO_ROOT / ".claude" / "skills" / "riot-log-triage" / "scripts" / "code_keyword_analyzer.py"


def _load_analyzer():
    module_name = "riot_log_triage_code_keyword_analyzer_test"
    if str(ANALYZER.parent) not in sys.path:
        sys.path.insert(0, str(ANALYZER.parent))
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ANALYZER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_code_keyword_analyzer_extracts_ranked_core_and_exception_terms(tmp_path):
    module = _load_analyzer()
    code_root = tmp_path / "allspark"
    source_dir = code_root / "src" / "main" / "java" / "com" / "standard"
    source_dir.mkdir(parents=True)
    (source_dir / "CrossMapManager.java").write_text(
        """
        package com.standard;
        public class CrossMapManager {
            public void crossMapSuccessAction() {
                log.warn("{}: doCrossMap taskKey is empty ignore", vehicle.getName());
                throw new AgvChangeMapTimeoutException("ChangeMapRequest timeout");
            }
        }
        class AgvChangeMapTimeoutException extends RuntimeException {}
        """,
        encoding="utf-8",
    )
    state = {
        "current_question": "确认 CrossMapManager 是否在 ChangeMapRequest 阶段阻塞",
        "primary_question": "",
        "hypotheses": [],
        "module_hypothesis": [],
        "incident_snapshot": {},
    }
    package = {
        "gate_terms": ["ChangeMapRequest", "CrossMapManager"],
        "include_terms": ["AG0019", "ChangeMapRequest"],
    }

    hints = module.build_hints(
        state=state,
        package=package,
        code_root=code_root,
        max_files=10,
        max_terms=10,
    )

    assert hints["files_considered"] == 1
    assert "CrossMapManager" in hints["core_terms"]
    assert "crossMapSuccessAction" in hints["core_terms"]
    assert "AgvChangeMapTimeoutException" in hints["exception_terms"]
    assert "doCrossMap taskKey is empty ignore" in hints["log_message_terms"]
    assert "doCrossMap taskKey is empty ignore" in hints["next_keyword_adjustments"]["log_message_terms"]
    assert hints["class_log_messages"][0]["classes"] == ["CrossMapManager", "AgvChangeMapTimeoutException"]
    assert "crossMapSuccessAction" in hints["class_log_messages"][0]["methods"]
    assert "doCrossMap taskKey is empty ignore" in hints["class_log_messages"][0]["log_message_terms"]
    scores = {item["term"]: item["score"] for item in hints["term_priorities"]}
    assert scores["AgvChangeMapTimeoutException"] > scores["crossMapSuccessAction"]
