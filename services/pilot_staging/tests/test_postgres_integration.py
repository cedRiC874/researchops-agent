from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlparse

import pytest

from pilot_staging.application import (
    PilotApplication,
    PilotWorker,
    task_pack_commitment_sha256,
)
from pilot_staging.config import Settings
from pilot_staging.domain import CandidateResult, Feedback, LOCKED_CANDIDATE_COMMITMENT
from pilot_staging.memory import StaticDatasetCatalog
from pilot_staging.postgres import PostgresPilotStore
from pilot_staging.migrate import run_migrations
from sqlalchemy import text


DATABASE_URL = os.environ.get("PILOT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="PILOT_TEST_DATABASE_URL not set")
DATASETS = (
    "palmer_penguins_v0_1_0",
    "uci_parkinsons_telemonitoring_189",
    "uci_heart_disease_cleveland_45",
)
CONSENT = "# PostgreSQL test consent\nVisible terms."
HASHES = {
    "protocol_sha256": "1" * 64,
    "consent_sha256": hashlib.sha256(CONSENT.encode()).hexdigest(),
    "feedback_schema_sha256": "2" * 64,
    "dataset_manifest_sha256": "3" * 64,
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def tick(self, seconds: int = 1):
        self.now += timedelta(seconds=seconds)


class Executor:
    def execute(self, task):
        return CandidateResult(
            final_output="## English\nAggregate answer.\n\n## 中文\n聚合回答。",
            outcome="clarification_required" if task.clarification_expected else "completed",
            provider_latency_ms=900,
            model_call_count=1,
            model_requested_tool_call_count=1,
            backend_executed_tool_call_count=1,
        )


def payload():
    scenarios = (
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "approval_pause",
        "prompt_injection",
        "unauthorized_resource",
    )
    tasks = []
    for index, scenario in enumerate(scenarios, 1):
        dataset_id = DATASETS[(index - 1) % 3]
        context = {"dataset_id": dataset_id}
        if scenario == "approval_pause":
            context.update(bundle_id="bundle", release_name="release")
        tasks.append(
            {
                "task_id": f"PILOT-TASK-{index:03d}",
                "source_task_id": f"V2-PUB-{index:03d}",
                "dataset_id": dataset_id,
                "scenario": scenario,
                "prompt_en": f"Task {index}",
                "prompt_zh": f"任务 {index}",
                "context": context,
                "clarification_expected": scenario == "clarification_required",
            }
        )
    return {
        "title": "PostgreSQL integration",
        **HASHES,
        "deployment_git_sha": None,
        "deployment_image_digest": None,
        "candidate_commitment_sha256": LOCKED_CANDIDATE_COMMITMENT,
        "provider": {
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "transport_id": "openai_compatible_responses",
        },
        "target_participants": 3,
        "max_provider_runs": 18,
        "tasks": tasks,
    }


def consent():
    return {
        "adult_and_voluntary": True,
        "experimental_system_understood": True,
        "public_data_only": True,
        "provider_transfer_understood": True,
        "pseudonymous_recording_agreed": True,
        "withdrawal_understood": True,
        "external_researcher_eligible": True,
    }


def feedback(clarification: bool):
    value = {
        "understandable": True,
        "useful_for_next_step": True,
        "confidence": "high",
        "needs_expert_review": False,
        "obvious_problem": False,
        "missing_information": False,
        "safety_concern": False,
        "notes": "",
    }
    if clarification:
        value["clarification_useful"] = True
    return value


def test_real_postgres_lifecycle_and_consent_replay(tmp_path) -> None:
    parsed = urlparse(DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1))
    password = tmp_path / "database.txt"
    admin = tmp_path / "admin.txt"
    pepper = tmp_path / "pepper.txt"
    password.write_text(unquote(parsed.password or ""), encoding="utf-8")
    admin.write_text("a" * 24, encoding="utf-8")
    pepper.write_text("p" * 32, encoding="utf-8")
    settings = Settings(
        database_host=parsed.hostname or "127.0.0.1",
        database_port=parsed.port or 5432,
        database_name=parsed.path.lstrip("/"),
        database_user=unquote(parsed.username or ""),
        database_password_file=password,
        database_sslmode="disable",
        admin_token_file=admin,
        token_pepper_file=pepper,
        registry_path=tmp_path / "unused-registry.json",
        project_root=tmp_path,
    )
    run_migrations(settings)
    clock = Clock()
    store = PostgresPilotStore(DATABASE_URL)
    try:
        application = PilotApplication(
            store=store,
            dataset_catalog=StaticDatasetCatalog(DATASETS),
            token_pepper=b"p" * 32,
            consent_document=CONSENT,
            expected_commitments=HASHES,
            provider_execution_enabled=True,
            clock=clock,
        )
        worker = PilotWorker(
            store=store,
            executor=Executor(),
            worker_id="postgres-integration-worker",
            clock=clock,
        )
        campaign = application.create_campaign(payload())
        campaign_id = campaign["campaign_id"]
        application.freeze_campaign(campaign_id)
        invite = application.create_invite(campaign_id)
        auth = application.exchange_invite(invite["invite_token"])
        session = application.authenticate(
            auth["session_token"], csrf_token=auth["csrf_token"]
        )
        application.record_consent(session, consent())
        session = application.authenticate(
            auth["session_token"], csrf_token=auth["csrf_token"]
        )
        for index in range(6):
            attempt = application.state(session)["attempt"]
            application.reveal(session, attempt["attempt_id"])
            worker.process_one()
            revealed = application.reveal(session, attempt["attempt_id"])["attempt"]
            clock.tick(5)
            application.record_feedback(
                session,
                attempt["attempt_id"],
                feedback(revealed["clarification_feedback_required"]),
            )
        assert application.state(session)["status"] == "complete"
        application.complete_campaign(campaign_id)
        replay = application.record_consent(session, consent())
        assert replay["status"] == "completed"
        summary = application.summary(campaign_id)
        assert summary["execution_environment"] == "local"
        assert summary["supervised_pretest"] is False
        assert summary["cohort"]["completed_participant_count"] == 1
        assert summary["interactions"]["feedback_completed_count"] == 6
        assert summary["external_validation_claim_allowed"] is False
        assert "eligible_participant_count_below_minimum" in summary[
            "external_validation_claim_reason_codes"
        ]
        assert store.campaign_summary_data(campaign_id)["audit_chain_valid"] is True
        assert store.campaign_summary_data(campaign_id)["task_pack_integrity_valid"] is True

        git_sha = "a" * 40
        image_digest = "sha256:" + "b" * 64
        supervised_application = PilotApplication(
            store=store,
            dataset_catalog=StaticDatasetCatalog(DATASETS),
            token_pepper=b"p" * 32,
            consent_document=CONSENT,
            expected_commitments=HASHES,
            deployment_git_sha=git_sha,
            deployment_image_digest=image_digest,
            supervised_task_pack_sha256=task_pack_commitment_sha256(
                payload()["tasks"]
            ),
            retention_schedule_confirmed=True,
            environment="supervised",
            provider_execution_enabled=True,
            clock=clock,
        )
        supervised_payload = payload()
        supervised_payload["target_participants"] = 2
        supervised_payload["max_provider_runs"] = 12
        supervised_payload["deployment_git_sha"] = git_sha
        supervised_payload["deployment_image_digest"] = image_digest
        supervised_id = supervised_application.create_campaign(supervised_payload)[
            "campaign_id"
        ]
        supervised_application.freeze_campaign(supervised_id)
        supervised_invite = supervised_application.create_invite(supervised_id)
        supervised_auth = supervised_application.exchange_invite(
            supervised_invite["invite_token"]
        )
        supervised_session = supervised_application.authenticate(
            supervised_auth["session_token"],
            csrf_token=supervised_auth["csrf_token"],
        )
        supervised_application.record_consent(supervised_session, consent())
        supervised_session = supervised_application.authenticate(
            supervised_auth["session_token"],
            csrf_token=supervised_auth["csrf_token"],
        )
        supervised_first = supervised_application.state(supervised_session)["attempt"]
        supervised_skipped = supervised_application.skip_attempt(
            supervised_session, supervised_first["attempt_id"]
        )
        assert supervised_skipped["next"]["attempt"]["sequence"] == 2
        assert store.campaign_summary_data(supervised_id)["technical_failure_count"] == 0

        supervised_worker = PilotWorker(
            store=store,
            executor=Executor(),
            worker_id="postgres-supervised-worker",
            execution_environment="supervised",
            deployment_image_digest=image_digest,
            clock=clock,
        )
        second_invite = supervised_application.create_invite(supervised_id)
        second_auth = supervised_application.exchange_invite(second_invite["invite_token"])
        second_session = supervised_application.authenticate(
            second_auth["session_token"], csrf_token=second_auth["csrf_token"]
        )
        supervised_application.record_consent(second_session, consent())
        second_session = supervised_application.authenticate(
            second_auth["session_token"], csrf_token=second_auth["csrf_token"]
        )
        second_attempt = supervised_application.state(second_session)["attempt"]
        supervised_application.reveal(second_session, second_attempt["attempt_id"])
        supervised_worker.process_one()
        supervised_application.reveal(second_session, second_attempt["attempt_id"])
        supervised_application.withdraw(second_session, second_auth["session_token"])
        with pytest.raises(Exception):
            store.record_feedback(
                participant_id=second_session.participant_id,
                attempt_id=second_attempt["attempt_id"],
                feedback=Feedback(
                    understandable=True,
                    useful_for_next_step=True,
                    confidence="high",
                    needs_expert_review=False,
                    obvious_problem=False,
                    missing_information=False,
                    safety_concern=False,
                    clarification_useful=None,
                    notes="",
                ),
                human_review_seconds=1,
                now=clock(),
            )
        assert store.get_campaign(supervised_id).execution_environment == "supervised"
        reloaded_summary = application.summary(supervised_id)
        assert reloaded_summary["external_validation_claim_allowed"] is False
        assert "supervised_environment_not_claim_eligible" in reloaded_summary[
            "external_validation_claim_reason_codes"
        ]

        run_migrations(settings)
        run_migrations(settings)
        with store.engine.begin() as connection:
            connection.execute(
                text("UPDATE pilot_schema_migrations SET source_sha256=:digest WHERE version=1"),
                {"digest": "0" * 64},
            )
        with pytest.raises(RuntimeError, match="checksum drift"):
            run_migrations(settings)
    finally:
        store.close()
