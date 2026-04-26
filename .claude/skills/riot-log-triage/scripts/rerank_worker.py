#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from triage_state import load_state, save_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use a model to rerank coarse search hits and keep only high-value evidence.",
    )
    parser.add_argument("--search-results", required=True, help="Path to search_results.json from search_worker.")
    parser.add_argument("--state", required=True, help="Path to 00-state.json.")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for rerank_results.json and rerank_summary.md. Defaults to the search result directory.",
    )
    parser.add_argument("--max-kept-hits", type=int, default=20, help="Maximum high-value hits to keep after rerank.")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_json_payload(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Empty rerank model output.")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        return json.loads(cleaned[brace_start:brace_end + 1])
    raise ValueError("Could not extract JSON from rerank model output.")


def build_hit_items(search_results: dict[str, Any]) -> list[dict[str, Any]]:
    hits = list(search_results.get("evidence_hits") or [])
    items: list[dict[str, Any]] = []
    for index, hit in enumerate(hits, start=1):
        items.append({
            "id": f"h{index}",
            "path": hit.get("path", ""),
            "line_number": int(hit.get("line_number") or 0),
            "timestamp": str(hit.get("timestamp") or ""),
            "matched_terms": list(hit.get("matched_terms") or []),
            "matched_line": str(hit.get("matched_line") or ""),
            "excerpt": str(hit.get("excerpt") or ""),
            "match_reason": str(hit.get("match_reason") or ""),
            "merged_summary": str(hit.get("merged_summary") or ""),
            "template_identity": dict(hit.get("template_identity") or {}),
            "template_merge": compact_template_merge(hit.get("template_merge") or {}),
        })
    return items


def compact_template_merge(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    hit_count = int(raw.get("hit_count") or 1)
    if hit_count <= 1:
        return {}
    return {
        "hit_count": hit_count,
        "first_timestamp": str(raw.get("first_timestamp") or ""),
        "last_timestamp": str(raw.get("last_timestamp") or ""),
        "first_location": str(raw.get("first_location") or ""),
        "last_location": str(raw.get("last_location") or ""),
        "change_facts": list(raw.get("change_facts") or []),
    }


def build_prompt(*, state: dict[str, Any], search_results: dict[str, Any], hit_items: list[dict[str, Any]], max_kept_hits: int) -> str:
    evidence_anchor = dict(state.get("evidence_anchor") or {})
    incident_snapshot = dict(state.get("incident_snapshot") or {})
    order_candidates = list(search_results.get("order_candidates") or [])
    compact_hits = [
        {
            "id": item["id"],
            "path": item["path"],
            "line_number": item["line_number"],
            "timestamp": item["timestamp"],
            "matched_terms": item["matched_terms"],
            "matched_line": item["matched_line"],
            "excerpt": item["excerpt"],
            "match_reason": item["match_reason"],
            "merged_summary": item.get("merged_summary") or "",
            "template_identity": item.get("template_identity") or {},
            "template_merge": item.get("template_merge") or {},
        }
        for item in hit_items
    ]
    hit_id_by_location = {
        (item["path"], item["line_number"]): item["id"]
        for item in hit_items
    }
    compact_timeline = []
    for hit in list(search_results.get("timeline_hits") or []):
        hit_id = hit_id_by_location.get((hit.get("path", ""), int(hit.get("line_number") or 0)), "")
        compact_timeline.append({
            "id": hit_id,
            "path": hit.get("path", ""),
            "line_number": int(hit.get("line_number") or 0),
            "timestamp": str(hit.get("timestamp") or ""),
            "matched_terms": list(hit.get("matched_terms") or []),
            "matched_line": str(hit.get("matched_line") or ""),
            "score": int(hit.get("score") or 0),
            "merged_summary": str(hit.get("merged_summary") or ""),
            "template_identity": dict(hit.get("template_identity") or {}),
            "template_merge": compact_template_merge(hit.get("template_merge") or {}),
        })
    payload = {
        "focus_question": state.get("current_question") or state.get("primary_question") or state.get("problem_summary") or "",
        "primary_question": state.get("primary_question") or "",
        "project": state.get("project") or "",
        "version": dict(state.get("version_info") or {}).get("value", ""),
        "time_alignment": dict(state.get("time_alignment") or {}),
        "evidence_anchor": evidence_anchor,
        "incident_snapshot": incident_snapshot,
        "search_overview": {
            "search_root": search_results.get("search_root", ""),
            "matched_terms": list(search_results.get("matched_terms") or []),
            "unmatched_terms": list(search_results.get("unmatched_terms") or []),
            "order_candidates": order_candidates,
            "suppressed_hits_total": int(search_results.get("suppressed_hits_total") or 0),
        },
        "hits": compact_hits,
        "timeline_hits": compact_timeline,
    }
    prompt = (
        "你在做 RIOT 日志排障的二次去噪。给定当前 focus question、state 和粗筛命中后，"
        "只保留真正有价值、最能帮助回答主问题的命中。不要改写原始日志内容，只能按 hit id 选择、排序、归类；"
        "分析结论必须先满足时间顺序，不能只按相关性高低倒推故事。\n\n"
        "规则：\n"
        "1. 优先保留能直接回答“问题时间点车辆处于什么状态、处于什么流程、为什么后续动作没有继续下发”的 hit。\n"
        "2. 优先保留带车辆锚点、订单候选、请求发送/完成、stage、状态变化、门禁判断的 hit。\n"
        "3. 充电、停车、周期监控、重复轮询、无关车辆、纯泛词 hang/fail 但没有直接主问题价值的 hit 视为噪音。\n"
        "4. 必须按 timeline_hits 复盘事件顺序：先发生、后发生、缺口、不确定点分别说明；没有时间戳的证据只能辅助，不能覆盖有时间戳证据。\n"
        "5. 必须先读懂执行链路再分析过程：入口动作 -> 状态写入 -> 请求下发/回调 -> 状态读取/门禁 -> 下一步动作或提前返回。\n"
        "6. 读取代码时先锁定日志对应类/方法/异常/错误码，再推导下一步；不要用未命中日志的代码路径替代现场证据。\n"
        "7. 如果证据指向具体类、方法、异常类、错误码、状态/阶段词或关键日志文案，把它们放入 core_terms / exception_terms / stage_terms / log_message_terms，并按后续搜索价值给 term_priorities 打分。\n"
        "8. 如果 hit 带 template_merge，说明同一车辆 + 类名 + 源码行模板已合并；优先读 hit_count、时间范围和 change_facts，不要把同一模板的周期性变化当成多条独立强证据。\n"
        "9. 只做精简，不改原始 hit 内容。\n"
        f"10. relevant_hit_ids 最多返回 {max_kept_hits} 个。\n\n"
        "返回严格 JSON：\n"
        "{\n"
        '  "summary": "...",\n'
        '  "execution_chain_summary": ["入口->状态->请求/回调->门禁->下一步/提前返回..."],\n'
        '  "execution_chain_gaps": ["链路缺口..."],\n'
        '  "timeline_summary": ["先后顺序事实..."],\n'
        '  "temporal_gaps": ["缺失或冲突的时间点..."],\n'
        '  "relevant_hit_ids": ["h1"],\n'
        '  "noise_hit_ids": ["h2"],\n'
        '  "noise_patterns": ["..."],\n'
        '  "suspected_process_stage": "...",\n'
        '  "candidate_order_ids": ["358208"],\n'
        '  "next_focus_question": "...",\n'
        '  "next_keyword_adjustments": {\n'
        '    "keep_terms": ["..."],\n'
        '    "drop_terms": ["..."],\n'
        '    "add_terms": ["..."],\n'
        '    "core_terms": ["CrossMapManager"],\n'
        '    "exception_terms": ["ReservationConflictException"],\n'
        '    "stage_terms": ["IN_CHANGE_MAP"],\n'
        '    "log_message_terms": ["车辆执行状态不符合"],\n'
        '    "term_priorities": [{"term": "CrossMapManager", "score": 18, "category": "core", "reason": "..."}],\n'
        '    "target_files": ["..."]\n'
        "  },\n"
        '  "confidence": "low|medium|high"\n'
        "}\n\n"
        f"输入数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    return prompt


async def request_rerank_decision(prompt: str) -> dict[str, Any]:
    from core.orchestrator.agent_client import agent_client

    result = await agent_client.run(
        prompt=prompt,
        skill="analysis",
        max_turns=3,
    )
    return extract_json_payload(result.get("text", ""))


def write_summary(output_path: Path, rerank_results: dict[str, Any]) -> None:
    lines = [
        "# Rerank Summary",
        "",
        f"- Summary: {rerank_results.get('summary') or ''}",
        f"- Confidence: {rerank_results.get('confidence') or ''}",
        f"- Suspected process stage: {rerank_results.get('suspected_process_stage') or ''}",
        f"- Candidate order ids: {', '.join(rerank_results.get('candidate_order_ids') or []) or 'none'}",
        "",
        "## Relevant Hits",
        "",
    ]
    relevant_hits = list(rerank_results.get("relevant_hits") or [])
    if relevant_hits:
        for hit in relevant_hits:
            lines.append(f"- `{hit['id']}` {hit['path']}:{hit['line_number']}")
    else:
        lines.append("- none")

    execution_chain_summary = list(rerank_results.get("execution_chain_summary") or [])
    if execution_chain_summary:
        lines.extend(["", "## Execution Chain Summary", ""])
        for item in execution_chain_summary:
            lines.append(f"- {item}")

    execution_chain_gaps = list(rerank_results.get("execution_chain_gaps") or [])
    if execution_chain_gaps:
        lines.extend(["", "## Execution Chain Gaps", ""])
        for item in execution_chain_gaps:
            lines.append(f"- {item}")

    timeline_summary = list(rerank_results.get("timeline_summary") or [])
    if timeline_summary:
        lines.extend(["", "## Timeline Summary", ""])
        for item in timeline_summary:
            lines.append(f"- {item}")

    temporal_gaps = list(rerank_results.get("temporal_gaps") or [])
    if temporal_gaps:
        lines.extend(["", "## Temporal Gaps", ""])
        for item in temporal_gaps:
            lines.append(f"- {item}")

    noise_patterns = list(rerank_results.get("noise_patterns") or [])
    if noise_patterns:
        lines.extend(["", "## Noise Patterns", ""])
        for item in noise_patterns:
            lines.append(f"- {item}")

    keyword_adjustments = dict(rerank_results.get("next_keyword_adjustments") or {})
    if keyword_adjustments:
        lines.extend(["", "## Next Keyword Adjustments", ""])
        for key in ("keep_terms", "drop_terms", "add_terms", "core_terms", "exception_terms", "stage_terms", "log_message_terms", "target_files"):
            values = list(keyword_adjustments.get(key) or [])
            lines.append(f"- {key}: {', '.join(values) if values else 'none'}")
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def update_state_after_rerank(*, state_path: Path, rerank_json: Path, rerank_md: Path, rerank_results: dict[str, Any]) -> None:
    state = load_state(state_path)
    search_artifacts = dict(state.get("search_artifacts") or {})
    search_artifacts["last_rerank_json"] = str(rerank_json)
    search_artifacts["last_rerank_md"] = str(rerank_md)
    state["search_artifacts"] = search_artifacts
    if rerank_results.get("candidate_order_ids"):
        state["order_candidates"] = [
            {"order_id": order_id}
            for order_id in rerank_results["candidate_order_ids"]
        ]
    if rerank_results.get("next_focus_question"):
        state["current_question"] = rerank_results["next_focus_question"]
    if rerank_results.get("noise_patterns"):
        state["noise_candidates"] = list(rerank_results["noise_patterns"])

    history = list(dict(state.get("narrowing_round") or {}).get("history") or [])
    if history:
        history[-1]["rerank"] = {
            "summary": rerank_results.get("summary", ""),
            "execution_chain_summary": list(rerank_results.get("execution_chain_summary") or []),
            "execution_chain_gaps": list(rerank_results.get("execution_chain_gaps") or []),
            "timeline_summary": list(rerank_results.get("timeline_summary") or []),
            "temporal_gaps": list(rerank_results.get("temporal_gaps") or []),
            "confidence": rerank_results.get("confidence", ""),
            "relevant_hit_ids": list(rerank_results.get("relevant_hit_ids") or []),
            "noise_hit_ids": list(rerank_results.get("noise_hit_ids") or []),
            "candidate_order_ids": list(rerank_results.get("candidate_order_ids") or []),
        }
        state["narrowing_round"]["history"] = history
    save_state(state_path, state)


async def rerank_search_results(*, search_results_path: Path, state_path: Path, output_dir: Path, max_kept_hits: int) -> dict[str, Any]:
    search_results = load_json(search_results_path)
    state = load_state(state_path)
    hit_items = build_hit_items(search_results)
    prompt = build_prompt(
        state=state,
        search_results=search_results,
        hit_items=hit_items,
        max_kept_hits=max_kept_hits,
    )
    decision = await request_rerank_decision(prompt)
    hit_map = {item["id"]: item for item in hit_items}
    relevant_ids = [hit_id for hit_id in decision.get("relevant_hit_ids", []) if hit_id in hit_map]
    noise_ids = [hit_id for hit_id in decision.get("noise_hit_ids", []) if hit_id in hit_map]
    rerank_results = {
        "summary": str(decision.get("summary") or "").strip(),
        "execution_chain_summary": [str(item).strip() for item in decision.get("execution_chain_summary", []) if str(item).strip()],
        "execution_chain_gaps": [str(item).strip() for item in decision.get("execution_chain_gaps", []) if str(item).strip()],
        "timeline_summary": [str(item).strip() for item in decision.get("timeline_summary", []) if str(item).strip()],
        "temporal_gaps": [str(item).strip() for item in decision.get("temporal_gaps", []) if str(item).strip()],
        "confidence": str(decision.get("confidence") or "").strip(),
        "suspected_process_stage": str(decision.get("suspected_process_stage") or "").strip(),
        "candidate_order_ids": [str(item).strip() for item in decision.get("candidate_order_ids", []) if str(item).strip()],
        "next_focus_question": str(decision.get("next_focus_question") or "").strip(),
        "next_keyword_adjustments": dict(decision.get("next_keyword_adjustments") or {}),
        "noise_patterns": [str(item).strip() for item in decision.get("noise_patterns", []) if str(item).strip()],
        "relevant_hit_ids": relevant_ids,
        "noise_hit_ids": noise_ids,
        "relevant_hits": [hit_map[hit_id] for hit_id in relevant_ids],
        "noise_hits": [hit_map[hit_id] for hit_id in noise_ids],
        "source_search_results": str(search_results_path),
        "source_state": str(state_path),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    rerank_json = output_dir / "rerank_results.json"
    rerank_md = output_dir / "rerank_summary.md"
    rerank_json.write_text(json.dumps(rerank_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_summary(rerank_md, rerank_results)
    update_state_after_rerank(
        state_path=state_path,
        rerank_json=rerank_json,
        rerank_md=rerank_md,
        rerank_results=rerank_results,
    )
    return {
        "rerank_json": str(rerank_json),
        "rerank_md": str(rerank_md),
        "relevant_hits": len(relevant_ids),
        "noise_hits": len(noise_ids),
        "candidate_order_ids": rerank_results["candidate_order_ids"],
    }


async def main_async() -> int:
    args = parse_args()
    search_results_path = Path(args.search_results).resolve()
    state_path = Path(args.state).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else search_results_path.parent
    payload = await rerank_search_results(
        search_results_path=search_results_path,
        state_path=state_path,
        output_dir=output_dir,
        max_kept_hits=args.max_kept_hits,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
