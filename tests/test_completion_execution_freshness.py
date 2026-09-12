"""Fresh-clock orchestration with synthetic signed grants; no real claim/Key."""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from researchops_completion_timing import execution_scope as scope
from researchops_completion_timing import local_claim
from researchops_completion_timing.contract import TimingContractError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_completion_execution_scope import ROOT, scoped_fixture
from tests.test_completion_timing_pre_execution import documents


class ExecutionFreshnessTests(unittest.TestCase):
    def invoke(self, times):
        fixture, plan = scoped_fixture()
        with patch.object(scope, "_current_utc", side_effect=times), \
             patch.object(local_claim, "provision_local_claim_store", side_effect=AssertionError("no provisioning")), \
             patch.object(local_claim, "reserve_local_claim", side_effect=AssertionError("no claim")), \
             patch.object(local_claim, "local_claim_store_status", side_effect=AssertionError("no store read")):
            return scope.verify_current_single_host_scope(ROOT, documents(fixture),
                external_observation_bundle=raw(fixture.observation), timing_plan_bytes=raw(plan))

    def test_signature_exposes_no_time_or_clock_override(self):
        self.assertEqual(set(inspect.signature(scope.verify_current_single_host_scope).parameters),
                         {"root", "documents", "external_observation_bundle", "timing_plan_bytes"})

    def test_valid_current_interval_returns_readonly_scope_not_authority(self):
        proof = self.invoke(["2026-09-04T00:00:17Z", "2026-09-04T00:00:18Z"])
        self.assertEqual(proof.timing.verified_as_of_utc, "2026-09-04T00:00:17Z")
        self.assertFalse(proof.summary()["runtime_authority_granted"])
        self.assertFalse(proof.summary()["one_shot_execution_claimed"])

    def test_expired_at_start_is_rejected_before_second_clock_read(self):
        with self.assertRaisesRegex(TimingContractError, "timing_pre_execution_as_of_invalid"):
            self.invoke(["2026-09-04T01:00:00Z"])

    def test_expiry_reached_during_verification_is_rejected(self):
        with self.assertRaisesRegex(TimingContractError, "execution_scope_expired_during_verification"):
            self.invoke(["2026-09-04T00:59:59.999999Z", "2026-09-04T01:00:00Z"])

    def test_clock_rollback_is_not_hidden_by_a_still_valid_grant(self):
        with self.assertRaisesRegex(TimingContractError, "execution_scope_clock_reversed"):
            self.invoke(["2026-09-04T00:00:18Z", "2026-09-04T00:00:17Z"])

    def test_clock_failure_does_not_return_a_partial_proof(self):
        with self.assertRaisesRegex(TimingContractError, "execution_scope_clock_unavailable"):
            self.invoke(["2026-09-04T00:00:17Z", OSError("synthetic clock failure")])


if __name__ == "__main__":
    unittest.main()
