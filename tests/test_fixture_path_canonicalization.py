"""Noncanonical fixture paths must not bypass or trip production identity gates.

Only the temporary-directory spelling is varied. The directory, Git objects,
source checks, claim IO, SQLite, and existing fault injection all remain real.
The batch's separate native 8.3 run covers the actual Windows CI spelling.
"""
from __future__ import annotations

import atexit
import asyncio
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class _NoncanonicalTemporaryDirectory:
    """Own a real directory but expose a deliberately noncanonical spelling."""
    def __init__(self, *args, **kwargs):
        self._real = tempfile.TemporaryDirectory(*args, **kwargs)
        directory = Path(self._real.name)
        anchor = directory / "existing-path-spelling-anchor"
        anchor.mkdir()
        self.name = str(anchor / "..")
    def __enter__(self):
        return self.name
    def __exit__(self, *args):
        return self._real.__exit__(*args)
    def cleanup(self):
        self._real.cleanup()


def _temporary_namespace():
    return SimpleNamespace(TemporaryDirectory=_NoncanonicalTemporaryDirectory)


class FixtureCanonicalPathTests(unittest.TestCase):
    def check_timed_fixture(self, version):
        from tests import timed_first_live_evidence_fixture as fixtures
        with patch.object(fixtures, "tempfile", _temporary_namespace()):
            fixture = fixtures.TimedEvidenceFixture(implementation_version=version)
        self.addCleanup(fixture.close)
        supplied = Path(fixture.temporary)
        self.assertNotEqual(supplied, fixture.root)
        self.assertEqual(supplied.resolve(strict=True), fixture.root)
        self.assertEqual(fixture.repository.root, fixture.root / "repo")
        self.assertEqual(fixture.repository.root.resolve(strict=True), fixture.repository.root)

    def test_v4_fixture_canonicalizes_actual_noncanonical_directory(self):
        self.check_timed_fixture(4)

    def test_v5_fixture_canonicalizes_actual_noncanonical_directory(self):
        self.check_timed_fixture(5)

    def test_historical_fixture_canonicalizes_location_without_replacing_evidence(self):
        from tests import historical_integrity_support as historical
        from researchops import deepseek_completion_first_live_validation as first_live
        with patch.object(historical, "_root", None), patch.object(historical, "_temporary", None), \
             patch.object(historical, "tempfile", _temporary_namespace()):
            try:
                root = historical.historical_integrity_root()
                self.assertNotEqual(Path(historical._temporary.name), root)
                self.assertEqual(root, root.resolve(strict=True))
                self.assertEqual(historical.HISTORICAL_COMMIT, "5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab")
                self.assertEqual(historical.HISTORICAL_TREE, "30ecfd86ac00aecd8b67305a7c6ed2af88eeee10")
                bound = first_live._safe_fixed_file(root, first_live.SOURCE_INTEGRITY_PLAN_RELATIVE_PATH)
                self.assertEqual(hashlib.sha256(bound.read_bytes()).hexdigest(),
                                 "ff39dd5a1aa09b7bc92b27f9d800b5d51fbd2fd69c2a599b5bf0f25aed490aae")
            finally:
                if historical._temporary is not None:
                    atexit.unregister(historical._temporary.cleanup)
                    historical._temporary.cleanup()

    @unittest.skipUnless(os.name == "nt", "existing Windows claim-store test")
    def test_post_claim_fault_reaches_its_existing_branch_with_noncanonical_temp(self):
        from tests import test_completion_campaign_start as fixtures
        helper = fixtures.CampaignPostClaimFailureTests()
        self.addCleanup(helper.doCleanups)
        with patch.object(fixtures, "tempfile", _temporary_namespace()):
            helper.test_post_claim_source_failure_keeps_the_real_marker_consumed()


class WorkspaceCanonicalPathTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "nt", "existing Windows held-workspace test")
    async def test_held_database_uses_same_spelling_as_ledger(self):
        from tests import test_completion_campaign_workspace as fixtures
        from researchops_completion_timing import campaign_phase as phase, campaign_runtime as runtime, campaign_tasks as tasks
        before = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
                  tasks._CampaignTaskOwner._check_current, runtime._CampaignModelFactory._check_current,
                  runtime._native_transport, phase._load_configured_key)
        helper = fixtures.HeldCampaignPublicationTests()
        try:
            with patch.object(fixtures, "tempfile", _temporary_namespace()):
                await helper.test_actual_runner_and_publication_keep_the_work_database_held()
        finally:
            # This borrowed IsolatedAsyncioTestCase has no runner. doCleanups()
            # would catch its own runner assertion and silently leave patches.
            # Its callbacks are synchronous, as in CampaignPhaseTests' helper.
            with ExitStack() as cleanup:
                for function, args, kwargs in helper._cleanups:
                    cleanup.callback(function, *args, **kwargs)
                helper._cleanups.clear()
        after = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
                 tasks._CampaignTaskOwner._check_current, runtime._CampaignModelFactory._check_current,
                 runtime._native_transport, phase._load_configured_key)
        for expected, observed in zip(before, after):
            self.assertIs(observed, expected)
        # Exercise the actual Windows self-pipe after teardown, not a mock of it.
        loop = asyncio.new_event_loop()
        loop.close()


if __name__ == "__main__":
    unittest.main()
