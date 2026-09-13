"""Fresh writer ownership on temporary stores; not runtime or Provider grants."""
from __future__ import annotations

import copy
import json
import os
import pickle
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import local_claim as claim
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests import test_completion_local_claim as fixtures


@unittest.skipUnless(os.name == "nt", "fixed Windows claim-store integration")
class ClaimOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        locator = patch.object(claim, "_windows_local_app_data", return_value=self.root)
        locator.start(); self.addCleanup(locator.stop)
        environment = claim.provision_local_claim_store()["execution_environment_id"]
        self.request = raw(fixtures.LocalClaimTests().request(environment))
        self.marker = self.root / "ResearchOpsAgent/completion-claims-v1/claims" / ("1" * 64 + ".json")

    def test_fresh_winner_transfers_once_but_does_not_grant_provider_permission(self):
        winner = claim._reserve_with_ownership(self.request)
        receipt = winner._take()
        self.assertEqual(receipt, self.marker.read_bytes())
        self.assertFalse(json.loads(receipt)["provider_actions_authorized"])
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_ownership_already_taken"):
            winner._take()
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim._reserve_with_ownership(self.request)

    def test_public_receipt_and_readable_marker_cannot_recover_ownership(self):
        summary = claim.reserve_local_claim(self.request)
        receipt = claim.read_reserved_local_claim(self.request, expected_receipt_sha256=summary["receipt_sha256"])
        self.assertEqual(receipt, self.marker.read_bytes())
        with self.assertRaises(TypeError):
            claim._WinningLocalClaim(object(), receipt)
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim._reserve_with_ownership(self.request)

    def test_copy_pickle_and_wrong_process_cannot_duplicate_a_winner(self):
        winner = claim._reserve_with_ownership(self.request)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.subTest(operation=operation.__name__), self.assertRaises(TypeError):
                operation(winner)
        with patch.object(claim.os, "getpid", return_value=os.getpid() + 1):
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_ownership_wrong_process"):
                winner._take()
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_ownership_already_taken"):
            winner._take()

    def test_concurrent_take_has_exactly_one_winner(self):
        winner = claim._reserve_with_ownership(self.request)
        barrier = threading.Barrier(2)
        def take():
            barrier.wait(timeout=5)
            try:
                winner._take()
                return "taken"
            except claim.LocalClaimError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(take) for _ in range(2)]
            results = [job.result(timeout=10) for job in jobs]
        self.assertCountEqual(results, ["taken", "local_claim_ownership_already_taken"])

    def test_abandonment_keeps_marker_and_never_releases_the_authorization(self):
        winner = claim._reserve_with_ownership(self.request)
        before = self.marker.read_bytes()
        winner._invalidate()
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_ownership_already_taken"):
            winner._take()
        self.assertEqual(self.marker.read_bytes(), before)
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim.reserve_local_claim(self.request)

    def test_fsync_failure_cannot_create_ownership_even_with_complete_bytes(self):
        with patch.object(claim.os, "fsync", side_effect=OSError("synthetic flush failure")), \
             patch.object(claim, "_WinningLocalClaim", side_effect=AssertionError("must not mint")) as constructor:
            with self.assertRaises(claim.LocalClaimError):
                claim._reserve_with_ownership(self.request)
            constructor.assert_not_called()
        self.assertGreater(len(self.marker.read_bytes()), 0)
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim._reserve_with_ownership(self.request)

    def test_handle_close_failure_cannot_create_ownership(self):
        original = claim._locked_chain
        @contextmanager
        def close_unknown(path):
            with original(path):
                yield
            raise claim.LocalClaimError("local_claim_handle_close_unknown", claim_may_exist=True)
        with patch.object(claim, "_locked_chain", close_unknown), \
             patch.object(claim, "_WinningLocalClaim", side_effect=AssertionError("must not mint")) as constructor:
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_handle_close_unknown"):
                claim._reserve_with_ownership(self.request)
            constructor.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_handle_allocation_failure_keeps_the_completed_claim_consumed(self):
        with patch.object(claim, "_WinningLocalClaim", side_effect=MemoryError("synthetic allocation failure")):
            with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_ownership_unavailable") as caught:
                claim._reserve_with_ownership(self.request)
        self.assertTrue(caught.exception.claim_may_exist)
        self.assertTrue(self.marker.exists())
        with self.assertRaisesRegex(claim.LocalClaimError, "local_claim_already_exists"):
            claim._reserve_with_ownership(self.request)


if __name__ == "__main__":
    unittest.main()
