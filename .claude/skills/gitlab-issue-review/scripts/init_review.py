#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from review_state import build_state, ensure_review_dir, parse_issue_url, save_state, slugify_topic


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a lightweight GitLab issue review state directory.",
    )
    parser.add_argument("--project", required=True, help="Project name, for example allspark or fms-java.")
    parser.add_argument("--issue-url", required=True, help="GitLab issue URL.")
    parser.add_argument("--topic", default="", help="Short review topic used for the state directory.")
    parser.add_argument(
        "--base-dir",
        default=".review",
        help="Base directory where review state folders are created. Default: .review",
    )
    parser.add_argument("--slug", default="", help="Optional directory slug override.")
    parser.add_argument(
        "--missing-item",
        action="append",
        default=[],
        help="Missing item to seed into the state. Can be passed multiple times.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing 00-state.json if it already exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = Path(args.base_dir).resolve()
    parsed_issue = parse_issue_url(args.issue_url)
    default_topic = f"issue-{parsed_issue['iid']}-review" if parsed_issue["iid"] else f"issue-{slugify_topic(args.issue_url, max_length=48)}"
    topic = args.topic.strip() or default_topic
    review_dir = ensure_review_dir(base_dir, topic, slug=args.slug or None)
    state_path = review_dir / "00-state.json"
    if state_path.exists() and not args.force:
        raise SystemExit(f"State file already exists: {state_path}. Use --force to overwrite it.")

    state = build_state(
        project=args.project,
        issue_url=args.issue_url,
        topic=topic,
        missing_items=args.missing_item,
    )
    state["work_dir"] = str(review_dir)
    save_state(state_path, state)

    result = {
        "review_dir": str(review_dir),
        "state_path": str(state_path),
        "project": state["project"],
        "issue_url": state["issue"]["url"],
        "phase": state["phase"],
        "mode": state["mode"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
