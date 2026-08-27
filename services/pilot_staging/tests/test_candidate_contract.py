from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pilot_staging import composition
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


def test_active_v5_preflight_fails_closed_on_current_source_drift(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    catalog = RegistryDatasetCatalog(registry)
    assert catalog.dataset_ids() == set(DATASET_IDS)
    with (
        patch(
            "pilot_staging.candidate.get_provider",
            side_effect=AssertionError("Provider must not be constructed"),
        ),
        pytest.raises(CampaignDrift, match="未绑定当前 source"),
    ):
        LockedCandidateExecutor(
            project_root=ROOT,
            registry_path=registry,
            api_key="offline-contract-placeholder",
        )


def test_historical_validation_result_is_rejected_before_provider(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    historical = {
        "candidate_commitment_sha256": LOCKED_CANDIDATE_COMMITMENT,
        "historical_snapshot_only": True,
        "network_calls": 0,
    }
    with (
        patch(
            "pilot_staging.candidate.validate_public_regression_candidate",
            return_value=historical,
        ),
        patch(
            "pilot_staging.candidate.get_provider",
            side_effect=AssertionError("Provider must not be constructed"),
        ),
        pytest.raises(CampaignDrift, match="Historical candidate"),
    ):
        LockedCandidateExecutor(
            project_root=ROOT,
            registry_path=registry,
            api_key="offline-contract-placeholder",
        )


class _OfflineSettings:
    def __init__(self, registry_path: Path, *, provider_execution_enabled: bool) -> None:
        self.project_root = ROOT
        self.registry_path = registry_path
        self.provider_execution_enabled = provider_execution_enabled
        self.deployment_git_sha = None
        self.deployment_image_digest = None
        self.retention_schedule_confirmed = False
        self.retention_days = 90
        self.environment = "local"
        self.session_ttl_hours = 8
        self.worker_lease_seconds = 300
        self.candidate_commitment_sha256 = LOCKED_CANDIDATE_COMMITMENT
        self.secure_cookies = False

    def database_url(self) -> str:
        return "postgresql+psycopg://offline:offline@127.0.0.1:1/offline"

    def token_pepper(self) -> bytes:
        return b"p" * 32

    def admin_token(self) -> str:
        return "a" * 24

    def allowed_host_values(self) -> tuple[str, ...]:
        return ("127.0.0.1", "localhost", "testserver")


class _OfflineStore:
    def healthcheck(self) -> bool:
        return True

    def close(self) -> None:
        return None


def test_offline_api_startup_does_not_claim_candidate_execution_validity(
    tmp_path: Path,
) -> None:
    settings = _OfflineSettings(_registry(tmp_path), provider_execution_enabled=False)
    with (
        patch.object(composition, "Settings", return_value=settings),
        patch.object(composition, "PostgresPilotStore", return_value=_OfflineStore()),
        patch.object(
            composition,
            "validate_locked_candidate_files",
            side_effect=AssertionError("offline API must not authorize Candidate execution"),
        ),
    ):
        app = composition.create_app()
    assert app is not None


def test_online_api_startup_requires_current_source_candidate_validation(
    tmp_path: Path,
) -> None:
    settings = _OfflineSettings(_registry(tmp_path), provider_execution_enabled=True)
    with (
        patch.object(composition, "Settings", return_value=settings),
        patch.object(
            composition,
            "validate_locked_candidate_files",
            side_effect=CampaignDrift("current source is not locked"),
        ) as validate,
        pytest.raises(CampaignDrift, match="current source is not locked"),
    ):
        composition.create_app()
    validate.assert_called_once_with(ROOT)


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
