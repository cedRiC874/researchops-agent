"""Bounded exclusive writes and post-phase clock behavior on temporary data."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live_publish as publish, first_live_start as start
from researchops_completion_timing.clock import _TimingClock, TimingCaptureError


class ExclusivePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "payload.json"

    def test_existing_file_is_never_overwritten(self):
        self.path.write_bytes(b"existing test data")
        created = []
        with self.assertRaises(FileExistsError):
            publish._write_exclusive(self.path, b"new", created)
        self.assertEqual(created, [])
        self.assertEqual(self.path.read_bytes(), b"existing test data")

    def test_partial_write_is_recorded_and_retained(self):
        original = os.write
        calls = []
        def write(fd, data):
            calls.append(True)
            if len(calls) == 1:
                return original(fd, data[:3])
            raise OSError("synthetic disk failure")
        created = []
        with patch.object(publish.os, "write", side_effect=write):
            with self.assertRaises(OSError):
                publish._write_exclusive(self.path, b"bounded fixture", created)
        self.assertEqual(created, ["payload.json"])
        self.assertEqual(self.path.read_bytes(), b"bou")
        with self.assertRaises(FileExistsError):
            publish._write_exclusive(self.path, b"retry forbidden", [])

    def test_fsync_failure_does_not_remove_the_written_file(self):
        created = []
        with patch.object(publish.os, "fsync", side_effect=OSError("synthetic flush failure")):
            with self.assertRaises(OSError):
                publish._write_exclusive(self.path, b"{}", created)
        self.assertEqual(created, ["payload.json"])
        self.assertEqual(self.path.read_bytes(), b"{}")

    def test_zero_progress_write_cannot_loop_or_report_success(self):
        created = []
        with patch.object(publish.os, "write", return_value=0):
            with self.assertRaisesRegex(ValueError, "first_live_publish_write_unknown"):
                publish._write_exclusive(self.path, b"{}", created)
        self.assertEqual(created, ["payload.json"])
        self.assertEqual(self.path.read_bytes(), b"")

    def test_sealing_uses_same_origin_without_reapplying_closed_phase_cap(self):
        native = [1000]
        with patch("researchops_completion_timing.clock.time.monotonic_ns", side_effect=lambda: native[0]):
            clock = _TimingClock(request_timeout_ns=50, phase_timeout_ns=100)
            with self.assertRaisesRegex(TimingCaptureError, "timing_sealing_not_ready"):
                clock._sealing_offset()
            clock = _TimingClock(request_timeout_ns=50, phase_timeout_ns=100)
            clock.task_released(); clock.key_loaded()
            clock.post_cleanup_terminal(status="completed"); clock.key_reference_released(status="released"); clock.phase_terminal()
            native[0] = 1150
            self.assertEqual(clock._sealing_offset(), 150)
            self.assertFalse(clock.snapshot()["halted"])
            clock.abort_new_work()
            with self.assertRaisesRegex(TimingCaptureError, "timing_sealing_not_ready"):
                clock._sealing_offset()

    def test_publication_can_use_reserved_time_but_not_extend_whole_deadline(self):
        with patch.object(start, "_monotonic_ns", side_effect=[0, 300_000_000_000, 330_000_000_000]), \
             patch.object(start, "_utc_now", return_value="2026-09-08T00:00:00Z"):
            budget = start._StartBudget()
            _stamp, deadline = budget.checkpoint(reserve_ns=0)
            self.assertEqual(deadline, 330_000_000_000)
            with self.assertRaisesRegex(ValueError, "budget_exhausted"):
                budget.checkpoint(reserve_ns=0)

    def test_failure_persistence_cannot_restart_an_existing_sealing_budget(self):
        with patch.object(start, '_monotonic_ns', side_effect=[0, 5_000_000_000, 35_000_000_000, 35_000_000_001]), \
             patch.object(start, '_utc_now', return_value='2026-09-08T00:00:00Z'):
            budget = start._StartBudget()
            budget.artifact_checkpoint('2026-09-08T00:10:00Z')
            budget.artifact_checkpoint('2026-09-08T00:10:00Z')
            with self.assertRaisesRegex(ValueError, 'artifact_budget_exhausted'):
                budget.artifact_checkpoint('2026-09-08T00:10:00Z')


if __name__ == "__main__":
    unittest.main()
