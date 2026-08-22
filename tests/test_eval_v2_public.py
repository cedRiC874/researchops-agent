from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_public import (
    EvalV2PublicTask,
    load_eval_v2_dataset_manifest,
    load_eval_v2_public_tasks,
    validate_eval_v2_suite,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = PROJECT_ROOT / "evals" / "v2"
CAMPAIGN_PATH = V2_ROOT / "campaign.json"
DATASETS_PATH = V2_ROOT / "external_datasets.json"
TASKS_PATH = V2_ROOT / "public_tasks.jsonl"
SCHEMA_PATH = V2_ROOT / "public_task_schema.json"
REVIEW_PATH = V2_ROOT / "internal_review.json"


class EvalV2PublicAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)
        self.dataset_payload = json.loads(DATASETS_PATH.read_text(encoding="utf-8"))
        self.task_payloads = [
            json.loads(line)
            for line in TASKS_PATH.read_text(encoding="utf-8").splitlines()
        ]

    def write_json(self, payload: dict[str, object], name: str) -> Path:
        path = self.temp_path / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return path

    def write_jsonl(self, payloads: list[dict[str, object]]) -> Path:
        path = self.temp_path / "tasks.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(item, ensure_ascii=False, allow_nan=False)
                for item in payloads
            ),
            encoding="utf-8",
        )
        return path

    def test_repository_public_assets_validate_without_network(self) -> None:
        result = validate_eval_v2_suite(
            campaign_path=CAMPAIGN_PATH,
            dataset_manifest_path=DATASETS_PATH,
            public_tasks_path=TASKS_PATH,
            task_schema_path=SCHEMA_PATH,
            internal_review_path=REVIEW_PATH,
        )

        public = result["public_assets"]
        self.assertEqual(public["verified_external_dataset_count"], 3)
        self.assertEqual(public["externally_reviewed_dataset_count"], 0)
        self.assertEqual(public["public_task_count"], 120)
        self.assertEqual(public["ready_public_task_count"], 120)
        self.assertEqual(
            public["public_task_split_counts"],
            {"development": 80, "public_regression": 40},
        )
        self.assertEqual(public["lifecycle_counts"], {"draft": 0, "ready": 120})
        self.assertEqual(public["internal_review"]["reviewed_task_count"], 120)
        self.assertFalse(public["internal_review"]["external_review"])
        self.assertEqual(public["private_task_count_in_repository"], 0)
        self.assertEqual(public["network_calls"], 0)
        self.assertRegex(public["dataset_manifest_sha256"], r"^[a-f0-9]{64}$")

    def test_verified_dataset_structure_is_frozen(self) -> None:
        manifest = load_eval_v2_dataset_manifest(DATASETS_PATH)
        datasets = manifest.by_id()

        penguins = datasets["palmer_penguins_v0_1_0"]
        self.assertEqual((penguins.row_count, penguins.column_count), (344, 8))
        self.assertEqual(penguins.missing_cell_count, 19)
        self.assertEqual(penguins.license_identifier, "CC0-1.0")

        parkinsons = datasets["uci_parkinsons_telemonitoring_189"]
        self.assertTrue(parkinsons.repeated_subjects)
        self.assertEqual(parkinsons.subject_count, 42)
        self.assertEqual(parkinsons.row_count, 5875)

        heart = datasets["uci_heart_disease_cleveland_45"]
        self.assertEqual((heart.row_count, heart.column_count), (303, 14))
        self.assertEqual(heart.missing_cell_count, 6)

    def test_public_projection_excludes_golden_canary(self) -> None:
        payload = copy.deepcopy(self.task_payloads[0])
        payload["expected"]["forbidden_phrases"] = ["GOLDEN-CANARY-PRIVATE"]
        task = EvalV2PublicTask.from_dict(payload)

        public_json = json.dumps(task.public_input(), ensure_ascii=False)

        self.assertNotIn("expected", task.public_input())
        self.assertNotIn("GOLDEN-CANARY-PRIVATE", public_json)

    def test_private_task_cannot_enter_public_corpus(self) -> None:
        payload = copy.deepcopy(self.task_payloads[2])
        payload["split"] = "private_holdout"

        with self.assertRaises(EvalV2ContractError) as context:
            EvalV2PublicTask.from_dict(payload)

        self.assertEqual(context.exception.code, "eval_v2_invalid_choice")

    def test_dataset_authorization_and_manifest_membership_are_enforced(self) -> None:
        mismatch = copy.deepcopy(self.task_payloads[0])
        mismatch["context"]["dataset_id"] = "uci_heart_disease_cleveland_45"
        with self.assertRaises(EvalV2ContractError) as mismatch_error:
            EvalV2PublicTask.from_dict(mismatch)
        self.assertEqual(
            mismatch_error.exception.code, "eval_v2_dataset_authorization_mismatch"
        )

        unknown = copy.deepcopy(self.task_payloads[0])
        unknown["dataset_id"] = "unknown_dataset"
        unknown["context"]["dataset_id"] = "unknown_dataset"
        tasks_path = self.write_jsonl([unknown])
        manifest = load_eval_v2_dataset_manifest(DATASETS_PATH)
        with self.assertRaises(EvalV2ContractError) as unknown_error:
            load_eval_v2_public_tasks(tasks_path, manifest)
        self.assertEqual(unknown_error.exception.code, "eval_v2_unknown_dataset")

    def test_ready_task_requires_review_and_campaign_count_update(self) -> None:
        unreviewed = copy.deepcopy(self.task_payloads[81])
        unreviewed["lifecycle_status"] = "ready"
        unreviewed["review_status"] = "unreviewed"
        with self.assertRaises(EvalV2ContractError) as review_error:
            EvalV2PublicTask.from_dict(unreviewed)
        self.assertEqual(review_error.exception.code, "eval_v2_unreviewed_ready_task")

        draft = copy.deepcopy(unreviewed)
        draft["lifecycle_status"] = "draft"
        payloads = copy.deepcopy(self.task_payloads)
        payloads[81] = draft
        tasks_path = self.write_jsonl(payloads)
        with self.assertRaises(EvalV2ContractError) as count_error:
            validate_eval_v2_suite(
                campaign_path=CAMPAIGN_PATH,
                dataset_manifest_path=DATASETS_PATH,
                public_tasks_path=tasks_path,
                task_schema_path=SCHEMA_PATH,
                internal_review_path=REVIEW_PATH,
            )
        self.assertEqual(
            count_error.exception.code, "eval_v2_campaign_task_count_mismatch"
        )

    def test_repeated_measure_dataset_requires_subject_metadata(self) -> None:
        payload = copy.deepcopy(self.dataset_payload)
        parkinsons = payload["datasets"][1]
        parkinsons["structure"]["subject_count"] = None

        with self.assertRaises(EvalV2ContractError) as context:
            load_eval_v2_dataset_manifest(
                self.write_json(payload, "datasets.json")
            )

        self.assertEqual(
            context.exception.code, "eval_v2_repeated_measure_metadata_missing"
        )

    def test_license_and_hash_metadata_are_strict(self) -> None:
        bad_license = copy.deepcopy(self.dataset_payload)
        bad_license["datasets"][0]["license"]["status"] = "assumed"
        with self.assertRaises(EvalV2ContractError) as license_error:
            load_eval_v2_dataset_manifest(
                self.write_json(bad_license, "bad-license.json")
            )
        self.assertEqual(license_error.exception.code, "eval_v2_license_unverified")

        bad_hash = copy.deepcopy(self.dataset_payload)
        bad_hash["datasets"][0]["source"]["selected_asset_sha256"] = "not-a-hash"
        with self.assertRaises(EvalV2ContractError) as hash_error:
            load_eval_v2_dataset_manifest(self.write_json(bad_hash, "bad-hash.json"))
        self.assertEqual(hash_error.exception.code, "eval_v2_invalid_sha256")

    def test_campaign_pins_dataset_manifest_hash(self) -> None:
        campaign = json.loads(CAMPAIGN_PATH.read_text(encoding="utf-8"))
        campaign["freeze_policy"]["hashes"]["dataset_manifest_sha256"] = "0" * 64
        campaign_path = self.write_json(campaign, "campaign.json")

        with self.assertRaises(EvalV2ContractError) as context:
            validate_eval_v2_suite(
                campaign_path=campaign_path,
                dataset_manifest_path=DATASETS_PATH,
                public_tasks_path=TASKS_PATH,
                task_schema_path=SCHEMA_PATH,
                internal_review_path=REVIEW_PATH,
            )

        self.assertEqual(
            context.exception.code, "eval_v2_dataset_manifest_hash_mismatch"
        )

    def test_campaign_pins_public_corpus_hash(self) -> None:
        campaign = json.loads(CAMPAIGN_PATH.read_text(encoding="utf-8"))
        campaign["freeze_policy"]["hashes"]["public_corpus_sha256"] = "0" * 64
        campaign_path = self.write_json(campaign, "campaign-public-hash.json")

        with self.assertRaises(EvalV2ContractError) as context:
            validate_eval_v2_suite(
                campaign_path=campaign_path,
                dataset_manifest_path=DATASETS_PATH,
                public_tasks_path=TASKS_PATH,
                task_schema_path=SCHEMA_PATH,
                internal_review_path=REVIEW_PATH,
            )

        self.assertEqual(
            context.exception.code, "eval_v2_public_corpus_hash_mismatch"
        )

    def test_internal_review_is_hash_bound_to_ready_task_scope(self) -> None:
        review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
        review["public_tasks_sha256"] = "0" * 64
        review_path = self.write_json(review, "review.json")

        with self.assertRaises(EvalV2ContractError) as context:
            validate_eval_v2_suite(
                campaign_path=CAMPAIGN_PATH,
                dataset_manifest_path=DATASETS_PATH,
                public_tasks_path=TASKS_PATH,
                task_schema_path=SCHEMA_PATH,
                internal_review_path=review_path,
            )

        self.assertEqual(
            context.exception.code, "eval_v2_internal_review_hash_mismatch"
        )


if __name__ == "__main__":
    unittest.main()
