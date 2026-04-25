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
    target_files = dedupe([str(item).strip() for item in keyword_adjustments.get("target_files") or []]) or previous_target_files
    preferred_files = dedupe(target_files[:1] if target_files else [])

    gate_terms = dedupe([
        *[term for term in keep_terms if term not in anchor_terms],
        *[term for term in add_terms if term not in anchor_terms],
    ])
    package = {
        "anchor_terms": anchor_terms,
        "gate_terms": gate_terms,
        "generic_terms": [],
        "include_terms": dedupe([*anchor_terms, *gate_terms]),
        "exclude_terms": [],
        "target_files": target_files,
        "preferred_files": preferred_files,
        "excluded_files": drop_terms,
        "hypotheses": [],
        "require_anchor": True,
        "time_window": previous_window,
        "dsl_query": build_dsl(
            anchor_terms=anchor_terms,
            keep_terms=keep_terms,
            add_terms=add_terms,
            drop_terms=drop_terms,
        ),
    }
    return package


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
