from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pilot_staging.candidate import LockedCandidateExecutor, RegistryDatasetCatalog
from pilot_staging.domain import CampaignDrift, LOCKED_CANDIDATE_COMMITMENT


ROOT = Path(__file__).resolve().parents[3]
DATASET_IDS = (
    "palmer_penguins_v0_1_0",
    "uci_parkinsons_telemonitoring_189",
    "uci_heart_disease_cleveland_45",
)


def _registry(tmp_path: Path) -> Path:
    entries = []
    for dataset_id in DATASET_IDS:
        path = tmp_path / f"{dataset_id}.csv"
        payload = b"value\n1\n"
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        entries.append(
            {
                "dataset_id": dataset_id,
                "relative_path": path.name,
                "prepared_sha256": digest,
                "prepared_bytes": len(payload),
                "row_count": 1,
                "column_count": 1,
                "source_asset_sha256": digest,
                "preparation_version": "1.0",
                "privacy_class": "synthetic_test_fixture",
                "model_access": "aggregate_tools_only",
                "domain": "contract_test",
                "repeated_subjects": False,
                "analysis_boundaries": ["synthetic_test_only"],
                "transformations": ["deterministic_fixture"],
            }
        )
    registry = tmp_path / "logical_dataset_registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "registry_id": "pilot-contract-test-registry",
                "dataset_manifest_sha256": "a" * 64,
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return registry


def test_locked_candidate_preflight_is_offline_and_exact(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    catalog = RegistryDatasetCatalog(registry)
    assert catalog.dataset_ids() == set(DATASET_IDS)
    executor = LockedCandidateExecutor(
        project_root=ROOT,
        registry_path=registry,
        api_key="offline-contract-placeholder",
    )
    assert "api_key" not in repr(executor)


def test_candidate_preflight_rejects_commitment_or_model_drift(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(CampaignDrift):
        LockedCandidateExecutor(
            project_root=ROOT,
            registry_path=registry,
            api_key="offline-contract-placeholder",
            candidate_commitment_sha256="0" * 64,
        )
    with pytest.raises(CampaignDrift):
        LockedCandidateExecutor(
            project_root=ROOT,
            registry_path=registry,
            api_key="offline-contract-placeholder",
            model_id="another-model",
            candidate_commitment_sha256=LOCKED_CANDIDATE_COMMITMENT,
        )
