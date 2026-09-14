"""The old accepted v11 remains valid at its original bytes, not the new tree."""
import hashlib
from pathlib import Path
import unittest

from researchops_external_closure.execution_current_v4 import verify_current_timed_profile
from tests.internal_v11_historical_support import historical_v11_root

ROOT=Path(__file__).resolve().parents[1]


class InternalHistoryTests(unittest.TestCase):
    def test_v11_original_commitments_and_bytes_remain_verifiable(self):
        historical=historical_v11_root()
        result=verify_current_timed_profile(historical,profile="first_live")
        self.assertEqual(result.source_integrity_commitment_sha256,"7ef11c4658967e47a56d92323e6ac118dfaef57ffd699b721bac1e25d0383b8d")
        self.assertEqual(result.implementation_commitment_sha256,"b4c384f5cd832f0bcbcfe1effbdf03da799aa4bd9b8f6910619ea575b487a24c")
        self.assertFalse(result.online_execution_authorized)
        for name in ("evals/phase6_deepseek_depth60_plan_v11.json",
            "evals/provider_completion_execution_binding_v4/implementation_manifest_first_live_v3.json"):
            self.assertEqual((ROOT/name).read_bytes(),(historical/name).read_bytes())

    def test_v11_does_not_silently_accept_internal_source(self):
        with self.assertRaises(Exception) as caught:verify_current_timed_profile(ROOT,profile="first_live")
        self.assertTrue(str(getattr(caught.exception,"code","")).startswith("execution_v4_"))


if __name__=="__main__":unittest.main()
