#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from triage_state import build_state, ensure_triage_dir, save_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a lightweight RIOT log triage state directory.",
    )
    parser.add_argument("--project", required=True, help="Project name, for example allspark or fms-java.")
    parser.add_argument("--topic", required=True, help="Short problem topic used for the triage directory.")
    parser.add_argument(
        "--base-dir",
        default=".triage",
        help="Base directory where triage state folders are created. Default: .triage",
    )
    parser.add_argument("--slug", default="", help="Optional directory slug override.")
    parser.add_argument("--version", default="", help="Known version, branch, or build identifier.")
    parser.add_argument("--problem-time", default="", help="Original problem time string from the user.")
    parser.add_argument("--module", default="", help="Best current guess for the affected module.")
    parser.add_argument("--timezone", default="", help="Timezone label for the reported problem time.")
    parser.add_argument(
        "--artifact-status",
        default="unknown",
        choices=["unknown", "partial", "complete"],
        help="How complete the current log artifact appears to be.",
    )
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
    triage_dir = ensure_triage_dir(base_dir, args.topic, slug=args.slug or None)
    state_path = triage_dir / "00-state.json"
    if state_path.exists() and not args.force:
        raise SystemExit(f"State file already exists: {state_path}. Use --force to overwrite it.")

    state = build_state(
        project=args.project,
        topic=args.topic,
        version=args.version,
        problem_time=args.problem_time,
        module=args.module,
        timezone=args.timezone,
        artifact_status=args.artifact_status,
        missing_items=args.missing_item,
    )
    state["work_dir"] = str(triage_dir)
    save_state(state_path, state)

    result = {
        "triage_dir": str(triage_dir),
        "state_path": str(state_path),
        "project": state["project"],
        "phase": state["phase"],
        "mode": state["mode"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
