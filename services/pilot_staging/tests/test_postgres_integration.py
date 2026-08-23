from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
import psycopg

from pilot_staging.application import (
    PilotApplication,
    PilotWorker,
    task_pack_commitment_sha256,
)
from pilot_staging.config import Settings
from pilot_staging.domain import (
    CampaignNotAvailable,
    CandidateResult,
    Feedback,
    LOCKED_CANDIDATE_COMMITMENT,
)
from pilot_staging.memory import StaticDatasetCatalog
from pilot_staging.postgres import PostgresPilotStore
from pilot_staging.telemetry import execution_telemetry_sha256
from pilot_staging.migrate import run_migrations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


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


class FailureExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task):
        self.calls += 1
        return CandidateResult(
            final_output="",
            outcome="controlled_failure",
            provider_latency_ms=1250,
            model_call_count=1,
            model_requested_tool_call_count=0,
            backend_executed_tool_call_count=0,
            error_code="provider_output_incomplete",
            completion_failure_source="final_output_missing",
        )


class RaisingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task):
        self.calls += 1
        raise RuntimeError("SECRET_RAW_PROVIDER_BODY must not be persisted")


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
        migrations_path=Path(__file__).resolve().parents[1] / "migrations",
    )
    migration_v1_path = Path(__file__).resolve().parents[1] / "migrations" / "0001_pilot_staging.sql"
    migration_v1 = migration_v1_path.read_bytes()
    dsn = settings.database_url().replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            CREATE TABLE pilot_schema_migrations (
                version integer PRIMARY KEY,
                source_sha256 char(64) NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        with connection.transaction():
            connection.execute(migration_v1.decode("utf-8"))
            connection.execute(
                "INSERT INTO pilot_schema_migrations(version,source_sha256) VALUES (%s,%s)",
                (1, hashlib.sha256(migration_v1).hexdigest()),
            )
    run_migrations(settings)
    clock = Clock()
    store = PostgresPilotStore(DATABASE_URL)
    try:
        with store.engine.connect() as connection:
            assert connection.execute(
                text("SELECT version FROM pilot_schema_migrations ORDER BY version")
            ).scalars().all() == [1, 2, 3]
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

        failure_executor = FailureExecutor()
        failure_worker = PilotWorker(
            store=store,
            executor=failure_executor,
            worker_id="postgres-failure-telemetry-worker",
            clock=clock,
        )
        failure_campaign_id = application.create_campaign(payload())["campaign_id"]
        application.freeze_campaign(failure_campaign_id)
        failure_auth = application.exchange_invite(
            application.create_invite(failure_campaign_id)["invite_token"]
        )
        failure_session = application.authenticate(
            failure_auth["session_token"], csrf_token=failure_auth["csrf_token"]
        )
        application.record_consent(failure_session, consent())
        failure_session = application.authenticate(
            failure_auth["session_token"], csrf_token=failure_auth["csrf_token"]
        )
        failure_attempt = application.state(failure_session)["attempt"]
        application.reveal(failure_session, failure_attempt["attempt_id"])
        failed = failure_worker.process_one()
        assert failed is not None
        assert failed.error_code == "provider_output_incomplete"
        assert failed.safe_output is None
        assert failed.output_sha256 is None
        assert failed.provider_latency_ms == 1250
        assert failed.outcome == "controlled_failure"
        assert failed.model_call_count == 1
        assert failed.model_requested_tool_call_count == 0
        assert failed.backend_executed_tool_call_count == 0
        assert failed.completion_failure_source == "final_output_missing"
        with store.engine.connect() as connection:
            stored_failure = connection.execute(
                text(
                    """
                    SELECT status,started_at,error_code,safe_output,output_sha256,
                           provider_latency_ms,outcome,
                           model_call_count,model_requested_tool_call_count,
                           backend_executed_tool_call_count,completion_failure_source,
                           lease_owner,lease_expires_at
                    FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": failure_attempt["attempt_id"]},
            ).mappings().one()
            failure_event = connection.execute(
                text(
                    """
                    SELECT payload FROM pilot_events
                    WHERE campaign_id=:campaign AND event_type='attempt_failed'
                      AND payload->>'attempt_id'=:attempt
                    ORDER BY sequence DESC LIMIT 1
                    """
                ),
                {
                    "campaign": failure_campaign_id,
                    "attempt": failure_attempt["attempt_id"],
                },
            ).scalar_one()
        assert stored_failure["safe_output"] is None
        assert stored_failure["output_sha256"] is None
        assert stored_failure["provider_latency_ms"] == 1250
        assert stored_failure["outcome"] == "controlled_failure"
        assert stored_failure["model_call_count"] == 1
        assert stored_failure["model_requested_tool_call_count"] == 0
        assert stored_failure["backend_executed_tool_call_count"] == 0
        assert stored_failure["completion_failure_source"] == "final_output_missing"
        assert stored_failure["lease_owner"] is None
        assert stored_failure["lease_expires_at"] is None
        assert stored_failure["status"] == "failed"
        assert stored_failure["started_at"] is not None
        assert stored_failure["error_code"] == "provider_output_incomplete"
        assert failure_event["completion_failure_source"] == "final_output_missing"
        assert failure_event["execution_telemetry_digest_version"] == (
            "pilot-execution-telemetry-v2"
        )
        assert len(failure_event["execution_telemetry_sha256"]) == 64
        assert len(failure_event["execution_telemetry_v2_sha256"]) == 64
        for field in (
            "model_call_count",
            "model_requested_tool_call_count",
            "backend_executed_tool_call_count",
        ):
            with pytest.raises(IntegrityError) as constraint_error:
                with store.engine.begin() as connection:
                    connection.execute(
                        text(
                            f"""
                            UPDATE pilot_attempts SET {field}=-1
                            WHERE attempt_id=CAST(:id AS uuid)
                            """
                        ),
                        {"id": failure_attempt["attempt_id"]},
                    )
            assert constraint_error.value.orig.sqlstate == "23514"
        with store.engine.connect() as connection:
            validated_constraints = dict(
                connection.execute(
                    text(
                        """
                        SELECT conname,convalidated FROM pg_constraint
                        WHERE conname IN (
                          'pilot_attempt_model_call_count_nonnegative',
                          'pilot_attempt_requested_tool_call_count_nonnegative',
                          'pilot_attempt_backend_tool_call_count_nonnegative',
                          'pilot_attempt_completion_failure_source_allowlist',
                          'pilot_attempt_completion_failure_source_mapping'
                        )
                        """
                    )
                ).all()
            )
        assert validated_constraints == {
            "pilot_attempt_model_call_count_nonnegative": True,
            "pilot_attempt_requested_tool_call_count_nonnegative": True,
            "pilot_attempt_backend_tool_call_count_nonnegative": True,
            "pilot_attempt_completion_failure_source_allowlist": True,
            "pilot_attempt_completion_failure_source_mapping": True,
        }
        for invalid_source in ("raw_provider_reason", "response_not_completed"):
            with pytest.raises(IntegrityError) as source_constraint_error:
                with store.engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            UPDATE pilot_attempts SET completion_failure_source=:source
                            WHERE attempt_id=CAST(:id AS uuid)
                            """
                        ),
                        {"id": failure_attempt["attempt_id"], "source": invalid_source},
                    )
                assert source_constraint_error.value.orig.sqlstate == "23514"
        with pytest.raises(IntegrityError) as outcome_constraint_error:
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE pilot_attempts SET outcome='completed'
                        WHERE attempt_id=CAST(:id AS uuid)
                        """
                    ),
                    {"id": failure_attempt["attempt_id"]},
                )
        assert outcome_constraint_error.value.orig.sqlstate == "23514"
        failure_summary = application.summary(failure_campaign_id)
        telemetry = failure_summary["provider_execution_telemetry"]
        assert telemetry["worker_started_attempt_count"] == 1
        assert telemetry["technical_failure_attempt_count"] == 1
        assert telemetry["executor_model_call_count"]["observed_sum"] == 1
        assert telemetry["model_requested_tool_call_count"]["observed_sum"] == 0
        assert telemetry["backend_executed_tool_call_count"]["observed_sum"] == 0
        assert telemetry["telemetry_coverage_status"] == "complete"
        assert telemetry["append_only_event_binding_status"] == "valid"
        assert telemetry["completion_failure_sources"] == {
            "semantics_version": "pilot-completion-failure-source-v2",
            "applicable_attempt_count": 1,
            "observed_attempt_count": 1,
            "unknown_attempt_count": 0,
            "coverage_rate": 1.0,
            "coverage_status": "complete",
            "counts": [
                {
                    "completion_failure_source": "final_output_missing",
                    "attempt_count": 1,
                }
            ],
        }
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET model_call_count=2
                    WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": failure_attempt["attempt_id"]},
            )
        tampered_raw = store.campaign_summary_data(failure_campaign_id)
        assert tampered_raw["audit_chain_valid"] is True
        assert tampered_raw["telemetry_integrity_valid"] is False
        tampered_summary = application.summary(failure_campaign_id)
        assert tampered_summary["provider_execution_telemetry"][
            "append_only_event_binding_status"
        ] == "invalid"
        assert "artifact_integrity_invalid" in tampered_summary[
            "external_validation_claim_reason_codes"
        ]
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET model_call_count=1
                    WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": failure_attempt["attempt_id"]},
            )
        assert store.campaign_summary_data(failure_campaign_id)[
            "telemetry_integrity_valid"
        ] is True
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE pilot_attempts
                    SET completion_failure_source='response_output_item_incomplete'
                    WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": failure_attempt["attempt_id"]},
            )
        assert store.campaign_summary_data(failure_campaign_id)[
            "telemetry_integrity_valid"
        ] is False
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE pilot_attempts
                    SET completion_failure_source='final_output_missing'
                    WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": failure_attempt["attempt_id"]},
            )
        assert store.campaign_summary_data(failure_campaign_id)[
            "telemetry_integrity_valid"
        ] is True
        for field, tampered_value, restored_value in (
            ("started_at", None, stored_failure["started_at"]),
            ("status", "assigned", "failed"),
        ):
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        f"""
                        UPDATE pilot_attempts SET {field}=:value
                        WHERE attempt_id=CAST(:id AS uuid)
                        """
                    ),
                    {"id": failure_attempt["attempt_id"], "value": tampered_value},
                )
            assert store.campaign_summary_data(failure_campaign_id)[
                "telemetry_integrity_valid"
            ] is False
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        f"""
                        UPDATE pilot_attempts SET {field}=:value
                        WHERE attempt_id=CAST(:id AS uuid)
                        """
                    ),
                    {"id": failure_attempt["attempt_id"], "value": restored_value},
                )
            assert store.campaign_summary_data(failure_campaign_id)[
                "telemetry_integrity_valid"
            ] is True
        application.exclude_failed_attempt(
            failure_session, failure_attempt["attempt_id"]
        )
        with store.engine.connect() as connection:
            after_exclusion = connection.execute(
                text(
                    """
                    SELECT status,error_code,safe_output,output_sha256,
                           provider_latency_ms,outcome,model_call_count,
                           model_requested_tool_call_count,backend_executed_tool_call_count,
                           completion_failure_source
                    FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": failure_attempt["attempt_id"]},
            ).mappings().one()
        assert dict(after_exclusion) == {
            "status": "excluded",
            "error_code": "provider_output_incomplete",
            "safe_output": None,
            "output_sha256": None,
            "provider_latency_ms": 1250,
            "outcome": "controlled_failure",
            "model_call_count": 1,
            "model_requested_tool_call_count": 0,
            "backend_executed_tool_call_count": 0,
            "completion_failure_source": "final_output_missing",
        }
        assert failure_executor.calls == 1

        raising_executor = RaisingExecutor()
        raising_worker = PilotWorker(
            store=store,
            executor=raising_executor,
            worker_id="postgres-unknown-telemetry-worker",
            clock=clock,
        )
        unknown_attempt = application.state(failure_session)["attempt"]
        application.reveal(failure_session, unknown_attempt["attempt_id"])
        unknown_failure = raising_worker.process_one()
        assert unknown_failure is not None
        assert unknown_failure.error_code == "provider_failed"
        assert unknown_failure.provider_latency_ms is None
        assert unknown_failure.outcome is None
        assert unknown_failure.model_call_count is None
        assert unknown_failure.model_requested_tool_call_count is None
        assert unknown_failure.backend_executed_tool_call_count is None
        with store.engine.connect() as connection:
            unknown_stored = connection.execute(
                text(
                    """
                    SELECT safe_output,output_sha256,provider_latency_ms,outcome,
                           model_call_count,model_requested_tool_call_count,
                           backend_executed_tool_call_count,completion_failure_source
                    FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": unknown_attempt["attempt_id"]},
            ).mappings().one()
        assert all(value is None for value in unknown_stored.values())
        unknown_summary = application.summary(failure_campaign_id)[
            "provider_execution_telemetry"
        ]
        assert unknown_summary["worker_started_attempt_count"] == 2
        assert unknown_summary["technical_failure_attempt_count"] == 2
        assert unknown_summary["telemetry_coverage_status"] == "partial"
        assert unknown_summary["executor_model_call_count"] == {
            "observed_sum": 1,
            "observed_attempt_count": 1,
            "unknown_attempt_count": 1,
            "coverage_rate": 0.5,
        }
        assert unknown_summary["failure_reason_counts"] == [
            {"reason_code": "provider_failed", "attempt_count": 1},
            {"reason_code": "provider_output_incomplete", "attempt_count": 1},
        ]
        assert raising_executor.calls == 1
        with store.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM pilot_participants WHERE participant_id=:id"),
                {"id": failure_session.participant_id},
            )
        manually_deleted = store.campaign_summary_data(failure_campaign_id)
        assert manually_deleted["audit_chain_valid"] is True
        assert manually_deleted["telemetry_integrity_valid"] is False

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
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET error_code='provider_failed'
                    WHERE attempt_id=CAST(:id AS uuid)
                    """
                ),
                {"id": second_attempt["attempt_id"]},
            )
        purge_result = store.purge_expired_records(
            now=clock(),
            retention_due_by=clock(),
            withdrawal_before=clock(),
            invite_retention_before=clock() - timedelta(days=90),
        )
        assert purge_result["participant_records_deleted"] == 1
        with store.engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM pilot_participants WHERE participant_id=:id"
                ),
                {"id": second_session.participant_id},
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id AND event_type='attempt_retention_deleted'
                    """
                ),
                {"id": supervised_id},
            ).scalar_one() == 1
            assert connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='attempt_retention_deleted'
                      AND payload->>'pre_delete_binding_status'='invalid'
                    """
                ),
                {"id": supervised_id},
            ).scalar_one() == 1
        retained_summary = store.campaign_summary_data(supervised_id)
        assert retained_summary["audit_chain_valid"] is True
        assert retained_summary["telemetry_integrity_valid"] is False
        assert retained_summary["participant_projection_integrity_valid"] is True
        assert retained_summary["withdrawn_participant_count"] == 1
        with store.engine.connect() as connection:
            tombstones = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='attempt_retention_deleted'
                    """
                ),
                {"id": supervised_id},
            ).scalar_one()
        assert tombstones == 1
        with store.engine.connect() as connection:
            participant_tombstones = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='participant_retention_deleted'
                      AND payload->>'withdrawal_recorded'='true'
                    """
                ),
                {"id": supervised_id},
            ).scalar_one()
        assert participant_tombstones == 1

        replacement_invite = supervised_application.create_invite(supervised_id)
        with pytest.raises(CampaignNotAvailable):
            supervised_application.exchange_invite(replacement_invite["invite_token"])
        with store.engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id AND event_type='invite_redeemed'
                    """
                ),
                {"id": supervised_id},
            ).scalar_one() == 2
            assert connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_invites
                    WHERE campaign_id=:id AND used_at IS NULL
                    """
                ),
                {"id": supervised_id},
            ).scalar_one() == 1

        replacement_payload = payload()
        replacement_payload["target_participants"] = 2
        replacement_payload["max_provider_runs"] = 12
        replacement_payload["deployment_git_sha"] = git_sha
        replacement_payload["deployment_image_digest"] = image_digest
        replacement_campaign = supervised_application.create_campaign(
            replacement_payload
        )["campaign_id"]
        supervised_application.freeze_campaign(replacement_campaign)
        replacement_auth = supervised_application.exchange_invite(
            supervised_application.create_invite(replacement_campaign)[
                "invite_token"
            ]
        )
        assert replacement_auth["campaign"]["execution_environment"] == "supervised"

        no_attempt_campaign_id = application.create_campaign(payload())["campaign_id"]
        application.freeze_campaign(no_attempt_campaign_id)
        no_attempt_auth = application.exchange_invite(
            application.create_invite(no_attempt_campaign_id)["invite_token"]
        )
        no_attempt_session = application.authenticate(
            no_attempt_auth["session_token"], csrf_token=no_attempt_auth["csrf_token"]
        )
        application.record_consent(no_attempt_session, consent())
        no_attempt_session = application.authenticate(
            no_attempt_auth["session_token"], csrf_token=no_attempt_auth["csrf_token"]
        )
        application.withdraw(no_attempt_session, no_attempt_auth["session_token"])
        no_attempt_purge = store.purge_expired_records(
            now=clock(),
            retention_due_by=clock(),
            withdrawal_before=clock(),
            invite_retention_before=clock() - timedelta(days=90),
        )
        assert no_attempt_purge["participant_records_deleted"] == 1
        no_attempt_summary = store.campaign_summary_data(no_attempt_campaign_id)
        assert no_attempt_summary["withdrawn_participant_count"] == 1
        assert no_attempt_summary["telemetry_integrity_valid"] is True
        assert no_attempt_summary["participant_projection_integrity_valid"] is True
        with store.engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='attempt_retention_deleted'
                    """
                ),
                {"id": no_attempt_campaign_id},
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='participant_retention_deleted'
                      AND payload->>'withdrawal_recorded'='true'
                    """
                ),
                {"id": no_attempt_campaign_id},
            ).scalar_one() == 1

        valid_retention_campaign = application.create_campaign(payload())["campaign_id"]
        application.freeze_campaign(valid_retention_campaign)
        valid_retention_auth = application.exchange_invite(
            application.create_invite(valid_retention_campaign)["invite_token"]
        )
        valid_retention_session = application.authenticate(
            valid_retention_auth["session_token"],
            csrf_token=valid_retention_auth["csrf_token"],
        )
        application.record_consent(valid_retention_session, consent())
        valid_retention_session = application.authenticate(
            valid_retention_auth["session_token"],
            csrf_token=valid_retention_auth["csrf_token"],
        )
        valid_attempt = application.state(valid_retention_session)["attempt"]
        application.reveal(valid_retention_session, valid_attempt["attempt_id"])
        worker.process_one()
        application.withdraw(
            valid_retention_session, valid_retention_auth["session_token"]
        )
        valid_purge = store.purge_expired_records(
            now=clock(),
            retention_due_by=clock(),
            withdrawal_before=clock(),
            invite_retention_before=clock() - timedelta(days=90),
        )
        assert valid_purge["participant_records_deleted"] == 1
        valid_retention_summary = store.campaign_summary_data(
            valid_retention_campaign
        )
        assert valid_retention_summary["telemetry_integrity_valid"] is True
        assert valid_retention_summary["participant_projection_integrity_valid"] is True
        assert valid_retention_summary["withdrawn_participant_count"] == 1

        manual_no_attempt_campaign = application.create_campaign(payload())["campaign_id"]
        application.freeze_campaign(manual_no_attempt_campaign)
        manual_no_attempt_auth = application.exchange_invite(
            application.create_invite(manual_no_attempt_campaign)["invite_token"]
        )
        manual_no_attempt_session = application.authenticate(
            manual_no_attempt_auth["session_token"],
            csrf_token=manual_no_attempt_auth["csrf_token"],
        )
        application.record_consent(manual_no_attempt_session, consent())
        manual_no_attempt_session = application.authenticate(
            manual_no_attempt_auth["session_token"],
            csrf_token=manual_no_attempt_auth["csrf_token"],
        )
        application.withdraw(
            manual_no_attempt_session, manual_no_attempt_auth["session_token"]
        )
        with store.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM pilot_participants WHERE participant_id=:id"),
                {"id": manual_no_attempt_session.participant_id},
            )
        manual_projection = store.campaign_summary_data(manual_no_attempt_campaign)
        assert manual_projection["telemetry_integrity_valid"] is True
        assert manual_projection["participant_projection_integrity_valid"] is False
        assert manual_projection["withdrawn_participant_count"] == 0
        assert "artifact_integrity_invalid" in application.summary(
            manual_no_attempt_campaign
        )["external_validation_claim_reason_codes"]

        running_campaign = application.create_campaign(payload())["campaign_id"]
        application.freeze_campaign(running_campaign)
        running_auth = application.exchange_invite(
            application.create_invite(running_campaign)["invite_token"]
        )
        running_session = application.authenticate(
            running_auth["session_token"], csrf_token=running_auth["csrf_token"]
        )
        application.record_consent(running_session, consent())
        running_session = application.authenticate(
            running_auth["session_token"], csrf_token=running_auth["csrf_token"]
        )
        running_attempt = application.state(running_session)["attempt"]
        application.reveal(running_session, running_attempt["attempt_id"])
        claimed = store.claim_attempt(
            worker_id="retention-running-worker",
            lease_seconds=300,
            execution_environment="local",
            deployment_image_digest=None,
            now=clock(),
        )
        assert claimed is not None and claimed.status.value == "running"
        application.withdraw(running_session, running_auth["session_token"])
        running_purge = store.purge_expired_records(
            now=clock(),
            retention_due_by=clock(),
            withdrawal_before=clock(),
            invite_retention_before=clock() - timedelta(days=90),
        )
        assert running_purge["participant_records_deleted"] == 1
        running_summary = store.campaign_summary_data(running_campaign)
        assert running_summary["telemetry_integrity_valid"] is True
        assert running_summary["participant_projection_integrity_valid"] is True
        assert running_summary["withdrawn_participant_count"] == 1
        late_result = Executor().execute(claimed.task)
        with pytest.raises(Exception, match="attempt 不存在"):
            store.complete_attempt(
                attempt_id=claimed.attempt_id,
                worker_id="retention-running-worker",
                result=late_result,
                safe_output=late_result.final_output,
                output_sha256=hashlib.sha256(
                    late_result.final_output.encode("utf-8")
                ).hexdigest(),
                now=clock(),
            )
        with store.engine.connect() as connection:
            running_tombstone = connection.execute(
                text(
                    """
                    SELECT payload FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='attempt_retention_deleted'
                    """
                ),
                {"id": running_campaign},
            ).scalar_one()
        assert running_tombstone["worker_started"] is True
        assert running_tombstone["final_status"] == "running"
        assert running_tombstone["pre_delete_binding_status"] == "valid"
        assert running_tombstone["execution_telemetry_digest_version"] == (
            "pilot-execution-telemetry-v2"
        )
        assert len(running_tombstone["execution_telemetry_sha256"]) == 64
        assert len(running_tombstone["execution_telemetry_v2_sha256"]) == 64
        assert running_tombstone["completion_failure_source"] is None

        legacy_campaign = application.create_campaign(payload())["campaign_id"]
        application.freeze_campaign(legacy_campaign)
        legacy_auth = application.exchange_invite(
            application.create_invite(legacy_campaign)["invite_token"]
        )
        legacy_session = application.authenticate(
            legacy_auth["session_token"], csrf_token=legacy_auth["csrf_token"]
        )
        application.record_consent(legacy_session, consent())
        legacy_session = application.authenticate(
            legacy_auth["session_token"], csrf_token=legacy_auth["csrf_token"]
        )
        legacy_attempt = application.state(legacy_session)["attempt"]
        application.reveal(legacy_session, legacy_attempt["attempt_id"])
        legacy_claimed = store.claim_attempt(
            worker_id="legacy-v1-telemetry-worker",
            lease_seconds=300,
            execution_environment="local",
            deployment_image_digest=None,
            now=clock(),
        )
        assert legacy_claimed is not None
        with store.engine.begin() as connection:
            legacy_row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET
                        status='failed',provider_completed_at=:now,
                        provider_latency_ms=400,outcome='controlled_failure',
                        error_code='provider_output_incomplete',model_call_count=1,
                        model_requested_tool_call_count=0,
                        backend_executed_tool_call_count=0,
                        completion_failure_source=NULL,
                        lease_owner=NULL,lease_expires_at=NULL
                    WHERE attempt_id=CAST(:id AS uuid)
                    RETURNING *
                    """
                ),
                {"id": legacy_attempt["attempt_id"], "now": clock()},
            ).mappings().one()
            store._append_event(
                connection,
                campaign_id=legacy_campaign,
                event_type="attempt_failed",
                payload={
                    "attempt_id": legacy_attempt["attempt_id"],
                    "error_code": "provider_output_incomplete",
                    "execution_telemetry_sha256": execution_telemetry_sha256(
                        legacy_row
                    ),
                },
                now=clock(),
            )
        legacy_before_exclusion = store.campaign_summary_data(legacy_campaign)
        assert legacy_before_exclusion["telemetry_integrity_valid"] is True
        assert legacy_before_exclusion["provider_execution_telemetry"][
            "completion_failure_sources"
        ]["coverage_status"] == "partial"
        application.exclude_failed_attempt(
            legacy_session, legacy_attempt["attempt_id"]
        )
        application.withdraw(legacy_session, legacy_auth["session_token"])
        legacy_purge = store.purge_expired_records(
            now=clock(),
            retention_due_by=clock(),
            withdrawal_before=clock(),
            invite_retention_before=clock() - timedelta(days=90),
        )
        assert legacy_purge["participant_records_deleted"] == 1
        legacy_after_retention = store.campaign_summary_data(legacy_campaign)
        assert legacy_after_retention["telemetry_integrity_valid"] is True
        with store.engine.connect() as connection:
            legacy_tombstone = connection.execute(
                text(
                    """
                        SELECT payload FROM pilot_events
                        WHERE campaign_id=:id
                          AND event_type='attempt_retention_deleted'
                          AND payload->>'attempt_id'=:attempt
                        """
                    ),
                    {
                        "id": legacy_campaign,
                        "attempt": legacy_attempt["attempt_id"],
                    },
                ).scalar_one()
        assert legacy_tombstone["execution_telemetry_digest_version"] == (
            "pilot-execution-telemetry-v2"
        )
        assert legacy_tombstone["completion_failure_source"] is None
        assert legacy_tombstone["pre_delete_binding_status"] == "valid"

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
