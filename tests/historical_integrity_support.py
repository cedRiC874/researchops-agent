"""Test-only materialization of verified bytes from the fixed PR #38 merge.

This preserves historical assertions while v6 checks the current source tree.
No expected hash is replaced, no object is fetched, and no snapshot code is
imported by this helper. Temporary writes are limited to the validated layout.
"""

from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

from researchops_external_closure.git_objects import read_git_object_snapshot


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_COMMIT = "5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab"
HISTORICAL_TREE = "30ecfd86ac00aecd8b67305a7c6ed2af88eeee10"
_temporary: tempfile.TemporaryDirectory | None = None
_root: Path | None = None


def historical_integrity_root() -> Path:
    global _temporary, _root
    if _root is not None:
        return _root
    metadata = read_git_object_snapshot(
        ROOT, HISTORICAL_COMMIT, ("requirements.lock",), expected_tree_oid=HISTORICAL_TREE
    )
    prefixes = (
        "src/", "evals/provider_completion_telemetry_v1/",
        "evals/provider_completion_telemetry_v2/",
        "evals/provider_completion_first_live_validation_v1/",
    )
    fixed = {
        "requirements.lock", "pyproject.toml", "probe_out_v3.json",
        "evals/phase6_deepseek_depth60_plan.json",
        *(f"evals/phase6_deepseek_depth60_plan_v{version}.json" for version in range(2, 6)),
        "evals/phase6_agent_tasks.jsonl", "evals/phase6_splits.json",
        "data/synthetic_trial.csv", "data/synthetic_trial_design.json",
        "artifacts/phase3/analysis_bundle.json", "artifacts/phase3/effect_estimates.png",
        "evals/v2/kimi_k3_handshake_plan_v1.json",
        "evals/v2/kimi_k3_handshake_contract_v1.json",
        "evals/v2/kimi_k3_nonstreaming_contract.json",
    }
    selected = tuple(sorted(
        entry.path for entry in metadata.entries
        if entry.mode in {"100644", "100755"}
        and (entry.path in fixed or entry.path.startswith(prefixes))
    ))
    if not fixed.issubset(selected):
        raise AssertionError("fixed historical integrity inputs are missing")
    snapshot = read_git_object_snapshot(
        ROOT, HISTORICAL_COMMIT, selected, expected_tree_oid=HISTORICAL_TREE
    )
    temporary = tempfile.TemporaryDirectory(prefix="researchops-historical-integrity-")
    root = Path(temporary.name)
    try:
        for blob in snapshot.blobs:
            target = root / blob.path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(blob.payload)
    except BaseException:
        temporary.cleanup()
        raise
    _temporary = temporary
    _root = root
    atexit.register(temporary.cleanup)
    return root
