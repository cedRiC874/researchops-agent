"""Signed execution environment scope, never an online permission test."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import local_claim
from researchops_completion_timing.contract import TimingContractError, runtime_plan_core_commitment
from researchops_completion_timing.execution_scope import (
    VerifiedSingleHostScope, build_scoped_claim_request, verify_scoped_store_identity,
    verify_single_host_pre_execution,
)
from researchops_completion_timing.pre_execution import verify_timed_pre_execution
from researchops_completion_timing.postrun_link import verify_scoped_timing_artifact_link, verify_signed_timing_artifact_link
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_completion_timing_pre_execution import signed_fixture, resign, documents
from tests.test_completion_timing_postrun_link import SignedTimingBundle


ROOT = Path(__file__).resolve().parents[1]


def scope_value(environment_id="PCEENV-" + "A" * 32):
    return dict(mode="single_host_fixed_store", execution_environment_id=environment_id,
                local_claim_contract_sha256=local_claim.CONTRACT_SHA256,
                cross_host_execution_allowed=False, temporary_store_override_allowed=False)


def scoped_fixture(environment_id="PCEENV-" + "A" * 32):
    fixture, plan = signed_fixture()
    runtime = fixture.documents["preregistration_envelope"]["runtime_plan"]
    runtime["execution_scope"] = scope_value(environment_id)
    plan["binding"]["runtime_plan_core_sha256"] = runtime_plan_core_commitment(runtime)
    resign(fixture, plan, envelope_version=3)
    return fixture, plan


def verify(fixture, plan):
    return verify_single_host_pre_execution(ROOT, documents(fixture), external_observation_bundle=raw(fixture.observation),
        timing_plan_bytes=raw(plan), verification_time_utc="2026-09-04T00:00:17Z")


class ExecutionScopeTests(unittest.TestCase):
    def test_schema_successor_changes_only_version_id_and_required_scope(self):
        before = json.loads((ROOT / "evals/provider_completion_pre_execution_v1/preregistration_v2.schema.json").read_bytes())
        after = json.loads((ROOT / "evals/provider_completion_execution_scope_v1/preregistration_v3.schema.json").read_bytes())
        normalized = copy.deepcopy(after)
        normalized["$id"] = before["$id"]
        normalized["properties"]["schema_version"] = before["properties"]["schema_version"]
        runtime = normalized["properties"]["runtime_plan"]
        runtime["required"].remove("execution_scope")
        del runtime["properties"]["execution_scope"]
        self.assertEqual(normalized, before)

    def test_signed_scope_derives_claim_hashes_without_store_or_provider_actions(self):
        fixture, plan = scoped_fixture()
        with patch.object(local_claim, "local_claim_store_status", side_effect=AssertionError("no real store lookup")), \
             patch.object(local_claim, "reserve_local_claim", side_effect=AssertionError("no claim")):
            proof = verify(fixture, plan)
            request = json.loads(build_scoped_claim_request(proof, clock_domain_id="PCECLOCK-" + "B" * 32))
        self.assertEqual(request["execution_environment_id"], scope_value()["execution_environment_id"])
        self.assertEqual(request["authorization_id_sha256"], fixture.documents["authorization_grant"]["explicit_user_authorization_id_sha256"])
        self.assertEqual(request["authorization_grant_sha256"], proof.timing.authorization_grant_sha256)
        self.assertEqual(request["timing_plan_commitment_sha256"], plan["plan_commitment_sha256"])
        for key in ("runtime_authority_granted", "local_store_identity_verified", "one_shot_execution_claimed"):
            self.assertFalse(proof.summary()[key])
        with self.assertRaises(FrozenInstanceError):
            proof.execution_environment_id = "PCEENV-" + "F" * 32
        with self.assertRaises(TypeError):
            VerifiedSingleHostScope()

    def test_old_unscoped_grant_and_wrong_entrypoint_are_rejected(self):
        fixture, plan = signed_fixture()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_document_schema_invalid"):
            verify(fixture, plan)
        resign(fixture, plan, envelope_version=3)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_document_schema_invalid"):
            verify(fixture, plan)
        fixture, plan = scoped_fixture()
        with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_document_schema_invalid"):
            verify_timed_pre_execution(ROOT, documents(fixture), external_observation_bundle=raw(fixture.observation),
                timing_plan_bytes=raw(plan), verification_time_utc="2026-09-04T00:00:17Z")

    def test_resigned_environment_change_cannot_reuse_old_timing_core(self):
        fixture, plan = scoped_fixture()
        fixture.documents["preregistration_envelope"]["runtime_plan"]["execution_scope"]["execution_environment_id"] = "PCEENV-" + "B" * 32
        resign(fixture, plan, envelope_version=3)
        with self.assertRaisesRegex(TimingContractError, "timing_pre_execution_binding_mismatch"):
            verify(fixture, plan)

    def test_cross_host_path_override_and_unknown_scope_fields_reject(self):
        for key, value in (("cross_host_execution_allowed", True), ("temporary_store_override_allowed", True),
                           ("local_claim_contract_sha256", "f" * 64), ("alternate_store", "ordinary")):
            fixture, plan = scoped_fixture()
            runtime = fixture.documents["preregistration_envelope"]["runtime_plan"]
            runtime["execution_scope"][key] = value
            plan["binding"]["runtime_plan_core_sha256"] = runtime_plan_core_commitment(runtime)
            resign(fixture, plan, envelope_version=3)
            with self.subTest(field=key), self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_document_schema_invalid"):
                verify(fixture, plan)

    def test_untyped_proof_or_invalid_domain_cannot_build_claim_request(self):
        fixture, plan = scoped_fixture(); proof = verify(fixture, plan)
        with self.assertRaisesRegex(TimingContractError, "execution_scope_proof_invalid"):
            build_scoped_claim_request(proof.summary(), clock_domain_id="PCECLOCK-" + "B" * 32)
        with self.assertRaisesRegex(TimingContractError, "execution_scope_proof_invalid"):
            build_scoped_claim_request(proof, clock_domain_id="not-a-clock-domain")

    def test_v3_signed_scope_reaches_real_artifact_link_without_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = SignedTimingBundle(Path(temporary), execution_scope=scope_value())
            arguments = dict(pre_execution_observation=raw(bundle.fixture.observation), postrun_observation=raw(bundle.post),
                             timing_plan_bytes=raw(bundle.plan), verification_time_utc="2026-09-04T00:00:17Z")
            checked = verify_scoped_timing_artifact_link(ROOT, bundle.directory, documents(bundle.fixture), **arguments)
            self.assertEqual(checked["signed_execution_environment_id"], scope_value()["execution_environment_id"])
            self.assertTrue(checked["timing_constraints_complete"])
            self.assertFalse(checked["local_claim_verified"])
            self.assertFalse(checked["runtime_authority_granted"])
            with self.assertRaisesRegex(ExternalClosurePrimitiveError, "external_closure_document_schema_invalid"):
                verify_signed_timing_artifact_link(ROOT, bundle.directory, documents(bundle.fixture), **arguments)

    @unittest.skipUnless(os.name == "nt", "designated Windows store")
    def test_real_temporary_store_match_then_claim_and_replay(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(local_claim, "_windows_local_app_data", return_value=Path(temporary)):
            environment_id = local_claim.provision_local_claim_store()["execution_environment_id"]
            fixture, plan = scoped_fixture(environment_id)
            proof = verify(fixture, plan)
            matched = verify_scoped_store_identity(proof)
            self.assertFalse(matched["one_shot_execution_claimed"])
            request = build_scoped_claim_request(proof, clock_domain_id="PCECLOCK-" + "B" * 32)
            reserved = local_claim.reserve_local_claim(request)
            self.assertFalse(reserved["runtime_authority_granted"])
            with self.assertRaisesRegex(local_claim.LocalClaimError, "local_claim_already_exists"):
                local_claim.reserve_local_claim(request)
            wrong, wrong_plan = scoped_fixture("PCEENV-" + "F" * 32)
            with self.assertRaisesRegex(TimingContractError, "execution_scope_store_mismatch"):
                verify_scoped_store_identity(verify(wrong, wrong_plan))


if __name__ == "__main__":
    unittest.main()
