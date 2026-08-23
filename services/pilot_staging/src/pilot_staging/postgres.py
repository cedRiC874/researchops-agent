from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .domain import (
    Attempt,
    AttemptStatus,
    Campaign,
    CampaignNotAvailable,
    CampaignStatus,
    CandidateResult,
    ExecutionTelemetry,
    Feedback,
    InvalidTransition,
    InviteInvalid,
    ParticipantSession,
    ParticipantStatus,
    PilotTask,
    ProviderBudgetExhausted,
)
from .telemetry import execution_telemetry_sha256, summarize_provider_execution_telemetry


class PostgresPilotStore:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )

    def close(self) -> None:
        self.engine.dispose()

    def healthcheck(self) -> bool:
        try:
            with self.engine.connect() as connection:
                return connection.execute(text("SELECT 1")).scalar_one() == 1
        except Exception:
            return False

    def create_campaign(
        self, campaign: Campaign, tasks: Sequence[PilotTask], *, now: datetime
    ) -> Campaign:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO pilot_campaigns (
                        campaign_id,title,status,execution_environment,
                        candidate_commitment_sha256,
                        protocol_sha256,consent_sha256,task_pack_sha256,
                        feedback_schema_sha256,dataset_manifest_sha256,
                        deployment_git_sha,deployment_image_digest,
                        provider_id,model_id,transport_id,
                        max_provider_runs,target_participants,created_at,frozen_at
                    ) VALUES (
                        :campaign_id,:title,:status,:execution_environment,
                        :candidate,:protocol,:consent,
                        :task_pack,:feedback_schema,:dataset_manifest,:deployment_git,
                        :deployment_image,:provider_id,:model_id,
                        :transport_id,:max_runs,:target,:created_at,:frozen_at
                    )
                    """
                ),
                {
                    "campaign_id": campaign.campaign_id,
                    "title": campaign.title,
                    "status": campaign.status.value,
                    "execution_environment": campaign.execution_environment,
                    "candidate": campaign.candidate_commitment_sha256,
                    "protocol": campaign.protocol_sha256,
                    "consent": campaign.consent_sha256,
                    "task_pack": campaign.task_pack_sha256,
                    "feedback_schema": campaign.feedback_schema_sha256,
                    "dataset_manifest": campaign.dataset_manifest_sha256,
                    "deployment_git": campaign.deployment_git_sha,
                    "deployment_image": campaign.deployment_image_digest,
                    "provider_id": campaign.provider_id,
                    "model_id": campaign.model_id,
                    "transport_id": campaign.transport_id,
                    "max_runs": campaign.max_provider_runs,
                    "target": campaign.target_participants,
                    "created_at": campaign.created_at,
                    "frozen_at": campaign.frozen_at,
                },
            )
            for task in tasks:
                connection.execute(
                    text(
                        """
                        INSERT INTO pilot_tasks (
                            campaign_id,task_id,sequence,source_task_id,dataset_id,
                            scenario,prompt_en,prompt_zh,context,clarification_expected
                        ) VALUES (
                            :campaign_id,:task_id,:sequence,:source_task_id,:dataset_id,
                            :scenario,:prompt_en,:prompt_zh,CAST(:context AS jsonb),:clarification
                        )
                        """
                    ),
                    {
                        "campaign_id": campaign.campaign_id,
                        "task_id": task.task_id,
                        "sequence": task.sequence,
                        "source_task_id": task.source_task_id,
                        "dataset_id": task.dataset_id,
                        "scenario": task.scenario,
                        "prompt_en": task.prompt_en,
                        "prompt_zh": task.prompt_zh,
                        "context": _canonical_json(task.context),
                        "clarification": task.clarification_expected,
                    },
                )
            self._append_event(
                connection,
                campaign_id=campaign.campaign_id,
                event_type="campaign_created",
                payload={
                    "task_pack_sha256": campaign.task_pack_sha256,
                    "execution_environment": campaign.execution_environment,
                },
                now=now,
            )
        return campaign

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM pilot_campaigns WHERE campaign_id=:id"),
                {"id": campaign_id},
            ).mappings().first()
        return _campaign(row) if row is not None else None

    def freeze_campaign(self, campaign_id: str, *, now: datetime) -> Campaign:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_campaigns
                    SET status='frozen', frozen_at=COALESCE(frozen_at,:now)
                    WHERE campaign_id=:id AND status IN ('draft','frozen')
                    RETURNING *
                    """
                ),
                {"id": campaign_id, "now": now},
            ).mappings().first()
            if row is None:
                raise InvalidTransition("只有 draft campaign 可以 freeze。")
            self._append_event(
                connection,
                campaign_id=campaign_id,
                event_type="campaign_frozen",
                payload={"candidate_commitment_sha256": row["candidate_commitment_sha256"]},
                now=now,
            )
        return _campaign(row)

    def create_invite(
        self,
        *,
        invite_id: str,
        campaign_id: str,
        token_digest: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            status_value = connection.execute(
                text("SELECT status FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"),
                {"id": campaign_id},
            ).scalar_one_or_none()
            if status_value not in {"frozen", "running"}:
                raise CampaignNotAvailable("campaign 当前不可邀请。")
            connection.execute(
                text(
                    """
                    INSERT INTO pilot_invites
                    (invite_id,campaign_id,token_digest,expires_at,created_at)
                    VALUES (CAST(:invite_id AS uuid),:campaign_id,:digest,:expires,:now)
                    """
                ),
                {
                    "invite_id": invite_id,
                    "campaign_id": campaign_id,
                    "digest": token_digest,
                    "expires": expires_at,
                    "now": now,
                },
            )
            self._append_event(
                connection,
                campaign_id=campaign_id,
                event_type="invite_created",
                payload={"invite_id": invite_id},
                now=now,
            )

    def complete_campaign(self, campaign_id: str, *, now: datetime) -> Campaign:
        with self.engine.begin() as connection:
            campaign = connection.execute(
                text("SELECT * FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"),
                {"id": campaign_id},
            ).mappings().first()
            if campaign is None:
                raise InvalidTransition("campaign 不存在。")
            if campaign["status"] == "complete":
                return _campaign(campaign)
            unresolved = connection.execute(
                text("SELECT count(*) FROM pilot_incidents WHERE campaign_id=:id AND status='unresolved'"),
                {"id": campaign_id},
            ).scalar_one()
            active = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_participants
                    WHERE campaign_id=:id AND consented_at IS NOT NULL
                      AND status NOT IN ('completed','withdrawn')
                    """
                ),
                {"id": campaign_id},
            ).scalar_one()
            cohort = connection.execute(
                text("SELECT count(*) FROM pilot_participants WHERE campaign_id=:id"),
                {"id": campaign_id},
            ).scalar_one()
            if unresolved or active or cohort == 0:
                raise InvalidTransition("campaign 仍有未解决事件或未完成 participant。")
            row = connection.execute(
                text("UPDATE pilot_campaigns SET status='complete' WHERE campaign_id=:id RETURNING *"),
                {"id": campaign_id},
            ).mappings().one()
            self._append_event(
                connection,
                campaign_id=campaign_id,
                event_type="campaign_completed",
                payload={},
                now=now,
            )
        return _campaign(row)

    def resolve_incident(
        self, *, campaign_id: str, incident_id: str, resolution: str, now: datetime
    ) -> None:
        with self.engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE pilot_incidents SET status=:resolution,resolved_at=:now
                    WHERE campaign_id=:campaign AND incident_id=CAST(:incident AS uuid)
                      AND status='unresolved'
                    RETURNING incident_type
                    """
                ),
                {
                    "resolution": resolution,
                    "now": now,
                    "campaign": campaign_id,
                    "incident": incident_id,
                },
            ).mappings().first()
            if updated is None:
                raise InvalidTransition("incident 不存在或已处理。")
            unresolved = connection.execute(
                text("SELECT count(*) FROM pilot_incidents WHERE campaign_id=:id AND status='unresolved'"),
                {"id": campaign_id},
            ).scalar_one()
            if unresolved == 0:
                connection.execute(
                    text("UPDATE pilot_campaigns SET status='running' WHERE campaign_id=:id AND status='paused'"),
                    {"id": campaign_id},
                )
            self._append_event(
                connection,
                campaign_id=campaign_id,
                event_type="incident_resolved",
                payload={"incident_id": incident_id, "resolution": resolution},
                now=now,
            )

    def exchange_invite(
        self,
        *,
        token_digest: str,
        participant_id: str,
        session_instance_id: str,
        session_digest: str,
        csrf_digest: str,
        session_expires_at: datetime,
        participant_delete_by: datetime,
        expected_execution_environment: str,
        expected_deployment_git_sha: str | None,
        expected_deployment_image_digest: str | None,
        expected_candidate_commitment_sha256: str,
        now: datetime,
    ) -> ParticipantSession:
        with self.engine.begin() as connection:
            invite = connection.execute(
                text(
                    """
                    SELECT * FROM pilot_invites
                    WHERE token_digest=:digest FOR UPDATE
                    """
                ),
                {"digest": token_digest},
            ).mappings().first()
            if invite is None or invite["used_at"] is not None or invite["expires_at"] <= now:
                raise InviteInvalid("邀请无效、已使用或已过期。")
            campaign = connection.execute(
                text("SELECT * FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"),
                {"id": invite["campaign_id"]},
            ).mappings().one()
            if campaign["execution_environment"] != expected_execution_environment:
                raise CampaignNotAvailable("campaign 与当前 execution environment 不匹配。")
            if (
                campaign["deployment_git_sha"] != expected_deployment_git_sha
                or campaign["deployment_image_digest"]
                != expected_deployment_image_digest
                or campaign["candidate_commitment_sha256"]
                != expected_candidate_commitment_sha256
            ):
                raise CampaignNotAvailable("campaign 与当前部署 identity 不匹配。")
            if campaign["status"] not in {"frozen", "running"}:
                raise CampaignNotAvailable("campaign 当前不可加入。")
            redeemed_participant_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id AND event_type='invite_redeemed'
                    """
                ),
                {"id": campaign["campaign_id"]},
            ).scalar_one()
            if redeemed_participant_count >= campaign["target_participants"]:
                raise CampaignNotAvailable("campaign 参与者名额已满。")
            connection.execute(
                text("UPDATE pilot_invites SET used_at=:now WHERE invite_id=:id"),
                {"now": now, "id": invite["invite_id"]},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO pilot_participants
                    (participant_id,campaign_id,status,session_instance_id,delete_by,created_at)
                    VALUES (:participant_id,:campaign_id,'invited',:instance_id,:delete_by,:now)
                    """
                ),
                {
                    "participant_id": participant_id,
                    "campaign_id": campaign["campaign_id"],
                    "instance_id": session_instance_id,
                    "delete_by": participant_delete_by,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO pilot_web_sessions
                    (session_digest,participant_id,csrf_digest,expires_at,created_at)
                    VALUES (:session_digest,:participant_id,:csrf_digest,:expires_at,:now)
                    """
                ),
                {
                    "session_digest": session_digest,
                    "participant_id": participant_id,
                    "csrf_digest": csrf_digest,
                    "expires_at": session_expires_at,
                    "now": now,
                },
            )
            self._append_event(
                connection,
                campaign_id=campaign["campaign_id"],
                event_type="invite_redeemed",
                payload={"invite_id": str(invite["invite_id"])},
                now=now,
            )
        return ParticipantSession(
            participant_id=participant_id,
            campaign_id=campaign["campaign_id"],
            participant_status=ParticipantStatus.INVITED,
            session_instance_id=session_instance_id,
            session_expires_at=session_expires_at,
            csrf_digest=csrf_digest,
            consented_at=None,
            withdrawn_at=None,
        )

    def get_session(
        self, *, session_digest: str, now: datetime
    ) -> ParticipantSession | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT p.*,s.csrf_digest,s.expires_at AS session_expires_at
                    FROM pilot_web_sessions s
                    JOIN pilot_participants p ON p.participant_id=s.participant_id
                    WHERE s.session_digest=:digest AND s.revoked_at IS NULL
                      AND s.expires_at>:now AND p.withdrawn_at IS NULL
                    """
                ),
                {"digest": session_digest, "now": now},
            ).mappings().first()
        return _participant_session(row) if row is not None else None

    def revoke_session(self, *, session_digest: str, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE pilot_web_sessions SET revoked_at=COALESCE(revoked_at,:now)
                    WHERE session_digest=:digest
                    """
                ),
                {"digest": session_digest, "now": now},
            )

    def record_consent(
        self,
        *,
        participant_id: str,
        consent_sha256: str,
        eligibility_confirmed: bool,
        now: datetime,
    ) -> ParticipantSession:
        with self.engine.begin() as connection:
            participant = connection.execute(
                text("SELECT * FROM pilot_participants WHERE participant_id=:id FOR UPDATE"),
                {"id": participant_id},
            ).mappings().one()
            if participant["withdrawn_at"] is not None or not eligibility_confirmed:
                raise InvalidTransition("participant 不可 consent。")
            campaign_status = connection.execute(
                text("SELECT status FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"),
                {"id": participant["campaign_id"]},
            ).scalar_one()
            if participant["consented_at"] is not None:
                row = connection.execute(
                    text(
                        """
                        SELECT p.*,s.csrf_digest,s.expires_at AS session_expires_at
                        FROM pilot_participants p JOIN pilot_web_sessions s
                          ON s.participant_id=p.participant_id
                        WHERE p.participant_id=:id AND s.revoked_at IS NULL
                        ORDER BY s.created_at DESC LIMIT 1
                        """
                    ),
                    {"id": participant_id},
                ).mappings().one()
                return _participant_session(row)
            if campaign_status not in {"frozen", "running"}:
                raise InvalidTransition("campaign 当前不接受新 consent。")
            expected = connection.execute(
                text("SELECT consent_sha256 FROM pilot_campaigns WHERE campaign_id=:id"),
                {"id": participant["campaign_id"]},
            ).scalar_one()
            if expected != consent_sha256:
                raise InvalidTransition("Consent document hash 不匹配。")
            connection.execute(
                text(
                    """
                    UPDATE pilot_participants
                    SET status='consented', consent_document_sha256=:consent,
                        eligibility_confirmed=true,
                        consented_at=COALESCE(consented_at,:now)
                    WHERE participant_id=:id
                    """
                ),
                {"consent": consent_sha256, "now": now, "id": participant_id},
            )
            self._append_event(
                connection,
                campaign_id=participant["campaign_id"],
                event_type="consent_recorded",
                payload={"consent_sha256": consent_sha256},
                now=now,
            )
            row = connection.execute(
                text(
                    """
                    SELECT p.*,s.csrf_digest,s.expires_at AS session_expires_at
                    FROM pilot_participants p JOIN pilot_web_sessions s
                      ON s.participant_id=p.participant_id
                    WHERE p.participant_id=:id AND s.revoked_at IS NULL
                    ORDER BY s.created_at DESC LIMIT 1
                    """
                ),
                {"id": participant_id},
            ).mappings().one()
        return _participant_session(row)

    def current_attempt(self, *, participant_id: str, now: datetime) -> Attempt | None:
        with self.engine.begin() as connection:
            participant = connection.execute(
                text("SELECT * FROM pilot_participants WHERE participant_id=:id FOR UPDATE"),
                {"id": participant_id},
            ).mappings().one()
            if participant["consented_at"] is None or participant["withdrawn_at"] is not None:
                return None
            tasks = connection.execute(
                text("SELECT * FROM pilot_tasks WHERE campaign_id=:id ORDER BY sequence"),
                {"id": participant["campaign_id"]},
            ).mappings().all()
            attempts = {
                row["task_id"]: row
                for row in connection.execute(
                    text("SELECT * FROM pilot_attempts WHERE participant_id=:id"),
                    {"id": participant_id},
                ).mappings()
            }
            for task_row in tasks:
                attempt_row = attempts.get(task_row["task_id"])
                if attempt_row is None:
                    attempt_id = str(uuid.uuid4())
                    connection.execute(
                        text(
                            """
                            INSERT INTO pilot_attempts
                            (attempt_id,participant_id,campaign_id,task_id,status)
                            VALUES (CAST(:attempt_id AS uuid),:participant_id,:campaign_id,:task_id,'assigned')
                            """
                        ),
                        {
                            "attempt_id": attempt_id,
                            "participant_id": participant_id,
                            "campaign_id": participant["campaign_id"],
                            "task_id": task_row["task_id"],
                        },
                    )
                    attempt_row = connection.execute(
                        text("SELECT * FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid)"),
                        {"id": attempt_id},
                    ).mappings().one()
                    return _attempt(attempt_row, task_row)
                if attempt_row["status"] not in {"completed", "excluded"}:
                    return _attempt(attempt_row, task_row)
            return None

    def queue_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self.engine.begin() as connection:
            self._lock_active_participant(connection, participant_id)
            attempt = connection.execute(
                text("SELECT * FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid) FOR UPDATE"),
                {"id": attempt_id},
            ).mappings().first()
            if attempt is None or attempt["participant_id"] != participant_id or attempt["status"] != "assigned":
                raise InvalidTransition("attempt 不可 queue。")
            campaign = connection.execute(
                text("SELECT * FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"),
                {"id": attempt["campaign_id"]},
            ).mappings().one()
            if campaign["status"] not in {"frozen", "running"}:
                raise CampaignNotAvailable("campaign 已暂停或终止。")
            used = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_attempts
                    WHERE campaign_id=:id
                      AND (status='queued' OR started_at IS NOT NULL)
                    """
                ),
                {"id": attempt["campaign_id"]},
            ).scalar_one()
            if used >= campaign["max_provider_runs"]:
                raise ProviderBudgetExhausted("Provider run budget 已用尽。")
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET status='queued',queued_at=:now
                    WHERE attempt_id=CAST(:id AS uuid) AND status='assigned'
                    RETURNING *
                    """
                ),
                {"id": attempt_id, "now": now},
            ).mappings().one()
            connection.execute(
                text("UPDATE pilot_campaigns SET status='running' WHERE campaign_id=:id AND status='frozen'"),
                {"id": attempt["campaign_id"]},
            )
            connection.execute(
                text("UPDATE pilot_participants SET status='active' WHERE participant_id=:id AND status='consented'"),
                {"id": participant_id},
            )
            self._append_event(
                connection,
                campaign_id=attempt["campaign_id"],
                event_type="attempt_queued",
                payload={"attempt_id": attempt_id},
                now=now,
            )
            task = self._task_row(connection, attempt["campaign_id"], attempt["task_id"])
        return _attempt(row, task)

    def claim_attempt(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
    ) -> Attempt | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT a.* FROM pilot_attempts a
                    JOIN pilot_participants p ON p.participant_id=a.participant_id
                    JOIN pilot_campaigns c ON c.campaign_id=a.campaign_id
                    WHERE p.withdrawn_at IS NULL AND (
                        a.status='queued'
                        OR (a.status='running' AND a.lease_expires_at<=:now)
                    ) AND c.status='running'
                      AND c.execution_environment=:execution_environment
                      AND c.deployment_image_digest IS NOT DISTINCT FROM :deployment_image_digest
                    ORDER BY a.queued_at NULLS LAST
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """
                ),
                {
                    "now": now,
                    "execution_environment": execution_environment,
                    "deployment_image_digest": deployment_image_digest,
                },
            ).mappings().first()
            if row is None:
                return None
            if row["status"] == "running":
                expired = connection.execute(
                    text(
                        """
                        UPDATE pilot_attempts
                        SET status='failed',provider_completed_at=:now,
                            error_code='provider_lease_expired',lease_owner=NULL,lease_expires_at=NULL
                        WHERE attempt_id=:id
                        RETURNING *
                        """
                    ),
                    {"now": now, "id": row["attempt_id"]},
                ).mappings().one()
                self._append_event(
                    connection,
                    campaign_id=row["campaign_id"],
                    event_type="attempt_failed",
                    payload={
                        "attempt_id": str(row["attempt_id"]),
                        "error_code": "provider_lease_expired",
                        "execution_telemetry_sha256": execution_telemetry_sha256(expired),
                    },
                    now=now,
                )
                return None
            claimed = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts
                    SET status='running',started_at=:now,lease_owner=:worker,
                        lease_expires_at=:expires
                    WHERE attempt_id=:id AND status='queued'
                    RETURNING *
                    """
                ),
                {
                    "now": now,
                    "worker": worker_id,
                    "expires": now + timedelta(seconds=lease_seconds),
                    "id": row["attempt_id"],
                },
            ).mappings().one()
            self._append_event(
                connection,
                campaign_id=claimed["campaign_id"],
                event_type="attempt_started",
                payload={"attempt_id": str(claimed["attempt_id"])},
                now=now,
            )
            task = self._task_row(connection, row["campaign_id"], row["task_id"])
        return _attempt(claimed, task)

    def complete_attempt(
        self,
        *,
        attempt_id: str,
        worker_id: str,
        result: CandidateResult,
        safe_output: str,
        output_sha256: str,
        now: datetime,
    ) -> Attempt:
        with self.engine.begin() as connection:
            owner = connection.execute(
                text(
                    "SELECT participant_id FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid)"
                ),
                {"id": attempt_id},
            ).mappings().first()
            if owner is None:
                raise InvalidTransition("attempt 不存在。")
            participant = connection.execute(
                text(
                    "SELECT status,withdrawn_at FROM pilot_participants WHERE participant_id=:id FOR UPDATE"
                ),
                {"id": owner["participant_id"]},
            ).mappings().one()
            if participant["withdrawn_at"] is not None or participant["status"] == "withdrawn":
                row = connection.execute(
                    text(
                        """
                        UPDATE pilot_attempts SET status='failed',provider_completed_at=:now,
                            safe_output=NULL,output_sha256=NULL,error_code='participant_withdrew',
                            provider_latency_ms=NULL,outcome=NULL,
                            model_call_count=NULL,model_requested_tool_call_count=NULL,
                            backend_executed_tool_call_count=NULL,
                            lease_owner=NULL,lease_expires_at=NULL
                        WHERE attempt_id=CAST(:id AS uuid) AND status='running'
                          AND lease_owner=:worker
                        RETURNING *
                        """
                    ),
                    {"id": attempt_id, "worker": worker_id, "now": now},
                ).mappings().first()
                if row is None:
                    raise InvalidTransition("attempt lease 已失效。")
                self._append_event(
                    connection,
                    campaign_id=row["campaign_id"],
                    event_type="attempt_failed",
                    payload={
                        "attempt_id": attempt_id,
                        "error_code": "participant_withdrew",
                        "execution_telemetry_sha256": execution_telemetry_sha256(row),
                    },
                    now=now,
                )
                task = self._task_row(connection, row["campaign_id"], row["task_id"])
                return _attempt(row, task)
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET
                        status='succeeded',provider_completed_at=:now,
                        safe_output=:output,output_sha256=:output_sha,
                        provider_latency_ms=:latency,outcome=:outcome,error_code=NULL,
                        model_call_count=:model_calls,
                        model_requested_tool_call_count=:requested_calls,
                        backend_executed_tool_call_count=:backend_calls,
                        lease_owner=NULL,lease_expires_at=NULL
                    WHERE attempt_id=CAST(:id AS uuid) AND status='running'
                      AND lease_owner=:worker AND lease_expires_at>:now
                    RETURNING *
                    """
                ),
                {
                    "id": attempt_id,
                    "worker": worker_id,
                    "now": now,
                    "output": safe_output,
                    "output_sha": output_sha256,
                    "latency": result.provider_latency_ms,
                    "outcome": result.outcome,
                    "model_calls": result.model_call_count,
                    "requested_calls": result.model_requested_tool_call_count,
                    "backend_calls": result.backend_executed_tool_call_count,
                },
            ).mappings().first()
            if row is None:
                raise InvalidTransition("attempt lease 已失效。")
            self._append_event(
                connection,
                campaign_id=row["campaign_id"],
                event_type="attempt_succeeded",
                payload={
                    "attempt_id": attempt_id,
                    "output_sha256": output_sha256,
                    "execution_telemetry_sha256": execution_telemetry_sha256(row),
                },
                now=now,
            )
            task = self._task_row(connection, row["campaign_id"], row["task_id"])
        return _attempt(row, task)

    def fail_attempt(
        self,
        *,
        attempt_id: str,
        worker_id: str,
        error_code: str,
        withheld: bool,
        telemetry: ExecutionTelemetry | None,
        now: datetime,
    ) -> Attempt:
        target = "withheld" if withheld else "failed"
        with self.engine.begin() as connection:
            owner = connection.execute(
                text(
                    "SELECT participant_id FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid)"
                ),
                {"id": attempt_id},
            ).mappings().first()
            if owner is None:
                raise InvalidTransition("attempt 不存在。")
            participant = connection.execute(
                text(
                    "SELECT status,withdrawn_at FROM pilot_participants WHERE participant_id=:id FOR UPDATE"
                ),
                {"id": owner["participant_id"]},
            ).mappings().one()
            participant_withdrew = (
                participant["withdrawn_at"] is not None
                or participant["status"] == "withdrawn"
            )
            if participant_withdrew:
                target = "failed"
                error_code = "participant_withdrew"
                withheld = False
                telemetry = None
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET status=:target,provider_completed_at=:now,
                        safe_output=NULL,output_sha256=NULL,
                        provider_latency_ms=:latency,outcome=:outcome,
                        error_code=:error,
                        model_call_count=:model_calls,
                        model_requested_tool_call_count=:requested_calls,
                        backend_executed_tool_call_count=:backend_calls,
                        lease_owner=NULL,lease_expires_at=NULL
                    WHERE attempt_id=CAST(:id AS uuid) AND status='running'
                      AND lease_owner=:worker AND lease_expires_at>:now
                    RETURNING *
                    """
                ),
                {
                    "target": target,
                    "now": now,
                    "error": error_code,
                    "latency": (
                        telemetry.provider_latency_ms if telemetry is not None else None
                    ),
                    "outcome": telemetry.outcome if telemetry is not None else None,
                    "model_calls": (
                        telemetry.model_call_count if telemetry is not None else None
                    ),
                    "requested_calls": (
                        telemetry.model_requested_tool_call_count
                        if telemetry is not None
                        else None
                    ),
                    "backend_calls": (
                        telemetry.backend_executed_tool_call_count
                        if telemetry is not None
                        else None
                    ),
                    "id": attempt_id,
                    "worker": worker_id,
                },
            ).mappings().first()
            if row is None:
                raise InvalidTransition("attempt lease 已失效。")
            if withheld:
                self._create_incident(
                    connection,
                    campaign_id=row["campaign_id"],
                    participant_id=row["participant_id"],
                    attempt_id=row["attempt_id"],
                    incident_type="secret_or_personal_data_exposure",
                    now=now,
                )
            self._append_event(
                connection,
                campaign_id=row["campaign_id"],
                event_type="attempt_withheld" if withheld else "attempt_failed",
                payload={
                    "attempt_id": attempt_id,
                    "error_code": error_code,
                    "execution_telemetry_sha256": execution_telemetry_sha256(row),
                },
                now=now,
            )
            task = self._task_row(connection, row["campaign_id"], row["task_id"])
        return _attempt(row, task)

    def reveal_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self.engine.begin() as connection:
            self._lock_active_participant(connection, participant_id)
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET revealed_at=COALESCE(revealed_at,:now)
                    WHERE attempt_id=CAST(:id AS uuid) AND participant_id=:participant
                      AND status IN ('succeeded','completed')
                    RETURNING *
                    """
                ),
                {"now": now, "id": attempt_id, "participant": participant_id},
            ).mappings().first()
            if row is None:
                raise InvalidTransition("attempt 不可 reveal。")
            task = self._task_row(connection, row["campaign_id"], row["task_id"])
        return _attempt(row, task)

    def record_feedback(
        self,
        *,
        participant_id: str,
        attempt_id: str,
        feedback: Feedback,
        human_review_seconds: int,
        now: datetime,
    ) -> Attempt:
        with self.engine.begin() as connection:
            participant = self._lock_active_participant(connection, participant_id)
            attempt = connection.execute(
                text("SELECT * FROM pilot_attempts WHERE attempt_id=CAST(:id AS uuid) FOR UPDATE"),
                {"id": attempt_id},
            ).mappings().first()
            if (
                attempt is None
                or attempt["participant_id"] != participant_id
                or attempt["status"] != "succeeded"
                or attempt["revealed_at"] is None
            ):
                raise InvalidTransition("feedback 不可记录。")
            connection.execute(
                text(
                    """
                    INSERT INTO pilot_feedback (
                        attempt_id,understandable,useful_for_next_step,confidence,
                        needs_expert_review,obvious_problem,missing_information,
                        safety_concern,clarification_useful,notes,human_review_seconds,
                        delete_by,created_at
                    ) VALUES (
                        CAST(:attempt_id AS uuid),:understandable,:useful,:confidence,
                        :expert,:obvious,:missing,:safety,:clarification,:notes,
                        :seconds,:delete_by,:now
                    )
                    """
                ),
                {
                    "attempt_id": attempt_id,
                    "understandable": feedback.understandable,
                    "useful": feedback.useful_for_next_step,
                    "confidence": feedback.confidence,
                    "expert": feedback.needs_expert_review,
                    "obvious": feedback.obvious_problem,
                    "missing": feedback.missing_information,
                    "safety": feedback.safety_concern,
                    "clarification": feedback.clarification_useful,
                    "notes": feedback.notes,
                    "seconds": human_review_seconds,
                    "delete_by": participant["delete_by"],
                    "now": now,
                },
            )
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET status='completed',completed_at=:now
                    WHERE attempt_id=CAST(:id AS uuid) RETURNING *
                    """
                ),
                {"now": now, "id": attempt_id},
            ).mappings().one()
            if feedback.safety_concern:
                self._create_incident(
                    connection,
                    campaign_id=attempt["campaign_id"],
                    participant_id=participant_id,
                    attempt_id=attempt["attempt_id"],
                    incident_type="user_reported_safety_concern",
                    now=now,
                )
            remaining = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_tasks t
                    LEFT JOIN pilot_attempts a
                      ON a.campaign_id=t.campaign_id AND a.task_id=t.task_id
                     AND a.participant_id=:participant
                    WHERE t.campaign_id=:campaign
                      AND (a.status IS NULL OR a.status NOT IN ('completed','excluded'))
                    """
                ),
                {"participant": participant_id, "campaign": attempt["campaign_id"]},
            ).scalar_one()
            if remaining == 0:
                connection.execute(
                    text("UPDATE pilot_participants SET status='completed' WHERE participant_id=:id AND withdrawn_at IS NULL AND status<>'withdrawn'"),
                    {"id": participant_id},
                )
            self._append_event(
                connection,
                campaign_id=attempt["campaign_id"],
                event_type="feedback_recorded",
                payload={"attempt_id": attempt_id, "safety_concern": feedback.safety_concern},
                now=now,
            )
            task = self._task_row(connection, attempt["campaign_id"], attempt["task_id"])
        return _attempt(row, task)

    def exclude_failed_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self.engine.begin() as connection:
            self._lock_active_participant(connection, participant_id)
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET status='excluded',completed_at=:now
                    WHERE attempt_id=CAST(:id AS uuid) AND participant_id=:participant
                      AND status IN ('failed','withheld')
                    RETURNING *
                    """
                ),
                {"id": attempt_id, "participant": participant_id, "now": now},
            ).mappings().first()
            if row is None:
                raise InvalidTransition("只有失败或被拦截的当前任务可以排除。")
            remaining = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_tasks t
                    LEFT JOIN pilot_attempts a
                      ON a.campaign_id=t.campaign_id AND a.task_id=t.task_id
                     AND a.participant_id=:participant
                    WHERE t.campaign_id=:campaign
                      AND (a.status IS NULL OR a.status NOT IN ('completed','excluded'))
                    """
                ),
                {"participant": participant_id, "campaign": row["campaign_id"]},
            ).scalar_one()
            if remaining == 0:
                connection.execute(
                    text("UPDATE pilot_participants SET status='completed' WHERE participant_id=:id AND withdrawn_at IS NULL AND status<>'withdrawn'"),
                    {"id": participant_id},
                )
            self._append_event(
                connection,
                campaign_id=row["campaign_id"],
                event_type="attempt_excluded",
                payload={
                    "attempt_id": attempt_id,
                    "error_code": row["error_code"],
                    "execution_telemetry_sha256": execution_telemetry_sha256(row),
                },
                now=now,
            )
            task = self._task_row(connection, row["campaign_id"], row["task_id"])
        return _attempt(row, task)

    def skip_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self.engine.begin() as connection:
            self._lock_active_participant(connection, participant_id)
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_attempts
                    SET status='excluded',completed_at=:now,
                        error_code='participant_skipped'
                    WHERE attempt_id=CAST(:id AS uuid) AND participant_id=:participant
                      AND status IN ('assigned','succeeded')
                    RETURNING *
                    """
                ),
                {"id": attempt_id, "participant": participant_id, "now": now},
            ).mappings().first()
            if row is None:
                raise InvalidTransition("当前任务不可跳过。")
            remaining = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_tasks t
                    LEFT JOIN pilot_attempts a
                      ON a.campaign_id=t.campaign_id AND a.task_id=t.task_id
                     AND a.participant_id=:participant
                    WHERE t.campaign_id=:campaign
                      AND (a.status IS NULL OR a.status NOT IN ('completed','excluded'))
                    """
                ),
                {"participant": participant_id, "campaign": row["campaign_id"]},
            ).scalar_one()
            if remaining == 0:
                connection.execute(
                    text("UPDATE pilot_participants SET status='completed' WHERE participant_id=:id AND withdrawn_at IS NULL AND status<>'withdrawn'"),
                    {"id": participant_id},
                )
            self._append_event(
                connection,
                campaign_id=row["campaign_id"],
                event_type="attempt_skipped",
                payload={
                    "attempt_id": attempt_id,
                    "error_code": "participant_skipped",
                    "execution_telemetry_sha256": execution_telemetry_sha256(row),
                },
                now=now,
            )
            task = self._task_row(connection, row["campaign_id"], row["task_id"])
        return _attempt(row, task)

    def withdraw(self, *, participant_id: str, now: datetime) -> None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    UPDATE pilot_participants SET status='withdrawn',withdrawn_at=COALESCE(withdrawn_at,:now)
                    WHERE participant_id=:id RETURNING campaign_id
                    """
                ),
                {"id": participant_id, "now": now},
            ).mappings().one()
            connection.execute(
                text("UPDATE pilot_web_sessions SET revoked_at=COALESCE(revoked_at,:now) WHERE participant_id=:id"),
                {"id": participant_id, "now": now},
            )
            connection.execute(
                text(
                    """
                    UPDATE pilot_attempts SET status='failed',error_code='participant_withdrew'
                    WHERE participant_id=:id AND status IN ('assigned','queued')
                    """
                ),
                {"id": participant_id},
            )
            self._append_event(
                connection,
                campaign_id=row["campaign_id"],
                event_type="participant_withdrew",
                payload={"withdrawal_recorded": True},
                now=now,
            )

    def purge_expired_records(
        self,
        *,
        now: datetime,
        retention_due_by: datetime,
        withdrawal_before: datetime,
        invite_retention_before: datetime,
    ) -> Mapping[str, int | bool]:
        with self.engine.begin() as connection:
            due_participants = connection.execute(
                text(
                    """
                    SELECT participant_id,campaign_id,delete_by,withdrawn_at
                    FROM pilot_participants
                    WHERE delete_by<=:retention_due_by
                       OR (withdrawn_at IS NOT NULL AND withdrawn_at<=:withdrawal_before)
                    ORDER BY campaign_id,participant_id
                    FOR UPDATE
                    """
                ),
                {
                    "retention_due_by": retention_due_by,
                    "withdrawal_before": withdrawal_before,
                },
            ).mappings().all()
            deleted_participants = 0
            for participant in due_participants:
                connection.execute(
                    text(
                        "SELECT campaign_id FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"
                    ),
                    {"id": participant["campaign_id"]},
                ).one()
                attempts = connection.execute(
                    text(
                        "SELECT * FROM pilot_attempts WHERE participant_id=:id ORDER BY attempt_id"
                    ),
                    {"id": participant["participant_id"]},
                ).mappings().all()
                campaign_attempts = connection.execute(
                    text("SELECT * FROM pilot_attempts WHERE campaign_id=:id"),
                    {"id": participant["campaign_id"]},
                ).mappings().all()
                pre_delete_binding_status = (
                    "valid"
                    if self._telemetry_integrity(
                        participant["campaign_id"], campaign_attempts, connection
                    )
                    else "invalid"
                )
                deletion_reason = (
                    "withdrawal_due"
                    if participant["withdrawn_at"] is not None
                    and participant["withdrawn_at"] <= withdrawal_before
                    else "retention_due"
                )
                for attempt in attempts:
                    self._append_event(
                        connection,
                        campaign_id=participant["campaign_id"],
                        event_type="attempt_retention_deleted",
                        payload={
                            "attempt_id": str(attempt["attempt_id"]),
                            "execution_telemetry_sha256": execution_telemetry_sha256(
                                attempt
                            ),
                            "deletion_reason": deletion_reason,
                            "worker_started": attempt["started_at"] is not None,
                            "final_status": attempt["status"],
                            "stable_error_code": attempt["error_code"],
                            "pre_delete_binding_status": pre_delete_binding_status,
                        },
                        now=now,
                    )
                self._append_event(
                    connection,
                    campaign_id=participant["campaign_id"],
                    event_type="participant_retention_deleted",
                    payload={
                        "deletion_reason": deletion_reason,
                        "withdrawal_recorded": participant["withdrawn_at"] is not None,
                    },
                    now=now,
                )
                deleted = connection.execute(
                    text(
                        "DELETE FROM pilot_participants WHERE participant_id=:id RETURNING participant_id"
                    ),
                    {"id": participant["participant_id"]},
                ).first()
                deleted_participants += deleted is not None

            deleted_invites = connection.execute(
                text(
                    """
                    DELETE FROM pilot_invites
                    WHERE expires_at<:now
                      AND (used_at IS NULL OR used_at<:retention_before)
                    RETURNING invite_id
                    """
                ),
                {"now": now, "retention_before": invite_retention_before},
            ).all()
            connection.execute(
                text("DELETE FROM pilot_rate_limits WHERE window_id<:window"),
                {"window": int(now.timestamp()) // 60 - 1440},
            )
        return {
            "participant_records_deleted": deleted_participants,
            "invite_records_deleted": len(deleted_invites),
            "secret_values_printed": False,
        }

    def consume_rate_limit(
        self,
        *,
        principal_key: str,
        route_key: str,
        window_seconds: int,
        limit: int,
        now: datetime,
    ) -> bool:
        window_id = int(now.timestamp()) // window_seconds
        with self.engine.begin() as connection:
            count = connection.execute(
                text(
                    """
                    INSERT INTO pilot_rate_limits
                    (principal_key,route_key,window_id,request_count)
                    VALUES (:principal,:route,:window,1)
                    ON CONFLICT (principal_key,route_key,window_id)
                    DO UPDATE SET request_count=pilot_rate_limits.request_count+1
                    RETURNING request_count
                    """
                ),
                {"principal": principal_key, "route": route_key, "window": window_id},
            ).scalar_one()
            connection.execute(
                text("DELETE FROM pilot_rate_limits WHERE window_id<:old"),
                {"old": window_id - 10},
            )
        return count <= limit

    def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        candidate_commitment_sha256: str,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO pilot_worker_heartbeats
                    (worker_id,candidate_commitment_sha256,execution_environment,
                     deployment_image_digest,last_seen_at)
                    VALUES (:worker,:candidate,:execution_environment,
                            :deployment_image_digest,:now)
                    ON CONFLICT (worker_id) DO UPDATE SET
                      candidate_commitment_sha256=EXCLUDED.candidate_commitment_sha256,
                      execution_environment=EXCLUDED.execution_environment,
                      deployment_image_digest=EXCLUDED.deployment_image_digest,
                      last_seen_at=EXCLUDED.last_seen_at
                    """
                ),
                {
                    "worker": worker_id,
                    "candidate": candidate_commitment_sha256,
                    "execution_environment": execution_environment,
                    "deployment_image_digest": deployment_image_digest,
                    "now": now,
                },
            )
            connection.execute(
                text("DELETE FROM pilot_worker_heartbeats WHERE last_seen_at<:cutoff"),
                {"cutoff": now - timedelta(days=1)},
            )

    def worker_ready(
        self,
        *,
        candidate_commitment_sha256: str,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
        max_age_seconds: int,
    ) -> bool:
        with self.engine.connect() as connection:
            count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_worker_heartbeats
                    WHERE candidate_commitment_sha256=:candidate
                      AND execution_environment=:execution_environment
                      AND deployment_image_digest IS NOT DISTINCT FROM :deployment_image_digest
                      AND last_seen_at>:threshold
                    """
                ),
                {
                    "candidate": candidate_commitment_sha256,
                    "execution_environment": execution_environment,
                    "deployment_image_digest": deployment_image_digest,
                    "threshold": now - timedelta(seconds=max_age_seconds),
                },
            ).scalar_one()
        return count > 0

    def list_incidents(self, campaign_id: str) -> Sequence[Mapping[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT incident_id,incident_type,status,created_at,resolved_at
                    FROM pilot_incidents WHERE campaign_id=:id ORDER BY created_at
                    """
                ),
                {"id": campaign_id},
            ).mappings().all()
        return tuple(
            {
                "incident_id": str(row["incident_id"]),
                "incident_type": str(row["incident_type"]),
                "status": str(row["status"]),
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
            }
            for row in rows
        )

    def campaign_summary_data(self, campaign_id: str) -> Mapping[str, Any]:
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection, connection.begin():
            tasks = connection.execute(
                text("SELECT * FROM pilot_tasks WHERE campaign_id=:id"),
                {"id": campaign_id},
            ).mappings().all()
            participants = connection.execute(
                text("SELECT * FROM pilot_participants WHERE campaign_id=:id"),
                {"id": campaign_id},
            ).mappings().all()
            attempts = connection.execute(
                text("SELECT * FROM pilot_attempts WHERE campaign_id=:id"),
                {"id": campaign_id},
            ).mappings().all()
            feedback_rows = connection.execute(
                text(
                    """
                    SELECT f.*,a.participant_id,a.provider_latency_ms,
                           t.dataset_id,t.scenario
                    FROM pilot_feedback f
                    JOIN pilot_attempts a ON a.attempt_id=f.attempt_id
                    JOIN pilot_tasks t ON t.campaign_id=a.campaign_id AND t.task_id=a.task_id
                    WHERE a.campaign_id=:id
                    """
                ),
                {"id": campaign_id},
            ).mappings().all()
            incidents = connection.execute(
                text("SELECT * FROM pilot_incidents WHERE campaign_id=:id"),
                {"id": campaign_id},
            ).mappings().all()
            retired_withdrawn_participants = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pilot_events
                    WHERE campaign_id=:id
                      AND event_type='participant_retention_deleted'
                      AND payload->>'withdrawal_recorded'='true'
                    """
                ),
                {"id": campaign_id},
            ).scalar_one()
            audit_chain_valid = self._verify_audit_chain(campaign_id, connection)
            task_pack_integrity_valid = self._task_pack_integrity(
                campaign_id, tasks, connection
            )
            telemetry_integrity_valid = self._telemetry_integrity(
                campaign_id, attempts, connection
            )
            participant_projection_integrity_valid = (
                self._participant_projection_integrity(
                    campaign_id, participants, connection
                )
            )
        qualifying_participants = {
            participant["participant_id"]
            for participant in participants
            if participant["consented_at"] is not None
            and participant["withdrawn_at"] is None
            and participant["status"] != "withdrawn"
        }
        feedback_rows = [
            row for row in feedback_rows if row["participant_id"] in qualifying_participants
        ]
        qualifying_attempts = [
            row for row in attempts if row["participant_id"] in qualifying_participants
        ]
        provider_execution_telemetry = summarize_provider_execution_telemetry(
            qualifying_attempts
        )
        provider_execution_telemetry["append_only_event_binding_status"] = (
            "valid" if telemetry_integrity_valid else "invalid"
        )
        completed_by_participant = [
            sum(row["participant_id"] == participant["participant_id"] for row in feedback_rows)
            for participant in participants
            if participant["participant_id"] in qualifying_participants
        ]
        datasets = sorted({task["dataset_id"] for task in tasks})
        scenarios = sorted({task["scenario"] for task in tasks})
        return {
            "task_count": len(tasks),
            "eligible_participant_count": len(qualifying_participants),
            "started_participant_count": sum(p["withdrawn_at"] is None and p["status"] in {"active","completed"} for p in participants),
            "completed_participant_count": sum(p["withdrawn_at"] is None and p["status"] == "completed" for p in participants),
            "withdrawn_participant_count": sum(p["status"] == "withdrawn" for p in participants) + int(retired_withdrawn_participants),
            "planned_interaction_count": len(qualifying_participants) * len(tasks),
            "started_interaction_count": sum(a["participant_id"] in qualifying_participants and a["status"] != "assigned" for a in attempts),
            "answer_displayed_count": sum(a["participant_id"] in qualifying_participants and a["revealed_at"] is not None for a in attempts),
            "feedback_completed_count": len(feedback_rows),
            "technical_failure_count": sum(a["participant_id"] in qualifying_participants and a["status"] in {"failed","withheld","excluded"} and a["error_code"] is not None and a["error_code"] not in {"participant_skipped","pilot_output_safety_filter"} for a in attempts),
            "provider_execution_telemetry": provider_execution_telemetry,
            "telemetry_integrity_valid": telemetry_integrity_valid,
            "participant_projection_integrity_valid": participant_projection_integrity_valid,
            "participant_projection_binding_status": (
                "valid" if participant_projection_integrity_valid else "invalid"
            ),
            "dataset_count": len({row["dataset_id"] for row in feedback_rows}),
            "scenario_count": len({row["scenario"] for row in feedback_rows}),
            "dataset_counts": {dataset_id: sum(row["dataset_id"] == dataset_id for row in feedback_rows) for dataset_id in datasets},
            "scenario_counts": {scenario: sum(row["scenario"] == scenario for row in feedback_rows) for scenario in scenarios},
            "completed_by_participant": completed_by_participant,
            "understandable_yes_count": sum(row["understandable"] for row in feedback_rows),
            "useful_yes_count": sum(row["useful_for_next_step"] for row in feedback_rows),
            "missing_information_yes_count": sum(row["missing_information"] for row in feedback_rows),
            "obvious_problem_yes_count": sum(row["obvious_problem"] for row in feedback_rows),
            "needs_expert_review_yes_count": sum(row["needs_expert_review"] for row in feedback_rows),
            "clarification_useful_yes_count": sum(row["clarification_useful"] is True for row in feedback_rows),
            "clarification_feedback_count": sum(row["clarification_useful"] is not None for row in feedback_rows),
            "user_reported_concern_count": sum(row["safety_concern"] for row in feedback_rows),
            "unresolved_incident_count": sum(row["status"] == "unresolved" for row in incidents),
            "confirmed_incident_count": sum(row["status"] == "confirmed" for row in incidents),
            "approval_bypass_count": sum(row["incident_type"] == "approval_bypass" for row in incidents),
            "secret_or_personal_data_exposure_count": sum(row["incident_type"] == "secret_or_personal_data_exposure" for row in incidents),
            "provider_latencies_ms": [row["provider_latency_ms"] for row in feedback_rows if row["provider_latency_ms"] is not None],
            "human_review_seconds": [row["human_review_seconds"] for row in feedback_rows],
            "audit_chain_valid": audit_chain_valid,
            "task_pack_integrity_valid": task_pack_integrity_valid,
        }

    def _verify_audit_chain(
        self, campaign_id: str, connection: Connection
    ) -> bool:
        rows = connection.execute(
            text("SELECT * FROM pilot_events WHERE campaign_id=:id ORDER BY sequence"),
            {"id": campaign_id},
        ).mappings().all()
        previous = "0" * 64
        for index, row in enumerate(rows):
            if int(row["sequence"]) != index or row["previous_hash"] != previous:
                return False
            expected = _event_hash(
                campaign_id,
                index,
                str(row["event_type"]),
                dict(row["payload"]),
                previous,
            )
            if row["event_hash"] != expected:
                return False
            previous = expected
        return bool(rows)

    def _telemetry_integrity(
        self,
        campaign_id: str,
        attempts: Sequence[Mapping[str, Any]],
        connection: Connection,
    ) -> bool:
        event_rows = connection.execute(
            text(
                """
                SELECT event_type,payload FROM pilot_events
                WHERE campaign_id=:id
                  AND event_type IN (
                    'attempt_started','attempt_succeeded','attempt_failed',
                    'attempt_withheld','attempt_excluded','attempt_skipped',
                    'attempt_retention_deleted'
                  )
                ORDER BY sequence
                """
            ),
            {"id": campaign_id},
        ).mappings().all()
        attempts_by_id = {str(attempt["attempt_id"]): attempt for attempt in attempts}
        pre_delete_integrity_valid = True
        terminal_statuses = {
            "assigned",
            "queued",
            "running",
            "succeeded",
            "failed",
            "withheld",
            "excluded",
            "completed",
        }
        retired_attempts: dict[str, Mapping[str, Any]] = {}
        for event in event_rows:
            if event["event_type"] != "attempt_retention_deleted":
                continue
            payload = dict(event["payload"])
            attempt_id = payload.get("attempt_id")
            digest = payload.get("execution_telemetry_sha256")
            if (
                not isinstance(attempt_id, str)
                or attempt_id in retired_attempts
                or attempt_id in attempts_by_id
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or payload.get("deletion_reason")
                not in {"withdrawal_due", "retention_due"}
                or payload.get("pre_delete_binding_status") not in {"valid", "invalid"}
                or not isinstance(payload.get("worker_started"), bool)
                or payload.get("final_status") not in terminal_statuses
                or (
                    payload.get("stable_error_code") is not None
                    and (
                        not isinstance(payload.get("stable_error_code"), str)
                        or len(payload["stable_error_code"]) > 64
                    )
                )
            ):
                return False
            if payload["pre_delete_binding_status"] == "invalid":
                pre_delete_integrity_valid = False
            retired_attempts[attempt_id] = payload
        started_event_ids: set[str] = set()
        latest_terminal_event: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for event in event_rows:
            payload = dict(event["payload"])
            attempt_id = payload.get("attempt_id")
            if event["event_type"] == "attempt_retention_deleted":
                continue
            if not isinstance(attempt_id, str):
                return False
            if attempt_id not in attempts_by_id:
                if attempt_id not in retired_attempts:
                    return False
                tombstone = retired_attempts[attempt_id]
                digest = payload.get("execution_telemetry_sha256")
                if digest is not None and digest != tombstone[
                    "execution_telemetry_sha256"
                ]:
                    return False
                if event["event_type"] == "attempt_started":
                    if attempt_id in started_event_ids:
                        return False
                    started_event_ids.add(attempt_id)
                else:
                    latest_terminal_event[attempt_id] = (
                        str(event["event_type"]),
                        payload,
                    )
                continue
            if event["event_type"] == "attempt_started":
                if attempt_id in started_event_ids:
                    return False
                started_event_ids.add(attempt_id)
                continue
            digest = payload.get("execution_telemetry_sha256")
            if not isinstance(digest, str) or digest != execution_telemetry_sha256(
                attempts_by_id[attempt_id]
            ):
                return False
            latest_terminal_event[attempt_id] = (str(event["event_type"]), payload)

        database_started_ids = {
            str(attempt["attempt_id"])
            for attempt in attempts
            if attempt["started_at"] is not None
        }
        if started_event_ids - set(retired_attempts) != database_started_ids:
            return False

        def terminal_matches(
            status: str,
            error_code: str | None,
            event_type: str,
            payload: Mapping[str, Any],
        ) -> bool:
            if event_type == "attempt_succeeded":
                return status in {"succeeded", "completed"} and error_code is None
            if event_type == "attempt_failed":
                return status == "failed" and error_code == payload.get("error_code")
            if event_type == "attempt_withheld":
                return status == "withheld" and error_code == payload.get("error_code")
            if event_type == "attempt_excluded":
                return status == "excluded" and error_code == payload.get("error_code")
            if event_type == "attempt_skipped":
                return (
                    status == "excluded"
                    and error_code == "participant_skipped"
                    and payload.get("error_code") == "participant_skipped"
                )
            return False

        for attempt_id, tombstone in retired_attempts.items():
            worker_started = bool(tombstone["worker_started"])
            if worker_started != (attempt_id in started_event_ids):
                return False
            terminal_event = latest_terminal_event.get(attempt_id)
            if terminal_event is None:
                if worker_started and tombstone["final_status"] != "running":
                    return False
                continue
            if not terminal_matches(
                str(tombstone["final_status"]),
                tombstone.get("stable_error_code"),
                terminal_event[0],
                terminal_event[1],
            ):
                return False

        for attempt_id, attempt in attempts_by_id.items():
            terminal_event = latest_terminal_event.get(attempt_id)
            if attempt_id not in started_event_ids:
                if terminal_event is None:
                    continue
                event_type, payload = terminal_event
                if not terminal_matches(
                    str(attempt["status"]),
                    attempt["error_code"],
                    event_type,
                    payload,
                ):
                    return False
                continue
            if terminal_event is None:
                if attempt["status"] != "running":
                    return False
                continue
            event_type, payload = terminal_event
            if not terminal_matches(
                str(attempt["status"]),
                attempt["error_code"],
                event_type,
                payload,
            ):
                return False
        return pre_delete_integrity_valid

    def _participant_projection_integrity(
        self,
        campaign_id: str,
        participants: Sequence[Mapping[str, Any]],
        connection: Connection,
    ) -> bool:
        rows = connection.execute(
            text(
                """
                SELECT event_type,payload FROM pilot_events
                WHERE campaign_id=:id
                  AND event_type IN (
                    'invite_redeemed','participant_withdrew',
                    'participant_retention_deleted'
                  )
                """
            ),
            {"id": campaign_id},
        ).mappings().all()
        redeemed_count = sum(row["event_type"] == "invite_redeemed" for row in rows)
        withdrawal_count = sum(
            row["event_type"] == "participant_withdrew" for row in rows
        )
        retired_rows = [
            dict(row["payload"])
            for row in rows
            if row["event_type"] == "participant_retention_deleted"
        ]
        if any(
            payload.get("deletion_reason") not in {"withdrawal_due", "retention_due"}
            or not isinstance(payload.get("withdrawal_recorded"), bool)
            for payload in retired_rows
        ):
            return False
        live_withdrawn = sum(
            participant["status"] == "withdrawn" for participant in participants
        )
        retired_withdrawn = sum(
            payload["withdrawal_recorded"] for payload in retired_rows
        )
        return (
            redeemed_count == len(participants) + len(retired_rows)
            and withdrawal_count == live_withdrawn + retired_withdrawn
        )

    def _task_pack_integrity(
        self,
        campaign_id: str,
        task_rows: Sequence[Mapping[str, Any]],
        connection: Connection,
    ) -> bool:
        expected = connection.execute(
            text("SELECT task_pack_sha256 FROM pilot_campaigns WHERE campaign_id=:id"),
            {"id": campaign_id},
        ).scalar_one()
        return expected == _task_pack_hash_from_rows(task_rows)

    def _task_row(self, connection: Connection, campaign_id: str, task_id: str):
        return connection.execute(
            text("SELECT * FROM pilot_tasks WHERE campaign_id=:campaign AND task_id=:task"),
            {"campaign": campaign_id, "task": task_id},
        ).mappings().one()

    def _lock_active_participant(
        self, connection: Connection, participant_id: str
    ) -> Mapping[str, Any]:
        participant = connection.execute(
            text(
                "SELECT * FROM pilot_participants WHERE participant_id=:id FOR UPDATE"
            ),
            {"id": participant_id},
        ).mappings().first()
        if (
            participant is None
            or participant["withdrawn_at"] is not None
            or participant["status"] == "withdrawn"
        ):
            raise InvalidTransition("participant 已撤回或不存在。")
        return participant

    def _create_incident(
        self,
        connection: Connection,
        *,
        campaign_id: str,
        participant_id: str,
        attempt_id: Any,
        incident_type: str,
        now: datetime,
    ) -> None:
        connection.execute(
            text("UPDATE pilot_campaigns SET status='paused' WHERE campaign_id=:id"),
            {"id": campaign_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO pilot_incidents
                (incident_id,campaign_id,participant_id,attempt_id,incident_type,status,created_at)
                VALUES (CAST(:incident AS uuid),:campaign,:participant,:attempt,:type,'unresolved',:now)
                """
            ),
            {
                "incident": str(uuid.uuid4()),
                "campaign": campaign_id,
                "participant": participant_id,
                "attempt": attempt_id,
                "type": incident_type,
                "now": now,
            },
        )

    def _append_event(
        self,
        connection: Connection,
        *,
        campaign_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            text("SELECT campaign_id FROM pilot_campaigns WHERE campaign_id=:id FOR UPDATE"),
            {"id": campaign_id},
        ).one()
        previous = connection.execute(
            text(
                """
                SELECT sequence,event_hash FROM pilot_events
                WHERE campaign_id=:id ORDER BY sequence DESC
                FOR UPDATE LIMIT 1
                """
            ),
            {"id": campaign_id},
        ).mappings().first()
        sequence = 0 if previous is None else int(previous["sequence"]) + 1
        previous_hash = "0" * 64 if previous is None else str(previous["event_hash"])
        event_hash = _event_hash(campaign_id, sequence, event_type, payload, previous_hash)
        connection.execute(
            text(
                """
                INSERT INTO pilot_events
                (campaign_id,sequence,event_type,payload,previous_hash,event_hash,created_at)
                VALUES (:campaign,:sequence,:type,CAST(:payload AS jsonb),:previous,:hash,:now)
                """
            ),
            {
                "campaign": campaign_id,
                "sequence": sequence,
                "type": event_type,
                "payload": _canonical_json(payload),
                "previous": previous_hash,
                "hash": event_hash,
                "now": now,
            },
        )


def _campaign(row: Mapping[str, Any]) -> Campaign:
    return Campaign(
        campaign_id=str(row["campaign_id"]),
        title=str(row["title"]),
        status=CampaignStatus(str(row["status"])),
        execution_environment=str(row["execution_environment"]),
        candidate_commitment_sha256=str(row["candidate_commitment_sha256"]),
        protocol_sha256=str(row["protocol_sha256"]),
        consent_sha256=str(row["consent_sha256"]),
        task_pack_sha256=str(row["task_pack_sha256"]),
        feedback_schema_sha256=str(row["feedback_schema_sha256"]),
        dataset_manifest_sha256=str(row["dataset_manifest_sha256"]),
        deployment_git_sha=row["deployment_git_sha"],
        deployment_image_digest=row["deployment_image_digest"],
        provider_id=str(row["provider_id"]),
        model_id=str(row["model_id"]),
        transport_id=str(row["transport_id"]),
        max_provider_runs=int(row["max_provider_runs"]),
        target_participants=int(row["target_participants"]),
        created_at=row["created_at"],
        frozen_at=row["frozen_at"],
    )


def _task(row: Mapping[str, Any]) -> PilotTask:
    return PilotTask(
        task_id=str(row["task_id"]),
        sequence=int(row["sequence"]),
        source_task_id=str(row["source_task_id"]),
        dataset_id=str(row["dataset_id"]),
        scenario=str(row["scenario"]),
        prompt_en=str(row["prompt_en"]),
        prompt_zh=str(row["prompt_zh"]),
        context=dict(row["context"]),
        clarification_expected=bool(row["clarification_expected"]),
    )


def _attempt(row: Mapping[str, Any], task_row: Mapping[str, Any]) -> Attempt:
    return Attempt(
        attempt_id=str(row["attempt_id"]),
        participant_id=str(row["participant_id"]),
        campaign_id=str(row["campaign_id"]),
        task=_task(task_row),
        status=AttemptStatus(str(row["status"])),
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        provider_completed_at=row["provider_completed_at"],
        revealed_at=row["revealed_at"],
        completed_at=row["completed_at"],
        safe_output=row["safe_output"],
        output_sha256=row["output_sha256"],
        provider_latency_ms=row["provider_latency_ms"],
        outcome=row["outcome"],
        error_code=row["error_code"],
        model_call_count=row["model_call_count"],
        model_requested_tool_call_count=row["model_requested_tool_call_count"],
        backend_executed_tool_call_count=row["backend_executed_tool_call_count"],
    )


def _participant_session(row: Mapping[str, Any]) -> ParticipantSession:
    return ParticipantSession(
        participant_id=str(row["participant_id"]),
        campaign_id=str(row["campaign_id"]),
        participant_status=ParticipantStatus(str(row["status"])),
        session_instance_id=str(row["session_instance_id"]),
        session_expires_at=row["session_expires_at"],
        csrf_digest=str(row["csrf_digest"]),
        consented_at=row["consented_at"],
        withdrawn_at=row["withdrawn_at"],
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _event_hash(
    campaign_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    previous_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "campaign_id": campaign_id,
            "sequence": sequence,
            "event_type": event_type,
            "payload": dict(payload),
            "previous_hash": previous_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _task_pack_hash_from_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "task_id": str(row["task_id"]),
            "sequence": int(row["sequence"]),
            "source_task_id": str(row["source_task_id"]),
            "dataset_id": str(row["dataset_id"]),
            "scenario": str(row["scenario"]),
            "prompt_en": str(row["prompt_en"]),
            "prompt_zh": str(row["prompt_zh"]),
            "context": dict(row["context"]),
            "clarification_expected": bool(row["clarification_expected"]),
        }
        for row in sorted(rows, key=lambda item: int(item["sequence"]))
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
