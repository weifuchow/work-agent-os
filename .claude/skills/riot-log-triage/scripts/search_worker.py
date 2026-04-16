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
    parser.add_argument("--max-hits", type=int, default=40, help="Maximum evidence hits to keep.")
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
    include_terms = [str(item).strip() for item in raw.get("include_terms", []) if str(item).strip()]
    exclude_terms = [str(item).strip() for item in raw.get("exclude_terms", []) if str(item).strip()]
    target_files = [str(item).strip() for item in raw.get("target_files", []) if str(item).strip()]
    hypotheses = [str(item).strip() for item in raw.get("hypotheses", []) if str(item).strip()]
    normalized = {
        "include_terms": include_terms,
        "exclude_terms": exclude_terms,
        "target_files": target_files,
        "hypotheses": hypotheses,
        "require_all_terms": bool(raw.get("require_all_terms", False)),
        "time_window": normalize_time_window(raw.get("time_window") or {}),
    }
    return normalized


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
    targets = package.get("target_files") or []
    if not targets:
        return True
    lowered = path_str.lower()
    return any(target.lower() in lowered for target in targets)


def iter_documents(root: Path, package: dict[str, Any]) -> Iterator[tuple[str, Iterator[str]]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        display_path = str(path.relative_to(root))
        if not should_scan_path(display_path, package):
            continue
        suffixes = [suffix.lower() for suffix in path.suffixes]
        if path.suffix.lower() in TEXT_EXTENSIONS or path.suffix == "":
            yield display_path, iter_text_lines(path)
            continue
        if suffixes[-2:] == [".tar", ".gz"] or path.suffix.lower() in {".tgz", ".tar"}:
            yield from iter_tar_documents(path, root, package)
            continue
        if path.suffix.lower() == ".zip":
            yield from iter_zip_documents(path, root, package)
            continue
        if path.suffix.lower() == ".gz":
            yield display_path, iter_gzip_lines(path)


def iter_text_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\n")


def iter_gzip_lines(path: Path) -> Iterator[str]:
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\n")


def iter_tar_documents(path: Path, root: Path, package: dict[str, Any]) -> Iterator[tuple[str, Iterator[str]]]:
    mode = "r:gz" if path.suffix.lower() in {".gz", ".tgz"} else "r:"
    with tarfile.open(path, mode) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            display_path = f"{path.relative_to(root)}::{member.name}"
            if not should_scan_path(display_path, package):
                continue
            if not is_text_member(member.name):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            if member.name.lower().endswith(".gz"):
                yield display_path, iter_gzip_member_lines(extracted)
            else:
                yield display_path, iter_bytes_lines(extracted)


def iter_zip_documents(path: Path, root: Path, package: dict[str, Any]) -> Iterator[tuple[str, Iterator[str]]]:
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            display_path = f"{path.relative_to(root)}::{name}"
            if not should_scan_path(display_path, package):
                continue
            if not is_text_member(name):
                continue
            with archive.open(name, "r") as handle:
                if name.lower().endswith(".gz"):
                    yield display_path, iter_gzip_member_lines(handle)
                else:
                    yield display_path, iter_bytes_lines(handle)


def is_text_member(name: str) -> bool:
    lower_name = name.lower()
    if any(lower_name.endswith(ext) for ext in TEXT_EXTENSIONS):
        return True
    return lower_name.endswith(".log.gz")


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


def match_terms(text: str, package: dict[str, Any]) -> list[str]:
    lowered = text.lower()
    exclude_terms = package.get("exclude_terms") or []
    if any(term.lower() in lowered for term in exclude_terms):
        return []
    include_terms = package.get("include_terms") or []
    if not include_terms:
        return []
    matched = [term for term in include_terms if term.lower() in lowered]
    if package.get("require_all_terms"):
        return matched if len(matched) == len(include_terms) else []
    return matched


def scan_documents(
    *,
    root: Path,
    package: dict[str, Any],
    max_hits: int,
    context_lines: int,
    max_documents: int,
) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    per_file_counter: Counter[str] = Counter()
    scanned_documents = 0
    truncated = False
    scanned_paths: list[str] = []
    skipped_documents: list[dict[str, str]] = []

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
            if pending_hit is not None and post_context > 0:
                pending_hit["excerpt_lines"].append(line)
                post_context -= 1
                if post_context == 0:
                    finalize_hit(hits, pending_hit)
                    pending_hit = None

            matched_terms = match_terms(line, package)
            if matched_terms and timestamp_in_window(last_timestamp, package):
                if len(hits) >= max_hits:
                    truncated = True
                    break
                pending_hit = {
                    "path": display_path,
                    "line_number": line_number,
                    "timestamp": last_timestamp,
                    "matched_terms": matched_terms,
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
    lines = [
        "# Evidence Summary",
        "",
        f"- Search root: `{result['search_root']}`",
        f"- Documents scanned: {result['documents_scanned']}",
        f"- Hits total: {result['hits_total']}",
        f"- Hits truncated: {'yes' if result['hits_truncated'] else 'no'}",
        f"- Matched terms: {', '.join(result['matched_terms']) if result['matched_terms'] else 'none'}",
        f"- Unmatched terms: {', '.join(result['unmatched_terms']) if result['unmatched_terms'] else 'none'}",
        "",
        "## Top Files",
        "",
    ]
    if result["top_files"]:
        for item in result["top_files"]:
            lines.append(f"- `{item['path']}`: {item['hits']} hits")
    else:
        lines.append("- No matching files")

    lines.extend(["", "## Evidence Hits", ""])
    if result["evidence_hits"]:
        for hit in result["evidence_hits"]:
            lines.append(f"### `{hit['path']}` line {hit['line_number']}")
            if hit["timestamp"]:
                lines.append(f"- Timestamp: `{hit['timestamp']}`")
            lines.append(f"- Matched terms: {', '.join(hit['matched_terms'])}")
            lines.append("```text")
            lines.append(hit["excerpt"])
            lines.append("```")
            lines.append("")
    else:
        lines.append("- No hits")
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def update_state_after_search(state_path: Path, result_json: Path, summary_md: Path, hits_total: int) -> None:
    state = load_state(state_path)
    state["search_status"] = "returned"
    state["search_artifacts"] = {
        "last_result_json": str(result_json),
        "last_result_md": str(summary_md),
    }
    if hits_total > 0 and state.get("evidence_chain_status") == "weak":
        state["evidence_chain_status"] = "partial"
    if state.get("phase") == "keywords_ready":
        state["phase"] = "search_delegated"
    save_state(state_path, state)


def mark_search_started(state_path: Path) -> None:
    state = load_state(state_path)
    state["search_status"] = "delegated"
    if state.get("phase") == "keywords_ready":
        state["phase"] = "search_delegated"
    save_state(state_path, state)


def main() -> int:
    args = parse_args()
    search_root = Path(args.search_root).resolve()
    if not search_root.exists():
        raise SystemExit(f"Search root does not exist: {search_root}")

    state_path = Path(args.state).resolve() if args.state else None
    package = normalize_package(load_keyword_package(args))
    output_dir = make_output_dir(args, state_path, search_root)
    if state_path:
        mark_search_started(state_path)

    result = scan_documents(
        root=search_root,
        package=package,
        max_hits=args.max_hits,
        context_lines=args.context_lines,
        max_documents=args.max_documents,
    )

    result_json = output_dir / "search_results.json"
    result_md = output_dir / "evidence_summary.md"
    result_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown_summary(result_md, result)

    if state_path:
        update_state_after_search(state_path, result_json, result_md, result["hits_total"])

    payload = {
        "output_dir": str(output_dir),
        "result_json": str(result_json),
        "summary_md": str(result_md),
        "hits_total": result["hits_total"],
        "top_files": result["top_files"],
        "unmatched_terms": result["unmatched_terms"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
