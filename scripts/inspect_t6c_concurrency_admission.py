"""Reproduce the semantic-layer concurrency review gap with synthetic data only."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"src"))

from tests.test_external_closure_semantics import ExternalClosureSemanticTests


def main() -> int:
    with patch("socket.socket",side_effect=AssertionError("network forbidden")):
        ExternalClosureSemanticTests.setUpClass()
        case=ExternalClosureSemanticTests()
        fixture=case._fixture()
        second_run=fixture["run_rows"][1]["run_id"]
        for group in ("run_rows","event_rows","model_call_rows"):
            for row in fixture[group]:
                if row["run_id"]==second_run:
                    for field in ("created_at_utc","updated_at_utc","occurred_at_utc","started_at_utc"):
                        if field in row:
                            observed=datetime.fromisoformat(row[field].replace("Z","+00:00"))-timedelta(seconds=59.5)
                            row[field]=observed.isoformat().replace("+00:00","Z")
        case._rehash_run(fixture,1)
        result=case._verify(fixture)
        events=fixture["event_rows"]
        receipt={"schema_version":"t6c-concurrency-review/1.0","status":"gap_reproduced" if result.semantic_closure_claim_allowed else "gap_not_reproduced",
            "scope":"pure_semantic_synthetic_counterexample_not_full_A_B",
            "bound_concurrency":fixture["expected_runtime_plan"]["transport_limits"]["concurrency"],
            "send_times":[row["occurred_at_utc"] for row in events if row["event_type"]=="provider_transport_request_sent"],
            "terminal_times":[row["occurred_at_utc"] for row in events if row["event_type"]=="model_response_telemetry_recorded"],
            "semantic_closure_claim_allowed":result.semantic_closure_claim_allowed,"reasons":list(result.semantic_closure_reasons),
            "semantics_source_sha256":hashlib.sha256((ROOT/"src/researchops_external_closure/semantics.py").read_bytes()).hexdigest(),
            "provider_calls":0,"real_key_loads":0,"utc_overlap_is_monotonic_concurrency_proof":False}
        print(json.dumps(receipt,sort_keys=True,indent=2))
        return 0


if __name__=="__main__":
    raise SystemExit(main())
