#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from triage_state import load_state, save_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the next-round keyword package and DSL query from rerank output.",
    )
    parser.add_argument("--rerank-results", required=True, help="Path to rerank_results.json.")
    parser.add_argument("--state", required=True, help="Path to 00-state.json.")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for keyword_package.roundN.json and query.roundN.dsl.txt. Defaults to the rerank directory.",
    )
    parser.add_argument("--round", type=int, default=2, help="Next round number. Default: 2")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def build_dsl(*, anchor_terms: list[str], keep_terms: list[str], add_terms: list[str], drop_terms: list[str]) -> str:
    anchor_parts = [f'"{term}"' for term in anchor_terms]
    positive_terms = dedupe([*keep_terms, *add_terms])
    positive_parts = [f'"{term}"' for term in positive_terms if term not in anchor_terms]
    negative_parts = [f'"{term}"' for term in drop_terms]

    sections: list[str] = []
    if anchor_parts:
        if len(anchor_parts) == 1:
            sections.append(anchor_parts[0])
        else:
            sections.append("(" + " OR ".join(anchor_parts) + ")")
    if positive_parts:
        sections.append("(" + " OR ".join(positive_parts) + ")")

    query = " AND ".join(section for section in sections if section)
    if negative_parts:
        query = query + (" AND " if query else "") + " AND ".join(f"NOT {part}" for part in negative_parts)
    return query.strip()


def normalize_term_priorities(raw: Any) -> list[dict[str, Any]]:
    priorities: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for term, meta in raw.items():
            term_text = str(term or "").strip()
            if not term_text:
                continue
            if isinstance(meta, dict):
                score_raw = meta.get("score", meta.get("weight", 0))
                category = str(meta.get("category") or "").strip()
                reason = str(meta.get("reason") or "").strip()
            else:
                score_raw = meta
                category = ""
                reason = ""
            try:
                score = int(score_raw)
            except (TypeError, ValueError):
                score = 0
            priorities.append({"term": term_text, "score": score, "category": category, "reason": reason})
        return priorities
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                term_text = str(item or "").strip()
                if term_text:
                    priorities.append({"term": term_text, "score": 0, "category": "", "reason": ""})
                continue
            term_text = str(item.get("term") or item.get("name") or "").strip()
            if not term_text:
                continue
            try:
                score = int(item.get("score", item.get("weight", 0)))
            except (TypeError, ValueError):
                score = 0
            priorities.append({
                "term": term_text,
                "score": score,
                "category": str(item.get("category") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            })
    return priorities


def infer_code_terms(terms: list[str]) -> tuple[list[str], list[str]]:
    core_terms: list[str] = []
    exception_terms: list[str] = []
    core_suffixes = (
        "Manager",
        "Service",
        "Listener",
        "Controller",
        "Request",
        "Response",
        "Action",
        "Actuator",
        "Handler",
        "Processor",
        "Executor",
        "Builder",
        "Factory",
        "Resolver",
        "Strategy",
    )
    for term in terms:
        if term.endswith(("Exception", "Error")):
            exception_terms.append(term)
            continue
        if term.endswith(core_suffixes) or "." in term:
            core_terms.append(term)
    return dedupe(core_terms), dedupe(exception_terms)


def default_term_priorities(*, core_terms: list[str], exception_terms: list[str], log_message_terms: list[str], stage_terms: list[str]) -> list[dict[str, Any]]:
    priorities: list[dict[str, Any]] = []
    for term in core_terms:
        priorities.append({
            "term": term,
            "score": 14,
            "category": "core",
            "reason": "代码关键类/方法词",
        })
    for term in exception_terms:
        priorities.append({
            "term": term,
            "score": 20,
            "category": "exception",
            "reason": "代码异常/错误词",
        })
    for term in log_message_terms:
        priorities.append({
            "term": term,
            "score": 12,
            "category": "log_message",
            "reason": "代码关键日志文案",
        })
    for term in stage_terms:
        priorities.append({
            "term": term,
            "score": 10,
            "category": "stage",
            "reason": "执行链路状态/阶段/门禁词",
        })
    return priorities


def build_next_package(*, state: dict[str, Any], rerank_results: dict[str, Any]) -> dict[str, Any]:
    evidence_anchor = dict(state.get("evidence_anchor") or {})
    current_history = list(dict(state.get("narrowing_round") or {}).get("history") or [])
    last_round = current_history[-1] if current_history else {}
    previous_window = dict(last_round.get("time_window") or dict(state.get("time_alignment", {}).get("normalized_window") or {}))
    previous_target_files = list(last_round.get("target_files") or state.get("target_log_files") or [])

    keyword_adjustments = dict(rerank_results.get("next_keyword_adjustments") or {})
    anchor_terms = dedupe([
        str(evidence_anchor.get("vehicle_name") or "").strip(),
        str(evidence_anchor.get("order_id") or "").strip(),
        *[str(item.get("order_id") or "").strip() for item in (state.get("order_candidates") or [])],
        *[str(item).strip() for item in rerank_results.get("candidate_order_ids") or []],
    ])
    keep_terms = dedupe([str(item).strip() for item in keyword_adjustments.get("keep_terms") or []])
    add_terms = dedupe([str(item).strip() for item in keyword_adjustments.get("add_terms") or []])
    drop_terms = dedupe([str(item).strip() for item in keyword_adjustments.get("drop_terms") or []])
    log_message_terms = dedupe([str(item).strip() for item in keyword_adjustments.get("log_message_terms") or []])
    stage_terms = dedupe([str(item).strip() for item in keyword_adjustments.get("stage_terms") or []])
    explicit_core_terms = dedupe([
        *[str(item).strip() for item in keyword_adjustments.get("core_terms") or []],
        *[str(item).strip() for item in keyword_adjustments.get("class_terms") or []],
        *[str(item).strip() for item in keyword_adjustments.get("method_terms") or []],
    ])
    explicit_exception_terms = dedupe([str(item).strip() for item in keyword_adjustments.get("exception_terms") or []])
    inferred_core_terms, inferred_exception_terms = infer_code_terms([*keep_terms, *add_terms])
    core_terms = dedupe([*explicit_core_terms, *inferred_core_terms])
    exception_terms = dedupe([*explicit_exception_terms, *inferred_exception_terms])
    term_priorities = dedupe_term_priorities([
        *normalize_term_priorities(keyword_adjustments.get("term_priorities") or []),
        *default_term_priorities(
            core_terms=core_terms,
            exception_terms=exception_terms,
            log_message_terms=log_message_terms,
            stage_terms=stage_terms,
        ),
    ])
    target_files = dedupe([str(item).strip() for item in keyword_adjustments.get("target_files") or []]) or previous_target_files
    preferred_files = dedupe(target_files[:1] if target_files else [])

    gate_terms = dedupe([
        *[term for term in keep_terms if term not in anchor_terms],
        *[term for term in add_terms if term not in anchor_terms],
        *[term for term in log_message_terms if term not in anchor_terms],
        *[term for term in stage_terms if term not in anchor_terms],
    ])
    positive_terms = dedupe([*gate_terms, *core_terms, *exception_terms])
    package = {
        "anchor_terms": anchor_terms,
        "gate_terms": gate_terms,
        "log_message_terms": log_message_terms,
        "stage_terms": stage_terms,
        "core_terms": core_terms,
        "exception_terms": exception_terms,
        "generic_terms": [],
        "include_terms": dedupe([*anchor_terms, *positive_terms]),
        "exclude_terms": [],
        "target_files": target_files,
        "preferred_files": preferred_files,
        "excluded_files": drop_terms,
        "term_priorities": term_priorities,
        "hypotheses": [],
        "require_anchor": True,
        "time_window": previous_window,
        "dsl_query": build_dsl(
            anchor_terms=anchor_terms,
            keep_terms=dedupe([*keep_terms, *core_terms, *exception_terms]),
            add_terms=add_terms,
            drop_terms=drop_terms,
        ),
    }
    return package


def dedupe_term_priorities(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_term: dict[str, dict[str, Any]] = {}
    for item in values:
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        normalized = {
            "term": term,
            "score": score,
            "category": str(item.get("category") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
        }
        current = by_term.get(term)
        if current is None or score > int(current.get("score") or 0):
            by_term[term] = normalized
    return sorted(by_term.values(), key=lambda item: int(item.get("score") or 0), reverse=True)


def update_state_after_next_round(*, state_path: Path, package_json: Path, dsl_txt: Path, rerank_results: dict[str, Any], package: dict[str, Any], round_no: int) -> None:
    state = load_state(state_path)
    search_artifacts = dict(state.get("search_artifacts") or {})
    search_artifacts[f"keyword_package_round{round_no}"] = str(package_json)
    search_artifacts[f"dsl_round{round_no}"] = str(dsl_txt)
    state["search_artifacts"] = search_artifacts
    state["keyword_package_status"] = "revised"
    if rerank_results.get("next_focus_question"):
        state["current_question"] = str(rerank_results["next_focus_question"]).strip()

    history = list(dict(state.get("narrowing_round") or {}).get("history") or [])
    if history:
        history[-1]["next_round"] = {
            "round": round_no,
            "keyword_package": str(package_json),
            "dsl_query": package.get("dsl_query", ""),
            "target_files": list(package.get("target_files") or []),
            "drop_terms": list(package.get("excluded_files") or []),
            "log_message_terms": list(package.get("log_message_terms") or []),
            "stage_terms": list(package.get("stage_terms") or []),
            "core_terms": list(package.get("core_terms") or []),
            "exception_terms": list(package.get("exception_terms") or []),
            "term_priorities": list(package.get("term_priorities") or []),
        }
        state["narrowing_round"]["history"] = history
    save_state(state_path, state)


def build_next_round(*, rerank_results_path: Path, state_path: Path, output_dir: Path, round_no: int) -> dict[str, Any]:
    rerank_results = load_json(rerank_results_path)
    state = load_state(state_path)
    package = build_next_package(state=state, rerank_results=rerank_results)

    output_dir.mkdir(parents=True, exist_ok=True)
    package_json = output_dir / f"keyword_package.round{round_no}.json"
    dsl_txt = output_dir / f"query.round{round_no}.dsl.txt"
    dump_json(package_json, package)
    dsl_txt.write_text(package.get("dsl_query", "").strip() + "\n", encoding="utf-8")
    update_state_after_next_round(
        state_path=state_path,
        package_json=package_json,
        dsl_txt=dsl_txt,
        rerank_results=rerank_results,
        package=package,
        round_no=round_no,
    )
    return {
        "keyword_package": str(package_json),
        "dsl_query_file": str(dsl_txt),
        "round": round_no,
        "dsl_query": package.get("dsl_query", ""),
        "target_files": package.get("target_files", []),
        "excluded_files": package.get("excluded_files", []),
    }


def main() -> int:
    args = parse_args()
    rerank_results_path = Path(args.rerank_results).resolve()
    state_path = Path(args.state).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else rerank_results_path.parent
    payload = build_next_round(
        rerank_results_path=rerank_results_path,
        state_path=state_path,
        output_dir=output_dir,
        round_no=args.round,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
