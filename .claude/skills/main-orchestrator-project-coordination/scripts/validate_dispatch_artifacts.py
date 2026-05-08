from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate(dispatch_file: Path) -> dict[str, Any]:
    dispatch = _read_json(dispatch_file)
    result_path = Path(str(dispatch.get("result_path") or ""))
    analysis_dir = Path(str(dispatch.get("analysis_dir") or ""))
    result = _read_json(result_path) if result_path else {}

    missing: list[str] = []
    if not dispatch_file.exists():
        missing.append("dispatch_file")
    if not dispatch:
        missing.append("dispatch_json")
    if not str(dispatch.get("project") or ""):
        missing.append("dispatch.project")
    if not str(dispatch.get("status") or ""):
        missing.append("dispatch.status")
    if analysis_dir and not analysis_dir.exists():
        missing.append("analysis_dir")
    if not result_path or not result_path.exists():
        missing.append("result_json")
    if result_path and result_path.exists() and not result:
        missing.append("result_json_valid")

    dispatch_status = str(dispatch.get("status") or "")
    result_failed = bool(result.get("failed")) if result else None
    ok = not missing and dispatch_status in {"success", "failed", "timeout", "cancelled"}
    if dispatch_status == "success" and result_failed:
        ok = False
        missing.append("success_dispatch_has_failed_result")

    return {
        "ok": ok,
        "dispatch_file": str(dispatch_file),
        "status": dispatch_status,
        "project": dispatch.get("project"),
        "skill": dispatch.get("skill"),
        "analysis_dir": str(analysis_dir) if analysis_dir else "",
        "result_path": str(result_path) if result_path else "",
        "missing_or_invalid": missing,
        "result_failed": result_failed,
        "error": result.get("error") or dispatch.get("error") or "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate dispatch_to_project artifacts.")
    parser.add_argument("dispatch_file", help="Path to .orchestration/message-*/dispatch-*.json")
    args = parser.parse_args()

    payload = validate(Path(args.dispatch_file))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if payload["ok"] else 1)


if __name__ == "__main__":
    main()
