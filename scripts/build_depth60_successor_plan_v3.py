"""Write the non-executable Depth-60 source-integrity successor v3.

Run this generator only after the local offline implementation is stable and
ready for PR review. It refuses to overwrite any plan and verifies that both
historical plan files remain byte-identical while writing. Generating this plan
does not perform or authorize online validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from researchops.phase6_depth60 import (  # noqa: E402
    DEPTH60_PLAN_PATH,
    DEPTH60_SUCCESSOR_PLAN_PATH,
    DEPTH60_SUCCESSOR_V3_PLAN_PATH,
    build_depth60_successor_plan_v3,
    validate_phase6_depth60_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locked-at-utc", default=None)
    arguments = parser.parse_args()

    target = ROOT / DEPTH60_SUCCESSOR_V3_PLAN_PATH
    if target.exists():
        print(
            "successor v3 plan already exists: "
            f"{DEPTH60_SUCCESSOR_V3_PLAN_PATH.as_posix()}",
            file=sys.stderr,
        )
        return 2
    locked_at = arguments.locked_at_utc or (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    historical_paths = (DEPTH60_PLAN_PATH, DEPTH60_SUCCESSOR_PLAN_PATH)
    historical_before = {
        relative: (ROOT / relative).read_bytes() for relative in historical_paths
    }
    plan = build_depth60_successor_plan_v3(ROOT, locked_at_utc=locked_at)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        for relative, expected in historical_before.items():
            if (ROOT / relative).read_bytes() != expected:
                raise SystemExit("historical Depth-60 plan was modified; aborting")

    validation = validate_phase6_depth60_plan(ROOT, DEPTH60_SUCCESSOR_V3_PLAN_PATH)
    print(
        json.dumps(
            {
                "successor_plan": DEPTH60_SUCCESSOR_V3_PLAN_PATH.as_posix(),
                "plan_commitment_sha256": validation["plan_commitment_sha256"],
                "online_execution_authorized": False,
                "network_calls": 0,
                "model_calls": 0,
                "historical_plans_unchanged": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
