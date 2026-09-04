"""Write the non-executable Depth-60 source-integrity successor v5 once.

The generator never overwrites v1 through v5 and performs no network or
Provider call.
"""

from __future__ import annotations

import argparse
import hashlib
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
    DEPTH60_SUCCESSOR_V4_PLAN_PATH,
    DEPTH60_SUCCESSOR_V5_PLAN_PATH,
    build_depth60_successor_plan_v5,
    validate_phase6_depth60_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locked-at-utc", default=None)
    arguments = parser.parse_args()

    target = ROOT / DEPTH60_SUCCESSOR_V5_PLAN_PATH
    if target.exists():
        print(
            "successor v5 plan already exists: "
            f"{DEPTH60_SUCCESSOR_V5_PLAN_PATH.as_posix()}",
            file=sys.stderr,
        )
        return 2
    locked_at = arguments.locked_at_utc or (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    historical_paths = (
        DEPTH60_PLAN_PATH,
        DEPTH60_SUCCESSOR_PLAN_PATH,
        DEPTH60_SUCCESSOR_V3_PLAN_PATH,
        DEPTH60_SUCCESSOR_V4_PLAN_PATH,
    )
    historical_before = {
        relative: (ROOT / relative).read_bytes() for relative in historical_paths
    }
    plan = build_depth60_successor_plan_v5(ROOT, locked_at_utc=locked_at)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        for relative, expected in historical_before.items():
            if (ROOT / relative).read_bytes() != expected:
                raise SystemExit("historical Depth-60 plan was modified; aborting")

    validation = validate_phase6_depth60_plan(ROOT, DEPTH60_SUCCESSOR_V5_PLAN_PATH)
    payload = target.read_bytes()
    print(
        json.dumps(
            {
                "successor_plan": DEPTH60_SUCCESSOR_V5_PLAN_PATH.as_posix(),
                "successor_plan_bytes": len(payload),
                "successor_plan_sha256": hashlib.sha256(payload).hexdigest(),
                "plan_commitment_sha256": validation["plan_commitment_sha256"],
                "component_hashes": validation["plan"]["component_hashes"],
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
