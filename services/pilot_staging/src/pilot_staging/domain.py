from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


LOCKED_CANDIDATE_COMMITMENT = (
    "7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11"
)
CLAIM_SCOPE = "external_researcher_usability_on_prepared_public_data"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    TERMINATED = "terminated"


class ParticipantStatus(StrEnum):
    INVITED = "invited"
    CONSENTED = "consented"
    ACTIVE = "active"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"


class AttemptStatus(StrEnum):
    ASSIGNED = "assigned"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WITHHELD = "withheld"
    EXCLUDED = "excluded"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class PilotTask:
    task_id: str
    sequence: int
    source_task_id: str
    dataset_id: str
    scenario: str
    prompt_en: str
    prompt_zh: str
    context: Mapping[str, str]
    clarification_expected: bool


@dataclass(frozen=True, slots=True)
class Campaign:
    campaign_id: str
    title: str
    status: CampaignStatus
    execution_environment: str
    candidate_commitment_sha256: str
    protocol_sha256: str
    consent_sha256: str
    task_pack_sha256: str
    feedback_schema_sha256: str
    dataset_manifest_sha256: str
    deployment_git_sha: str | None
    deployment_image_digest: str | None
    provider_id: str
    model_id: str
    transport_id: str
    max_provider_runs: int
    target_participants: int
    created_at: datetime
    frozen_at: datetime | None


@dataclass(frozen=True, slots=True)
class ParticipantSession:
    participant_id: str
    campaign_id: str
    participant_status: ParticipantStatus
    session_instance_id: str
    session_expires_at: datetime
    csrf_digest: str
    consented_at: datetime | None
    withdrawn_at: datetime | None


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    participant_id: str
    campaign_id: str
    task: PilotTask
    status: AttemptStatus
    queued_at: datetime | None
    started_at: datetime | None
    provider_completed_at: datetime | None
    revealed_at: datetime | None
    completed_at: datetime | None
    safe_output: str | None
    output_sha256: str | None
    provider_latency_ms: int | None
    outcome: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class CandidateResult:
    final_output: str
    outcome: str
    provider_latency_ms: int
    model_call_count: int | None
    model_requested_tool_call_count: int | None
    backend_executed_tool_call_count: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Feedback:
    understandable: bool
    useful_for_next_step: bool
    confidence: str
    needs_expert_review: bool
    obvious_problem: bool
    missing_information: bool
    safety_concern: bool
    clarification_useful: bool | None
    notes: str


class PilotError(RuntimeError):
    code = "pilot_error"


class InvalidRequest(PilotError):
    code = "invalid_request"


class AuthenticationRequired(PilotError):
    code = "authentication_required"


class AuthorizationDenied(PilotError):
    code = "authorization_denied"


class CampaignNotFound(PilotError):
    code = "campaign_not_found"


class CampaignNotAvailable(PilotError):
    code = "campaign_not_available"


class CampaignDrift(PilotError):
    code = "campaign_commitment_drift"


class InviteInvalid(PilotError):
    code = "invite_invalid"


class ConsentRequired(PilotError):
    code = "consent_required"


class SessionExpired(PilotError):
    code = "session_expired"


class CsrfInvalid(PilotError):
    code = "csrf_invalid"


class AttemptNotFound(PilotError):
    code = "attempt_not_found"


class InvalidTransition(PilotError):
    code = "invalid_transition"


class ProviderBudgetExhausted(PilotError):
    code = "provider_budget_exhausted"


class RateLimitExceeded(PilotError):
    code = "rate_limit_exceeded"


class ProhibitedDataDetected(PilotError):
    code = "prohibited_data_detected"


class OutputWithheld(PilotError):
    code = "output_withheld"


class ProviderUnavailable(PilotError):
    code = "provider_unavailable"


def public_attempt(attempt: Attempt, *, include_output: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "attempt_id": attempt.attempt_id,
        "sequence": attempt.task.sequence,
        "task_count": None,
        "task": {
            "prompt_en": attempt.task.prompt_en,
            "prompt_zh": attempt.task.prompt_zh,
        },
        "status": attempt.status.value,
        "answer_available": attempt.status
        in {AttemptStatus.SUCCEEDED, AttemptStatus.COMPLETED},
        "clarification_feedback_required": (
            attempt.outcome == "clarification_required"
            if attempt.revealed_at is not None
            else False
        ),
        "provider_latency_ms": (
            attempt.provider_latency_ms
            if attempt.revealed_at is not None
            else None
        ),
        "error_code": (
            attempt.error_code
            if attempt.status in {AttemptStatus.FAILED, AttemptStatus.WITHHELD}
            else None
        ),
    }
    if include_output and attempt.revealed_at is not None:
        value["agent_output"] = attempt.safe_output
    return value
