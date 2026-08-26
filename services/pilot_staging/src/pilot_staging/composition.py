from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from .api import PilotContainer, create_app as create_http_app
from .application import PilotApplication, PilotWorker, task_pack_commitment_sha256
from .candidate import (
    LockedCandidateExecutor,
    RegistryDatasetCatalog,
    validate_locked_candidate_files,
)
from .config import Settings
from .postgres import PostgresPilotStore


def create_app() -> FastAPI:
    settings = Settings()
    validate_locked_candidate_files(settings.project_root)
    store = PostgresPilotStore(settings.database_url())
    catalog = RegistryDatasetCatalog(settings.registry_path)
    consent_path = (
        settings.project_root
        / "services"
        / "pilot_staging"
        / "content"
        / "consent.zh-CN.md"
    )
    consent_document = consent_path.read_text(encoding="utf-8")
    expected_commitments = {
        "protocol_sha256": _sha256_file(
            settings.project_root / "docs" / "EXTERNAL_RESEARCHER_PILOT_PROTOCOL.md"
        ),
        "consent_sha256": hashlib.sha256(consent_document.encode("utf-8")).hexdigest(),
        "feedback_schema_sha256": _sha256_file(
            settings.project_root
            / "services"
            / "pilot_staging"
            / "contracts"
            / "task_feedback.schema.json"
        ),
        "dataset_manifest_sha256": _sha256_file(
            settings.project_root / "evals" / "v2" / "external_datasets.json"
        ),
    }
    supervised_pack = json.loads(
        (
            settings.project_root
            / "services"
            / "pilot_staging"
            / "content"
            / "pilot_pack.supervised_v6.json"
        ).read_text(encoding="utf-8")
    )
    supervised_task_pack_sha256 = task_pack_commitment_sha256(
        supervised_pack["tasks"]
    )
    application = PilotApplication(
        store=store,
        dataset_catalog=catalog,
        token_pepper=settings.token_pepper(),
        consent_document=consent_document,
        expected_commitments=expected_commitments,
        deployment_git_sha=settings.deployment_git_sha,
        deployment_image_digest=settings.deployment_image_digest,
        supervised_task_pack_sha256=supervised_task_pack_sha256,
        retention_schedule_confirmed=settings.retention_schedule_confirmed,
        retention_days=settings.retention_days,
        environment=settings.environment,
        session_ttl_hours=settings.session_ttl_hours,
        provider_execution_enabled=settings.provider_execution_enabled,
    )
    ready_checks = [store.healthcheck]
    if settings.provider_execution_enabled:
        ready_checks.append(
            lambda: store.worker_ready(
                candidate_commitment_sha256=settings.candidate_commitment_sha256,
                execution_environment=settings.environment,
                deployment_image_digest=settings.deployment_image_digest,
                now=datetime.now(UTC),
                max_age_seconds=settings.worker_lease_seconds + 60,
            )
        )
    return create_http_app(
        PilotContainer(
            application=application,
            admin_token=settings.admin_token(),
            allowed_hosts=settings.allowed_host_values(),
            secure_cookies=settings.secure_cookies,
            ready_checks=tuple(ready_checks),
            close=store.close,
        )
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_worker(settings: Settings, worker_id: str) -> tuple[PilotWorker, PostgresPilotStore]:
    if not settings.provider_execution_enabled:
        raise RuntimeError("Pilot online kill switch 未开启；worker 拒绝启动。")
    store = PostgresPilotStore(settings.database_url())
    executor = LockedCandidateExecutor(
        project_root=settings.project_root,
        registry_path=settings.registry_path,
        api_key=settings.provider_api_key(),
        provider_id=settings.provider_id,
        model_id=settings.model_id,
        candidate_commitment_sha256=settings.candidate_commitment_sha256,
    )
    return (
        PilotWorker(
            store=store,
            executor=executor,
            worker_id=worker_id,
            execution_environment=settings.environment,
            deployment_image_digest=settings.deployment_image_digest,
            lease_seconds=settings.worker_lease_seconds,
        ),
        store,
    )
