#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.projects as projects_mod

from triage_state import build_state, ensure_triage_dir, save_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare an offline replay bundle for a riot-log-triage regression case.",
    )
    parser.add_argument("--case-file", required=True, help="Path to a replay case JSON file.")
    parser.add_argument(
        "--base-dir",
        default=".triage/replay-cases",
        help="Base directory where replay workspaces are created.",
    )
    parser.add_argument(
        "--ones-root",
        default=".ones",
        help="Root directory containing frozen ONES task exports. Default: .ones",
    )
    parser.add_argument(
        "--prepare-worktree",
        action="store_true",
        help="Create or reuse the recommended worktree for the case version.",
    )
    parser.add_argument("--window-before-minutes", type=int, default=30, help="Initial search window before problem time.")
    parser.add_argument("--window-after-minutes", type=int, default=30, help="Initial search window after problem time.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing replay workspace if present.")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_case(case_path: Path) -> dict[str, Any]:
    return load_json(case_path)


def find_ones_task_dir(case: dict[str, Any], ones_root: Path) -> Path | None:
    source = case.get("source") or {}
    if str(source.get("type") or "").strip() != "ones":
        return None

    explicit = str(source.get("task_dir") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.exists():
            return candidate

    task_number = str(source.get("task_number") or "").strip()
    task_uuid = str(source.get("task_uuid") or "").strip()
    if not task_number or not task_uuid:
        return None

    candidate = (ones_root / f"{task_number}_{task_uuid}").resolve()
    if candidate.exists():
        return candidate
    return None


def load_ones_artifacts(task_dir: Path | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    if task_dir is None:
        return {}, {}, None

    task_json = task_dir / "task.json"
    messages_json = task_dir / "messages.json"
    ones_result = load_json(task_json) if task_json.exists() else {}
    messages_payload = load_json(messages_json) if messages_json.exists() else {}

    snapshot_path = ""
    if ones_result:
        snapshot_path = str((ones_result.get("paths") or {}).get("summary_snapshot_json") or "").strip()
    snapshot = load_json(Path(snapshot_path)) if snapshot_path and Path(snapshot_path).exists() else None
    return ones_result, messages_payload, snapshot


def resolve_case_attachments(case: dict[str, Any], messages_payload: dict[str, Any]) -> list[dict[str, Any]]:
    expected = list((case.get("artifacts") or {}).get("attachments") or [])
    downloads = list(messages_payload.get("attachment_downloads") or [])
    resolved: list[dict[str, Any]] = []
    for item in expected:
        expected_name = str(item.get("name") or "").strip()
        match = next((dl for dl in downloads if str(dl.get("label") or "").strip() == expected_name), None)
        resolved.append({
            "name": expected_name,
            "kind": str(item.get("kind") or "").strip(),
            "path": str(match.get("path") or "").strip() if match else "",
            "uuid": str(match.get("uuid") or "").strip() if match else "",
            "exists": bool(match and Path(str(match.get("path") or "")).exists()),
        })
    return resolved


def select_log_bundle(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in attachments:
        if item.get("kind") == "log_bundle" and item.get("path"):
            return item
    for item in attachments:
        path = str(item.get("path") or "").lower()
        if path.endswith((".tar", ".tar.gz", ".tgz", ".zip", ".gz")):
            return item
    return None


def parse_problem_time(value: str) -> datetime:
    text = str(value or "").strip().replace("T", " ")
    return datetime.fromisoformat(text)


def parse_utc_offset(value: str) -> timedelta:
    text = str(value or "").strip().upper()
    if text.startswith("UTC"):
        text = text[3:]
    text = text.strip()
    if not text:
        return timedelta(0)
    sign = 1
    if text[0] == "+":
        sign = 1
        text = text[1:]
    elif text[0] == "-":
        sign = -1
        text = text[1:]
    if ":" in text:
        hours_str, minutes_str = text.split(":", 1)
    else:
        hours_str, minutes_str = text, "0"
    hours = int(hours_str or "0")
    minutes = int(minutes_str or "0")
    return timedelta(hours=sign * hours, minutes=sign * minutes)


def project_log_offset(project_name: str) -> timedelta:
    normalized = str(project_name or "").strip().lower()
    if normalized in {"allspark", "riot3"}:
        return timedelta(0)
    return parse_utc_offset("UTC+8")


def build_search_window(
    *,
    project_name: str,
    problem_time_local: str,
    problem_timezone: str,
    before_minutes: int,
    after_minutes: int,
) -> dict[str, str]:
    local_time = parse_problem_time(problem_time_local)
    local_offset = parse_utc_offset(problem_timezone)
    log_offset = project_log_offset(project_name)
    as_utc = local_time - local_offset
    log_time = as_utc + log_offset
    start = log_time - timedelta(minutes=before_minutes)
    end = log_time + timedelta(minutes=after_minutes)
    return {
        "problem_time_local": local_time.strftime("%Y-%m-%d %H:%M:%S"),
        "problem_time_log": log_time.strftime("%Y-%m-%d %H:%M:%S"),
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_primary_question(case: dict[str, Any]) -> str:
    input_payload = case.get("input") or {}
    prompt = str(input_payload.get("prompt_without_hints") or "").strip()
    if prompt:
        return prompt
    vehicle_name = str(input_payload.get("vehicle_name") or "").strip()
    problem_time = str(input_payload.get("problem_time_local") or "").strip()
    summary = str(input_payload.get("summary") or "").strip()
    return f"{vehicle_name} 在 {problem_time} 的问题：{summary}".strip()


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def infer_log_routing(case: dict[str, Any]) -> dict[str, Any]:
    input_payload = case.get("input") or {}
    text = " ".join([
        str(input_payload.get("summary") or "").strip(),
        str(input_payload.get("prompt_without_hints") or "").strip(),
        str(case.get("title") or "").strip(),
    ])
    project_name = str(input_payload.get("project") or "").strip().lower()
    target_files: list[str] = []
    preferred_files: list[str] = []
    excluded_files: list[str] = []
    anchor_terms: list[str] = []
    gate_terms: list[str] = []
    generic_terms: list[str] = []
    reasons: list[str] = []

    vehicle_name = str(input_payload.get("vehicle_name") or "").strip()
    if vehicle_name:
        anchor_terms.append(vehicle_name)

    if project_name in {"allspark", "riot3"}:
        target_files.extend(["bootstrap", "reservation", "mini_trace"])
        preferred_files.extend(["bootstrap"])
        reasons.append("RIOT3 默认先看 bootstrap / reservation / mini_trace，优先还原订单执行与状态链。")
    elif project_name in {"riot-standalone", "riot2"}:
        target_files.extend(["bootstrap"])
        preferred_files.extend(["bootstrap"])
        reasons.append("RIOT2 默认先看 bootstrap，再按专项 logger 文件补充。")
    elif project_name in {"fms-java", "riot1", "fms"}:
        target_files.extend(["fms"])
        preferred_files.extend(["fms"])
        reasons.append("RIOT1 默认先看 fms 主业务日志，再按请求/监控需要补 monitor。")

    if _contains_any(text, ("电梯", "乘梯", "elevator", "lift")):
        target_files.extend(["bootstrap", "reservation", "mini_trace"])
        gate_terms.extend(["电梯", "乘梯", "elevator"])
        reasons.append("问题描述包含电梯/乘梯，优先看主流程和状态日志，不先扫性能监控。")

    if _contains_any(text, ("切换地图", "切图", "change map", "cross map")):
        target_files.extend(["bootstrap", "mini_trace", "reservation"])
        gate_terms.extend(["切换地图", "切图", "ChangeMapRequest"])
        reasons.append("问题描述包含切换地图，优先收敛到切图前后请求、状态和后续动作。")

    if _contains_any(text, ("不出来", "不动", "没继续移动", "没有继续下发", "十多分钟")):
        target_files.extend(["bootstrap", "reservation", "notify", "mini_trace"])
        gate_terms.extend(["不出来", "不动"])
        generic_terms.extend(["等待", "hang"])
        reasons.append("问题描述更像运行过程卡住，补看 reservation / notify 判断是否停在门禁或等待；`hang` 只作为辅助词。")

    if _contains_any(text, ("路径", "规划", "route", "mapf", "no path")):
        target_files.extend(["mapf", "bootstrap", "reservation"])
        gate_terms.extend(["路径", "规划", "route"])
        reasons.append("问题描述涉及路径规划，补充 mapf 日志。")

    if _contains_any(text, ("死锁", "解锁", "deadlock", "unlock", "reroute", "锁路", "交通冲突")):
        if project_name in {"allspark", "riot3"}:
            target_files.extend(["mapf", "bootstrap", "reservation", "mini_trace"])
            preferred_files.extend(["bootstrap", "mapf"])
            reasons.append("RIOT3 死锁/解锁问题仍先看 bootstrap 还原任务执行状态机，再补 mapf / reservation / mini_trace 看解锁与重规划。")
        elif project_name in {"riot-standalone", "riot2"}:
            target_files.extend(["DeadLockDetector", "TrafficSubSystem", "ShapeTrafficManager", "WorldRoute", "bootstrap"])
            preferred_files.extend(["bootstrap", "DeadLockDetector", "TrafficSubSystem", "ShapeTrafficManager", "WorldRoute"])
            reasons.append("RIOT2 死锁/解锁问题仍先看 bootstrap 还原任务执行状态机，再补 DeadLockDetector / TrafficSubSystem / ShapeTrafficManager / WorldRoute。")
        elif project_name in {"fms-java", "riot1", "fms"}:
            target_files.extend(["fms", "fms-monitor"])
            preferred_files.extend(["fms", "fms-monitor"])
            reasons.append("RIOT1 死锁/解锁问题首轮优先 fms 主业务日志，必要时补 fms-monitor。")
        gate_terms.extend(["死锁", "解锁", "deadlock", "unlock", "reroute"])

    if _contains_any(text, ("http", "回调", "callback", "notify", "httpact")):
        target_files.extend(["notify", "reservation", "bootstrap"])
        gate_terms.extend(["callback", "notify", "http"])
        reasons.append("问题描述涉及回调/通知，补充 notify 日志。")

    if str((case.get("expected_progression") or {}).get("issue_type") or "").strip() == "order_execution":
        excluded_files.append("metric")
        if project_name in {"allspark", "riot3", "riot-standalone", "riot2"}:
            preferred_files.insert(0, "bootstrap")
            reasons.append("RIOT2/RIOT3 任务执行流程类问题首轮优先看 bootstrap，状态机日志基本在这里。")
        reasons.append("订单执行问题首轮默认排除 metric，避免车号先落到性能监控噪音。")

    dedup_target_files = list(dict.fromkeys(item for item in target_files if item))
    dedup_preferred_files = list(dict.fromkeys(item for item in preferred_files if item))
    dedup_excluded_files = list(dict.fromkeys(item for item in excluded_files if item))
    dedup_anchor_terms = list(dict.fromkeys(item for item in anchor_terms if item))
    dedup_gate_terms = list(dict.fromkeys(item for item in gate_terms if item))
    dedup_generic_terms = list(dict.fromkeys(item for item in generic_terms if item))
    dedup_include_terms = [*dedup_anchor_terms, *dedup_gate_terms, *dedup_generic_terms]
    return {
        "target_files": dedup_target_files,
        "preferred_files": dedup_preferred_files,
        "excluded_files": dedup_excluded_files,
        "anchor_terms": dedup_anchor_terms,
        "gate_terms": dedup_gate_terms,
        "generic_terms": dedup_generic_terms,
        "include_terms": dedup_include_terms,
        "reasons": reasons,
    }


def build_seed_keyword_package(case: dict[str, Any], *, normalized_window: dict[str, str]) -> dict[str, Any]:
    input_payload = case.get("input") or {}
    routing = infer_log_routing(case)
    return {
        "include_terms": routing["include_terms"],
        "anchor_terms": routing["anchor_terms"],
        "gate_terms": routing["gate_terms"],
        "generic_terms": routing["generic_terms"],
        "exclude_terms": [],
        "target_files": routing["target_files"],
        "preferred_files": routing["preferred_files"],
        "excluded_files": routing["excluded_files"],
        "hypotheses": [],
        "require_anchor": True,
        "time_window": {
            "start": normalized_window["start"],
            "end": normalized_window["end"],
        },
    }


def render_replay_input(case: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    lines = [
        f"# Replay Input: {case['case_id']}",
        "",
        "## Prompt",
        "",
        str((case.get("input") or {}).get("prompt_without_hints") or "").strip(),
        "",
        "## Attachments",
        "",
    ]
    if attachments:
        for item in attachments:
            path = str(item.get("path") or "").strip()
            lines.append(f"- {item['name']}: {path or 'missing'}")
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def build_replay_bundle(
    *,
    case_path: Path,
    base_dir: Path,
    ones_root: Path,
    prepare_worktree: bool = False,
    force: bool = False,
    window_before_minutes: int = 30,
    window_after_minutes: int = 30,
) -> dict[str, Any]:
    case = load_case(case_path)
    replay_dir = ensure_triage_dir(base_dir.resolve(), case["case_id"], slug=case["case_id"])
    state_path = replay_dir / "00-state.json"
    if state_path.exists() and not force:
        raise RuntimeError(f"Replay workspace already exists: {state_path}")

    ones_task_dir = find_ones_task_dir(case, ones_root.resolve())
    ones_result, messages_payload, snapshot = load_ones_artifacts(ones_task_dir)
    attachments = resolve_case_attachments(case, messages_payload)
    log_bundle = select_log_bundle(attachments)

    runtime_context = (
        projects_mod.prepare_project_runtime_context(case["input"]["project"], ones_result=ones_result)
        if prepare_worktree
        else projects_mod.resolve_project_runtime_context(case["input"]["project"], ones_result=ones_result)
    )
    runtime_payload = runtime_context.to_payload() if runtime_context else {}
    project_name = str(case["input"]["project"] or "")
    if runtime_payload:
        runtime_payload.setdefault("name", project_name)
        runtime_payload.setdefault("source_path", runtime_payload.get("project_path", ""))
        runtime_payload.setdefault("worktree_path", runtime_payload.get("execution_path", ""))
    project_workspace = {
        "schema_version": "1.0",
        "workspace_scope": "replay",
        "active_project": project_name,
        "project_order": [project_name] if project_name else [],
        "projects": {project_name: runtime_payload} if project_name and runtime_payload else {},
    }

    normalized_window = build_search_window(
        project_name=str(case["input"]["project"] or ""),
        problem_time_local=str(case["input"]["problem_time_local"] or ""),
        problem_timezone=str(case["input"]["problem_timezone"] or ""),
        before_minutes=window_before_minutes,
        after_minutes=window_after_minutes,
    )
    log_routing = infer_log_routing(case)
    seed_keyword_package = build_seed_keyword_package(case, normalized_window=normalized_window)

    state = build_state(
        project=str(case["input"]["project"] or ""),
        topic=str(case["title"] or ""),
        version=str(case["input"].get("version") or ""),
        problem_time=str(case["input"].get("problem_time_local") or ""),
        timezone=str(case["input"].get("problem_timezone") or ""),
        artifact_status="complete" if log_bundle and log_bundle.get("exists") else "partial",
        issue_type=str((case.get("expected_progression") or {}).get("issue_type") or ""),
        vehicle_name=str(case["input"].get("vehicle_name") or ""),
        primary_question=build_primary_question(case),
    )
    state["work_dir"] = str(replay_dir)
    state["project_workspace"] = project_workspace
    state["current_question"] = "问题时间点车辆处于什么状态、卡在哪个流程门禁"
    state["time_alignment"]["normalized_problem_time"] = normalized_window["problem_time_log"]
    state["time_alignment"]["log_timezone"] = "UTC+0" if str(case["input"]["project"]).strip().lower() in {"allspark", "riot3"} else str(case["input"]["problem_timezone"] or "")
    state["time_alignment"]["normalized_window"] = {
        "start": normalized_window["start"],
        "end": normalized_window["end"],
    }
    state["missing_items"] = ["order_id"] if state["evidence_anchor"]["order_id"] == "" else []
    save_state(state_path, state)

    keyword_package_path = replay_dir / "keyword_package.round1.json"
    dump_json(keyword_package_path, seed_keyword_package)

    replay_input_path = replay_dir / "replay_input.md"
    replay_input_path.write_text(render_replay_input(case, attachments), encoding="utf-8")

    manifest = {
        "case_id": case["case_id"],
        "case_file": str(case_path.resolve()),
        "replay_dir": str(replay_dir),
        "state_path": str(state_path),
        "replay_input_md": str(replay_input_path),
        "keyword_package_round1": str(keyword_package_path),
        "prompt_without_hints": str(case["input"].get("prompt_without_hints") or ""),
        "ones": {
            "task_dir": str(ones_task_dir) if ones_task_dir else "",
            "task_json": str(ones_task_dir / "task.json") if ones_task_dir else "",
            "messages_json": str(ones_task_dir / "messages.json") if ones_task_dir else "",
            "summary_snapshot_loaded": bool(snapshot),
        },
        "attachments": attachments,
        "log_bundle": log_bundle or {},
        "search_root": str(Path(log_bundle["path"]).resolve().parent) if log_bundle and log_bundle.get("path") else "",
        "runtime_context": runtime_payload,
        "project_workspace": project_workspace,
        "normalized_window": normalized_window,
        "log_routing": log_routing,
        "expected_progression": dict(case.get("expected_progression") or {}),
        "expected_convergence": dict(case.get("expected_convergence") or {}),
        "acceptance": list(case.get("acceptance") or []),
    }
    manifest_path = replay_dir / "replay_manifest.json"
    dump_json(manifest_path, manifest)
    manifest["replay_manifest"] = str(manifest_path)
    return manifest


def main() -> int:
    args = parse_args()
    manifest = build_replay_bundle(
        case_path=Path(args.case_file).resolve(),
        base_dir=Path(args.base_dir),
        ones_root=Path(args.ones_root),
        prepare_worktree=args.prepare_worktree,
        force=args.force,
        window_before_minutes=args.window_before_minutes,
        window_after_minutes=args.window_after_minutes,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
