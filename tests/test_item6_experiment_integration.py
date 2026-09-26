import unittest
from . import item6_experiment_fixture as f


class IntegrationTests(unittest.TestCase):
    def test_actual_entry_factory_adapter_event_chain_and_fcc(self):
        result = f.normal()
        self.assertEqual(result["process_exit_code"], 0, result.get("error") or result.get("code"))
        self.assertEqual(result["claim_files"], 1)
        self.assertEqual(len(result["calls"]), 30)
        self.assertEqual(result["network_attempts"], 0)
        self.assertFalse(result["real_store_touched"])
        artifact = result["artifact"]
        self.assertEqual(artifact["dispatches"], 30)
        self.assertEqual(artifact["mode"], "offline_test")
        for path in ("agent", "fixed_workflow"):
            self.assertEqual(len(artifact["business"][path]), 16)
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["planned"], 16)
            self.assertEqual(artifact["scores"][path]["raw_report"]["counts"]["pass"], 16)
        self.assertTrue(all(r["model_request_count"] == 0 for r in artifact["business"]["fixed_workflow"].values()))

    def test_new_process_independent_readback(self):
        result = f.verify_result(f.normal())
        self.assertEqual(result["actual_exit_code"], 0, result)
        self.assertTrue(result["archive_verified"])
        self.assertFalse(result["runtime_authority_granted"])
        self.assertEqual(result["business_denominator"], 32)

    def test_free_expression_unknown_is_not_rewritten_or_retried(self):
        result = f.run_case("free_expression")
        self.assertEqual(result["process_exit_code"], 0)
        artifact = result["artifact"]
        self.assertEqual(artifact["business"]["agent"]["IC-01"]["final_output"], "The source contains a small mass; see the aggregate package.")
        self.assertEqual(artifact["scores"]["agent"]["raw_report"]["counts"]["unknown"], 1)
        self.assertEqual(len(result["calls"]), 30)
