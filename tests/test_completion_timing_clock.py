"""Mock-clock lifecycle tests, not live capture or admission integration."""
from __future__ import annotations

import inspect
import json
import threading
import unittest
from unittest.mock import patch

from researchops_completion_timing import contract
from researchops_completion_timing.clock import MAX_OFFSET_NS, TimingCaptureError, _TimingClock
from tests import test_completion_timing_contract as authoring


class TimingClockTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.native = patch("researchops_completion_timing.clock.time.monotonic_ns")
        self.clock = self.native.start()
        self.addCleanup(self.native.stop)
        self.clock.return_value = 1000

    def capture(self):
        return _TimingClock(request_timeout_ns=50, phase_timeout_ns=200)

    def ready(self):
        capture = self.capture()
        capture.task_released()
        capture.key_loaded()
        return capture

    def accepted(self, capture):
        handle = capture.begin_attempt()
        capture.send_started(handle)
        capture.transport_terminal(handle)
        capture.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")
        return handle

    def test_cli_and_runtime_share_identical_validation_functions(self):
        for name in ("validate_documents", "load_contract", "commitment", "runtime_plan_core_commitment"):
            self.assertIs(getattr(authoring.timing, name), getattr(contract, name))

    def test_no_clock_callback_or_caller_timestamp_inputs(self):
        self.assertEqual(set(inspect.signature(_TimingClock).parameters), {"request_timeout_ns", "phase_timeout_ns"})
        for name in ("task_released", "key_loaded", "begin_attempt", "phase_terminal", "snapshot"):
            self.assertEqual(list(inspect.signature(getattr(_TimingClock, name)).parameters), ["self"])
        frozen, _ = contract.load_contract(authoring.ROOT)
        self.assertEqual(MAX_OFFSET_NS, frozen["limits"]["max_safe_offset_ns"])
        for request, phase in ((True, 200), (1.0, 200), (0, 200), (50, -1),
                               (frozen["limits"]["max_request_timeout_ns"] + 1, 200),
                               (50, frozen["limits"]["max_phase_timeout_ns"] + 1)):
            with self.assertRaisesRegex(TimingCaptureError, "timing_clock_limits_invalid"):
                _TimingClock(request_timeout_ns=request, phase_timeout_ns=phase)
        self.clock.assert_not_called()

    def test_mock_offsets_exact_caps_and_equal_lifetimes_pass_shared_conformance_only(self):
        self.clock.side_effect = [1000, 1010, 1020, 1030, 1040, 1080, 1090,
                                  1090, 1100, 1150, 1170, 1180, 1190, 1200]
        capture = self.ready()
        self.accepted(capture)
        self.accepted(capture)
        capture.post_cleanup_terminal(status="completed")
        capture.key_reference_released(status="released")
        capture.phase_terminal()
        data = capture.snapshot()
        helper = authoring.TimingAuthoringTests()
        fixture = helper.fixture()
        fixture["evidence"]["phase"] = data["phase"]
        for segment, measured in zip(fixture["evidence"]["segments"], data["attempts"]):
            segment.update(measured)
        result = helper.check(helper.seal(fixture))
        self.assertTrue(result["timing_gate_complete"])
        self.assertFalse(result["actual_clock_capture_proved"])
        self.assertFalse(result["current_closure_claim_allowed"])
        self.assertEqual(data["phase"]["phase_terminal_ns"], 200)
        self.assertFalse(data["halted"])
        self.assertNotIn("origin", json.dumps(data))
        self.assertNotIn("provenance", json.dumps(data))
        data["attempts"][0]["send_started_ns"] = -1
        self.assertEqual(capture.snapshot()["attempts"][0]["send_started_ns"], 40)

    def test_request_overrun_is_preserved_and_forbids_next_attempt(self):
        capture = self.ready()
        handle = capture.begin_attempt()
        capture.send_started(handle)
        self.clock.return_value = 1051
        capture.transport_terminal(handle)
        self.clock.return_value = 1090
        capture.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")
        self.assertEqual(capture.snapshot()["attempts"][0]["transport_terminal_ns"], 51)
        with self.assertRaisesRegex(TimingCaptureError, "timing_capture_halted"):
            capture.begin_attempt()

    def test_send_at_phase_cap_is_blocked_without_fabricating_send_offset(self):
        capture = self.ready()
        handle = capture.begin_attempt()
        self.clock.return_value = 1200
        with self.assertRaisesRegex(TimingCaptureError, "timing_phase_timeout_exceeded"):
            capture.send_started(handle)
        self.assertIsNone(capture.snapshot()["active_attempt"]["send_started_ns"])

    def test_600_661_phase_overrun_retains_observed_terminal(self):
        second = 1_000_000_000
        capture = _TimingClock(request_timeout_ns=100 * second, phase_timeout_ns=600 * second)
        capture.task_released(); capture.key_loaded()
        self.accepted(capture)
        capture.post_cleanup_terminal(status="completed")
        capture.key_reference_released(status="released")
        self.clock.return_value = 1000 + 661 * second
        capture.phase_terminal()
        data = capture.snapshot()
        self.assertEqual(data["phase"]["phase_terminal_ns"], 661 * second)
        self.assertTrue(data["halted"])

    def test_remaining_budget_uses_tighter_deadline_and_reaches_zero(self):
        capture = self.ready()
        handle = capture.begin_attempt()
        self.clock.return_value = 1180
        capture.send_started(handle)
        self.clock.return_value = 1190
        self.assertEqual(capture.remaining_request_ns(handle), 10)
        self.clock.return_value = 1200
        self.assertEqual(capture.remaining_request_ns(handle), 0)
        capture.transport_terminal(handle)
        capture.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")
        with self.assertRaisesRegex(TimingCaptureError, "timing_capture_halted"):
            capture.begin_attempt()

    def test_overlap_during_raw_cleanup_is_rejected_but_cleanup_can_finish(self):
        capture = self.ready()
        handle = capture.begin_attempt()
        capture.send_started(handle); capture.transport_terminal(handle)
        with self.assertRaisesRegex(TimingCaptureError, "timing_concurrency_overlap"):
            capture.begin_attempt()
        capture.finish_attempt(handle, terminal_kind="response_accepted", raw_cleanup_status="completed")
        self.assertEqual(len(capture.snapshot()["attempts"]), 1)
        with self.assertRaisesRegex(TimingCaptureError, "timing_capture_halted"):
            capture.begin_attempt()

    def test_two_threads_cannot_reserve_two_attempts(self):
        capture = self.ready()
        barrier = threading.Barrier(3)
        results = []
        def reserve():
            barrier.wait(timeout=5)
            try:
                results.append(capture.begin_attempt())
            except TimingCaptureError as error:
                results.append(error.code)
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.count("timing_concurrency_overlap"), 1)
        self.assertEqual(sum(type(value) is not str for value in results), 1)

    def test_unknown_lifetime_never_becomes_zero_or_allows_resume(self):
        capture = self.ready()
        handle = capture.begin_attempt(); capture.send_started(handle)
        capture.finish_attempt(handle, terminal_kind="outcome_unknown", raw_cleanup_status="unknown")
        observed = capture.snapshot()["attempts"][0]
        for name in ("transport_terminal_ns", "raw_cleanup_terminal_ns", "lifetime_ended_ns"):
            self.assertIsNone(observed[name])
        with self.assertRaisesRegex(TimingCaptureError, "timing_capture_halted"):
            capture.begin_attempt()

    def test_no_send_failure_uses_transport_terminal_without_fake_raw_cleanup(self):
        capture = self.ready()
        handle = capture.begin_attempt(); capture.transport_terminal(handle)
        capture.finish_attempt(handle, terminal_kind="no_response", raw_cleanup_status="not_applicable")
        attempt = capture.snapshot()["attempts"][0]
        self.assertIsNone(attempt["send_started_ns"])
        self.assertIsNone(attempt["raw_cleanup_terminal_ns"])
        self.assertEqual(attempt["lifetime_ended_ns"], attempt["transport_terminal_ns"])
        self.assertTrue(capture.snapshot()["halted"])

    def test_clock_failures_poison_without_exposing_exception_or_epoch(self):
        for failure in (True, 1.5, -1, 999, 1001 + MAX_OFFSET_NS,
                        RuntimeError("Authorization: Bearer FAKE-CANARY")):
            with self.subTest(failure_type=type(failure).__name__):
                self.clock.side_effect = None
                self.clock.return_value = 1000
                capture = self.ready()
                self.clock.side_effect = [failure]
                with self.assertRaises(TimingCaptureError) as caught:
                    capture.begin_attempt()
                self.assertNotIn("Authorization", str(caught.exception))
                self.assertNotIn("1000", str(caught.exception))
                self.assertTrue(capture.snapshot()["clock_failed"])
                count = self.clock.call_count
                with self.assertRaises(TimingCaptureError):
                    capture.begin_attempt()
                self.assertEqual(self.clock.call_count, count)

    def test_key_release_and_post_cleanup_either_order_and_closed_capture_stays_closed(self):
        for order in (0, 1):
            capture = self.ready()
            self.accepted(capture)
            methods = [lambda: capture.post_cleanup_terminal(status="completed"),
                       lambda: capture.key_reference_released(status="released")]
            for method in methods[::1 if order == 0 else -1]:
                method()
            capture.phase_terminal()
            with self.assertRaisesRegex(TimingCaptureError, "timing_capture_halted"):
                capture.begin_attempt()

    def test_handles_cannot_be_reused_or_cross_capture(self):
        capture = self.ready()
        handle = self.accepted(capture)
        for target in (capture, self.ready()):
            with self.assertRaisesRegex(TimingCaptureError, "timing_attempt_handle_invalid"):
                target.send_started(handle)

    def test_phase_cannot_end_before_cleanup_or_key_release(self):
        capture = self.ready()
        self.accepted(capture)
        capture.post_cleanup_terminal(status="completed")
        with self.assertRaisesRegex(TimingCaptureError, "timing_lifecycle_invalid"):
            capture.phase_terminal()
        self.assertIsNone(capture.snapshot()["phase"]["phase_terminal_ns"])


if __name__ == "__main__":
    unittest.main()
