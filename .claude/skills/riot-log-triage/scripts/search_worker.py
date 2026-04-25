#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, UTC
import gzip
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


def normalize_package(raw: dict[str, Any]) -> dict[str, Any]:
    anchor_terms = [str(item).strip() for item in raw.get("anchor_terms", []) if str(item).strip()]
    gate_terms = [str(item).strip() for item in raw.get("gate_terms", []) if str(item).strip()]
    generic_terms = [str(item).strip() for item in raw.get("generic_terms", []) if str(item).strip()]
    include_terms = [str(item).strip() for item in raw.get("include_terms", []) if str(item).strip()]
    if not include_terms and (anchor_terms or gate_terms or generic_terms):
        include_terms = [*anchor_terms, *gate_terms, *generic_terms]
    exclude_terms = [str(item).strip() for item in raw.get("exclude_terms", []) if str(item).strip()]
    target_files = [str(item).strip() for item in raw.get("target_files", []) if str(item).strip()]
    preferred_files = [str(item).strip() for item in raw.get("preferred_files", []) if str(item).strip()]
    excluded_files = [str(item).strip() for item in raw.get("excluded_files", []) if str(item).strip()]
    hypotheses = [str(item).strip() for item in raw.get("hypotheses", []) if str(item).strip()]
    normalized = {
        "anchor_terms": anchor_terms,
        "gate_terms": gate_terms,
        "generic_terms": generic_terms,
        "include_terms": include_terms,
        "exclude_terms": exclude_terms,
        "target_files": target_files,
        "preferred_files": preferred_files,
        "excluded_files": excluded_files,
        "hypotheses": hypotheses,
        "require_all_terms": bool(raw.get("require_all_terms", False)),
        "require_anchor": bool(raw.get("require_anchor", False)),
        "time_window": normalize_time_window(raw.get("time_window") or {}),
    }
    return normalized


def enforce_state_constraints(package: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return package

    constrained = dict(package)
    evidence_anchor = dict(state.get("evidence_anchor") or {})
    vehicle_name = str(evidence_anchor.get("vehicle_name") or "").strip()
    anchor_terms = list(constrained.get("anchor_terms") or [])
    include_terms = list(constrained.get("include_terms") or [])
    order_id = str(evidence_anchor.get("order_id") or "").strip()

    if vehicle_name:
        if vehicle_name not in anchor_terms:
            anchor_terms.insert(0, vehicle_name)
        if vehicle_name not in include_terms:
            include_terms.insert(0, vehicle_name)
    if order_id:
        if order_id not in anchor_terms:
            anchor_terms.append(order_id)
        if order_id not in include_terms:
            include_terms.append(order_id)

    if not anchor_terms:
        return constrained

    constrained["anchor_terms"] = anchor_terms
    constrained["include_terms"] = include_terms
    constrained["require_anchor"] = True
    return constrained


def normalize_time_window(raw: dict[str, Any]) -> dict[str, str]:
    start = str(raw.get("start", "") or "").strip()
    end = str(raw.get("end", "") or "").strip()
    return {"start": start, "end": end}


def make_output_dir(args: argparse.Namespace, state_path: Path | None, search_root: Path) -> Path:
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    elif state_path:
        output_dir = state_path.parent / "search-runs" / utc_now_label()
    else:
        output_dir = search_root / "search-runs" / utc_now_label()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def should_scan_path(path_str: str, package: dict[str, Any]) -> bool:
    excluded = package.get("excluded_files") or []
    lowered = path_str.lower()
    if any(excluded_name.lower() in lowered for excluded_name in excluded):
        return False
    targets = package.get("target_files") or []
    if not targets:
        return True
    return any(target.lower() in lowered for target in targets)


def _path_priority(path_str: str, package: dict[str, Any]) -> tuple[int, str]:
    lowered = path_str.lower()
    preferred = package.get("preferred_files") or []
    for index, preferred_name in enumerate(preferred):
        if preferred_name.lower() in lowered:
            return index, lowered
    return len(preferred), lowered


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
            if any(excluded_name.lower() in lowered for excluded_name in excluded):
                continue
            yield from iter_tar_documents(path, root, package)
            continue
        if path.suffix.lower() == ".zip":
            lowered = display_path.lower()
            excluded = package.get("excluded_files") or []
            if any(excluded_name.lower() in lowered for excluded_name in excluded):
                continue
            yield from iter_zip_documents(path, root, package)
            continue
        if not should_scan_path(display_path, package):
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS or path.suffix == "":
            yield display_path, iter_text_lines(path)
            continue
        if path.suffix.lower() == ".gz":
            yield display_path, iter_maybe_compressed_file(path)


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
    if any(lower_name.endswith(ext) for ext in TEXT_EXTENSIONS):
        return True
    if lower_name.endswith(".log.gz"):
        return True
    if lower_name.endswith(".gz"):
        base_name = lower_name[:-3]
        return any(ext in base_name for ext in TEXT_EXTENSIONS)
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
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("ts")
    return ""


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


def classify_match(text: str, package: dict[str, Any]) -> dict[str, Any]:
    lowered = text.lower()
    exclude_terms = package.get("exclude_terms") or []
    if any(term.lower() in lowered for term in exclude_terms):
        return {"accepted": False, "matched_terms": [], "suppressed_reason": ""}

    anchor_terms = package.get("anchor_terms") or []
    gate_terms = package.get("gate_terms") or []
    generic_terms = package.get("generic_terms") or []
    matched_anchor = [term for term in anchor_terms if term.lower() in lowered]
    matched_gate = [term for term in gate_terms if term.lower() in lowered]
    matched_generic = [term for term in generic_terms if term.lower() in lowered]

    categorized_terms = [*matched_anchor, *matched_gate, *matched_generic]
    if categorized_terms:
        if package.get("require_anchor") and anchor_terms and not matched_anchor:
            return {
                "accepted": False,
                "matched_terms": categorized_terms,
                "suppressed_reason": "missing_anchor",
            }
        return {
            "accepted": True,
            "matched_terms": categorized_terms,
            "suppressed_reason": "",
        }

    include_terms = package.get("include_terms") or []
    if not include_terms:
        return {"accepted": False, "matched_terms": [], "suppressed_reason": ""}
    matched = [term for term in include_terms if term.lower() in lowered]
    if package.get("require_all_terms"):
        return {
            "accepted": len(matched) == len(include_terms),
            "matched_terms": matched if len(matched) == len(include_terms) else [],
            "suppressed_reason": "",
        }
    return {"accepted": bool(matched), "matched_terms": matched, "suppressed_reason": ""}


def matched_file_targets(path_str: str, package: dict[str, Any]) -> list[str]:
    targets = package.get("target_files") or []
    lowered = path_str.lower()
    return [target for target in targets if target.lower() in lowered]


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
) -> str:
    reasons = [f"关键词命中: {', '.join(matched_terms)}"]
    file_targets = matched_file_targets(path_str, package)
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


def scan_documents(
    *,
    root: Path,
    package: dict[str, Any],
    max_hits: int,
    context_lines: int,
    max_documents: int,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    per_file_counter: Counter[str] = Counter()
    suppressed_counter: Counter[str] = Counter()
    order_candidate_counter: Counter[str] = Counter()
    order_candidate_sources: dict[str, set[str]] = {}
    order_candidate_samples: dict[str, list[dict[str, Any]]] = {}
    scanned_documents = 0
    truncated = False
    scanned_paths: list[str] = []
    skipped_documents: list[dict[str, str]] = []
    collect_order_candidates = should_collect_order_candidates(state)
    vehicle_name = ""
    if state:
        vehicle_name = str(dict(state.get("evidence_anchor") or {}).get("vehicle_name") or "").strip()

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

        for line_number, raw_line in enumerate(lines_iter, start=1):
            line = raw_line.rstrip("\r")
            ts = parse_timestamp(line)
            if ts:
                last_timestamp = ts
            if (
                collect_order_candidates
                and vehicle_name
                and timestamp_in_window(last_timestamp, package)
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
                    finalize_hit(hits, pending_hit)
                    pending_hit = None

            match_info = classify_match(line, package)
            matched_terms = list(match_info.get("matched_terms") or [])
            if (
                not match_info.get("accepted")
                and matched_terms
                and match_info.get("suppressed_reason")
                and timestamp_in_window(last_timestamp, package)
            ):
                suppressed_counter[display_path] += 1

            if match_info.get("accepted") and matched_terms and timestamp_in_window(last_timestamp, package):
                if len(hits) >= max_hits:
                    truncated = True
                    break
                pending_hit = {
                    "path": display_path,
                    "line_number": line_number,
                    "timestamp": last_timestamp,
                    "matched_terms": matched_terms,
                    "matched_file_targets": matched_file_targets(display_path, package),
                    "matched_line": line,
                    "match_reason": build_match_reason(
                        path_str=display_path,
                        timestamp_text=last_timestamp,
                        matched_terms=matched_terms,
                        package=package,
                    ),
                    "excerpt_lines": list(recent_lines) + [line],
                    "exclude_terms": package.get("exclude_terms") or [],
                }
                per_file_counter[display_path] += 1
                post_context = context_lines
                if post_context == 0:
                    finalize_hit(hits, pending_hit)
                    pending_hit = None

            recent_lines.append(line)

        if pending_hit is not None:
            finalize_hit(hits, pending_hit)
        if truncated:
            break

    searched_terms = package.get("include_terms") or []
    matched_terms = sorted({term for hit in hits for term in hit["matched_terms"]})
    result = {
        "search_root": str(root.resolve()),
        "keyword_package": package,
        "documents_scanned": scanned_documents,
        "documents_considered": len(scanned_paths),
        "hits_total": len(hits),
        "hits_truncated": truncated,
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
        "skipped_documents": skipped_documents,
    }
    return result


def finalize_hit(hits: list[dict[str, Any]], pending_hit: dict[str, Any]) -> None:
    exclude_terms = [term.lower() for term in pending_hit.pop("exclude_terms", []) if term]
    excerpt_lines = [
        line for line in pending_hit.pop("excerpt_lines")
        if not any(term in line.lower() for term in exclude_terms)
    ]
    if not excerpt_lines:
        excerpt_lines = [""]
    excerpt = "\n".join(excerpt_lines)
    pending_hit["excerpt"] = excerpt[:1200]
    hits.append(pending_hit)


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
    lines.extend([
        f"- Search root: `{result['search_root']}`",
        f"- Documents scanned: {result['documents_scanned']}",
        f"- Hits total: {result['hits_total']}",
        f"- Hits truncated: {'yes' if result['hits_truncated'] else 'no'}",
        f"- Suppressed hits: {int(result.get('suppressed_hits_total') or 0)}",
        f"- Matched terms: {', '.join(result['matched_terms']) if result['matched_terms'] else 'none'}",
        f"- Unmatched terms: {', '.join(result['unmatched_terms']) if result['unmatched_terms'] else 'none'}",
        "",
        "## Top Files",
        "",
    ])
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

    lines.extend(["", "## Evidence Table", ""])
    if result["evidence_hits"]:
        lines.append("| 日志原文 | 日志文件 | 匹配关键词 |")
        lines.append("| --- | --- | --- |")
        for hit in result["evidence_hits"]:
            matched_line = markdown_table_cell(str(hit.get("matched_line") or hit.get("excerpt") or ""))
            log_file = markdown_table_cell(f"{hit['path']}:{hit['line_number']}", limit=120)
            keywords = markdown_table_cell(", ".join(hit.get("matched_terms") or []), limit=120)
            lines.append(f"| {matched_line} | {log_file} | {keywords} |")
    else:
        lines.append("- No hits")

    lines.extend(["", "## Evidence Hits", ""])
    if result["evidence_hits"]:
        for hit in result["evidence_hits"]:
            lines.append(f"### `{hit['path']}` line {hit['line_number']}")
            if hit["timestamp"]:
                lines.append(f"- Timestamp: `{hit['timestamp']}`")
            lines.append(f"- Matched terms: {', '.join(hit['matched_terms'])}")
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
        "hits_total": result["hits_total"],
        "top_files": result["top_files"],
        "unmatched_terms": result["unmatched_terms"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
