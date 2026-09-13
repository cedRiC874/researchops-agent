"""Offline review reproducer, NOT a validation pass, live capture, or grant.

The entire signed graph and artifact set are synthetic, in owned temporary
directories. No production artifact, frozen contract, task or Key is changed.
Only caller-supplied timestamp evidence is changed: the two request windows
remain individually short but their total span exceeds the bound 600 seconds.
All hashes and signatures are honestly recomputed, then real A/B is called.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"src"))

from researchops_completion_telemetry.surface_mapping import VerifiedRuntimeCompletionBinding
from researchops_external_closure.evaluator import evaluate_closure_bundle
from researchops_external_closure.final import verify_closed_campaign
from researchops_external_closure.primitives import canonical_json_bytes
from researchops_external_closure.types import FinalDocumentBytes,ReceiptProjectionReady
from tests import test_external_closure_evaluator as helpers
from tests.full_closure_fixture import full_campaign_fixture
from tests.test_admission_evidence import AdmissionEvidenceChainTests


def main() -> int:
    setup=AdmissionEvidenceChainTests
    try:
        with patch.object(VerifiedRuntimeCompletionBinding,"_create",side_effect=AssertionError("runtime factory forbidden")),patch(
            "socket.socket",side_effect=AssertionError("network forbidden")
        ):
            setup.setUpClass()
            fixture,semantic,postrun,admission=full_campaign_fixture(setup)
            root=setup.repo.root
            second_run=semantic["run_rows"][1]["run_id"]
            for group in ("run_rows","event_rows","model_call_rows"):
                for row in semantic[group]:
                    if row["run_id"]==second_run:
                        for field in ("created_at_utc","updated_at_utc","occurred_at_utc","started_at_utc"):
                            if field in row:
                                observed=datetime.fromisoformat(row[field].replace("Z","+00:00"))+timedelta(minutes=10)
                                row[field]=observed.isoformat().replace("+00:00","Z")
            envelope=fixture.documents["preregistration_envelope"]
            runtime=envelope["runtime_plan"]
            helpers._rebind_semantic_fixture(semantic,envelope_id=envelope["envelope_id"],
                campaign_id=runtime["campaign_topology"]["campaign_id"],runtime_plan=runtime,
                authorization_grant_sha256=fixture.documents["authorization_grant"]["document_sha256"],
                model_id=envelope["execution_binding"]["model_id"])
            postrun.update(completed_at_utc="2026-09-05T02:13:00Z",receipt_issued_at_utc="2026-09-05T02:14:00Z")
            directory=root/"synthetic-timeout-review"
            directory.mkdir()
            manifest=helpers._write_complete_bundle(directory,fixture,semantic)
            fixture.observation["manifest_observation"].update(
                observed_at_utc="2026-09-05T02:13:30Z",manifest_sha256=hashlib.sha256(manifest).hexdigest())
            documents,observation=fixture.inputs()
            ready=evaluate_closure_bundle(root,documents,external_observation_bundle=observation,
                artifact_directory=directory,postrun_attested_facts=canonical_json_bytes(postrun),admission_evidence=admission)
            if type(ready) is not ReceiptProjectionReady:
                print(json.dumps({"status":"reproducer_rejected_before_receipt","scope":"synthetic_offline_only"}))
                return 2
            entry=helpers._final_entry(fixture,receipt_sha256=ready.receipt_document_sha256,occurred_at_utc="2026-09-05T02:15:00Z")
            final_observation=helpers._final_observation_bytes(observation,receipt_sha256=ready.receipt_document_sha256,
                receipt_observed_at_utc="2026-09-05T02:14:30Z",entry=entry,entry_observed_at_utc="2026-09-05T02:15:30Z")
            closed=verify_closed_campaign(root,FinalDocumentBytes(documents,helpers._signed_receipt_bytes(fixture,ready),canonical_json_bytes(entry)),
                external_observation_bundle=final_observation,artifact_directory=directory,
                postrun_attested_facts=canonical_json_bytes(postrun),admission_evidence=admission)
            sends=[row for row in semantic["event_rows"] if row["event_type"]=="provider_transport_request_sent"]
            terminals=[row for row in semantic["event_rows"] if row["event_type"]=="model_response_telemetry_recorded"]
            parse=lambda value:datetime.fromisoformat(value.replace("Z","+00:00"))
            span=int((parse(terminals[-1]["occurred_at_utc"])-parse(sends[0]["occurred_at_utc"])).total_seconds())
            receipt={"schema_version":"t6c-timeout-review/1.0","status":"gap_reproduced" if closed.closure_claim_allowed else "gap_not_reproduced",
                "scope":"synthetic_offline_protocol_counterexample_not_execution_evidence",
                "bound_total_timeout_seconds":runtime["transport_limits"]["total_timeout_seconds"],
                "recorded_first_send_to_last_terminal_seconds":span,
                "layer_a_evidence_valid":ready.evidence_valid,"layer_a_eligible":ready.pre_anchor_closure_eligible,
                "layer_a_error":ready.evidence_error_code,"layer_b_status":closed.status,
                "layer_b_closure_claim_allowed":closed.closure_claim_allowed,"layer_b_error":closed.error_code,
                "verdict_stub_used":False,"runtime_authority_constructed":False,"provider_calls":0,"real_key_loads":0,
                "utc_span_is_monotonic_duration_proof":False,
                "semantics_source_sha256":hashlib.sha256((ROOT/"src/researchops_external_closure/semantics.py").read_bytes()).hexdigest()}
            print(json.dumps(receipt,sort_keys=True,indent=2))
            return 0
    finally:
        setup.doClassCleanups()


if __name__=="__main__":
    raise SystemExit(main())
