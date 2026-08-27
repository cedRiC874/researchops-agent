from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = ROOT / "docs/evidence/kimi-historical-status-overlays-v1"
PACK_OVERLAY = OVERLAY_ROOT / "pilot_pack_v7_v8_post_lock_status.json"
LINKABILITY_OVERLAY = OVERLAY_ROOT / "kimi_v1_chain_linkability_disclosure.json"
TASK_COMMITMENT = "83363291f30c7edd62d30e88da38fcf966b7d01c5ac16d3a2964ee9571555d72"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_commitment(tasks: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "task_id": task["task_id"],
            "sequence": index,
            "source_task_id": task["source_task_id"],
            "dataset_id": task["dataset_id"],
            "scenario": task["scenario"],
            "prompt_en": task["prompt_en"],
            "prompt_zh": task["prompt_zh"],
            "context": task["context"],
            "clarification_expected": task["clarification_expected"],
        }
        for index, task in enumerate(tasks, 1)
    ]
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class KimiHistoricalStatusOverlayTests(unittest.TestCase):
    def test_pack_overlay_binds_locked_lineage_and_post_lock_observations(self) -> None:
        overlay = _load(PACK_OVERLAY)
        self.assertEqual(
            set(overlay),
            {
                "schema_version",
                "overlay_id",
                "snapshot_date_local",
                "timezone",
                "locked_artifacts_modified",
                "active_supervised_baseline",
                "historical_entries",
                "authorizes_online_run",
                "authorizes_retry",
                "authorizes_provider_registration",
                "authorizes_model_quality_claim",
                "authorizes_external_validation_claim",
                "authorizes_private_evaluation",
                "authorizes_non_synthetic_data",
            },
        )
        self.assertEqual(
            overlay["schema_version"],
            "supervised-pilot-pack-post-lock-overlay/1.0",
        )
        self.assertEqual(overlay["snapshot_date_local"], "2026-08-27")
        self.assertEqual(overlay["timezone"], "Asia/Shanghai")
        self.assertFalse(overlay["locked_artifacts_modified"])
        for claim in (
            "authorizes_online_run",
            "authorizes_retry",
            "authorizes_provider_registration",
            "authorizes_model_quality_claim",
            "authorizes_external_validation_claim",
            "authorizes_private_evaluation",
            "authorizes_non_synthetic_data",
        ):
            self.assertFalse(overlay[claim])

        active = overlay["active_supervised_baseline"]
        self.assertEqual(
            active,
            {
                "candidate_commitment_sha256": "105b7def81148566219673fd40e88e392674070656c72a17cbcf60405165dffc",
                "pack_file": "services/pilot_staging/content/pilot_pack.supervised_v6.json",
                "pack_sha256": "7536b148fd3873135707962421e57482602a3daccb1d982f6bb4d72219cec3c5",
                "review_file": "services/pilot_staging/content/pilot_pack.supervised_v6.review.json",
                "review_sha256": "6b62f4125c1b4b8ee99c1dcd4fc28a8ffb87de746457c6e345965e87fe9cbdfd",
                "current_source_execution_authorized": False,
                "current_successor_required_before_online_execution": True,
            },
        )

        entries = overlay["historical_entries"]
        self.assertEqual(len(entries), 2)
        expected = (
            {
                "candidate_commitment": "57d0c1b054367794fa08ec2dbdecdeb0c75bc4be2c45894b69db832a1f6f5641",
                "pack": "services/pilot_staging/content/pilot_pack.supervised_v7.json",
                "pack_bytes": 3503,
                "pack_sha": "636701f8038c48a8c89b2bf024eb579b1bb7a730e8c5d5afaf38457d64756316",
                "review": "services/pilot_staging/content/pilot_pack.supervised_v7.review.json",
                "review_bytes": 1541,
                "review_sha": "e7bf9f76faaffacc34e573fd6ebff0de18250b2d9c97666773a4cefdcc9f5106",
                "predecessor": "services/pilot_staging/content/pilot_pack.supervised_v6.json",
                "predecessor_sha": "7536b148fd3873135707962421e57482602a3daccb1d982f6bb4d72219cec3c5",
                "predecessor_review": "services/pilot_staging/content/pilot_pack.supervised_v6.review.json",
                "predecessor_review_sha": "6b62f4125c1b4b8ee99c1dcd4fc28a8ffb87de746457c6e345965e87fe9cbdfd",
                "projection": "docs/evidence/kimi-controlled-pilot-usage-failure-v1/public_receipt_projection.json",
                "projection_sha": "156ecbdc73206663aa9c87aa874ce7ace93133dcbba18fb6c6a9431237dad1d1",
            },
            {
                "candidate_commitment": "2d0b9952a556eed6982eac2b4e4d050efb40f518551738e51ceaf671a1d223d5",
                "pack": "services/pilot_staging/content/pilot_pack.supervised_v8.json",
                "pack_bytes": 3502,
                "pack_sha": "e5c2c895ea838753356360041be28701f41a74b057d9782b2ef8c7c74d721100",
                "review": "services/pilot_staging/content/pilot_pack.supervised_v8.review.json",
                "review_bytes": 1642,
                "review_sha": "45df91122f06bc0e538082b58b47dadcd4cd0bd71fdc0afea54d461165c39ee7",
                "predecessor": "services/pilot_staging/content/pilot_pack.supervised_v7.json",
                "predecessor_sha": "636701f8038c48a8c89b2bf024eb579b1bb7a730e8c5d5afaf38457d64756316",
                "predecessor_review": "services/pilot_staging/content/pilot_pack.supervised_v7.review.json",
                "predecessor_review_sha": "e7bf9f76faaffacc34e573fd6ebff0de18250b2d9c97666773a4cefdcc9f5106",
                "projection": "docs/evidence/kimi-controlled-pilot-v2-response-failure-v1/public_receipt_projection.json",
                "projection_sha": "694f871d9a66673f5357330240c2ee63f09c3aff3e50089117cae168cc2bc680",
            },
        )
        entry_keys = {
            "candidate_id",
            "candidate_commitment_sha256",
            "pack_file",
            "pack_bytes",
            "pack_sha256",
            "review_file",
            "review_bytes",
            "review_sha256",
            "predecessor_pack_file",
            "predecessor_pack_sha256",
            "predecessor_review_file",
            "predecessor_review_sha256",
            "task_count",
            "task_pack_commitment_sha256",
            "tasks_equal_predecessor",
            "provider_equal_predecessor",
            "post_lock_observation",
        }
        observation_keys = {
            "public_projection_file",
            "public_projection_sha256",
            "occurred",
            "status",
            "model_request_count",
            "network_calls",
            "candidate_result_created",
            "inherited_into_candidate",
            "inherited_into_pack",
            "model_quality_claim_allowed",
        }
        for entry, binding in zip(entries, expected, strict=True):
            self.assertEqual(set(entry), entry_keys)
            self.assertEqual(entry["candidate_commitment_sha256"], binding["candidate_commitment"])
            self.assertEqual(entry["pack_file"], binding["pack"])
            self.assertEqual(entry["pack_bytes"], binding["pack_bytes"])
            self.assertEqual(entry["pack_sha256"], binding["pack_sha"])
            self.assertEqual(entry["review_file"], binding["review"])
            self.assertEqual(entry["review_bytes"], binding["review_bytes"])
            self.assertEqual(entry["review_sha256"], binding["review_sha"])
            self.assertEqual(entry["predecessor_pack_file"], binding["predecessor"])
            self.assertEqual(entry["predecessor_pack_sha256"], binding["predecessor_sha"])
            self.assertEqual(entry["predecessor_review_file"], binding["predecessor_review"])
            self.assertEqual(
                entry["predecessor_review_sha256"], binding["predecessor_review_sha"]
            )
            self.assertEqual(entry["task_count"], 6)
            self.assertEqual(entry["task_pack_commitment_sha256"], TASK_COMMITMENT)
            self.assertTrue(entry["tasks_equal_predecessor"])
            self.assertTrue(entry["provider_equal_predecessor"])

            pack_path = ROOT / entry["pack_file"]
            review_path = ROOT / entry["review_file"]
            predecessor_path = ROOT / entry["predecessor_pack_file"]
            predecessor_review_path = ROOT / entry["predecessor_review_file"]
            self.assertEqual(pack_path.stat().st_size, entry["pack_bytes"])
            self.assertEqual(_sha256(pack_path), entry["pack_sha256"])
            self.assertEqual(review_path.stat().st_size, entry["review_bytes"])
            self.assertEqual(_sha256(review_path), entry["review_sha256"])
            self.assertEqual(_sha256(predecessor_path), entry["predecessor_pack_sha256"])
            self.assertEqual(
                _sha256(predecessor_review_path), entry["predecessor_review_sha256"]
            )
            pack = _load(pack_path)
            predecessor = _load(predecessor_path)
            self.assertEqual(pack["tasks"], predecessor["tasks"])
            self.assertEqual(pack["provider"], predecessor["provider"])
            self.assertEqual(_task_commitment(pack["tasks"]), TASK_COMMITMENT)

            observation = entry["post_lock_observation"]
            self.assertEqual(set(observation), observation_keys)
            self.assertEqual(observation["public_projection_file"], binding["projection"])
            self.assertEqual(observation["public_projection_sha256"], binding["projection_sha"])
            self.assertEqual(
                _sha256(ROOT / observation["public_projection_file"]),
                observation["public_projection_sha256"],
            )
            self.assertTrue(observation["occurred"])
            self.assertEqual(observation["status"], "failed")
            self.assertEqual(observation["model_request_count"], 1)
            self.assertEqual(observation["network_calls"], 1)
            self.assertFalse(observation["candidate_result_created"])
            self.assertFalse(observation["inherited_into_candidate"])
            self.assertFalse(observation["inherited_into_pack"])
            self.assertFalse(observation["model_quality_claim_allowed"])

    def test_v1_chain_head_linkability_is_explicit_without_copying_private_values(self) -> None:
        overlay = _load(LINKABILITY_OVERLAY)
        self.assertEqual(
            set(overlay),
            {
                "schema_version",
                "overlay_id",
                "snapshot_date_local",
                "timezone",
                "applies_to",
                "locked_artifact_modified",
                "disclosures",
                "authorization_identifier_or_hash_published",
                "authorization_binding_hashes_published",
                "raw_artifact_published",
                "authorizes_retry_or_new_run",
                "authorizes_provider_registration",
                "authorizes_model_quality_claim",
                "authorizes_private_evaluation",
                "authorizes_non_synthetic_data",
            },
        )
        applies_to = overlay["applies_to"]
        self.assertEqual(
            applies_to,
            {
                "artifact_commitments_file": "docs/evidence/kimi-controlled-pilot-usage-failure-v1/artifact_commitments.json",
                "artifact_commitments_bytes": 1307,
                "artifact_commitments_sha256": "5159ed6ff5d143406fb3fddc06387a98927a107d3e313c1a4244e4df4ad50ba3",
            },
        )
        artifact_path = ROOT / applies_to["artifact_commitments_file"]
        self.assertEqual(artifact_path.stat().st_size, applies_to["artifact_commitments_bytes"])
        self.assertEqual(_sha256(artifact_path), applies_to["artifact_commitments_sha256"])
        self.assertFalse(overlay["locked_artifact_modified"])
        self.assertEqual(
            overlay["disclosures"],
            [
                {
                    "json_path": "event_chain.head_sha256",
                    "classification": "intentionally_linkable_opaque_commitment",
                    "links_exact_private_artifact_chain": True,
                    "is_authorization_identifier_or_binding": False,
                }
            ],
        )
        for field in (
            "authorization_identifier_or_hash_published",
            "authorization_binding_hashes_published",
            "raw_artifact_published",
            "authorizes_retry_or_new_run",
            "authorizes_provider_registration",
            "authorizes_model_quality_claim",
            "authorizes_private_evaluation",
            "authorizes_non_synthetic_data",
        ):
            self.assertFalse(overlay[field])
        self.assertNotIn(
            "559eab399fcb795dbb5c90495efe39ec90ff1a067df400f8cf2003b280e51f59",
            LINKABILITY_OVERLAY.read_text(encoding="utf-8"),
        )

    def test_live_evidence_directories_remain_byte_locked(self) -> None:
        expected = {
            "docs/evidence/kimi-controlled-pilot-usage-failure-v1": {
                "README.md": "9297dbf27385078515dd3981d068fda32b827bfa46a10e73844f28165b447f03",
                "artifact_commitments.json": "5159ed6ff5d143406fb3fddc06387a98927a107d3e313c1a4244e4df4ad50ba3",
                "public_receipt_projection.json": "156ecbdc73206663aa9c87aa874ce7ace93133dcbba18fb6c6a9431237dad1d1",
                "public_source_commitments.json": "2642583ef8a5c80c3281d808338714b37f389e4a0202a43f4fb2cef967798bc3",
            },
            "docs/evidence/kimi-controlled-pilot-v2-response-failure-v1": {
                "README.md": "da0ed36a09fafcff4eb4129350a8cb11a069972ff95b75342918dd1d7abd741b",
                "artifact_commitments.json": "9c8b3180bfdac1268a6e1811f47f1e59c5e1fdbce67c0c3fc1db7aa8feaec219",
                "public_receipt_projection.json": "694f871d9a66673f5357330240c2ee63f09c3aff3e50089117cae168cc2bc680",
                "public_source_commitments.json": "51e4e21de458a5e309a277d5ee7dd6c74df6cdc6248ab4d08d470d95cb9423af",
            },
        }
        for directory, files in expected.items():
            root = ROOT / directory
            self.assertEqual({path.name for path in root.iterdir()}, set(files))
            for name, digest in files.items():
                self.assertEqual(_sha256(root / name), digest)


if __name__ == "__main__":
    unittest.main()
