"""Write the Depth-60 successor source-integrity plan.

The historical plan is never read for writing and never modified. This script
refuses to overwrite an existing successor plan; remove it deliberately if you
intend to re-commit the current bound components.
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
    DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256,
    DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256,
    DEPTH60_PLAN_PATH,
    DEPTH60_SUCCESSOR_PLAN_DOMAIN,
    DEPTH60_SUCCESSOR_PLAN_ID,
    DEPTH60_SUCCESSOR_PLAN_PATH,
    DEPTH60_SUCCESSOR_PLAN_SCHEMA_VERSION,
    build_depth60_component_hashes,
    validate_phase6_depth60_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locked-at-utc", default=None)
    arguments = parser.parse_args()

    target = ROOT / DEPTH60_SUCCESSOR_PLAN_PATH
    if target.exists():
        print(
            "successor plan already exists: "
            f"{DEPTH60_SUCCESSOR_PLAN_PATH.as_posix()}",
            file=sys.stderr,
        )
        return 2

    locked_at = arguments.locked_at_utc or (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    body = {
        "schema_version": DEPTH60_SUCCESSOR_PLAN_SCHEMA_VERSION,
        "plan_id": DEPTH60_SUCCESSOR_PLAN_ID,
        "status": "locked_offline_not_run",
        "locked_at_utc": locked_at,
        "evaluation_scope": "source_integrity_commitment_only",
        "source_bundle_algorithm": "v2",
        "component_hashes": build_depth60_component_hashes(ROOT, "v2"),
        "supersedes": {
            "plan_id": "phase6-deepseek-depth60-v1",
            "plan_commitment_sha256": (
                DEPTH60_HISTORICAL_PLAN_COMMITMENT_SHA256
            ),
            "source_bundle_sha256": DEPTH60_HISTORICAL_SOURCE_BUNDLE_SHA256,
            "source_bundle_algorithm": "v1",
            "historical_plan_relative_path": DEPTH60_PLAN_PATH.as_posix(),
            "historical_commitment_preserved": True,
            "historical_run_superseded": False,
        },
        "authorization_boundary": {
            "plan_alone_authorizes_online_run": False,
            "online_execution_authorized": False,
            "usable_as_runtime_binding": False,
            "supersedes_historical_online_authorization": False,
        },
        "claim_boundary": {
            "model_quality_claim_allowed": False,
            "reproduces_historical_depth60_run": False,
            "historical_result_revalidated": False,
            "source_integrity_scope": "current_tree_only",
        },
    }
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    plan = dict(body)
    plan["plan_commitment_sha256"] = hashlib.sha256(
        DEPTH60_SUCCESSOR_PLAN_DOMAIN + payload
    ).hexdigest()

    historical_before = (ROOT / DEPTH60_PLAN_PATH).read_bytes()
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    if (ROOT / DEPTH60_PLAN_PATH).read_bytes() != historical_before:
        raise SystemExit("historical plan was modified; aborting")

    validation = validate_phase6_depth60_plan(ROOT, DEPTH60_SUCCESSOR_PLAN_PATH)
    print(json.dumps(validation["plan_commitment_sha256"]))
    print(
        json.dumps(
            {
                "successor_plan": target.relative_to(ROOT).as_posix(),
                "source_bundle_sha256": plan["component_hashes"][
                    "source_bundle_sha256"
                ],
                "plan_commitment_sha256": plan["plan_commitment_sha256"],
                "historical_plan_unchanged": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
