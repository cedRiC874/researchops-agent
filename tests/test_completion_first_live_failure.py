"""Failure-receipt conformance with real clock markers, not live provenance."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from researchops_completion_timing import first_live_failure as failure
from researchops_completion_timing.first_live_start import FirstLiveStartError, _ClaimedFirstLiveStart
from researchops_completion_timing.first_live_runtime import _FirstLiveModelFactory
from researchops_completion_timing.first_live_publish import FirstLivePublishError
from researchops_completion_timing.clock import _TimingClock
from researchops_completion_timing.contract import TimingContractError
from researchops_external_closure.primitives import canonical_json_bytes as raw


ROOT = Path(__file__).resolve().parents[1]


def observed_factory(kind=None, *, active=False):
    # Only the receipt's observation inputs are constructed. This is deliberately
    # not a successful source/G3/claim or Provider-factory fixture.
    clock = _TimingClock(request_timeout_ns=1_000_000_000, phase_timeout_ns=10_000_000_000)
    clock.task_released(); clock.key_loaded()
    if kind is not None or active:
        handle = clock.begin_attempt(); clock.send_started(handle)
        if not active:
            clock.transport_terminal(handle)
            clock.raw_cleanup_terminal(handle, status="completed")
            clock.finish_attempt(handle, terminal_kind=kind, raw_cleanup_status="completed")
    prepared = object.__new__(_ClaimedFirstLiveStart)
    prepared._clock = clock; prepared._pid = os.getpid()
    factory = object.__new__(_FirstLiveModelFactory)
    factory._prepared, factory._inflight = prepared, active
    factory._records = [{}] if kind == "response_accepted" else []
    return factory


class FirstLiveFailureTests(unittest.TestCase):
    def receipt(self, error, **kwargs):
        return json.loads(failure.build_first_live_failure_receipt(ROOT, error, **kwargs))

    def test_generic_exception_with_missing_state_is_unknown_not_zero(self):
        result = self.receipt(RuntimeError("untrusted exception body"))
        self.assertEqual(result["status"], "outcome_unknown")
        for name in ("dispatch_attempt_count", "observed_send_markers", "validated_response_count", "inflight", "token_usage", "provider_bill"):
            self.assertIsNone(result[name])
        self.assertFalse(result["retry_authorized"])

    def test_known_preparation_branch_has_zero_dispatch_without_assuming_unconsumed(self):
        for consumed in (False, True):
            result = self.receipt(FirstLiveStartError("first_live_start_failed", claim_consumed=consumed, origin="preparation"))
            self.assertEqual(result["status"], "failed_before_dispatch")
            self.assertEqual(result["dispatch_attempt_count"], 0)
            self.assertIs(result["claim_consumed"], consumed)

    def test_runtime_start_error_without_observations_is_not_misclassified_as_preparation(self):
        result = self.receipt(FirstLiveStartError("first_live_response_contract_mismatch", claim_consumed=True))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIsNone(result["dispatch_attempt_count"])

    def test_settled_http_error_has_one_dispatch_but_unknown_usage(self):
        result = self.receipt(RuntimeError("ignored"), factory=observed_factory("http_error"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["dispatch_attempt_count"], 1)
        self.assertEqual(result["validated_response_count"], 0)
        self.assertIsNone(result["token_usage"])

    def test_inflight_marker_is_lower_bound_not_exact_count(self):
        result = self.receipt(RuntimeError("ignored"), factory=observed_factory(active=True))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["observed_send_markers"], 1)
        self.assertIsNone(result["dispatch_attempt_count"])

    def test_unknown_response_outcome_can_still_have_known_dispatch_count(self):
        result = self.receipt(RuntimeError("ignored"), factory=observed_factory("outcome_unknown"))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["dispatch_attempt_count"], 1)

    def test_publication_failure_does_not_erase_known_runtime_observation(self):
        error = FirstLivePublishError("first_live_publish_failed", directory_created=True, created_files=["audit.sqlite3"])
        result = self.receipt(error, factory=observed_factory("response_accepted"))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["dispatch_attempt_count"], 1)
        self.assertEqual(result["validated_response_count"], 1)

    def test_exception_content_and_hooks_are_never_used(self):
        class HostileError(Exception):
            @property
            def code(self): raise AssertionError("code hook must not run")
            def __str__(self): raise AssertionError("exception must not be stringified")
        payload = failure.build_first_live_failure_receipt(ROOT, HostileError("sk-syntheticsecret12345"))
        self.assertNotIn(b"syntheticsecret", payload)
        self.assertNotIn(b"HostileError", payload)
        error = FirstLiveStartError("sk_syntheticsecret12345", claim_consumed=False, origin="preparation")
        self.assertNotIn(b"syntheticsecret", failure.build_first_live_failure_receipt(ROOT, error))

    def test_empty_or_corrupt_factory_does_not_fabricate_zero(self):
        result = self.receipt(RuntimeError("ignored"), factory=object.__new__(_FirstLiveModelFactory))
        self.assertEqual(result["observation_scope"], "insufficient_observation")
        self.assertIsNone(result["dispatch_attempt_count"])

    def test_semantic_receipt_tamper_is_rejected(self):
        result = self.receipt(RuntimeError("ignored"))
        result["dispatch_attempt_count"] = 0
        with self.assertRaisesRegex(TimingContractError, "first_live_failure_observation_mismatch"):
            failure.validate_failure_receipt(ROOT, raw(result))


if __name__ == "__main__":
    unittest.main()
