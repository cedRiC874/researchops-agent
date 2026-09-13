"""Direct-authorization metadata and temporary local-claim linkage; not a live run."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import localcontext
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live, first_live_control as control, local_claim
from researchops_completion_timing.contract import TimingContractError, digest
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_first_live_timing as timing_tests


ROOT = Path(__file__).resolve().parents[1]


class FirstLiveControlTests(unittest.TestCase):
    def fixture(self, *, environment_id=None, now=None):
        plan = timing_tests.FirstLiveTimingTests().fixture()["plan"]
        if environment_id is not None:
            plan["binding"]["execution_environment_id"] = environment_id
            plan["binding"]["validation_run_id"] = first_live.validation_run_id(plan["binding"])
        plan["plan_commitment_sha256"] = first_live.commitment("plan", plan)
        now = now or datetime(2026, 9, 6, 1, tzinfo=timezone.utc)
        stamp = lambda value: value.isoformat().replace("+00:00", "Z")
        auth = dict(schema_version="provider-completion-first-live-authorization/1.0",
            first_live_profile_sha256=first_live.PROFILE_SHA256, authorization_id_sha256=plan["binding"]["authorization_id_sha256"],
            execution_environment_id=plan["binding"]["execution_environment_id"], timing_plan_commitment_sha256=plan["plan_commitment_sha256"],
            authorized_at_utc=stamp(now), expires_at_utc=stamp(now + timedelta(seconds=600)),
            pricing_snapshot_date=now.date().isoformat(), pricing_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            pricing_retrieved_at_utc=stamp(now), input_price_per_million_cny="3.000000", output_price_per_million_cny="9.000000",
            official_pricing_attested_current=True, accept_locked_caps=True, confirm_online=True, scope="single_host_fixed_store",
            cross_host_execution_allowed=False, retries=0, resume=False, fallback=False, provider_registration_allowed=False,
            model_quality_claim_allowed=False, closure_claim_allowed=False)
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        return plan, auth

    def verify(self, plan, auth, **overrides):
        values = dict(plan_bytes=raw(plan), authorization_bytes=raw(auth),
            expected_authorization_binding_sha256=auth["authorization_binding_sha256"], verification_time_utc=auth["authorized_at_utc"])
        values.update(overrides)
        return control.verify_first_live_authorization(ROOT, **values)

    def test_external_binding_is_required_and_self_rehash_does_not_supply_consent(self):
        plan, auth = self.fixture()
        proof = self.verify(plan, auth)
        self.assertEqual(proof.authorization_document_sha256, digest(raw(auth)))
        class EqualAnything:
            def __eq__(self, other): return True
            def __ne__(self, other): return False
        for expected in (None, "f" * 64, EqualAnything()):
            with self.subTest(kind=type(expected).__name__), self.assertRaisesRegex(TimingContractError, "first_live_authorization_binding_mismatch"):
                self.verify(plan, auth, expected_authorization_binding_sha256=expected)

    def test_authorization_cannot_change_the_environment_or_plan(self):
        plan, auth = self.fixture()
        auth["execution_environment_id"] = "PCEENV-" + "F" * 32
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        with self.assertRaisesRegex(TimingContractError, "first_live_authorization_plan_mismatch"):
            self.verify(plan, auth)

    def test_exact_minimum_window_and_expiry_boundary(self):
        plan, auth = self.fixture()
        auth["expires_at_utc"] = "2026-09-06T01:05:30Z"
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        self.verify(plan, auth)
        with self.assertRaisesRegex(TimingContractError, "first_live_authorization_time_invalid"):
            self.verify(plan, auth, verification_time_utc=auth["expires_at_utc"])
        auth["expires_at_utc"] = "2026-09-06T01:05:29.999999Z"
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        with self.assertRaisesRegex(TimingContractError, "first_live_authorization_time_invalid"):
            self.verify(plan, auth)

    def test_cost_reservation_does_not_inherit_decimal_precision(self):
        plan, auth = self.fixture()
        auth.update(input_price_per_million_cny="711.000000", output_price_per_million_cny="1000.000000")
        auth["authorization_binding_sha256"] = control.authorization_binding(auth)
        for precision in (1, 28, 80):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                with self.assertRaisesRegex(TimingContractError, "first_live_authorization_cost_reservation_invalid"):
                    self.verify(plan, auth)

    def test_intent_is_not_reservation_and_claim_fields_are_bound(self):
        plan, auth = self.fixture(); proof = self.verify(plan, auth)
        with patch.object(local_claim, "reserve_local_claim", side_effect=AssertionError("no reservation")):
            intent, request = control.build_first_live_claim_intent(ROOT, proof,
                clock_domain_id="PCECLOCK-" + "A" * 32, intended_at_utc=auth["authorized_at_utc"])
        self.assertEqual(json.loads(intent)["status"], "intent_only_not_reserved")
        receipt = dict(json.loads(request), schema_version="provider-completion-local-claim/1.0", status="reserved",
            claimed_at_utc="2026-09-06T01:00:01Z", claim_contract_sha256=local_claim.CONTRACT_SHA256,
            store_scope="single_host_fixed_store", provider_actions_authorized=False)
        def check(value):
            return control.verify_first_live_claim_link(ROOT, proof, intent_bytes=intent, claim_receipt_bytes=raw(value),
                expected_claim_receipt_sha256=digest(raw(value)))
        result = check(receipt)
        self.assertFalse(result["runtime_authority_granted"])
        self.assertFalse(result["local_store_entry_independently_verified"])
        for field, value in (("clock_domain_id", "PCECLOCK-" + "B" * 32), ("consumption_entry_sha256", "f" * 64),
                             ("provider_actions_authorized", 0)):
            changed = copy.deepcopy(receipt); changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(TimingContractError, "first_live_claim_link_mismatch"):
                check(changed)

    @unittest.skipUnless(os.name == "nt", "designated Windows environment")
    def test_real_temporary_claim_receipt_matches_direct_control_binding(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(local_claim, "_windows_local_app_data", return_value=Path(temporary)):
            environment_id = local_claim.provision_local_claim_store()["execution_environment_id"]
            now = datetime.now(timezone.utc)
            plan, auth = self.fixture(environment_id=environment_id, now=now)
            proof = self.verify(plan, auth)
            intent, request = control.build_first_live_claim_intent(ROOT, proof,
                clock_domain_id="PCECLOCK-" + "A" * 32, intended_at_utc=auth["authorized_at_utc"])
            reserved = local_claim.reserve_local_claim(request)
            receipt = (Path(temporary) / "ResearchOpsAgent/completion-claims-v1/claims" / (proof.authorization_id_sha256 + ".json")).read_bytes()
            checked = control.verify_first_live_claim_link(ROOT, proof, intent_bytes=intent, claim_receipt_bytes=receipt,
                expected_claim_receipt_sha256=reserved["receipt_sha256"])
            self.assertEqual(checked["status"], "first_live_control_link_verified")
            self.assertFalse(checked["first_live_success_verified"])
            stored = control.verify_first_live_stored_claim_link(ROOT, proof, intent_bytes=intent,
                expected_claim_receipt_sha256=reserved["receipt_sha256"])
            self.assertTrue(stored["local_store_entry_independently_verified"])
            self.assertEqual(stored["store_verification_scope"], "readonly_snapshot_only")
            self.assertFalse(stored["successful_reservation_return_proved"])
            self.assertFalse(stored["retry_or_resume_authorized"])
            self.assertFalse(stored["runtime_authority_granted"])
            self.assertFalse(stored["first_live_success_verified"])

    @unittest.skipUnless(os.name == "nt", "designated Windows environment")
    def test_fabricated_link_receipt_cannot_substitute_for_a_stored_claim(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(local_claim, "_windows_local_app_data", return_value=Path(temporary)):
            environment_id = local_claim.provision_local_claim_store()["execution_environment_id"]
            plan, auth = self.fixture(environment_id=environment_id)
            proof = self.verify(plan, auth)
            intent, request = control.build_first_live_claim_intent(ROOT, proof,
                clock_domain_id="PCECLOCK-" + "A" * 32, intended_at_utc=auth["authorized_at_utc"])
            receipt = dict(json.loads(request), schema_version="provider-completion-local-claim/1.0", status="reserved",
                claimed_at_utc="2026-09-06T01:00:01Z", claim_contract_sha256=local_claim.CONTRACT_SHA256,
                store_scope="single_host_fixed_store", provider_actions_authorized=False)
            fake_hash = digest(raw(receipt))
            # Self-consistent supplied metadata is intentionally not a store proof.
            control.verify_first_live_claim_link(ROOT, proof, intent_bytes=intent,
                claim_receipt_bytes=raw(receipt), expected_claim_receipt_sha256=fake_hash)
            with self.assertRaisesRegex(local_claim.LocalClaimError, "local_claim_receipt_unverifiable"):
                control.verify_first_live_stored_claim_link(ROOT, proof, intent_bytes=intent,
                    expected_claim_receipt_sha256=fake_hash)
            self.assertEqual(list((Path(temporary) / "ResearchOpsAgent/completion-claims-v1/claims").iterdir()), [])

    def controlled_fixture(self):
        authoring = timing_tests.FirstLiveTimingTests()
        fixture = authoring.fixture()
        plan, auth = self.fixture()
        fixture["plan"] = plan
        proof = self.verify(plan, auth)
        intent, request = control.build_first_live_claim_intent(ROOT, proof,
            clock_domain_id=fixture["evidence"]["clock_domain_id"], intended_at_utc=auth["authorized_at_utc"])
        receipt = dict(json.loads(request), schema_version="provider-completion-local-claim/1.0", status="reserved",
            claimed_at_utc="2026-09-06T01:00:01Z", claim_contract_sha256=local_claim.CONTRACT_SHA256,
            store_scope="single_host_fixed_store", provider_actions_authorized=False)
        fixture.update(auth=auth, intent=json.loads(intent), receipt=receipt, records=authoring.records())
        fixture["evidence"]["binding"].update(authorization_binding_sha256=proof.authorization_binding_sha256,
            local_claim_receipt_sha256=digest(raw(receipt)))
        for segment, record in zip(fixture["evidence"]["segments"], fixture["records"]):
            segment.update(completion_record_sha256=digest(raw(record)), usage_sha256=digest(raw(record["usage"])))
        return authoring.seal(fixture)

    def controlled_check(self, fixture, **overrides):
        values = dict(plan_bytes=raw(fixture["plan"]), authorization_bytes=raw(fixture["auth"]),
            expected_authorization_binding_sha256=fixture["auth"]["authorization_binding_sha256"],
            verification_time_utc=fixture["auth"]["authorized_at_utc"], intent_bytes=raw(fixture["intent"]),
            claim_receipt_bytes=raw(fixture["receipt"]), expected_claim_receipt_sha256=digest(raw(fixture["receipt"])),
            evidence_bytes=raw(fixture["evidence"]), record_bytes=raw({"records": fixture["records"]}),
            terminal_projection_bytes=raw(fixture["events"]), publication_bytes=raw(fixture["publication"]),
            expected_manifest_file_sha256=fixture["publication"]["bundle_manifest_file_sha256"])
        values.update(overrides)
        with patch("socket.socket", side_effect=AssertionError("no network")), \
             patch.object(local_claim, "reserve_local_claim", side_effect=AssertionError("no reservation")), \
             patch.object(local_claim, "provision_local_claim_store", side_effect=AssertionError("no provisioning")):
            return control.verify_first_live_controlled_timing(ROOT, **values)

    def test_control_record_timing_join_does_not_promote_synthetic_projections(self):
        fixture = self.controlled_fixture()
        checked = self.controlled_check(fixture)
        self.assertTrue(checked["timing_gate_complete"])
        self.assertEqual(checked["record_count"], 2)
        for field in ("local_store_entry_independently_verified", "authoritative_ledger_verified",
                      "actual_clock_capture_proved", "fresh_execution_time_verified", "source_admission_verified",
                      "runtime_authority_granted", "first_live_success_verified", "current_closure_claim_allowed"):
            self.assertIs(checked[field], False, field)
        fixture["evidence"]["record_provenance"] = "synthetic_fixture"
        timing_tests.FirstLiveTimingTests().seal(fixture)
        self.assertFalse(self.controlled_check(fixture)["timing_gate_complete"])
        self.assertFalse(self.controlled_check(fixture, publication_bytes=None)["timing_gate_complete"])

    def test_rehashed_timing_cannot_substitute_authorization_or_local_claim(self):
        for field in ("authorization_binding_sha256", "local_claim_receipt_sha256"):
            fixture = self.controlled_fixture()
            fixture["evidence"]["binding"][field] = "f" * 64
            timing_tests.FirstLiveTimingTests().seal(fixture)
            # The independent authoring check has no control receipt to compare.
            timing_tests.FirstLiveTimingTests().check(fixture)
            with self.subTest(field=field), self.assertRaisesRegex(TimingContractError, "first_live_control_timing_binding_mismatch"):
                self.controlled_check(fixture)

    def test_rehashed_clock_domain_must_equal_the_claimed_domain(self):
        fixture = self.controlled_fixture()
        fixture["evidence"]["clock_domain_id"] = "PCECLOCK-" + "F" * 32
        fixture["publication"]["clock_domain_id"] = fixture["evidence"]["clock_domain_id"]
        for segment in fixture["evidence"]["segments"]:
            segment["clock_domain_id"] = fixture["evidence"]["clock_domain_id"]
        timing_tests.FirstLiveTimingTests().seal(fixture)
        timing_tests.FirstLiveTimingTests().check(fixture)
        with self.assertRaisesRegex(TimingContractError, "first_live_control_timing_binding_mismatch"):
            self.controlled_check(fixture)

    def test_rehashed_record_and_usage_substitutions_are_rejected(self):
        for field in ("completion_record_sha256", "usage_sha256"):
            fixture = self.controlled_fixture()
            fixture["evidence"]["segments"][0][field] = "f" * 64
            timing_tests.FirstLiveTimingTests().seal(fixture)
            timing_tests.FirstLiveTimingTests().check(fixture)
            with self.subTest(field=field), self.assertRaisesRegex(TimingContractError, "first_live_control_record_binding_mismatch"):
                self.controlled_check(fixture)

    def test_missing_segment_cannot_zip_away_a_valid_record(self):
        fixture = self.controlled_fixture(); fixture["evidence"]["segments"].pop()
        timing_tests.FirstLiveTimingTests().seal(fixture)
        with self.assertRaisesRegex(TimingContractError, "first_live_control_record_coverage_mismatch"):
            self.controlled_check(fixture)

    def test_control_metadata_is_in_the_caller_canary_scan(self):
        fixture = self.controlled_fixture()
        # This complete timestamp exists only in the direct authorization; the
        # timing/record documents have relative offsets, not wall-clock times.
        with self.assertRaisesRegex(ValueError, "sensitive_content"):
            self.controlled_check(fixture, sensitive_canaries=iter((fixture["auth"]["expires_at_utc"].encode(),)))


if __name__ == "__main__":
    unittest.main()
