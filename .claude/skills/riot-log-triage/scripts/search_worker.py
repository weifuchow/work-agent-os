#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, UTC
import fnmatch
import gzip
import heapq
import io
import json
from pathlib import Path
import re
import tarfile
from typing import Any, Iterator
import zipfile

from triage_state import load_state, save_state


TIMESTAMP_PATTERNS = [
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{3,6})?(?:Z|[+-]\d{2}:\d{2})?)"),
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"),
]
ORDER_CANDIDATE_PATTERNS = [
    ("vehicle_pipe", re.compile(r"(?P<vehicle>[A-Za-z0-9_-]+)\|(?P<order>\d{5,12})[.|:]")),
    ("order_field", re.compile(r"\border(?:Key|Id|No)?\b\s*[:=]\s*['\"]?(?P<order>\d{5,12})['\"]?", re.IGNORECASE)),
    ("json_order_field", re.compile(r'"order(?:Key|Id|No)?"\s*:\s*"?(?P<order>\d{5,12})"?', re.IGNORECASE)),
]
TEXT_EXTENSIONS = {
    ".log",
    ".txt",
    ".out",
    ".err",
    ".trace",
    ".json",
    ".jsonl",
    ".csv",
    ".yaml",
    ".yml",
    ".xml",
    ".properties",
}
GZIP_MAGIC = b"\x1f\x8b"
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
DEFAULT_FILE_PRIORITIES = [
    ("bootstrap", 40),
    ("reservation", 34),
    ("mini_trace", 28),
    ("mapf", 22),
    ("notify", 12),
    ("metric", -30),
]
MAX_CANDIDATE_MULTIPLIER = 25
DEFAULT_NON_ANCHOR_SAMPLE_LIMIT_PER_FILE = 200
DEFAULT_MIN_TEMPLATE_MERGE_HITS = 5
MAX_TEMPLATE_SAMPLE_LINES = 5
MAX_TEMPLATE_VALUE_SAMPLES = 60

LOG_SOURCE_RE = re.compile(r"\s(?P<class_name>[A-Za-z_$][\w.$]*)\s+line:(?P<source_line>\d+)\s+-")
VEHICLE_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\d{3,6})(?![A-Za-z0-9])")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
LONG_NUMBER_RE = re.compile(r"\b\d{5,}\b")
REQUEST_TYPE_RE = re.compile(r"\b(?P<request_type>[A-Za-z][A-Za-z0-9]*Request)\{")
TRACE_ID_RE = re.compile(r"\btraceId[=:](?P<trace_id>[^,\]\)\s}]+)")
PIPE_TRACE_RE = re.compile(r"(?P<trace_id>[A-Za-z0-9_-]+\|\d{5,12}\.[A-Za-z0-9_-]+)")
ORDER_ID_RE = re.compile(r"\borderId[=:](?P<order_id>\d{5,12})", re.IGNORECASE)
DISPATCH_ORDER_RE = re.compile(r"\|(?P<dispatch_order>\d{5,12})(?:[.:])")
CHECKPOINT_RE = re.compile(r"\bcurCheckPointNo is (?P<checkpoint_no>\d+)")
FSM_STATE_RE = re.compile(r"\bfsmState[:=](?P<fsm_state>[A-Za-z_]+)")
FSM_SHORT_RE = re.compile(r"\bfsm[:=](?P<fsm>[A-Za-z_]+)")
PROC_STATE_RE = re.compile(r"\bvehicleProcState[=:](?P<vehicle_proc_state>[A-Za-z_]+)")
CROSS_MAP_MISMATCH_RE = re.compile(
    r"车辆执行状态不符合[：:](?P<actual_proc_state>[A-Za-z_]+)\s+pre[：:](?P<pre_order>\d{5,12})[，,]now:(?P<now_order>\d{5,12})"
)
MAP_STEP_RE = re.compile(r"\b(?P<map_field>startMapId|endMapId)=(?P<map_id>\d+)")
TASK_STATE_RE = re.compile(r"\bnextState (?P<next_state>[A-Za-z_]+) curState (?P<cur_state>[A-Za-z_]+)")
TASK_KEY_RE = re.compile(r"\b(?:Task|taskKey)\[(?P<task_key>[^\]]+)\]")
STAGE_RE = re.compile(r"\bstage:\s*(?P<stage>[A-Za-z_]+)")
TASK_FIELD_RE = re.compile(r"\btaskKey\s*[:=]\s*['\"]?(?P<task_key>\d{5,12})['\"]?", re.IGNORECASE)
DEVICE_NAME_RE = re.compile(r"\bdeviceName[:=]\[?(?P<device_name>[^\s,\]}:]+)\]?")
RESOURCE_ID_RE = re.compile(r"\bresourceId[:=](?P<resource_id>[^\s,\]}]+)")
RESOURCE_RE = re.compile(r"\bresource[:=](?P<resource>[^\s,\]}]+)")
STATION_RE = re.compile(r"\bstation[:=](?P<station>[^\s,\]}]+)")
ELEVATOR_NAME_RE = re.compile(r"\s-\s*(?P<elevator_name>[^:\s]+电梯):")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search RIOT logs using a compact keyword package and output summarized evidence only.",
    )
    parser.add_argument("--search-root", required=True, help="Log directory or exported archive root to search.")
    parser.add_argument(
        "--keyword-package-file",
        default="",
        help="Path to a JSON file containing include/exclude terms, target files, and time window.",
    )
    parser.add_argument(
        "--keyword-package-json",
        default="",
        help="Inline JSON string for the keyword package. Used when no file is provided.",
    )
    parser.add_argument("--state", default="", help="Optional path to 00-state.json to update.")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory where search_results.json and evidence_summary.md are written. "
             "Defaults to <state_dir>/search-runs/<timestamp> or <search_root>/search-runs/<timestamp>.",
    )
    parser.add_argument("--max-hits", type=int, default=100, help="Maximum evidence hits to keep.")
    parser.add_argument(
        "--context-lines",
        type=int,
        default=1,
        help="How many lines before and after a hit are included in the excerpt.",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=500,
        help="Safety limit for how many text documents are scanned.",
    )
    return parser.parse_args()


def utc_now_label() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def load_keyword_package(args: argparse.Namespace) -> dict[str, Any]:
    if args.keyword_package_file:
        return json.loads(Path(args.keyword_package_file).read_text(encoding="utf-8"))
    if args.keyword_package_json:
        return json.loads(args.keyword_package_json)
    raise SystemExit("Provide --keyword-package-file or --keyword-package-json.")


def clean_terms(raw: Any, *, allow_sentence_question: bool = False) -> list[str]:
    values = raw if isinstance(raw, list) else []
    return dedupe_terms([
        str(item).strip()
        for item in values
        if is_usable_term(str(item).strip(), allow_sentence_question=allow_sentence_question)
    ])


def is_usable_term(term: str, *, allow_sentence_question: bool = False) -> bool:
    if not term:
        return False
    compact = re.sub(r"\s+", "", term)
    if not compact:
        return False
    if not re.search(r"[A-Za-z0-9_\-\u4e00-\u9fff]", compact):
        return False

    placeholder_count = sum(1 for char in compact if char in {"?", "？", "\ufffd"})
    if placeholder_count == 0:
        return True
    if allow_sentence_question and placeholder_count == 1 and compact[-1] in {"?", "？"}:
        return True
    if placeholder_count >= 2 or placeholder_count / max(len(compact), 1) > 0.2:
        return False
    return False


def normalize_package(raw: dict[str, Any]) -> dict[str, Any]:
    anchor_terms = clean_terms(raw.get("anchor_terms", []))
    gate_terms = clean_terms(raw.get("gate_terms", []))
    log_message_terms = clean_terms(raw.get("log_message_terms", []))
    stage_terms = clean_terms(raw.get("stage_terms", []))
    gate_terms = dedupe_terms([*gate_terms, *log_message_terms, *stage_terms])
    class_terms = clean_terms(raw.get("class_terms", []))
    method_terms = clean_terms(raw.get("method_terms", []))
    explicit_core_terms = clean_terms(raw.get("core_terms", []))
    core_terms = dedupe_terms([*explicit_core_terms, *class_terms, *method_terms])
    exception_terms = clean_terms(raw.get("exception_terms", []))
    generic_terms = clean_terms(raw.get("generic_terms", []))
    include_terms = clean_terms(raw.get("include_terms", []))
    require_all_terms = bool(raw.get("require_all_terms", False))
    categorized_include_terms = [*anchor_terms, *gate_terms, *core_terms, *exception_terms, *generic_terms]
    if not include_terms and categorized_include_terms:
        include_terms = categorized_include_terms
    elif not require_all_terms:
        include_terms = dedupe_terms([*include_terms, *core_terms, *exception_terms])
    exclude_terms = clean_terms(raw.get("exclude_terms", []))
    target_files = [str(item).strip() for item in raw.get("target_files", []) if str(item).strip()]
    preferred_files = [str(item).strip() for item in raw.get("preferred_files", []) if str(item).strip()]
    excluded_files = [str(item).strip() for item in raw.get("excluded_files", []) if str(item).strip()]
    hypotheses = clean_terms(raw.get("hypotheses", []), allow_sentence_question=True)
    file_priorities = normalize_file_priorities(raw.get("file_priorities", raw.get("file_priority", [])))
    term_priorities = normalize_term_priorities(raw.get("term_priorities", raw.get("term_priority", [])))
    require_anchor = bool(raw.get("require_anchor", False))
    anchor_match_mode = str(raw.get("anchor_match_mode") or "").strip().lower()
    focus_terms = [*gate_terms, *core_terms, *exception_terms]
    try:
        min_template_merge_hits = int(raw.get("min_template_merge_hits") or DEFAULT_MIN_TEMPLATE_MERGE_HITS)
    except (TypeError, ValueError):
        min_template_merge_hits = DEFAULT_MIN_TEMPLATE_MERGE_HITS
    min_template_merge_hits = max(2, min_template_merge_hits)
    normalized = {
        "anchor_terms": anchor_terms,
        "gate_terms": gate_terms,
        "log_message_terms": log_message_terms,
        "stage_terms": stage_terms,
        "core_terms": core_terms,
        "class_terms": class_terms,
        "method_terms": method_terms,
        "exception_terms": exception_terms,
        "generic_terms": generic_terms,
        "include_terms": include_terms,
        "exclude_terms": exclude_terms,
        "target_files": target_files,
        "preferred_files": preferred_files,
        "excluded_files": excluded_files,
        "file_priorities": file_priorities,
        "term_priorities": term_priorities,
        "hypotheses": hypotheses,
        "anchor_match_mode": anchor_match_mode,
        "require_all_terms": require_all_terms,
        "require_anchor": require_anchor,
        "require_gate_when_present": bool(
            raw.get("require_gate_when_present", require_anchor and bool(focus_terms))
        ),
        "min_template_merge_hits": min_template_merge_hits,
        "time_window": normalize_time_window(raw.get("time_window") or {}),
        "dsl_query": str(raw.get("dsl_query") or "").strip(),
    }
    return normalized


def normalize_file_priorities(raw: Any) -> list[dict[str, Any]]:
    priorities: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        items = raw.items()
        for pattern, score in items:
            pattern_text = str(pattern or "").strip()
            if not pattern_text:
                continue
            try:
                score_value = int(score)
            except (TypeError, ValueError):
                score_value = 0
            priorities.append({"pattern": pattern_text, "score": score_value})
        return priorities
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, dict):
                pattern_text = str(item.get("pattern") or item.get("file") or item.get("name") or "").strip()
                if not pattern_text:
                    continue
                try:
                    score_value = int(item.get("score", item.get("weight", max(50 - index * 5, 5))))
                except (TypeError, ValueError):
                    score_value = max(50 - index * 5, 5)
                priorities.append({"pattern": pattern_text, "score": score_value})
                continue
            pattern_text = str(item or "").strip()
            if pattern_text:
                priorities.append({"pattern": pattern_text, "score": max(50 - index * 5, 5)})
    return priorities


def normalize_term_priorities(raw: Any) -> list[dict[str, Any]]:
    priorities: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for term, meta in raw.items():
            term_text = str(term or "").strip()
            if not is_usable_term(term_text):
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
                score_value = int(score_raw)
            except (TypeError, ValueError):
                score_value = 0
            priorities.append({
                "term": term_text,
                "score": score_value,
                "category": category,
                "reason": reason,
            })
        return priorities
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                term_text = str(item.get("term") or item.get("name") or "").strip()
                if not is_usable_term(term_text):
                    continue
                try:
                    score_value = int(item.get("score", item.get("weight", 0)))
                except (TypeError, ValueError):
                    score_value = 0
                priorities.append({
                    "term": term_text,
                    "score": score_value,
                    "category": str(item.get("category") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                })
                continue
            term_text = str(item or "").strip()
            if is_usable_term(term_text):
                priorities.append({"term": term_text, "score": 0, "category": "", "reason": ""})
    return priorities


def enforce_state_constraints(package: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return package

    constrained = dict(package)
    evidence_anchor = dict(state.get("evidence_anchor") or {})
    vehicle_name = str(evidence_anchor.get("vehicle_name") or "").strip()
    anchor_terms = list(constrained.get("anchor_terms") or [])
    include_terms = list(constrained.get("include_terms") or [])
    order_id = str(evidence_anchor.get("order_id") or "").strip()
    preserve_strict_include = bool(constrained.get("require_all_terms") and include_terms)

    if vehicle_name:
        if vehicle_name not in anchor_terms:
            anchor_terms.insert(0, vehicle_name)
        if not preserve_strict_include and vehicle_name not in include_terms:
            include_terms.insert(0, vehicle_name)
    if order_id:
        if order_id not in anchor_terms:
            anchor_terms.append(order_id)
        if not preserve_strict_include and order_id not in include_terms:
            include_terms.append(order_id)

    if not anchor_terms:
        return constrained

    constrained["anchor_terms"] = anchor_terms
    constrained["include_terms"] = include_terms
    if str(constrained.get("anchor_match_mode") or "").strip().lower() != "prefer":
        constrained["require_anchor"] = True
    return constrained


def normalize_time_window(raw: dict[str, Any]) -> dict[str, str]:
    start = str(raw.get("start", "") or "").strip()
    end = str(raw.get("end", "") or "").strip()
    return {"start": start, "end": end}


def infer_round_from_package_path(path: str) -> int:
    if not path:
        return 0
    match = re.search(r"round(\d+)", Path(path).name, re.IGNORECASE)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def quote_dsl_term(term: str) -> str:
    return '"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dsl_group(terms: list[str], *, operator: str) -> str:
    parts = [quote_dsl_term(term) for term in terms if term]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + f" {operator} ".join(parts) + ")"


def has_placeholder_mojibake(text: str) -> bool:
    compact = str(text or "")
    return "\ufffd" in compact or "??" in compact or "？？" in compact


def build_boolean_dsl_query(package: dict[str, Any]) -> str:
    anchor_terms = dedupe_terms(list(package.get("anchor_terms") or []))
    positive_terms = dedupe_terms([
        *list(package.get("gate_terms") or []),
        *list(package.get("core_terms") or []),
        *list(package.get("exception_terms") or []),
        *list(package.get("generic_terms") or []),
        *list(package.get("include_terms") or []),
    ])
    positive_terms = [term for term in positive_terms if term not in anchor_terms]
    exclude_terms = dedupe_terms([
        *list(package.get("exclude_terms") or []),
        *list(package.get("excluded_files") or []),
    ])

    sections: list[str] = []
    anchor_section = dsl_group(anchor_terms, operator="OR")
    if anchor_section:
        sections.append(anchor_section)

    positive_operator = "AND" if package.get("require_all_terms") else "OR"
    positive_section = dsl_group(positive_terms, operator=positive_operator)
    if positive_section:
        sections.append(positive_section)

    query = " AND ".join(sections)
    negative_parts = [f"NOT {quote_dsl_term(term)}" for term in exclude_terms]
    if negative_parts:
        query = query + (" AND " if query else "") + " AND ".join(negative_parts)
    return query.strip()


def join_dsl_terms(terms: list[str], *, operator: str = ", ", quote: bool = False) -> str:
    values = dedupe_terms([str(item or "").strip() for item in terms])
    if quote:
        values = [quote_dsl_term(item) for item in values]
    return operator.join(values)


def format_time_window_for_dsl(time_window: dict[str, Any]) -> list[str]:
    start = str(time_window.get("start") or "").strip()
    end = str(time_window.get("end") or "").strip()
    source_timezone = str(time_window.get("source_timezone") or "").strip()
    display_timezone = str(time_window.get("display_timezone") or "").strip()
    lines: list[str] = []
    if start or end:
        label = "search"
        if source_timezone:
            label = "log_" + source_timezone.lower().replace("+", "").replace(":", "").replace(" ", "_") + "_search"
        lines.append(f"  {label}: {start or '?'} ~ {end or '?'}")
    reported = str(time_window.get("reported_local_utc8") or time_window.get("reported") or "").strip()
    if reported:
        lines.insert(0, f"  reported_local_utc8: {reported}")
    if display_timezone and display_timezone != source_timezone:
        lines.append(f"  display_timezone: {display_timezone}")
    return lines or ["  search: unknown"]


def build_structured_dsl_query(package: dict[str, Any], *, round_no: int | None = None) -> str:
    anchor_terms = dedupe_terms(list(package.get("anchor_terms") or []))
    business_terms = dedupe_terms([
        *list(package.get("gate_terms") or []),
        *list(package.get("core_terms") or []),
        *list(package.get("exception_terms") or []),
        *list(package.get("generic_terms") or []),
    ])
    business_terms = [term for term in business_terms if term not in anchor_terms]
    target_files = dedupe_terms(list(package.get("target_files") or []))
    preferred_files = dedupe_terms(list(package.get("preferred_files") or []))
    excluded_files = dedupe_terms(list(package.get("excluded_files") or []))
    time_window = dict(package.get("time_window") or {})
    focus_question = str(package.get("focus_question") or package.get("question") or "").strip()

    anchor_mode = str(package.get("anchor_match_mode") or "").strip().lower()
    require_anchor = bool(package.get("require_anchor"))
    mode = "verification"
    if anchor_mode == "prefer" and not require_anchor:
        mode = "verification/wide-recall"
    elif anchor_mode == "prefer":
        mode = "verification/prefer-anchor"

    anchor_label = "prefer" if anchor_mode == "prefer" and not require_anchor else "require"
    lines = [
        f"round: {round_no or package.get('round_no') or 1}",
        f"mode: {mode}",
    ]
    if focus_question:
        lines.append(f"question: {focus_question}")
    lines.append("time_window:")
    lines.extend(format_time_window_for_dsl(time_window))
    lines.append("anchors:")
    lines.append(f"  {anchor_label}: {join_dsl_terms(anchor_terms, operator=' OR ', quote=True) or 'none'}")
    lines.append("gates:")
    lines.append(f"  {join_dsl_terms(business_terms) or 'none'}")
    lines.append("files:")
    if target_files:
        lines.append(f"  target: {join_dsl_terms(target_files)}")
    if preferred_files:
        lines.append(f"  prefer: {join_dsl_terms(preferred_files)}")
    if excluded_files:
        lines.append(f"  exclude: {join_dsl_terms(excluded_files)}")
    if not any([target_files, preferred_files, excluded_files]):
        lines.append("  scan: all supported log/archive files")
    boolean_query = build_boolean_dsl_query(package)
    if boolean_query:
        lines.append("query:")
        lines.append(f"  {boolean_query}")
    lines.append("notes:")
    if str(time_window.get("source_timezone") or "") == "UTC+0" and str(time_window.get("display_timezone") or "") == "UTC+8":
        lines.append("  allspark logs use UTC+0; display/report times should be converted to UTC+8.")
    else:
        lines.append("  keep order, vehicle, and time window closed before final conclusion.")
    return "\n".join(lines).rstrip()


def build_dsl_query(package: dict[str, Any], *, round_no: int | None = None) -> str:
    explicit = str(package.get("dsl_query") or "").strip()
    if explicit and not has_placeholder_mojibake(explicit):
        return explicit
    return build_structured_dsl_query(package, round_no=round_no)


def materialize_dsl_query(
    *,
    args: argparse.Namespace,
    package: dict[str, Any],
    state_path: Path | None,
    output_dir: Path,
    round_no: int,
) -> tuple[dict[str, Any], Path | None]:
    effective_round = round_no or infer_round_from_package_path(args.keyword_package_file) or 1
    dsl_query = build_dsl_query(package, round_no=effective_round)
    if not dsl_query:
        return package, None

    if state_path:
        dsl_path = state_path.parent / f"query.round{effective_round}.dsl.txt"
    elif args.keyword_package_file:
        dsl_path = Path(args.keyword_package_file).resolve().parent / f"query.round{effective_round}.dsl.txt"
    else:
        dsl_path = output_dir / f"query.round{effective_round}.dsl.txt"

    dsl_path.write_text(dsl_query.rstrip() + "\n", encoding="utf-8")
    updated = dict(package)
    updated["dsl_query"] = dsl_query
    updated["dsl_query_file"] = str(dsl_path)
    if args.keyword_package_file:
        persist_normalized_keyword_package(Path(args.keyword_package_file).resolve(), updated, dsl_path)
    return updated, dsl_path


def persist_normalized_keyword_package(package_path: Path, package: dict[str, Any], dsl_path: Path) -> None:
    try:
        raw = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    for key in (
        "anchor_terms",
        "gate_terms",
        "log_message_terms",
        "stage_terms",
        "core_terms",
        "class_terms",
        "method_terms",
        "exception_terms",
        "generic_terms",
        "include_terms",
        "exclude_terms",
        "hypotheses",
    ):
        raw[key] = list(package.get(key) or [])
    raw["term_priorities"] = list(package.get("term_priorities") or [])
    raw["dsl_query"] = str(package.get("dsl_query") or "")
    raw["dsl_query_file"] = str(dsl_path)
    raw["_normalized_by_search_worker"] = True
    package_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_output_dir(args: argparse.Namespace, state_path: Path | None, search_root: Path) -> Path:
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
        if state_path:
            search_runs_dir = (state_path.parent / "search-runs").resolve()
            try:
                output_dir.relative_to(search_runs_dir)
            except ValueError:
                output_name = output_dir.name or utc_now_label()
                output_dir = search_runs_dir / output_name
    elif state_path:
        output_dir = state_path.parent / "search-runs" / utc_now_label()
    else:
        output_dir = search_root / "search-runs" / utc_now_label()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def should_scan_path(path_str: str, package: dict[str, Any]) -> bool:
    excluded = package.get("excluded_files") or []
    if any(path_matches_pattern(path_str, excluded_name) for excluded_name in excluded):
        return False
    targets = package.get("target_files") or []
    if not targets:
        return True
    return any(path_matches_pattern(path_str, target) for target in targets)


def _path_priority(path_str: str, package: dict[str, Any]) -> tuple[int, str]:
    lowered = path_str.lower()
    preferred = package.get("preferred_files") or []
    for index, preferred_name in enumerate(preferred):
        if path_matches_pattern(path_str, preferred_name):
            return index, lowered
    return len(preferred), lowered


def path_matches_pattern(path_str: str, pattern: str) -> bool:
    lowered = path_str.lower().replace("\\", "/")
    pattern_lower = str(pattern or "").strip().lower().replace("\\", "/")
    if not pattern_lower:
        return False
    if any(char in pattern_lower for char in "*?[]"):
        return fnmatch.fnmatch(lowered, pattern_lower)
    return pattern_lower in lowered


def iter_documents(root: Path, package: dict[str, Any]) -> Iterator[tuple[str, Iterator[str]]]:
    paths = [path for path in root.rglob("*") if path.is_file()]
    paths.sort(key=lambda path: _path_priority(str(path.relative_to(root)), package))
    for path in paths:
        display_path = str(path.relative_to(root))
        suffixes = [suffix.lower() for suffix in path.suffixes]
        if suffixes[-2:] == [".tar", ".gz"] or path.suffix.lower() in {".tgz", ".tar"}:
            # For archives, filter on members inside the bundle rather than the outer archive name.
            lowered = display_path.lower()
            excluded = package.get("excluded_files") or []
            if any(path_matches_pattern(lowered, excluded_name) for excluded_name in excluded):
                continue
            try:
                yield from iter_tar_documents(path, root, package)
            except (tarfile.TarError, EOFError, OSError):
                continue
            continue
        if path.suffix.lower() == ".zip":
            lowered = display_path.lower()
            excluded = package.get("excluded_files") or []
            if any(path_matches_pattern(lowered, excluded_name) for excluded_name in excluded):
                continue
            try:
                yield from iter_zip_documents(path, root, package)
            except (zipfile.BadZipFile, EOFError, OSError):
                continue
            continue
        if not should_scan_path(display_path, package):
            continue
        if path.suffix.lower() == ".gz":
            yield display_path, iter_maybe_compressed_file(path)
            continue
        if is_text_member(display_path):
            yield display_path, iter_text_lines(path)


def iter_text_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\n")


def iter_gzip_lines(path: Path) -> Iterator[str]:
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\n")


def detect_binary_format(header: bytes) -> str:
    if header.startswith(GZIP_MAGIC):
        return "gzip"
    if any(header.startswith(magic) for magic in ZIP_MAGICS):
        return "zip"
    return "text"


def iter_zip_bytes_lines(data: bytes) -> Iterator[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        text_names = [name for name in names if is_text_member(name)]
        selected_names = text_names or names
        for name in selected_names:
            with archive.open(name, "r") as handle:
                yield from iter_bytes_lines(handle)


def iter_maybe_compressed_file(path: Path) -> Iterator[str]:
    with path.open("rb") as handle:
        header = handle.read(4)
    binary_format = detect_binary_format(header)
    if binary_format == "gzip":
        yield from iter_gzip_lines(path)
        return
    if binary_format == "zip":
        yield from iter_zip_bytes_lines(path.read_bytes())
        return
    yield from iter_text_lines(path)


def iter_tar_documents(path: Path, root: Path, package: dict[str, Any]) -> Iterator[tuple[str, Iterator[str]]]:
    mode = "r:gz" if path.suffix.lower() in {".gz", ".tgz"} else "r:"
    with tarfile.open(path, mode) as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        members.sort(key=lambda member: _path_priority(f"{path.relative_to(root)}::{member.name}", package))
        for member in members:
            display_path = f"{path.relative_to(root)}::{member.name}"
            if not should_scan_path(display_path, package):
                continue
            if not is_text_member(member.name):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            if member.name.lower().endswith(".gz"):
                yield display_path, iter_maybe_compressed_member_lines(extracted)
            else:
                yield display_path, iter_bytes_lines(extracted)


def iter_zip_documents(path: Path, root: Path, package: dict[str, Any]) -> Iterator[tuple[str, Iterator[str]]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        names.sort(key=lambda name: _path_priority(f"{path.relative_to(root)}::{name}", package))
        for name in names:
            display_path = f"{path.relative_to(root)}::{name}"
            if not should_scan_path(display_path, package):
                continue
            if not is_text_member(name):
                continue
            with archive.open(name, "r") as handle:
                if name.lower().endswith(".gz"):
                    yield display_path, iter_maybe_compressed_member_lines(handle)
                else:
                    yield display_path, iter_bytes_lines(handle)


def is_text_member(name: str) -> bool:
    lower_name = name.lower()
    if ".log." in lower_name:
        return True
    if any(lower_name.endswith(ext) for ext in TEXT_EXTENSIONS):
        return True
    if lower_name.endswith(".log.gz"):
        return True
    if lower_name.endswith(".gz"):
        base_name = lower_name[:-3]
        return ".log." in base_name or any(ext in base_name for ext in TEXT_EXTENSIONS)
    return False


def iter_bytes_lines(handle: io.BufferedIOBase) -> Iterator[str]:
    if hasattr(handle, "readable") and handle.readable():
        buffered = io.BufferedReader(handle)
    else:
        buffered = io.BufferedReader(handle)
    with io.TextIOWrapper(buffered, encoding="utf-8", errors="replace") as wrapper:
        for line in wrapper:
            yield line.rstrip("\n")


def iter_gzip_member_lines(handle: io.BufferedIOBase) -> Iterator[str]:
    with gzip.GzipFile(fileobj=handle, mode="rb") as gzip_handle:
        with io.TextIOWrapper(gzip_handle, encoding="utf-8", errors="replace") as wrapper:
            for line in wrapper:
                yield line.rstrip("\n")


def iter_maybe_compressed_member_lines(handle: io.BufferedIOBase) -> Iterator[str]:
    buffered = io.BufferedReader(handle)
    header = buffered.peek(4)[:4]
    binary_format = detect_binary_format(header)
    if binary_format == "gzip":
        yield from iter_gzip_member_lines(buffered)
        return
    if binary_format == "zip":
        data = buffered.read()
        yield from iter_zip_bytes_lines(data)
        return
    yield from iter_bytes_lines(buffered)


def parse_timestamp(text: str) -> str:
    fast = parse_leading_timestamp(text)
    if fast:
        return fast
    if len(text) < 19 or "-" not in text or ":" not in text:
        return ""
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("ts")
    return ""


def parse_leading_timestamp(text: str) -> str:
    if len(text) < 19:
        return ""
    separator = text[10]
    if (
        text[4] != "-"
        or text[7] != "-"
        or (separator != " " and separator != "T")
        or text[13] != ":"
        or text[16] != ":"
    ):
        return ""
    if not (
        text[0:4].isdigit()
        and text[5:7].isdigit()
        and text[8:10].isdigit()
        and text[11:13].isdigit()
        and text[14:16].isdigit()
        and text[17:19].isdigit()
    ):
        return ""
    end = 19
    if len(text) > end and text[end] in {".", ","}:
        fraction_end = end + 1
        while fraction_end < len(text) and fraction_end < end + 7 and text[fraction_end].isdigit():
            fraction_end += 1
        if fraction_end > end + 1:
            end = fraction_end
    if len(text) > end and text[end] == "Z":
        end += 1
    elif len(text) >= end + 6 and text[end] in {"+", "-"} and text[end + 3] == ":":
        if text[end + 1:end + 3].isdigit() and text[end + 4:end + 6].isdigit():
            end += 6
    return text[:end]


def timestamp_in_window(timestamp_text: str, package: dict[str, Any]) -> bool:
    if not timestamp_text:
        return True
    window = package.get("time_window") or {}
    start = window.get("start") or ""
    end = window.get("end") or ""
    if not start and not end:
        return True
    try:
        candidate = comparable_datetime(timestamp_text)
        if start and candidate < comparable_datetime(start):
            return False
        if end and candidate > comparable_datetime(end):
            return False
    except (ValueError, TypeError):
        return True
    return True


def compile_time_window(package: dict[str, Any]) -> tuple[datetime | None, datetime | None, str, str, bool, bool, bool]:
    window = package.get("time_window") or {}
    start = str(window.get("start") or "").strip()
    end = str(window.get("end") or "").strip()
    try:
        start_dt = comparable_datetime(start) if start else None
    except (ValueError, TypeError):
        start_dt = None
    try:
        end_dt = comparable_datetime(end) if end else None
    except (ValueError, TypeError):
        end_dt = None
    start_text = normalize_timestamp_for_text_compare(start)
    end_text = normalize_timestamp_for_text_compare(end)
    start_plain = is_plain_timestamp_text(start_text)
    end_plain = is_plain_timestamp_text(end_text)
    plain_window = (not start_text or start_plain) and (not end_text or end_plain)
    return start_dt, end_dt, start_text, end_text, start_plain, end_plain, plain_window


def timestamp_window_position(timestamp_text: str, bounds: tuple[datetime | None, datetime | None, str, str, bool, bool, bool]) -> int:
    if not timestamp_text:
        return 0
    start, end, start_text, end_text, start_plain, end_plain, plain_window = bounds
    if not start and not end:
        return 0
    candidate_text = timestamp_text
    if plain_window and is_plain_timestamp_text(candidate_text):
        if start_text and start_plain and candidate_text < start_text:
            return -1
        if end_text and end_plain and candidate_text > end_text:
            return 1
        if start_text or end_text:
            return 0
    candidate_text = normalize_timestamp_for_text_compare(timestamp_text)
    if candidate_text != timestamp_text and is_plain_timestamp_text(candidate_text):
        if start_text and start_plain and candidate_text < start_text:
            return -1
        if end_text and end_plain and candidate_text > end_text:
            return 1
        if (start_text or end_text) and (not start_text or start_plain) and (not end_text or end_plain):
            return 0
    try:
        candidate = comparable_datetime(timestamp_text)
    except (ValueError, TypeError):
        return 0
    if start and candidate < start:
        return -1
    if end and candidate > end:
        return 1
    return 0


def fast_plain_window_position(
    timestamp_text: str,
    bounds: tuple[datetime | None, datetime | None, str, str, bool, bool, bool],
) -> int | None:
    if not timestamp_text:
        return 0
    _start, _end, start_text, end_text, start_plain, end_plain, plain_window = bounds
    if not plain_window or len(timestamp_text) < 19 or timestamp_text[10] != " ":
        return None
    suffix = timestamp_text[19:]
    if "," in suffix or suffix.endswith("Z") or "+" in suffix:
        return None
    if len(suffix) >= 6:
        timezone_tail = suffix[-6:]
        if timezone_tail[0] == "-" and timezone_tail[3] == ":" and timezone_tail[1:3].isdigit() and timezone_tail[4:6].isdigit():
            return None
    if start_text and start_plain and timestamp_text < start_text:
        return -1
    if end_text and end_plain and timestamp_text > end_text:
        return 1
    return 0


def normalize_timestamp_for_text_compare(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" not in text and "," not in text:
        return text
    return text.replace("T", " ").replace(",", ".")


def is_plain_timestamp_text(value: str) -> bool:
    text = str(value or "")
    if len(text) < 19:
        return False
    suffix = text[19:]
    if not suffix:
        return True
    if suffix.endswith("Z"):
        return False
    if len(suffix) >= 6:
        tz = suffix[-6:]
        if tz[0] in {"+", "-"} and tz[3] == ":" and tz[1:3].isdigit() and tz[4:6].isdigit():
            return False
    return "+" not in suffix


def parse_datetime(value: str) -> datetime:
    normalized = value.replace(",", ".")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


def comparable_datetime(value: str) -> datetime:
    dt = parse_datetime(value)
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def should_collect_order_candidates(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    evidence_anchor = dict(state.get("evidence_anchor") or {})
    issue_type = str(evidence_anchor.get("issue_type") or "").strip()
    vehicle_name = str(evidence_anchor.get("vehicle_name") or "").strip()
    return issue_type == "order_execution" and bool(vehicle_name)


def extract_order_candidates_from_line(*, line: str, vehicle_name: str) -> list[dict[str, str]]:
    if not vehicle_name or vehicle_name not in line:
        return []
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, pattern in ORDER_CANDIDATE_PATTERNS:
        for match in pattern.finditer(line):
            matched_vehicle = str(match.groupdict().get("vehicle") or "").strip()
            if matched_vehicle and matched_vehicle != vehicle_name:
                continue
            order_id = str(match.group("order") or "").strip()
            if not order_id:
                continue
            key = (order_id, source)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"order_id": order_id, "source": source})
    return candidates


def compile_search_runtime(package: dict[str, Any]) -> dict[str, Any]:
    def entries(key: str) -> list[tuple[str, str]]:
        return [
            (term, term.lower())
            for term in package.get(key) or []
            if term
        ]

    priority_by_lc: dict[str, tuple[str, int]] = {}
    for item in package.get("term_priorities") or []:
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score:
            priority_by_lc[term.lower()] = (term, score)

    gate_entries = entries("gate_terms")
    core_entries = entries("core_terms")
    exception_entries = entries("exception_terms")
    return {
        "exclude_entries": entries("exclude_terms"),
        "anchor_entries": entries("anchor_terms"),
        "gate_entries": gate_entries,
        "core_entries": core_entries,
        "exception_entries": exception_entries,
        "generic_entries": entries("generic_terms"),
        "include_entries": entries("include_terms"),
        "has_focus_terms": bool(gate_entries or core_entries or exception_entries),
        "term_priority_by_lc": priority_by_lc,
        "file_priority_rules": file_priority_rules(package),
    }


def match_runtime_terms(lowered: str, entries: list[tuple[str, str]]) -> list[str]:
    return [term for term, term_lc in entries if term_lc in lowered]


def classify_match(
    text: str,
    package: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    lowered: str | None = None,
) -> dict[str, Any]:
    lowered = lowered if lowered is not None else text.lower()
    runtime = runtime or compile_search_runtime(package)
    if any(term_lc in lowered for _term, term_lc in runtime["exclude_entries"]):
        return {"accepted": False, "matched_terms": [], "suppressed_reason": ""}

    anchor_entries = runtime["anchor_entries"]
    gate_entries = runtime["gate_entries"]
    core_entries = runtime["core_entries"]
    exception_entries = runtime["exception_entries"]
    generic_entries = runtime["generic_entries"]
    include_entries = runtime["include_entries"]
    matched_anchor = match_runtime_terms(lowered, anchor_entries)
    matched_gate = match_runtime_terms(lowered, gate_entries)
    matched_core = match_runtime_terms(lowered, core_entries)
    matched_exception = match_runtime_terms(lowered, exception_entries)
    matched_generic = match_runtime_terms(lowered, generic_entries)
    matched_include = match_runtime_terms(lowered, include_entries)
    matched_focus = dedupe_terms([*matched_gate, *matched_core, *matched_exception])

    if package.get("require_all_terms"):
        all_required_terms_present = bool(include_entries) and len(matched_include) == len(include_entries)
        if not all_required_terms_present:
            return {
                "accepted": False,
                "matched_terms": dedupe_terms(matched_include),
                "matched_anchor_terms": dedupe_terms(matched_anchor),
                "matched_gate_terms": dedupe_terms(matched_gate),
                "matched_core_terms": dedupe_terms(matched_core),
                "matched_exception_terms": dedupe_terms(matched_exception),
                "matched_generic_terms": dedupe_terms(matched_generic),
                "matched_include_terms": dedupe_terms(matched_include),
                "suppressed_reason": "missing_required_terms" if matched_include else "",
            }
        if package.get("require_anchor") and anchor_entries and not matched_anchor:
            return {
                "accepted": False,
                "matched_terms": dedupe_terms(matched_include),
                "matched_anchor_terms": [],
                "matched_gate_terms": dedupe_terms(matched_gate),
                "matched_core_terms": dedupe_terms(matched_core),
                "matched_exception_terms": dedupe_terms(matched_exception),
                "matched_generic_terms": dedupe_terms(matched_generic),
                "matched_include_terms": dedupe_terms(matched_include),
                "suppressed_reason": "missing_anchor",
            }
        if package.get("require_gate_when_present") and runtime["has_focus_terms"] and not matched_focus:
            return {
                "accepted": False,
                "matched_terms": dedupe_terms(matched_include),
                "matched_anchor_terms": dedupe_terms(matched_anchor),
                "matched_gate_terms": [],
                "matched_core_terms": [],
                "matched_exception_terms": [],
                "matched_generic_terms": dedupe_terms(matched_generic),
                "matched_include_terms": dedupe_terms(matched_include),
                "suppressed_reason": "missing_gate",
            }
        return {
            "accepted": True,
            "matched_terms": dedupe_terms(matched_include),
            "matched_anchor_terms": dedupe_terms(matched_anchor),
            "matched_gate_terms": dedupe_terms(matched_gate),
            "matched_core_terms": dedupe_terms(matched_core),
            "matched_exception_terms": dedupe_terms(matched_exception),
            "matched_generic_terms": dedupe_terms(matched_generic),
            "matched_include_terms": dedupe_terms(matched_include),
            "suppressed_reason": "",
        }

    categorized_terms = dedupe_terms([*matched_anchor, *matched_gate, *matched_core, *matched_exception, *matched_generic])
    if categorized_terms:
        if package.get("require_anchor") and anchor_entries and not matched_anchor:
            return {
                "accepted": False,
                "matched_terms": categorized_terms,
                "matched_anchor_terms": [],
                "matched_gate_terms": dedupe_terms(matched_gate),
                "matched_core_terms": dedupe_terms(matched_core),
                "matched_exception_terms": dedupe_terms(matched_exception),
                "matched_generic_terms": dedupe_terms(matched_generic),
                "matched_include_terms": dedupe_terms(matched_include),
                "suppressed_reason": "missing_anchor",
            }
        if package.get("require_gate_when_present") and runtime["has_focus_terms"] and not matched_focus:
            return {
                "accepted": False,
                "matched_terms": categorized_terms,
                "matched_anchor_terms": dedupe_terms(matched_anchor),
                "matched_gate_terms": [],
                "matched_core_terms": [],
                "matched_exception_terms": [],
                "matched_generic_terms": dedupe_terms(matched_generic),
                "matched_include_terms": dedupe_terms(matched_include),
                "suppressed_reason": "missing_gate",
            }
        return {
            "accepted": True,
            "matched_terms": categorized_terms,
            "matched_anchor_terms": dedupe_terms(matched_anchor),
            "matched_gate_terms": dedupe_terms(matched_gate),
            "matched_core_terms": dedupe_terms(matched_core),
            "matched_exception_terms": dedupe_terms(matched_exception),
            "matched_generic_terms": dedupe_terms(matched_generic),
            "matched_include_terms": dedupe_terms(matched_include),
            "suppressed_reason": "",
        }

    if not include_entries:
        return {"accepted": False, "matched_terms": [], "suppressed_reason": ""}
    if package.get("require_anchor") and anchor_entries and matched_include and not matched_anchor:
        return {
            "accepted": False,
            "matched_terms": dedupe_terms(matched_include),
            "matched_anchor_terms": [],
            "matched_gate_terms": [],
            "matched_core_terms": [],
            "matched_exception_terms": [],
            "matched_generic_terms": [],
            "matched_include_terms": dedupe_terms(matched_include),
            "suppressed_reason": "missing_anchor",
        }
    if package.get("require_gate_when_present") and runtime["has_focus_terms"] and matched_include and not matched_focus:
        return {
            "accepted": False,
            "matched_terms": dedupe_terms(matched_include),
            "matched_anchor_terms": dedupe_terms(matched_anchor),
            "matched_gate_terms": [],
            "matched_core_terms": [],
            "matched_exception_terms": [],
            "matched_generic_terms": [],
            "matched_include_terms": dedupe_terms(matched_include),
            "suppressed_reason": "missing_gate",
        }
    return {
        "accepted": bool(matched_include),
        "matched_terms": dedupe_terms(matched_include),
        "matched_anchor_terms": dedupe_terms(matched_anchor),
        "matched_gate_terms": dedupe_terms(matched_gate),
        "matched_core_terms": dedupe_terms(matched_core),
        "matched_exception_terms": dedupe_terms(matched_exception),
        "matched_generic_terms": dedupe_terms(matched_generic),
        "matched_include_terms": dedupe_terms(matched_include),
        "suppressed_reason": "",
    }


def dedupe_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def matched_file_targets(path_str: str, package: dict[str, Any]) -> list[str]:
    targets = package.get("target_files") or []
    return [target for target in targets if path_matches_pattern(path_str, target)]


def file_priority_rules(package: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = list(package.get("file_priorities") or [])
    if explicit:
        return explicit

    preferred = list(package.get("preferred_files") or [])
    rules: list[dict[str, Any]] = [
        {"pattern": pattern, "score": max(50 - index * 5, 5)}
        for index, pattern in enumerate(preferred)
        if str(pattern or "").strip()
    ]
    existing = {str(rule["pattern"]).lower() for rule in rules}
    for pattern, score in DEFAULT_FILE_PRIORITIES:
        if pattern.lower() not in existing:
            rules.append({"pattern": pattern, "score": score})
    return rules


def file_priority_score(path_str: str, package: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> tuple[int, str]:
    best_score = 0
    best_pattern = ""
    for rule in rules if rules is not None else file_priority_rules(package):
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern or not path_matches_pattern(path_str, pattern):
            continue
        try:
            score_value = int(rule.get("score") or 0)
        except (TypeError, ValueError):
            score_value = 0
        if score_value > best_score or not best_pattern:
            best_score = score_value
            best_pattern = pattern
    return best_score, best_pattern


def score_hit(
    *,
    path_str: str,
    timestamp_text: str,
    match_info: dict[str, Any],
    package: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    file_priority: tuple[int, str] | None = None,
) -> tuple[int, list[str], int]:
    anchor_terms = list(match_info.get("matched_anchor_terms") or [])
    gate_terms = list(match_info.get("matched_gate_terms") or [])
    core_terms = list(match_info.get("matched_core_terms") or [])
    exception_terms = list(match_info.get("matched_exception_terms") or [])
    generic_terms = list(match_info.get("matched_generic_terms") or [])
    include_terms = [
        term for term in list(match_info.get("matched_include_terms") or [])
        if (
            term not in anchor_terms
            and term not in gate_terms
            and term not in core_terms
            and term not in exception_terms
            and term not in generic_terms
        )
    ]
    file_score, file_pattern = file_priority or file_priority_score(path_str, package)
    priority_score, priority_reasons = matched_term_priority_score(
        [*anchor_terms, *gate_terms, *core_terms, *exception_terms, *generic_terms, *include_terms],
        package,
        runtime,
    )
    score = file_score
    reasons: list[str] = []
    if file_pattern:
        reasons.append(f"file_priority:{file_pattern}+{file_score}")
    if priority_score:
        score += priority_score
        reasons.extend(priority_reasons)
    if anchor_terms:
        value = len(anchor_terms) * 12
        score += value
        reasons.append(f"anchor_terms:{len(anchor_terms)}+{value}")
    if gate_terms:
        value = len(gate_terms) * 6
        score += value
        reasons.append(f"gate_terms:{len(gate_terms)}+{value}")
        if len(gate_terms) >= 2:
            score += 4
            reasons.append("multi_gate_bonus+4")
    if core_terms:
        value = len(core_terms) * 10
        score += value
        reasons.append(f"core_terms:{len(core_terms)}+{value}")
    if exception_terms:
        value = len(exception_terms) * 14
        score += value
        reasons.append(f"exception_terms:{len(exception_terms)}+{value}")
    if generic_terms:
        value = len(generic_terms)
        score += value
        reasons.append(f"generic_terms:{len(generic_terms)}+{value}")
    if include_terms:
        value = len(include_terms) * 2
        score += value
        reasons.append(f"include_terms:{len(include_terms)}+{value}")
    if len(anchor_terms) >= 2:
        score += 8
        reasons.append("vehicle_order_pair+8")
    if package.get("time_window") and not timestamp_text:
        score -= 3
        reasons.append("missing_timestamp-3")
    return score, reasons, file_score


def matched_term_priority_score(
    matched_terms: list[str],
    package: dict[str, Any],
    runtime: dict[str, Any] | None = None,
) -> tuple[int, list[str]]:
    priority_by_lc = dict((runtime or {}).get("term_priority_by_lc") or {})
    if not priority_by_lc:
        priorities = list(package.get("term_priorities") or [])
        if not priorities:
            return 0, []
        for item in priorities:
            term = str(item.get("term") or "").strip()
            if not term:
                continue
            try:
                score = int(item.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            if score:
                priority_by_lc[term.lower()] = (term, score)
    if not priority_by_lc:
        return 0, []
    total = 0
    reasons: list[str] = []
    seen: set[str] = set()
    for matched_term in matched_terms:
        key = str(matched_term or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        priority = priority_by_lc.get(key)
        if not priority:
            continue
        term, score = priority
        if not score:
            continue
        total += score
        reasons.append(f"term_priority:{term}+{score}")
    return total, reasons


def hit_rank_key(hit: dict[str, Any]) -> tuple[int, int, int, str, int]:
    return (
        int(hit.get("score") or 0),
        int(hit.get("file_priority_score") or 0),
        len(hit.get("matched_terms") or []),
        str(hit.get("timestamp") or ""),
        -int(hit.get("line_number") or 0),
    )


def current_worst_candidate(
    candidates: dict[str, dict[str, Any]],
    rank_heap: list[tuple[tuple[int, int, int, str, int], str]],
) -> tuple[tuple[int, int, int, str, int], str, dict[str, Any]] | None:
    while rank_heap:
        rank, key = rank_heap[0]
        hit = candidates.get(key)
        if hit is not None and hit_rank_key(hit) == rank:
            return rank, key, hit
        heapq.heappop(rank_heap)
    return None


def hit_timeline_key(hit: dict[str, Any]) -> tuple[int, datetime, str, int]:
    timestamp_text = str(hit.get("timestamp") or "").strip()
    if timestamp_text:
        try:
            return (0, comparable_datetime(timestamp_text), str(hit.get("path") or ""), int(hit.get("line_number") or 0))
        except (ValueError, TypeError):
            pass
    return (1, datetime.max, str(hit.get("path") or ""), int(hit.get("line_number") or 0))


def extract_vehicle_for_template(hit: dict[str, Any]) -> str:
    line = str(hit.get("matched_line") or "")
    for term in hit.get("matched_anchor_terms") or []:
        text = str(term or "").strip()
        if VEHICLE_ID_RE.fullmatch(text):
            return text
    match = VEHICLE_ID_RE.search(line)
    if match:
        return match.group(1)
    return ""


def extract_template_identity(hit: dict[str, Any]) -> dict[str, str]:
    line = str(hit.get("matched_line") or "")
    if " line:" not in line or " - " not in line:
        return {}
    source_match = LOG_SOURCE_RE.search(line)
    if not source_match:
        return {}
    vehicle = extract_vehicle_for_template(hit)
    if not vehicle:
        return {}
    task_key = extract_task_for_template(line)
    class_name = source_match.group("class_name")
    source_line = source_match.group("source_line")
    task_scope = task_key or "none"
    template_variant = ""
    if not task_key:
        template_variant = extract_no_task_template_variant(line)
        if not template_variant:
            return {}
    key = f"{vehicle}|task:{task_scope}|{class_name}|line:{source_line}"
    if template_variant:
        key += f"|variant:{template_variant}"
    return {
        "key": key,
        "vehicle": vehicle,
        "task_key": task_key,
        "task_scope": task_scope,
        "template_variant": template_variant,
        "class_name": class_name,
        "source_line": source_line,
    }


def extract_task_for_template(text: str) -> str:
    if "Task[" in text or "taskKey" in text:
        for pattern in (TASK_KEY_RE, TASK_FIELD_RE):
            match = pattern.search(text)
            if not match:
                continue
            value = str(next((item for item in match.groupdict().values() if item), "")).strip()
            if value:
                return value
    pipe_index = text.find("|")
    if pipe_index >= 0:
        match = DISPATCH_ORDER_RE.search(text, pipe_index)
        if match:
            return match.group("dispatch_order")
    if "车辆执行状态不符合" in text:
        mismatch = CROSS_MAP_MISMATCH_RE.search(text)
        if mismatch and mismatch.group("pre_order") == mismatch.group("now_order"):
            return mismatch.group("pre_order")
    return ""


def extract_no_task_template_variant(text: str) -> str:
    marker = " - "
    if marker not in text:
        return ""
    message = text.split(marker, 1)[1].strip()
    if not message:
        return ""
    brace_index = message.find("{")
    if brace_index > 0:
        message = message[:brace_index].strip()
    message = UUID_RE.sub("<uuid>", message)
    message = VEHICLE_ID_RE.sub("<vehicle>", message)
    message = re.sub(r"\b(deviceName|resourceId|resource|station)\s*[:=]\s*\[?[^\s,\]}:]+", r"\1:<value>", message)
    message = re.sub(r"\b(key|traceId|orderId|orderKey|orderNo)\s*[:=]\s*['\"]?[^,\]\)\s}]+", r"\1:<value>", message, flags=re.IGNORECASE)
    message = LONG_NUMBER_RE.sub("<number>", message)
    message = " ".join(message.split())
    return message[:180]


def append_change_value(values: dict[str, list[str]], field: str, value: str) -> None:
    normalized = str(value or "").strip().strip("'\"")
    if not normalized:
        return
    values.setdefault(field, []).append(normalized)


def extract_change_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    patterns: list[re.Pattern[str]] = []
    if "Request{" in text:
        patterns.append(REQUEST_TYPE_RE)
    if "traceId" in text:
        patterns.append(TRACE_ID_RE)
    if "|" in text:
        patterns.extend([PIPE_TRACE_RE, DISPATCH_ORDER_RE])
    if "orderId" in text or "orderid" in text.lower():
        patterns.append(ORDER_ID_RE)
    if "curCheckPointNo is" in text:
        patterns.append(CHECKPOINT_RE)
    if "fsm" in text:
        patterns.extend([FSM_STATE_RE, FSM_SHORT_RE])
    if "vehicleProcState" in text:
        patterns.append(PROC_STATE_RE)
    if "车辆执行状态不符合" in text:
        patterns.append(CROSS_MAP_MISMATCH_RE)
    if "nextState " in text and "curState " in text:
        patterns.append(TASK_STATE_RE)
    if "Task[" in text or "taskKey" in text:
        patterns.extend([TASK_KEY_RE, TASK_FIELD_RE])
    if "stage:" in text:
        patterns.append(STAGE_RE)
    if "deviceName" in text:
        patterns.append(DEVICE_NAME_RE)
    if "resourceId" in text:
        patterns.append(RESOURCE_ID_RE)
    if "resource:" in text or "resource=" in text:
        patterns.append(RESOURCE_RE)
    if "station:" in text or "station=" in text:
        patterns.append(STATION_RE)
    if "电梯" in text:
        patterns.append(ELEVATOR_NAME_RE)
    for pattern in patterns:
        for match in pattern.finditer(text):
            for field, value in match.groupdict().items():
                append_change_value(values, field, value)
    if "startMapId" in text or "endMapId" in text:
        for match in MAP_STEP_RE.finditer(text):
            append_change_value(values, match.group("map_field"), match.group("map_id"))
    return values


def location_label(hit: dict[str, Any]) -> str:
    return f"{hit.get('path') or ''}:{int(hit.get('line_number') or 0)}"


def make_template_sample(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": str(hit.get("timestamp") or ""),
        "path": str(hit.get("path") or ""),
        "line_number": int(hit.get("line_number") or 0),
        "matched_line": str(hit.get("matched_line") or "")[:600],
    }


def update_template_observations(template_merge: dict[str, Any], values: dict[str, list[str]]) -> None:
    observations = template_merge.setdefault("value_observations", {})
    value_sets = template_merge.setdefault("_value_sets", {})
    for field, field_values in values.items():
        if not field_values:
            continue
        record = observations.setdefault(field, {
            "first": "",
            "last": "",
            "values": [],
            "unique_count": 0,
            "truncated": False,
        })
        seen = value_sets.setdefault(field, set(record.get("values") or []))
        for value in field_values:
            if not record["first"]:
                record["first"] = value
            record["last"] = value
            if value in seen:
                continue
            seen.add(value)
            record["unique_count"] = len(seen)
            if len(record["values"]) < MAX_TEMPLATE_VALUE_SAMPLES:
                record["values"].append(value)
            else:
                record["truncated"] = True


def build_template_change_facts(template_merge: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    observations = dict(template_merge.get("value_observations") or {})
    for field in sorted(observations):
        record = dict(observations.get(field) or {})
        unique_count = int(record.get("unique_count") or len(record.get("values") or []))
        first_value = str(record.get("first") or "")
        last_value = str(record.get("last") or "")
        values = [str(item) for item in (record.get("values") or []) if str(item)]
        if unique_count <= 0:
            continue
        if unique_count == 1 and first_value:
            facts.append(f"{field}: {first_value}")
            continue
        if first_value or last_value:
            fact = f"{field}: {first_value} -> {last_value} ({unique_count} unique)"
        else:
            fact = f"{field}: {unique_count} unique"
        if values and unique_count <= 6:
            fact += f" [{', '.join(values)}]"
        elif values:
            shown = ", ".join(values[:5])
            suffix = ", ..." if unique_count > 5 or record.get("truncated") else ""
            fact += f" [{shown}{suffix}]"
        facts.append(fact)
    return facts[:12]


def prepare_template_merge_hit(hit: dict[str, Any]) -> dict[str, Any]:
    identity = extract_template_identity(hit)
    if not identity:
        return hit
    values = extract_change_values(str(hit.get("matched_line") or ""))
    template_merge = {
        "hit_count": 1,
        "first_timestamp": str(hit.get("timestamp") or ""),
        "last_timestamp": str(hit.get("timestamp") or ""),
        "first_location": location_label(hit),
        "last_location": location_label(hit),
        "samples": [make_template_sample(hit)],
        "value_observations": {},
        "_value_sets": {},
        "change_facts": [],
    }
    update_template_observations(template_merge, values)
    hit["template_key"] = identity["key"]
    hit["template_identity"] = {
        "vehicle": identity["vehicle"],
        "task_key": identity["task_key"],
        "task_scope": identity["task_scope"],
        "template_variant": identity["template_variant"],
        "class_name": identity["class_name"],
        "source_line": identity["source_line"],
    }
    hit["template_merge"] = template_merge
    return hit


def merge_unique_list(left: list[Any], right: list[Any]) -> list[Any]:
    if not right:
        return left
    if not left:
        return right
    if all(value in left for value in right):
        return left
    merged: list[Any] = []
    seen: set[str] = set()
    for value in [*left, *right]:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        merged.append(value)
    return merged


def merge_template_hits(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    existing_merge = existing.get("template_merge") or {}
    incoming_merge = incoming.get("template_merge") or {}
    existing_merge["hit_count"] = int(existing_merge.get("hit_count") or 1) + int(incoming_merge.get("hit_count") or 1)

    first_ts = str(existing_merge.get("first_timestamp") or "")
    incoming_first_ts = str(incoming_merge.get("first_timestamp") or incoming.get("timestamp") or "")
    if incoming_first_ts and (not first_ts or incoming_first_ts < first_ts):
        existing_merge["first_timestamp"] = incoming_first_ts
        existing_merge["first_location"] = incoming_merge.get("first_location") or location_label(incoming)
        existing["timestamp"] = incoming.get("timestamp", existing.get("timestamp", ""))
        existing["path"] = incoming.get("path", existing.get("path", ""))
        existing["line_number"] = incoming.get("line_number", existing.get("line_number", 0))
        existing["matched_line"] = incoming.get("matched_line", existing.get("matched_line", ""))
        existing["excerpt"] = incoming.get("excerpt", existing.get("excerpt", ""))

    last_ts = str(existing_merge.get("last_timestamp") or "")
    incoming_last_ts = str(incoming_merge.get("last_timestamp") or incoming.get("timestamp") or "")
    if incoming_last_ts and (not last_ts or incoming_last_ts >= last_ts):
        existing_merge["last_timestamp"] = incoming_last_ts
        existing_merge["last_location"] = incoming_merge.get("last_location") or location_label(incoming)

    existing["matched_terms"] = merge_unique_list(list(existing.get("matched_terms") or []), list(incoming.get("matched_terms") or []))
    existing["matched_anchor_terms"] = merge_unique_list(list(existing.get("matched_anchor_terms") or []), list(incoming.get("matched_anchor_terms") or []))
    existing["matched_gate_terms"] = merge_unique_list(list(existing.get("matched_gate_terms") or []), list(incoming.get("matched_gate_terms") or []))
    existing["matched_core_terms"] = merge_unique_list(list(existing.get("matched_core_terms") or []), list(incoming.get("matched_core_terms") or []))
    existing["matched_exception_terms"] = merge_unique_list(list(existing.get("matched_exception_terms") or []), list(incoming.get("matched_exception_terms") or []))
    existing["matched_generic_terms"] = merge_unique_list(list(existing.get("matched_generic_terms") or []), list(incoming.get("matched_generic_terms") or []))
    existing["score_reasons"] = merge_unique_list(list(existing.get("score_reasons") or []), list(incoming.get("score_reasons") or []))

    if int(incoming.get("score") or 0) > int(existing.get("score") or 0):
        existing["score"] = incoming.get("score", existing.get("score", 0))
        existing["file_priority_score"] = incoming.get("file_priority_score", existing.get("file_priority_score", 0))

    samples = list(existing_merge.get("samples") or [])
    incoming_sample = make_template_sample(incoming)
    if incoming_sample not in samples and len(samples) < MAX_TEMPLATE_SAMPLE_LINES:
        samples.append(incoming_sample)
    existing_merge["samples"] = samples
    update_template_observations(existing_merge, extract_change_values(str(incoming.get("matched_line") or "")))
    existing["template_merge"] = existing_merge
    return existing


def finalize_template_merge_metadata(hit: dict[str, Any]) -> dict[str, Any]:
    template_merge = hit.get("template_merge")
    if not isinstance(template_merge, dict):
        return hit
    template_merge.pop("_value_sets", None)
    template_merge["change_facts"] = build_template_change_facts(template_merge)
    hit["template_merge"] = template_merge
    hit["merged_summary"] = build_merged_summary(hit)
    hit.pop("template_key", None)
    return hit


def build_merged_summary(hit: dict[str, Any]) -> str:
    template_merge = dict(hit.get("template_merge") or {})
    hit_count = int(template_merge.get("hit_count") or 1)
    if hit_count <= 1:
        return ""
    identity = dict(hit.get("template_identity") or {})
    vehicle = identity.get("vehicle") or "-"
    task_key = identity.get("task_key") or "none"
    class_name = identity.get("class_name") or "-"
    source_line = identity.get("source_line") or "-"
    first_ts = str(template_merge.get("first_timestamp") or hit.get("timestamp") or "")
    last_ts = str(template_merge.get("last_timestamp") or "")
    time_range = first_ts
    if last_ts and last_ts != first_ts:
        time_range = f"{first_ts} ~ {last_ts}"
    facts = "; ".join(template_merge.get("change_facts") or [])
    summary = (
        f"模板合并: {vehicle} task:{task_key} {class_name} line:{source_line}; "
        f"{hit_count} hits; 时间: {time_range or 'no timestamp'}"
    )
    if facts:
        summary += f"; 变化: {facts}"
    return summary


def summarize_template_group(hit: dict[str, Any]) -> dict[str, Any]:
    template_merge = dict(hit.get("template_merge") or {})
    identity = dict(hit.get("template_identity") or {})
    return {
        "vehicle": identity.get("vehicle") or "",
        "task_key": identity.get("task_key") or "",
        "class_name": identity.get("class_name") or "",
        "source_line": identity.get("source_line") or "",
        "hit_count": int(template_merge.get("hit_count") or 1),
        "first_timestamp": str(template_merge.get("first_timestamp") or ""),
        "last_timestamp": str(template_merge.get("last_timestamp") or ""),
        "first_location": str(template_merge.get("first_location") or ""),
        "last_location": str(template_merge.get("last_location") or ""),
        "change_facts": list(template_merge.get("change_facts") or []),
    }


def format_time_window(window: dict[str, Any]) -> str:
    start = str(window.get("start") or "").strip()
    end = str(window.get("end") or "").strip()
    if start and end:
        return f"{start} ~ {end}"
    if start:
        return f">= {start}"
    if end:
        return f"<= {end}"
    return ""


def describe_time_match(timestamp_text: str, package: dict[str, Any]) -> str:
    window = package.get("time_window") or {}
    window_label = format_time_window(window)
    if not window_label:
        if timestamp_text:
            return f"命中时间: {timestamp_text}；当前未限制时间窗口"
        return "当前未限制时间窗口"
    if timestamp_text:
        return f"命中时间: {timestamp_text}；落在时间窗口 {window_label}"
    return f"当前行未解析到时间戳；本次命中主要依据关键词/文件筛选，仍需结合时间窗口 {window_label} 人工复核"


def build_match_reason(
    *,
    path_str: str,
    timestamp_text: str,
    matched_terms: list[str],
    package: dict[str, Any],
    file_targets: list[str] | None = None,
) -> str:
    reasons = [f"关键词命中: {', '.join(matched_terms)}"]
    file_targets = file_targets if file_targets is not None else matched_file_targets(path_str, package)
    if file_targets:
        reasons.append(f"目标文件命中: {', '.join(file_targets)}")
    reasons.append(describe_time_match(timestamp_text, package))
    if package.get("require_all_terms"):
        reasons.append("匹配策略: require_all_terms=true")
    return "；".join(reason for reason in reasons if reason)


def markdown_table_cell(text: str, *, limit: int = 160) -> str:
    cleaned = " ".join((text or "").split())
    cleaned = cleaned.replace("|", "\\|")
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned or "-"


def hit_time_label(hit: dict[str, Any]) -> str:
    template_merge = dict(hit.get("template_merge") or {})
    hit_count = int(template_merge.get("hit_count") or 1)
    if hit_count <= 1:
        return str(hit.get("timestamp") or "no timestamp")
    first_ts = str(template_merge.get("first_timestamp") or hit.get("timestamp") or "")
    last_ts = str(template_merge.get("last_timestamp") or "")
    if first_ts and last_ts and first_ts != last_ts:
        return f"{first_ts} ~ {last_ts} ({hit_count} hits)"
    return f"{first_ts or 'no timestamp'} ({hit_count} hits)"


def hit_location_label(hit: dict[str, Any]) -> str:
    template_merge = dict(hit.get("template_merge") or {})
    hit_count = int(template_merge.get("hit_count") or 1)
    if hit_count <= 1:
        return f"{hit['path']}:{hit['line_number']}"
    first_location = str(template_merge.get("first_location") or f"{hit['path']}:{hit['line_number']}")
    last_location = str(template_merge.get("last_location") or "")
    if last_location and last_location != first_location:
        return f"{first_location} ~ {last_location}"
    return first_location


def hit_line_label(hit: dict[str, Any]) -> str:
    line = str(hit.get("matched_line") or hit.get("excerpt") or "")
    template_merge = dict(hit.get("template_merge") or {})
    hit_count = int(template_merge.get("hit_count") or 1)
    if hit_count <= 1:
        return line
    return str(hit.get("merged_summary") or build_merged_summary(hit) or line)


def build_root_diagnostics(root: Path, package: dict[str, Any]) -> dict[str, Any]:
    files: list[Path] = []
    try:
        files = [path for path in root.rglob("*") if path.is_file()]
    except OSError:
        files = []

    target_files = list(package.get("target_files") or [])
    excluded_files = list(package.get("excluded_files") or [])
    matching_outer_paths = []
    excluded_outer_paths = []
    unsupported_archives = []
    text_like_files = 0
    archive_like_files = 0
    for path in files:
        try:
            display_path = str(path.relative_to(root))
        except ValueError:
            display_path = str(path)
        lower_name = display_path.lower()
        if any(path_matches_pattern(display_path, item) for item in excluded_files):
            excluded_outer_paths.append(display_path)
            continue
        if lower_name.endswith(".zst"):
            archive_like_files += 1
            unsupported_archives.append(display_path)
            continue
        if (
            path.suffix.lower() in {".tar", ".tgz", ".zip"}
            or [suffix.lower() for suffix in path.suffixes][-2:] == [".tar", ".gz"]
        ):
            archive_like_files += 1
            matching_outer_paths.append(display_path)
            continue
        if is_text_member(display_path):
            text_like_files += 1
        if should_scan_path(display_path, package):
            matching_outer_paths.append(display_path)

    warnings: list[str] = []
    if not files:
        warnings.append("No files found under search root; check --search-root and attachment materialization.")
    elif not matching_outer_paths:
        if target_files:
            warnings.append("Files exist, but none matched target_files/excluded_files filters; loosen target_files or choose a log root.")
        else:
            warnings.append("Files exist, but no supported text/log/archive documents were selected.")
    if unsupported_archives:
        warnings.append("Found .zst archives; search_worker cannot read zstd payloads in-place, so decompress them before searching or point --search-root at an extracted directory.")

    return {
        "root_file_count": len(files),
        "text_like_file_count": text_like_files,
        "archive_like_file_count": archive_like_files,
        "unsupported_archive_count": len(unsupported_archives),
        "sample_unsupported_archives": unsupported_archives[:10],
        "outer_candidate_count": len(matching_outer_paths),
        "excluded_outer_file_count": len(excluded_outer_paths),
        "sample_outer_candidates": matching_outer_paths[:10],
        "warnings": warnings,
    }


def build_search_warnings(result: dict[str, Any], diagnostics: dict[str, Any]) -> list[str]:
    warnings = [str(item) for item in diagnostics.get("warnings", []) if str(item).strip()]
    if int(result.get("documents_scanned") or 0) == 0:
        root_file_count = int(diagnostics.get("root_file_count") or 0)
        if root_file_count:
            warnings.append(
                "Documents scanned is 0 although files exist; likely target_files are too narrow, archives are unsupported/corrupt, or the root is not the extracted log directory."
            )
    if result.get("hits_truncated"):
        warnings.append("Accepted hits exceed returned hits; evidence_hits are score-ranked and should be reranked/noise-filtered before final reasoning.")
    if int(result.get("non_anchor_seed_suppressed_total") or 0) > 0:
        warnings.append("Non-anchor seed hits were sampled because anchor_match_mode=prefer; anchor-bearing hits remain fully considered for ranking.")
    return dedupe_terms(warnings)


def scan_documents(
    *,
    root: Path,
    package: dict[str, Any],
    max_hits: int,
    context_lines: int,
    max_documents: int,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root_diagnostics = build_root_diagnostics(root, package)
    candidate_hits_by_key: dict[str, dict[str, Any]] = {}
    candidate_rank_heap: list[tuple[tuple[int, int, int, str, int], str]] = []
    pending_template_hits_by_key: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    per_file_counter: Counter[str] = Counter()
    suppressed_counter: Counter[str] = Counter()
    non_anchor_seed_counter: Counter[str] = Counter()
    non_anchor_seed_suppressed_counter: Counter[str] = Counter()
    order_candidate_counter: Counter[str] = Counter()
    order_candidate_sources: dict[str, set[str]] = {}
    order_candidate_samples: dict[str, list[dict[str, Any]]] = {}
    scanned_documents = 0
    accepted_hits_total = 0
    candidate_pool_truncated = False
    raw_candidate_sequence = 0
    scanned_paths: list[str] = []
    skipped_documents: list[dict[str, str]] = []
    collect_order_candidates = should_collect_order_candidates(state)
    vehicle_name = ""
    if state:
        vehicle_name = str(dict(state.get("evidence_anchor") or {}).get("vehicle_name") or "").strip()
    max_candidate_hits = max(max_hits * MAX_CANDIDATE_MULTIPLIER, max_hits + 1000)
    time_window_bounds = compile_time_window(package)
    runtime = compile_search_runtime(package)
    anchor_terms_lc = [term_lc for _term, term_lc in runtime["anchor_entries"]]
    anchor_prefer_mode = (
        bool(anchor_terms_lc)
        and str(package.get("anchor_match_mode") or "").strip().lower() == "prefer"
        and not package.get("require_anchor")
    )
    try:
        non_anchor_sample_limit = int(
            package.get("non_anchor_sample_limit_per_file")
            or DEFAULT_NON_ANCHOR_SAMPLE_LIMIT_PER_FILE
        )
    except (TypeError, ValueError):
        non_anchor_sample_limit = DEFAULT_NON_ANCHOR_SAMPLE_LIMIT_PER_FILE
    try:
        min_template_merge_hits = int(package.get("min_template_merge_hits") or DEFAULT_MIN_TEMPLATE_MERGE_HITS)
    except (TypeError, ValueError):
        min_template_merge_hits = DEFAULT_MIN_TEMPLATE_MERGE_HITS
    min_template_merge_hits = max(2, min_template_merge_hits)

    def add_candidate(candidate_key: str, prepared_hit: dict[str, Any]) -> None:
        nonlocal candidate_pool_truncated
        if len(candidate_hits_by_key) < max_candidate_hits:
            candidate_hits_by_key[candidate_key] = prepared_hit
            heapq.heappush(candidate_rank_heap, (hit_rank_key(prepared_hit), candidate_key))
            return
        candidate_pool_truncated = True
        worst = current_worst_candidate(candidate_hits_by_key, candidate_rank_heap)
        if not worst:
            candidate_hits_by_key[candidate_key] = prepared_hit
            heapq.heappush(candidate_rank_heap, (hit_rank_key(prepared_hit), candidate_key))
            return
        worst_rank, worst_key, _worst_hit = worst
        prepared_rank = hit_rank_key(prepared_hit)
        if prepared_rank > worst_rank:
            heapq.heappop(candidate_rank_heap)
            del candidate_hits_by_key[worst_key]
            candidate_hits_by_key[candidate_key] = prepared_hit
            heapq.heappush(candidate_rank_heap, (prepared_rank, candidate_key))

    def next_raw_candidate_key() -> str:
        nonlocal raw_candidate_sequence
        raw_candidate_sequence += 1
        return f"raw:{raw_candidate_sequence}"

    def keep_hit(hit: dict[str, Any]) -> None:
        prepared_hit = prepare_template_merge_hit(hit)
        merge_key = str(prepared_hit.get("template_key") or "").strip()
        if not merge_key:
            add_candidate(next_raw_candidate_key(), prepared_hit)
            return

        if merge_key in candidate_hits_by_key:
            candidate_hits_by_key[merge_key] = merge_template_hits(candidate_hits_by_key[merge_key], prepared_hit)
            heapq.heappush(candidate_rank_heap, (hit_rank_key(candidate_hits_by_key[merge_key]), merge_key))
            return

        pending_hits = pending_template_hits_by_key.setdefault(merge_key, [])
        if len(pending_hits) + 1 < min_template_merge_hits:
            raw_key = next_raw_candidate_key()
            pending_hits.append((raw_key, prepared_hit))
            add_candidate(raw_key, prepared_hit)
            return

        pending_template_hits_by_key.pop(merge_key, None)
        for raw_key, _pending_hit in pending_hits:
            candidate_hits_by_key.pop(raw_key, None)
        if pending_hits:
            merged_hit = pending_hits[0][1]
            for _raw_key, pending_hit in pending_hits[1:]:
                merged_hit = merge_template_hits(merged_hit, pending_hit)
            merged_hit = merge_template_hits(merged_hit, prepared_hit)
        else:
            merged_hit = prepared_hit
        add_candidate(merge_key, merged_hit)

    for display_path, lines_iter in iter_documents(root, package):
        if scanned_documents >= max_documents:
            skipped_documents.append({"path": display_path, "reason": "max_documents_reached"})
            continue
        scanned_documents += 1
        scanned_paths.append(display_path)
        recent_lines: deque[str] = deque(maxlen=max(0, context_lines))
        post_context = 0
        pending_hit: dict[str, Any] | None = None
        last_timestamp = ""
        last_window_position = 0
        path_file_priority = file_priority_score(display_path, package, runtime["file_priority_rules"])
        path_file_targets = matched_file_targets(display_path, package)

        try:
            for line_number, raw_line in enumerate(lines_iter, start=1):
                line = raw_line.rstrip("\r")
                ts = parse_timestamp(line)
                if ts:
                    last_timestamp = ts
                    fast_position = fast_plain_window_position(ts, time_window_bounds)
                    last_window_position = (
                        fast_position
                        if fast_position is not None
                        else timestamp_window_position(ts, time_window_bounds)
                    )
                window_position = last_window_position
                if window_position > 0:
                    break
                if window_position < 0:
                    recent_lines.append(line)
                    continue
                line_lower = line.lower()
                line_has_anchor = any(term in line_lower for term in anchor_terms_lc)
                if (
                    anchor_prefer_mode
                    and not line_has_anchor
                    and non_anchor_seed_counter[display_path] >= non_anchor_sample_limit
                ):
                    non_anchor_seed_suppressed_counter[display_path] += 1
                    recent_lines.append(line)
                    continue
                if (
                    collect_order_candidates
                    and vehicle_name
                    and vehicle_name in line
                ):
                    for candidate in extract_order_candidates_from_line(line=line, vehicle_name=vehicle_name):
                        order_id = candidate["order_id"]
                        source = candidate["source"]
                        order_candidate_counter[order_id] += 1
                        order_candidate_sources.setdefault(order_id, set()).add(source)
                        samples = order_candidate_samples.setdefault(order_id, [])
                        if len(samples) < 3:
                            samples.append({
                                "path": display_path,
                                "line_number": line_number,
                                "timestamp": last_timestamp,
                                "line": line[:600],
                            })
                if pending_hit is not None and post_context > 0:
                    pending_hit["excerpt_lines"].append(line)
                    post_context -= 1
                    if post_context == 0:
                        keep_hit(finalize_hit(pending_hit))
                        pending_hit = None

                match_info = classify_match(line, package, runtime, line_lower)
                matched_terms = list(match_info.get("matched_terms") or [])
                if (
                    not match_info.get("accepted")
                    and matched_terms
                    and match_info.get("suppressed_reason")
                ):
                    suppressed_counter[display_path] += 1

                if match_info.get("accepted") and matched_terms:
                    if anchor_prefer_mode and not match_info.get("matched_anchor_terms"):
                        non_anchor_seed_counter[display_path] += 1
                    if pending_hit is not None:
                        keep_hit(finalize_hit(pending_hit))
                        pending_hit = None
                        post_context = 0
                    hit_score, score_reasons, priority_score = score_hit(
                        path_str=display_path,
                        timestamp_text=last_timestamp,
                        match_info=match_info,
                        package=package,
                        runtime=runtime,
                        file_priority=path_file_priority,
                    )
                    pending_hit = {
                        "path": display_path,
                        "line_number": line_number,
                        "timestamp": last_timestamp,
                        "matched_terms": matched_terms,
                        "matched_anchor_terms": list(match_info.get("matched_anchor_terms") or []),
                        "matched_gate_terms": list(match_info.get("matched_gate_terms") or []),
                        "matched_core_terms": list(match_info.get("matched_core_terms") or []),
                        "matched_exception_terms": list(match_info.get("matched_exception_terms") or []),
                        "matched_generic_terms": list(match_info.get("matched_generic_terms") or []),
                        "matched_file_targets": path_file_targets,
                        "score": hit_score,
                        "score_reasons": score_reasons,
                        "file_priority_score": priority_score,
                        "matched_line": line,
                        "match_reason": build_match_reason(
                            path_str=display_path,
                            timestamp_text=last_timestamp,
                            matched_terms=matched_terms,
                            package=package,
                            file_targets=path_file_targets,
                        ),
                        "excerpt_lines": list(recent_lines) + [line],
                        "exclude_terms": package.get("exclude_terms") or [],
                    }
                    per_file_counter[display_path] += 1
                    accepted_hits_total += 1
                    post_context = context_lines
                    if post_context == 0:
                        keep_hit(finalize_hit(pending_hit))
                        pending_hit = None

                recent_lines.append(line)
        except (OSError, EOFError, UnicodeDecodeError, gzip.BadGzipFile, zipfile.BadZipFile) as exc:
            skipped_documents.append({"path": display_path, "reason": f"read_error:{type(exc).__name__}"})
            continue

        if pending_hit is not None:
            keep_hit(finalize_hit(pending_hit))

    candidate_hits = [
        finalize_template_merge_metadata(hit)
        for hit in candidate_hits_by_key.values()
    ]
    hits = sorted(candidate_hits, key=hit_rank_key, reverse=True)[:max_hits]
    timeline_hits = sorted(hits, key=hit_timeline_key)
    searched_terms = package.get("include_terms") or []
    matched_terms = sorted({term for hit in hits for term in hit["matched_terms"]})
    hits_truncated = accepted_hits_total > len(hits) or candidate_pool_truncated
    template_groups = [
        summarize_template_group(hit)
        for hit in candidate_hits
        if int(dict(hit.get("template_merge") or {}).get("hit_count") or 1) > 1
    ]
    template_groups.sort(key=lambda item: (-int(item.get("hit_count") or 0), str(item.get("first_timestamp") or "")))
    template_merged_hits_total = sum(max(0, int(item.get("hit_count") or 0) - 1) for item in template_groups)
    result = {
        "search_root": str(root.resolve()),
        "keyword_package": package,
        "root_diagnostics": root_diagnostics,
        "documents_scanned": scanned_documents,
        "documents_considered": len(scanned_paths),
        "hits_total": len(hits),
        "returned_hits_total": len(hits),
        "returned_hits_limit": max_hits,
        "accepted_hits_total": accepted_hits_total,
        "candidate_hits_total": len(candidate_hits),
        "candidate_pool_limit": max_candidate_hits,
        "ranking_mode": "template_merge_then_score_desc_then_file_priority_then_term_count_then_timestamp",
        "hits_truncated": hits_truncated,
        "needs_rerank": bool(hits and (hits_truncated or accepted_hits_total > min(max_hits, 20) or sum(suppressed_counter.values()))),
        "template_merge_min_hits": min_template_merge_hits,
        "template_groups_total": len(template_groups),
        "template_merged_hits_total": template_merged_hits_total,
        "template_top_groups": template_groups[:10],
        "searched_terms": searched_terms,
        "matched_terms": matched_terms,
        "unmatched_terms": [term for term in searched_terms if term not in matched_terms],
        "top_files": [
            {"path": path, "hits": count}
            for path, count in per_file_counter.most_common(10)
        ],
        "suppressed_hits_total": int(sum(suppressed_counter.values())),
        "suppressed_top_files": [
            {"path": path, "hits": count}
            for path, count in suppressed_counter.most_common(10)
        ],
        "non_anchor_seed_hits_total": int(sum(non_anchor_seed_counter.values())),
        "non_anchor_seed_suppressed_total": int(sum(non_anchor_seed_suppressed_counter.values())),
        "non_anchor_seed_top_files": [
            {"path": path, "hits": count}
            for path, count in non_anchor_seed_suppressed_counter.most_common(10)
        ],
        "order_candidates": [
            {
                "order_id": order_id,
                "hits": count,
                "sources": sorted(order_candidate_sources.get(order_id) or []),
                "samples": order_candidate_samples.get(order_id) or [],
            }
            for order_id, count in order_candidate_counter.most_common(10)
        ],
        "evidence_hits": hits,
        "timeline_hits": timeline_hits,
        "skipped_documents": skipped_documents,
    }
    result["search_warnings"] = build_search_warnings(result, root_diagnostics)
    return result


def finalize_hit(pending_hit: dict[str, Any]) -> dict[str, Any]:
    exclude_terms = [term.lower() for term in pending_hit.pop("exclude_terms", []) if term]
    excerpt_lines = [
        line for line in pending_hit.pop("excerpt_lines")
        if not any(term in line.lower() for term in exclude_terms)
    ]
    if not excerpt_lines:
        excerpt_lines = [""]
    excerpt = "\n".join(excerpt_lines)
    pending_hit["excerpt"] = excerpt[:1200]
    return pending_hit


def write_markdown_summary(output_path: Path, result: dict[str, Any]) -> None:
    round_no = int(result.get("round_no") or 0)
    focus_question = str(result.get("focus_question") or "").strip()
    window_label = format_time_window(dict(result.get("keyword_package", {}).get("time_window") or {}))
    lines = [
        "# Evidence Summary",
        "",
    ]
    if round_no:
        lines.append(f"- Narrowing round: {round_no}")
    if focus_question:
        lines.append(f"- Focus question: {focus_question}")
    if window_label:
        lines.append(f"- Time window: {window_label}")
    root_diagnostics = dict(result.get("root_diagnostics") or {})
    lines.extend([
        f"- Search root: `{result['search_root']}`",
        f"- Root files: {int(root_diagnostics.get('root_file_count') or 0)}",
        f"- Documents scanned: {result['documents_scanned']}",
        f"- Ranking mode: {result.get('ranking_mode') or 'score_desc'}",
        f"- Returned hits: {int(result.get('returned_hits_total') or result['hits_total'])}/{int(result.get('returned_hits_limit') or result['hits_total'])}",
        f"- Accepted hits total: {int(result.get('accepted_hits_total') or result['hits_total'])}",
        f"- Template merge min hits: {int(result.get('template_merge_min_hits') or 0)}",
        f"- Template groups merged: {int(result.get('template_groups_total') or 0)}",
        f"- Template duplicate hits merged: {int(result.get('template_merged_hits_total') or 0)}",
        f"- Hits truncated: {'yes' if result['hits_truncated'] else 'no'}",
        f"- Needs rerank: {'yes' if result.get('needs_rerank') else 'no'}",
        f"- Suppressed hits: {int(result.get('suppressed_hits_total') or 0)}",
        f"- Non-anchor seed hits sampled: {int(result.get('non_anchor_seed_hits_total') or 0)}",
        f"- Non-anchor seed hits suppressed: {int(result.get('non_anchor_seed_suppressed_total') or 0)}",
        f"- Matched terms: {', '.join(result['matched_terms']) if result['matched_terms'] else 'none'}",
        f"- Unmatched terms: {', '.join(result['unmatched_terms']) if result['unmatched_terms'] else 'none'}",
    ])
    if result.get("search_warnings"):
        lines.extend(["", "## Search Warnings", ""])
        for warning in result["search_warnings"]:
            lines.append(f"- {warning}")

    lines.extend(["", "## Top Files", ""])
    if result["top_files"]:
        for item in result["top_files"]:
            lines.append(f"- `{item['path']}`: {item['hits']} hits")
    else:
        lines.append("- No matching files")

    if result.get("order_candidates"):
        lines.extend(["", "## Order Candidates", ""])
        for item in result["order_candidates"]:
            source_label = ", ".join(item.get("sources") or []) or "unknown"
            lines.append(f"- `{item['order_id']}`: {item['hits']} hits ({source_label})")

    if result.get("suppressed_top_files"):
        lines.extend(["", "## Suppressed Files", ""])
        for item in result["suppressed_top_files"]:
            lines.append(f"- `{item['path']}`: {item['hits']} suppressed hits")

    if result.get("non_anchor_seed_top_files"):
        lines.extend(["", "## Non-Anchor Seed Sampling", ""])
        for item in result["non_anchor_seed_top_files"]:
            lines.append(f"- `{item['path']}`: {item['hits']} sampled-out seed hits")

    if result.get("template_top_groups"):
        lines.extend(["", "## Template Groups", ""])
        lines.append("| 模板 | 次数 | 时间范围 | 位置范围 | 变化事实 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in result["template_top_groups"]:
            template = markdown_table_cell(
                f"{item.get('vehicle') or '-'} task:{item.get('task_key') or '-'} {item.get('class_name') or '-'} line:{item.get('source_line') or '-'}",
                limit=120,
            )
            count = markdown_table_cell(str(item.get("hit_count") or 0), limit=20)
            time_range = markdown_table_cell(
                f"{item.get('first_timestamp') or ''} ~ {item.get('last_timestamp') or ''}",
                limit=80,
            )
            location_range = markdown_table_cell(
                f"{item.get('first_location') or ''} ~ {item.get('last_location') or ''}",
                limit=120,
            )
            facts = markdown_table_cell("; ".join(item.get("change_facts") or []), limit=180)
            lines.append(f"| {template} | {count} | {time_range} | {location_range} | {facts} |")

    lines.extend(["", "## Evidence Timeline", ""])
    timeline_hits = list(result.get("timeline_hits") or result.get("evidence_hits") or [])
    if timeline_hits:
        lines.append("| 时间 | 得分 | 日志原文 | 日志文件 | 匹配关键词 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for hit in timeline_hits:
            timestamp = markdown_table_cell(hit_time_label(hit), limit=60)
            score = markdown_table_cell(str(hit.get("score") or 0), limit=20)
            matched_line = markdown_table_cell(hit_line_label(hit))
            log_file = markdown_table_cell(hit_location_label(hit), limit=120)
            keywords = markdown_table_cell(", ".join(hit.get("matched_terms") or []), limit=120)
            lines.append(f"| {timestamp} | {score} | {matched_line} | {log_file} | {keywords} |")
    else:
        lines.append("- No hits")

    lines.extend(["", "## Evidence Table", ""])
    if result["evidence_hits"]:
        lines.append("| 得分 | 日志原文 | 日志文件 | 匹配关键词 |")
        lines.append("| --- | --- | --- | --- |")
        for hit in result["evidence_hits"]:
            score = markdown_table_cell(str(hit.get("score") or 0), limit=20)
            matched_line = markdown_table_cell(hit_line_label(hit))
            log_file = markdown_table_cell(hit_location_label(hit), limit=120)
            keywords = markdown_table_cell(", ".join(hit.get("matched_terms") or []), limit=120)
            lines.append(f"| {score} | {matched_line} | {log_file} | {keywords} |")
    else:
        lines.append("- No hits")

    lines.extend(["", "## Evidence Hits", ""])
    if result["evidence_hits"]:
        for hit in result["evidence_hits"]:
            lines.append(f"### `{hit['path']}` line {hit['line_number']}")
            lines.append(f"- Score: `{int(hit.get('score') or 0)}`")
            if hit.get("score_reasons"):
                lines.append(f"- Score reasons: {', '.join(hit.get('score_reasons') or [])}")
            if hit["timestamp"]:
                lines.append(f"- Timestamp: `{hit['timestamp']}`")
            lines.append(f"- Matched terms: {', '.join(hit['matched_terms'])}")
            template_merge = dict(hit.get("template_merge") or {})
            if int(template_merge.get("hit_count") or 1) > 1:
                if hit.get("merged_summary"):
                    lines.append(f"- Merged summary: {hit.get('merged_summary')}")
                lines.append(f"- Template merged hits: `{int(template_merge.get('hit_count') or 1)}`")
                lines.append(f"- Template time range: `{template_merge.get('first_timestamp') or ''}` ~ `{template_merge.get('last_timestamp') or ''}`")
                if template_merge.get("change_facts"):
                    lines.append(f"- Template change facts: {'; '.join(template_merge.get('change_facts') or [])}")
                if template_merge.get("samples"):
                    sample_labels = [
                        f"{sample.get('timestamp') or ''} {sample.get('path') or ''}:{sample.get('line_number') or 0}"
                        for sample in template_merge.get("samples") or []
                    ]
                    lines.append(f"- Template samples: {'; '.join(sample_labels)}")
            if hit.get("matched_file_targets"):
                lines.append(f"- Matched target files: {', '.join(hit['matched_file_targets'])}")
            if hit.get("match_reason"):
                lines.append(f"- Match reason: {hit['match_reason']}")
            lines.append("```text")
            lines.append(hit["excerpt"])
            lines.append("```")
            lines.append("")
    else:
        lines.append("- No hits")
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

def update_state_after_search(
    state_path: Path,
    result_json: Path,
    summary_md: Path,
    result: dict[str, Any],
    round_no: int,
) -> None:
    state = load_state(state_path)
    state["search_status"] = "returned"
    search_artifacts = dict(state.get("search_artifacts") or {})
    search_artifacts.update({
        "last_result_json": str(result_json),
        "last_result_md": str(summary_md),
    })
    dsl_query_file = str(result.get("dsl_query_file") or "").strip()
    if dsl_query_file and round_no:
        search_artifacts[f"dsl_round{round_no}"] = dsl_query_file
    state["search_artifacts"] = search_artifacts
    state["order_candidates"] = list(result.get("order_candidates") or [])
    state["target_log_files"] = list(result.get("keyword_package", {}).get("target_files") or [])
    time_window = dict(result.get("keyword_package", {}).get("time_window") or {})
    state["time_alignment"]["normalized_window"] = {
        "start": str(time_window.get("start") or "").strip(),
        "end": str(time_window.get("end") or "").strip(),
    }
    state["delegation"]["status"] = "completed"
    state["delegation"]["last_scope"] = result.get("search_root", "")
    history = list(state.get("narrowing_round", {}).get("history") or [])
    history.append({
        "round": round_no,
        "focus_question": state.get("current_question") or state.get("primary_question") or state.get("problem_summary", ""),
        "time_window": time_window,
        "target_files": state["target_log_files"],
        "include_terms": list(result.get("keyword_package", {}).get("include_terms") or []),
        "exclude_terms": list(result.get("keyword_package", {}).get("exclude_terms") or []),
        "hits_total": int(result.get("hits_total") or 0),
        "matched_terms": list(result.get("matched_terms") or []),
        "unmatched_terms": list(result.get("unmatched_terms") or []),
        "dsl_query": str(result.get("dsl_query") or ""),
        "dsl_query_file": dsl_query_file,
        "result_json": str(result_json),
        "summary_md": str(summary_md),
    })
    state["narrowing_round"]["history"] = history
    if result["hits_total"] > 0 and state.get("evidence_chain_status") == "weak":
        state["evidence_chain_status"] = "partial"
    if state.get("phase") in {"keywords_ready", "search_delegated"}:
        state["phase"] = "evidence_reviewed"
    save_state(state_path, state)


def mark_search_started(state_path: Path, *, search_root: Path) -> int:
    state = load_state(state_path)
    next_round = int(state.get("narrowing_round", {}).get("current") or 0) + 1
    state["narrowing_round"]["current"] = next_round
    state["search_status"] = "delegated"
    state["delegation"]["search_mode"] = "subagent_or_skill"
    state["delegation"]["status"] = "running"
    state["delegation"]["last_scope"] = str(search_root)
    if state.get("phase") == "keywords_ready":
        state["phase"] = "search_delegated"
    save_state(state_path, state)
    return next_round


def main() -> int:
    args = parse_args()
    search_root = Path(args.search_root).resolve()
    if not search_root.exists():
        raise SystemExit(f"Search root does not exist: {search_root}")

    state_path = Path(args.state).resolve() if args.state else None
    state = load_state(state_path) if state_path else None
    package = normalize_package(load_keyword_package(args))
    package = enforce_state_constraints(package, state)
    output_dir = make_output_dir(args, state_path, search_root)
    round_no = 0
    if state_path:
        round_no = mark_search_started(state_path, search_root=search_root)
    package, dsl_path = materialize_dsl_query(
        args=args,
        package=package,
        state_path=state_path,
        output_dir=output_dir,
        round_no=round_no,
    )

    result = scan_documents(
        root=search_root,
        package=package,
        max_hits=args.max_hits,
        context_lines=args.context_lines,
        max_documents=args.max_documents,
        state=state,
    )
    if state_path:
        current_state = load_state(state_path)
        result["focus_question"] = (
            current_state.get("current_question")
            or current_state.get("primary_question")
            or current_state.get("problem_summary")
            or ""
        )
    else:
        result["focus_question"] = ""
    result["round_no"] = round_no
    result["dsl_query"] = str(package.get("dsl_query") or "")
    result["dsl_query_file"] = str(dsl_path) if dsl_path else ""

    result_json = output_dir / "search_results.json"
    result_md = output_dir / "evidence_summary.md"
    result_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown_summary(result_md, result)

    if state_path:
        update_state_after_search(state_path, result_json, result_md, result, round_no)

    payload = {
        "output_dir": str(output_dir),
        "result_json": str(result_json),
        "summary_md": str(result_md),
        "round_no": round_no,
        "dsl_query_file": str(dsl_path) if dsl_path else "",
        "hits_total": result["hits_total"],
        "top_files": result["top_files"],
        "unmatched_terms": result["unmatched_terms"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
