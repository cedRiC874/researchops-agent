from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from researchops.eval_v2_contracts import (
    EvalV2Campaign,
    EvalV2ContractError,
    load_eval_v2_campaign,
    validate_eval_v2_campaign,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = PROJECT_ROOT / "evals" / "v2" / "campaign.json"


class EvalV2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(CAMPAIGN_PATH.read_text(encoding="utf-8"))
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)

    def write_campaign(self, payload: dict[str, object]) -> Path:
        path = self.temp_path / "campaign.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return path

    def test_repository_campaign_is_valid_but_explicitly_not_run(self) -> None:
        result = validate_eval_v2_campaign(CAMPAIGN_PATH)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["campaign_status"], "design_only")
        self.assertEqual(result["evidence_status"], "not_run")
        self.assertFalse(result["ready_for_freeze"])
        self.assertEqual(result["target_task_count"], 170)
        self.assertEqual(result["registered_task_count"], 120)
        self.assertEqual(
            result["split_registered_counts"],
            {"development": 80, "public_regression": 40, "private_holdout": 0},
        )
        self.assertEqual(result["repetitions_per_provider"], 3)
        self.assertEqual(result["network_calls"], 0)
        self.assertRegex(result["campaign_sha256"], r"^[a-f0-9]{64}$")

    def test_public_summary_does_not_expose_private_content_or_locator(self) -> None:
        campaign = load_eval_v2_campaign(CAMPAIGN_PATH)
        serialized = json.dumps(campaign.public_summary(), ensure_ascii=False)

        self.assertNotIn("locator", serialized.lower())
        self.assertNotIn("golden_review_status", serialized)
        self.assertNotIn("private_task", serialized)
        self.assertNotIn("commitment_sha256", serialized)
        self.assertIn('"private_holdout_content_in_repository": false', serialized)

    def test_rejects_private_content_or_prompt_tuning_in_repository(self) -> None:
        private_content = copy.deepcopy(self.payload)
        private_content["private_holdout_policy"]["content_in_repository"] = True
        with self.assertRaises(EvalV2ContractError) as content_error:
            EvalV2Campaign.from_dict(private_content)
        self.assertEqual(content_error.exception.code, "eval_v2_private_content_in_repo")

        private_tuning = copy.deepcopy(self.payload)
        private_tuning["splits"]["private_holdout"]["prompt_tuning_allowed"] = True
        with self.assertRaises(EvalV2ContractError) as tuning_error:
            EvalV2Campaign.from_dict(private_tuning)
        self.assertEqual(tuning_error.exception.code, "eval_v2_invalid_tuning_policy")

    def test_rejects_targets_below_pre_registered_minimums(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["splits"]["private_holdout"]["target_task_count"] = 49

        with self.assertRaises(EvalV2ContractError) as context:
            EvalV2Campaign.from_dict(payload)

        self.assertEqual(context.exception.code, "eval_v2_target_too_small")

    def test_rejects_unreviewed_external_dataset_marked_registered(self) -> None:
        payload = copy.deepcopy(self.payload)
        dataset = payload["dataset_policy"]["datasets"][1]
        dataset["status"] = "registered"

        with self.assertRaises(EvalV2ContractError) as context:
            EvalV2Campaign.from_dict(payload)

        self.assertEqual(context.exception.code, "eval_v2_dataset_not_reviewed")

    def test_rejects_premature_frozen_claim(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["status"] = "frozen"

        with self.assertRaises(EvalV2ContractError) as context:
            EvalV2Campaign.from_dict(payload)

        self.assertEqual(context.exception.code, "eval_v2_not_ready_to_freeze")

    def test_accepts_frozen_status_only_after_all_readiness_gates(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["status"] = "frozen"
        for split in payload["splits"].values():
            split["registered_task_count"] = split["target_task_count"]
        for dataset in payload["dataset_policy"]["datasets"][1:]:
            dataset["status"] = "registered"
            dataset["license_status"] = "approved"
            dataset["external_review_status"] = "completed"
        second_provider = payload["run_policy"]["providers"][1]
        second_provider["status"] = "registered"
        second_provider["model_id"] = "provider-model-v1"
        second_provider["transport_id"] = "provider-transport-v1"
        for name in payload["freeze_policy"]["hashes"]:
            payload["freeze_policy"]["hashes"][name] = "a" * 64
        payload["private_holdout_policy"]["commitment_sha256"] = "b" * 64
        payload["external_review_policy"]["golden_review_status"] = "completed"
        payload["external_review_policy"]["statistical_crosscheck_status"] = "completed"

        campaign = EvalV2Campaign.from_dict(payload)

        self.assertEqual(campaign.status, "frozen")
        self.assertEqual(campaign.readiness_gaps(), ())
        self.assertTrue(campaign.public_summary()["ready_for_freeze"])

    def test_rejects_duplicate_json_keys(self) -> None:
        serialized = CAMPAIGN_PATH.read_text(encoding="utf-8").replace(
            '"schema_version": "2.0"',
            '"schema_version": "2.0", "schema_version": "2.0"',
            1,
        )
        path = self.temp_path / "duplicate.json"
        path.write_text(serialized, encoding="utf-8")

        with self.assertRaises(EvalV2ContractError) as context:
            load_eval_v2_campaign(path)

        self.assertEqual(context.exception.code, "eval_v2_duplicate_json_key")

    def test_unknown_field_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["private_holdout_path"] = "do-not-allow-locators"

        with self.assertRaises(EvalV2ContractError) as context:
            EvalV2Campaign.from_dict(payload)

        self.assertEqual(context.exception.code, "eval_v2_unknown_field")


if __name__ == "__main__":
    unittest.main()
