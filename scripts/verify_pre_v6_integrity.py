"""CI replay of frozen v5/first-live/Kimi/v11 plus current Internal source.

Verified historical Git blobs are materialized in a temporary test directory.
No snapshot code is imported, no Key is read and no Provider is constructed.
Missing Git history is an error; this verifier never fetches it implicitly.
"""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from researchops.phase6_depth60 import validate_phase6_depth60_plan
from researchops.deepseek_completion_first_live_validation import deepseek_first_live_validation_status
from researchops.kimi_k3_handshake import validate_kimi_k3_handshake
from researchops_external_closure.execution_current_v4 import verify_current_timed_profile
from tests.historical_integrity_support import HISTORICAL_COMMIT, HISTORICAL_TREE, historical_integrity_root
from tests.internal_v11_historical_support import historical_v11_root, COMMIT as V11_COMMIT, TREE as V11_TREE
from researchops_internal_telemetry.source import verify_source, MANIFEST


def main() -> int:
    try:
        historical = historical_integrity_root()
        depth60 = validate_phase6_depth60_plan(historical, "evals/phase6_deepseek_depth60_plan_v5.json")
        first_live = deepseek_first_live_validation_status(historical)
        kimi = validate_kimi_k3_handshake(historical)
        # Do not send v11 through the legacy Depth-60 execution dispatcher.
        checked = verify_current_timed_profile(historical_v11_root(), profile="first_live")
        historical_v11 = dict(status="valid", plan_id="phase6-deepseek-depth60-v11",
            source_recipe_version=4, profile=checked.profile,
            plan_commitment_sha256=checked.source_integrity_commitment_sha256,
            implementation_commitment_sha256=checked.implementation_commitment_sha256,
            verified_file_count=checked.selected_file_count,
            source_integrity_only=checked.source_integrity_only,
            online_execution_authorized=checked.online_execution_authorized,
            runtime_admission_verified=checked.runtime_admission_verified,
            historical_result_revalidated=checked.historical_result_revalidated,
            network_calls=0, model_calls=0)
        internal = verify_source(ROOT)
        current = dict(status="valid", algorithm=internal["algorithm"],
            source_commitment_sha256=internal["commitment_sha256"],
            manifest_sha256=hashlib.sha256((ROOT/MANIFEST).read_bytes()).hexdigest(),
            verified_file_count=len(internal["files"]),source_integrity_only=True,
            online_execution_authorized=False,runtime_admission_verified=False,network_calls=0,model_calls=0)
        frozen_v7 = {
            "evals/phase6_deepseek_depth60_plan_v7.json": "396b2103399af1a68854f1f61abd5547f841a68edde78702fbba4df2910806c1",
            "evals/provider_completion_execution_binding_v2/implementation_manifest_first_live_v1.json": "13457502cf2fe55ff9bbf071caf86b473812212f42acad4081001b2036b4f6a1",
        }
        for name, expected_hash in frozen_v7.items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected_hash:
                raise ValueError("historical_v7_bytes_changed")
        expected = {
            "depth60_v5": "phase6_depth60_v5_component_drift",
            "depth60_v6": "phase6_depth60_v6_component_drift",
            "depth60_v7": "phase6_depth60_profile_component_drift",
            "first_live": "deepseek_first_live_source_integrity_invalid",
            "kimi": "kimi_k3_handshake_plan_drift",
            "depth60_v11": "execution_v4_component_drift",
        }
        observed = {}
        checks = {
            "depth60_v5": lambda: validate_phase6_depth60_plan(ROOT, "evals/phase6_deepseek_depth60_plan_v5.json"),
            "depth60_v6": lambda: validate_phase6_depth60_plan(ROOT, "evals/phase6_deepseek_depth60_plan_v6.json"),
            "depth60_v7": lambda: validate_phase6_depth60_plan(ROOT, "evals/phase6_deepseek_depth60_plan_v7.json"),
            "first_live": lambda: deepseek_first_live_validation_status(ROOT),
            "kimi": lambda: validate_kimi_k3_handshake(ROOT),
            "depth60_v11": lambda: verify_current_timed_profile(ROOT, profile="first_live"),
        }
        for name, check in checks.items():
            try:
                check()
            except Exception as error:
                observed[name] = getattr(error, "code", "unexpected_error")
            else:
                observed[name] = "unexpectedly_valid"
        if observed != expected:
            raise ValueError("current_historical_rejection_mismatch")
        print(json.dumps({
            "status": "valid",
            "historical_commit": HISTORICAL_COMMIT,
            "historical_tree": HISTORICAL_TREE,
            "legacy_validation_scope": "pinned_git_snapshot_only",
            "current_validation_scope": "internal_v1_source_integrity_only",
            "depth60_v5": depth60,
            "first_live": first_live,
            "kimi": kimi,
            "historical_v11_commit": V11_COMMIT,
            "historical_v11_tree": V11_TREE,
            "historical_v11": historical_v11,
            "current_internal": current,
            "historical_v7_file_sha256": frozen_v7,
            "current_legacy_rejections": observed,
            "network_calls": 0,
            "model_calls": 0,
            "provider_key_loaded": False,
        }, ensure_ascii=True, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "invalid", "error_code": getattr(error, "code", "historical_integrity_replay_failed")}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
