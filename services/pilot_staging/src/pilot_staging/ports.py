from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from .domain import (
    Attempt,
    Campaign,
    CandidateResult,
    ExecutionTelemetry,
    Feedback,
    ParticipantSession,
    PilotTask,
)


class PilotStore(Protocol):
    def healthcheck(self) -> bool: ...

    def create_campaign(
        self, campaign: Campaign, tasks: Sequence[PilotTask], *, now: datetime
    ) -> Campaign: ...

    def get_campaign(self, campaign_id: str) -> Campaign | None: ...

    def freeze_campaign(self, campaign_id: str, *, now: datetime) -> Campaign: ...

    def complete_campaign(self, campaign_id: str, *, now: datetime) -> Campaign: ...

    def resolve_incident(
        self, *, campaign_id: str, incident_id: str, resolution: str, now: datetime
    ) -> None: ...

    def create_invite(
        self,
        *,
        invite_id: str,
        campaign_id: str,
        token_digest: str,
        expires_at: datetime,
        now: datetime,
    ) -> None: ...

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
    ) -> ParticipantSession: ...

    def get_session(
        self, *, session_digest: str, now: datetime
    ) -> ParticipantSession | None: ...

    def revoke_session(self, *, session_digest: str, now: datetime) -> None: ...

    def record_consent(
        self,
        *,
        participant_id: str,
        consent_sha256: str,
        eligibility_confirmed: bool,
        now: datetime,
    ) -> ParticipantSession: ...

    def current_attempt(self, *, participant_id: str, now: datetime) -> Attempt | None: ...

    def queue_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt: ...

    def claim_attempt(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
    ) -> Attempt | None: ...

    def complete_attempt(
        self,
        *,
        attempt_id: str,
        worker_id: str,
        result: CandidateResult,
        safe_output: str,
        output_sha256: str,
        now: datetime,
    ) -> Attempt: ...

    def fail_attempt(
        self,
        *,
        attempt_id: str,
        worker_id: str,
        error_code: str,
        withheld: bool,
        telemetry: ExecutionTelemetry | None,
        now: datetime,
    ) -> Attempt: ...

    def reveal_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt: ...

    def exclude_failed_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt: ...

    def skip_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt: ...

    def record_feedback(
        self,
        *,
        participant_id: str,
        attempt_id: str,
        feedback: Feedback,
        human_review_seconds: int,
        now: datetime,
    ) -> Attempt: ...

    def withdraw(self, *, participant_id: str, now: datetime) -> None: ...

    def consume_rate_limit(
        self,
        *,
        principal_key: str,
        route_key: str,
        window_seconds: int,
        limit: int,
        now: datetime,
    ) -> bool: ...

    def campaign_summary_data(self, campaign_id: str) -> Mapping[str, Any]: ...

    def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        candidate_commitment_sha256: str,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
    ) -> None: ...

    def worker_ready(
        self,
        *,
        candidate_commitment_sha256: str,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
        max_age_seconds: int,
    ) -> bool: ...

    def list_incidents(self, campaign_id: str) -> Sequence[Mapping[str, Any]]: ...


class CandidateExecutor(Protocol):
    def execute(self, task: PilotTask) -> CandidateResult: ...


class DatasetCatalog(Protocol):
    def dataset_ids(self) -> frozenset[str]: ...
