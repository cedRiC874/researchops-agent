from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal, Mapping, Sequence

from .domain import (
    CLAIM_SCOPE,
    LOCKED_CANDIDATE_COMMITMENT,
    Attempt,
    AttemptNotFound,
    AttemptStatus,
    AuthenticationRequired,
    Campaign,
    CampaignDrift,
    CampaignNotAvailable,
    CampaignNotFound,
    CampaignStatus,
    CandidateResult,
    ConsentRequired,
    CsrfInvalid,
    Feedback,
    InvalidRequest,
    InvalidTransition,
    OutputWithheld,
    ParticipantSession,
    ParticipantStatus,
    PilotTask,
    ProhibitedDataDetected,
    ProviderUnavailable,
    RateLimitExceeded,
    SessionExpired,
    public_attempt,
)
from .ports import CandidateExecutor, DatasetCatalog, PilotStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_TASK_ID = re.compile(r"^PILOT-TASK-[A-Z0-9-]{3,48}$")
_SCENARIOS = frozenset(
    {
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "unauthorized_resource",
        "prompt_injection",
        "duplicate_tool_call",
        "approval_pause",
    }
)
_REQUIRED_PILOT_DATASETS = frozenset(
    {
        "palmer_penguins_v0_1_0",
        "uci_parkinsons_telemonitoring_189",
        "uci_heart_disease_cleveland_45",
    }
)
_REQUIRED_PILOT_SCENARIOS = frozenset(
    {
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "approval_pause",
        "prompt_injection",
    }
)
_CONTEXT_KEYS = frozenset({"dataset_id", "design_id", "bundle_id", "release_name"})
_CONFIDENCE = frozenset({"low", "medium", "high"})
_SECRET = re.compile(r"\b(?:sk-|Bearer\s+)[A-Za-z0-9._-]{8,}", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s<>\"']+")
_UNIX_PATH = re.compile(r"(?:^|\s)/(?:Users|home|tmp|var|etc)/[^\s<>\"']+")
_SUBJECT_KEY = re.compile(r"\bSUBJ-[A-F0-9]{8,}\b", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(UTC)


class PilotApplication:
    """Application boundary for an invite-only prepared-task usability pilot."""

    def __init__(
        self,
        *,
        store: PilotStore,
        dataset_catalog: DatasetCatalog,
        token_pepper: bytes,
        consent_document: str,
        expected_commitments: Mapping[str, str],
        deployment_git_sha: str | None = None,
        deployment_image_digest: str | None = None,
        supervised_task_pack_sha256: str | None = None,
        retention_schedule_confirmed: bool = False,
        retention_days: int = 90,
        environment: Literal["local", "staging", "supervised"] = "local",
        session_ttl_hours: int = 8,
        provider_execution_enabled: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if len(token_pepper) < 32:
            raise ValueError("token_pepper 至少需要 32 bytes。")
        if not 1 <= session_ttl_hours <= 72:
            raise ValueError("session_ttl_hours 必须在 1..72。")
        if not isinstance(consent_document, str) or not consent_document.strip():
            raise ValueError("consent_document 不能为空。")
        commitment_keys = {
            "protocol_sha256",
            "consent_sha256",
            "feedback_schema_sha256",
            "dataset_manifest_sha256",
        }
        if set(expected_commitments) != commitment_keys or not all(
            isinstance(value, str) and _SHA256.fullmatch(value)
            for value in expected_commitments.values()
        ):
            raise ValueError("expected_commitments 无效。")
        if hashlib.sha256(consent_document.encode("utf-8")).hexdigest() != expected_commitments["consent_sha256"]:
            raise ValueError("consent_document hash 与 commitment 不一致。")
        if not 1 <= retention_days <= 90:
            raise ValueError("retention_days 必须在 1..90。")
        if environment not in {"local", "staging", "supervised"}:
            raise ValueError("environment 必须为 local/staging/supervised。")
        if environment == "supervised":
            if not provider_execution_enabled:
                raise ValueError("supervised 必须启用 Provider execution。")
            if not retention_schedule_confirmed:
                raise ValueError("supervised 必须确认 retention schedule。")
            if (
                deployment_git_sha is None
                or re.fullmatch(r"[0-9a-f]{40,64}", deployment_git_sha) is None
                or deployment_image_digest is None
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}", deployment_image_digest
                )
                is None
            ):
                raise ValueError(
                    "supervised 必须绑定有效 deployment Git SHA 与 image digest。"
                )
            if (
                supervised_task_pack_sha256 is None
                or _SHA256.fullmatch(supervised_task_pack_sha256) is None
            ):
                raise ValueError("supervised 必须绑定预注册 task pack SHA-256。")
        self._store = store
        self._datasets = dataset_catalog
        self._pepper = token_pepper
        self._session_ttl_hours = session_ttl_hours
        self._provider_execution_enabled = provider_execution_enabled
        self._consent_document = consent_document
        self._expected_commitments = dict(expected_commitments)
        self._deployment_git_sha = deployment_git_sha
        self._deployment_image_digest = deployment_image_digest
        self._supervised_task_pack_sha256 = supervised_task_pack_sha256
        self._retention_schedule_confirmed = retention_schedule_confirmed
        self._retention_days = retention_days
        self._environment = environment
        self._clock = clock

    def create_campaign(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = _mapping(payload, "campaign")
        _exact_keys(
            value,
            {
                "title",
                "protocol_sha256",
                "consent_sha256",
                "feedback_schema_sha256",
                "dataset_manifest_sha256",
                "deployment_git_sha",
                "deployment_image_digest",
                "candidate_commitment_sha256",
                "provider",
                "target_participants",
                "max_provider_runs",
                "tasks",
            },
            "campaign",
        )
        commitment = _sha256(value["candidate_commitment_sha256"], "candidate commitment")
        if commitment != LOCKED_CANDIDATE_COMMITMENT:
            raise CampaignDrift("只允许已锁定 Eval v2 candidate。")
        for field in (
            "protocol_sha256",
            "consent_sha256",
            "feedback_schema_sha256",
            "dataset_manifest_sha256",
        ):
            actual = _sha256(value[field], field)
            if actual != self._expected_commitments[field]:
                raise CampaignDrift(f"{field} 与部署文件不一致。")
        if value["deployment_git_sha"] != self._deployment_git_sha:
            raise CampaignDrift("deployment_git_sha 与服务部署配置不一致。")
        if value["deployment_image_digest"] != self._deployment_image_digest:
            raise CampaignDrift("deployment_image_digest 与服务部署配置不一致。")
        provider = _mapping(value["provider"], "provider")
        _exact_keys(provider, {"provider_id", "model_id", "transport_id"}, "provider")
        expected_provider = {
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "transport_id": "openai_compatible_responses",
        }
        if dict(provider) != expected_provider:
            raise CampaignDrift("Pilot campaign 必须绑定已锁定 Provider/model/transport。")

        raw_tasks = value["tasks"]
        if not isinstance(raw_tasks, list):
            raise InvalidRequest("tasks 必须是 array。")
        tasks = tuple(_parse_task(item, index) for index, item in enumerate(raw_tasks, 1))
        _validate_task_pack(tasks, self._datasets.dataset_ids())
        task_pack_sha256 = _task_pack_sha256(tasks)
        now = self._clock()
        participant_minimum, participant_maximum = (
            (1, 2) if self._environment == "supervised" else (3, 5)
        )
        campaign = Campaign(
            campaign_id="EXT-PILOT-" + secrets.token_hex(8).upper(),
            title=_bounded_string(value["title"], "title", 120),
            status=CampaignStatus.DRAFT,
            execution_environment=self._environment,
            candidate_commitment_sha256=commitment,
            protocol_sha256=self._expected_commitments["protocol_sha256"],
            consent_sha256=self._expected_commitments["consent_sha256"],
            task_pack_sha256=task_pack_sha256,
            feedback_schema_sha256=self._expected_commitments["feedback_schema_sha256"],
            dataset_manifest_sha256=self._expected_commitments["dataset_manifest_sha256"],
            deployment_git_sha=_optional_git_sha(self._deployment_git_sha),
            deployment_image_digest=_optional_image_digest(self._deployment_image_digest),
            provider_id=expected_provider["provider_id"],
            model_id=expected_provider["model_id"],
            transport_id=expected_provider["transport_id"],
            max_provider_runs=_bounded_int(
                value["max_provider_runs"], "max_provider_runs", 1, 100
            ),
            target_participants=_bounded_int(
                value["target_participants"],
                "target_participants",
                participant_minimum,
                participant_maximum,
            ),
            created_at=now,
            frozen_at=None,
        )
        required_provider_runs = campaign.target_participants * len(tasks)
        if self._environment == "supervised":
            if len(tasks) != 6:
                raise InvalidRequest("supervised task pack 必须恰好包含 6 题。")
            if task_pack_sha256 != self._supervised_task_pack_sha256:
                raise CampaignDrift("supervised task pack 与部署预注册版本不一致。")
            if campaign.max_provider_runs != required_provider_runs:
                raise InvalidRequest(
                    "supervised Provider run budget 必须精确等于参与者数乘以 6。"
                )
        elif campaign.max_provider_runs < required_provider_runs:
            raise InvalidRequest("Provider run budget 小于计划参与者任务数。")
        created = self._store.create_campaign(campaign, tasks, now=now)
        return _admin_campaign(created, task_count=len(tasks))

    def freeze_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._store.get_campaign(_safe_id(campaign_id, "campaign_id"))
        if campaign is None:
            raise CampaignNotFound("campaign 不存在。")
        if campaign.candidate_commitment_sha256 != LOCKED_CANDIDATE_COMMITMENT:
            raise CampaignDrift("Candidate commitment 已漂移。")
        frozen = self._store.freeze_campaign(campaign.campaign_id, now=self._clock())
        return _admin_campaign(frozen)

    def create_invite(self, campaign_id: str, *, ttl_hours: int = 72) -> dict[str, Any]:
        campaign = self._store.get_campaign(_safe_id(campaign_id, "campaign_id"))
        if campaign is None:
            raise CampaignNotFound("campaign 不存在。")
        if not self._campaign_matches_runtime(campaign):
            raise CampaignNotAvailable("campaign 与当前部署 identity 不匹配。")
        if campaign.status not in {CampaignStatus.FROZEN, CampaignStatus.RUNNING}:
            raise CampaignNotAvailable("只有 frozen/running campaign 可以发邀请。")
        ttl = _bounded_int(ttl_hours, "ttl_hours", 1, 168)
        raw_token = secrets.token_urlsafe(32)
        expires = self._clock() + timedelta(hours=ttl)
        self._store.create_invite(
            invite_id=str(uuid.uuid4()),
            campaign_id=campaign.campaign_id,
            token_digest=self._digest("invite", raw_token),
            expires_at=expires,
            now=self._clock(),
        )
        return {
            "campaign_id": campaign.campaign_id,
            "invite_token": raw_token,
            "expires_at": expires,
            "token_returned_once": True,
        }

    def complete_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._store.complete_campaign(
            _safe_id(campaign_id, "campaign_id"), now=self._clock()
        )
        return _admin_campaign(campaign)

    def resolve_incident(
        self, campaign_id: str, incident_id: str, *, resolution: str
    ) -> dict[str, str]:
        if resolution not in {"dismissed", "confirmed"}:
            raise InvalidRequest("incident resolution 必须为 dismissed/confirmed。")
        self._store.resolve_incident(
            campaign_id=_safe_id(campaign_id, "campaign_id"),
            incident_id=_safe_id(incident_id, "incident_id"),
            resolution=resolution,
            now=self._clock(),
        )
        return {"status": "resolved", "resolution": resolution}

    def exchange_invite(self, invite_token: str) -> dict[str, Any]:
        token = _bounded_string(invite_token, "invite_token", 256)
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        now = self._clock()
        session = self._store.exchange_invite(
            token_digest=self._digest("invite", token),
            participant_id="PX-" + secrets.token_hex(6).upper(),
            session_instance_id="PILOT-RUN-" + secrets.token_hex(16).upper(),
            session_digest=self._digest("session", session_token),
            csrf_digest=self._digest("csrf", csrf_token),
            session_expires_at=now + timedelta(hours=self._session_ttl_hours),
            participant_delete_by=now + timedelta(days=self._retention_days),
            expected_execution_environment=self._environment,
            expected_deployment_git_sha=self._deployment_git_sha,
            expected_deployment_image_digest=self._deployment_image_digest,
            expected_candidate_commitment_sha256=LOCKED_CANDIDATE_COMMITMENT,
            now=now,
        )
        campaign = self._require_campaign(session.campaign_id)
        return {
            "session_token": session_token,
            "csrf_token": csrf_token,
            "session_instance_id": session.session_instance_id,
            "expires_at": session.session_expires_at,
            "campaign": {
                "title": campaign.title,
                "execution_environment": campaign.execution_environment,
                "supervised_pretest": campaign.execution_environment == "supervised",
                "claim_scope": CLAIM_SCOPE,
                "external_pilot": campaign.execution_environment != "supervised",
                "external_participant_pretest": campaign.execution_environment
                == "supervised",
                "professional_correctness_assessed": False,
            },
            "consent_document": self._consent_document,
            "consent_document_sha256": campaign.consent_sha256,
        }

    def authenticate(
        self, session_token: str | None, *, csrf_token: str | None = None
    ) -> ParticipantSession:
        if not session_token:
            raise AuthenticationRequired("需要 pilot session。")
        session = self._store.get_session(
            session_digest=self._digest("session", session_token), now=self._clock()
        )
        if session is None:
            raise SessionExpired("Session 无效、已撤销或已过期。")
        campaign = self._require_campaign(session.campaign_id)
        if not self._campaign_matches_runtime(campaign):
            raise SessionExpired("Session 与当前部署 identity 不匹配。")
        if csrf_token is not None and not hmac.compare_digest(
            session.csrf_digest, self._digest("csrf", csrf_token)
        ):
            raise CsrfInvalid("CSRF token 无效。")
        return session

    def record_consent(
        self, session: ParticipantSession, confirmations: Mapping[str, Any]
    ) -> dict[str, Any]:
        required = {
            "adult_and_voluntary",
            "experimental_system_understood",
            "public_data_only",
            "provider_transfer_understood",
            "pseudonymous_recording_agreed",
            "withdrawal_understood",
            "external_researcher_eligible",
        }
        value = _mapping(confirmations, "consent")
        _exact_keys(value, required, "consent")
        if any(value[key] is not True for key in required):
            raise InvalidRequest("所有 consent 项必须主动确认。")
        campaign = self._require_campaign(session.campaign_id)
        updated = self._store.record_consent(
            participant_id=session.participant_id,
            consent_sha256=campaign.consent_sha256,
            eligibility_confirmed=True,
            now=self._clock(),
        )
        return {
            "status": updated.participant_status.value,
            "consent_receipt": {
                "schema_version": "external-pilot-consent-receipt/1.0",
                "campaign_id": campaign.campaign_id,
                "session_instance_id": updated.session_instance_id,
                "candidate_commitment_sha256": campaign.candidate_commitment_sha256,
                "consent_document_sha256": campaign.consent_sha256,
                "consented_at": updated.consented_at,
                "recording_or_audio_consent": False,
            },
        }

    def state(self, session: ParticipantSession) -> dict[str, Any]:
        if session.participant_status is ParticipantStatus.WITHDRAWN:
            raise SessionExpired("Participant 已退出。")
        campaign = self._require_campaign(session.campaign_id)
        if session.consented_at is None:
            return {
                "status": "consent_required",
                "campaign_title": campaign.title,
                "session_instance_id": session.session_instance_id,
                "execution_environment": campaign.execution_environment,
                "supervised_pretest": campaign.execution_environment == "supervised",
                "consent_document": self._consent_document,
                "consent_document_sha256": campaign.consent_sha256,
            }
        attempt = self._store.current_attempt(
            participant_id=session.participant_id, now=self._clock()
        )
        if attempt is None:
            return {
                "status": "complete",
                "campaign_title": campaign.title,
                "session_instance_id": session.session_instance_id,
                "execution_environment": campaign.execution_environment,
                "supervised_pretest": campaign.execution_environment == "supervised",
            }
        public = public_attempt(
            attempt,
            include_output=attempt.revealed_at is not None,
        )
        summary_data = self._store.campaign_summary_data(campaign.campaign_id)
        public["task_count"] = int(summary_data["task_count"])
        return {
            "status": "task",
            "campaign_title": campaign.title,
            "session_instance_id": session.session_instance_id,
            "execution_environment": campaign.execution_environment,
            "supervised_pretest": campaign.execution_environment == "supervised",
            "attempt": public,
        }

    def reveal(self, session: ParticipantSession, attempt_id: str) -> dict[str, Any]:
        self._require_consent(session)
        attempt_id = _safe_id(attempt_id, "attempt_id")
        attempt = self._store.current_attempt(
            participant_id=session.participant_id, now=self._clock()
        )
        if attempt is None or attempt.attempt_id != attempt_id:
            raise AttemptNotFound("attempt 不存在。")
        if attempt.status is AttemptStatus.ASSIGNED:
            if not self._provider_execution_enabled:
                raise ProviderUnavailable("Pilot online kill switch 当前关闭。")
            if not self._store.worker_ready(
                candidate_commitment_sha256=LOCKED_CANDIDATE_COMMITMENT,
                execution_environment=self._environment,
                deployment_image_digest=self._deployment_image_digest,
                now=self._clock(),
                max_age_seconds=360,
            ):
                raise ProviderUnavailable("Pilot worker 未通过最近的 candidate/key preflight。")
            queued = self._store.queue_attempt(
                participant_id=session.participant_id,
                attempt_id=attempt_id,
                now=self._clock(),
            )
            return {"status": "pending", "attempt": public_attempt(queued)}
        if attempt.status in {AttemptStatus.QUEUED, AttemptStatus.RUNNING}:
            return {"status": "pending", "attempt": public_attempt(attempt)}
        if attempt.status is AttemptStatus.WITHHELD:
            raise OutputWithheld("输出被安全过滤拦截，campaign 已暂停。")
        if attempt.status is AttemptStatus.FAILED:
            raise ProviderUnavailable("Provider 运行失败；未自动重试。")
        if attempt.status in {AttemptStatus.SUCCEEDED, AttemptStatus.COMPLETED}:
            revealed = self._store.reveal_attempt(
                participant_id=session.participant_id,
                attempt_id=attempt_id,
                now=self._clock(),
            )
            return {
                "status": "revealed",
                "attempt": public_attempt(revealed, include_output=True),
            }
        raise InvalidTransition("当前任务状态不可 reveal。")

    def attempt_status(
        self, session: ParticipantSession, attempt_id: str
    ) -> dict[str, Any]:
        self._require_consent(session)
        attempt = self._store.current_attempt(
            participant_id=session.participant_id, now=self._clock()
        )
        if attempt is None or attempt.attempt_id != _safe_id(attempt_id, "attempt_id"):
            raise AttemptNotFound("attempt 不存在。")
        return {"status": attempt.status.value, "attempt": public_attempt(attempt)}

    def exclude_failed_attempt(
        self, session: ParticipantSession, attempt_id: str
    ) -> dict[str, Any]:
        self._require_consent(session)
        excluded = self._store.exclude_failed_attempt(
            participant_id=session.participant_id,
            attempt_id=_safe_id(attempt_id, "attempt_id"),
            now=self._clock(),
        )
        return {"status": excluded.status.value, "next": self.state(session)}

    def skip_attempt(
        self, session: ParticipantSession, attempt_id: str
    ) -> dict[str, Any]:
        self._require_consent(session)
        skipped = self._store.skip_attempt(
            participant_id=session.participant_id,
            attempt_id=_safe_id(attempt_id, "attempt_id"),
            now=self._clock(),
        )
        return {
            "status": skipped.status.value,
            "exclusion_reason": "participant_skipped",
            "next": self.state(session),
        }

    def record_feedback(
        self,
        session: ParticipantSession,
        attempt_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_consent(session)
        attempt = self._store.current_attempt(
            participant_id=session.participant_id, now=self._clock()
        )
        if attempt is None or attempt.attempt_id != attempt_id:
            raise AttemptNotFound("attempt 不存在。")
        if attempt.revealed_at is None:
            raise InvalidTransition("答案显示前不能提交反馈。")
        feedback = _parse_feedback(
            payload, attempt.outcome == "clarification_required"
        )
        _reject_prohibited_text(feedback.notes, label="notes", row_dump_check=False)
        elapsed = max(0, int((self._clock() - attempt.revealed_at).total_seconds()))
        completed = self._store.record_feedback(
            participant_id=session.participant_id,
            attempt_id=attempt_id,
            feedback=feedback,
            human_review_seconds=min(elapsed, 7200),
            now=self._clock(),
        )
        return {"status": completed.status.value, "next": self.state(session)}

    def withdraw(self, session: ParticipantSession, session_token: str) -> dict[str, Any]:
        self._store.withdraw(participant_id=session.participant_id, now=self._clock())
        self._store.revoke_session(
            session_digest=self._digest("session", session_token), now=self._clock()
        )
        return {"status": "withdrawn", "further_provider_calls_blocked": True}

    def logout(self, session_token: str) -> None:
        self._store.revoke_session(
            session_digest=self._digest("session", session_token), now=self._clock()
        )

    def enforce_rate_limit(
        self,
        *,
        principal_key: str,
        route_key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> None:
        if not self._store.consume_rate_limit(
            principal_key=principal_key,
            route_key=route_key,
            window_seconds=window_seconds,
            limit=limit,
            now=self._clock(),
        ):
            raise RateLimitExceeded("请求过于频繁。")

    def summary(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._require_campaign(_safe_id(campaign_id, "campaign_id"))
        raw = dict(self._store.campaign_summary_data(campaign.campaign_id))
        completed_by_participant = [int(v) for v in raw["completed_by_participant"]]
        feedback_completed = int(raw["feedback_completed_count"])
        max_contribution = (
            max(completed_by_participant) / feedback_completed
            if feedback_completed and completed_by_participant
            else None
        )
        feedback_rate = _rate(
            feedback_completed, int(raw["answer_displayed_count"])
        )
        blockers: list[str] = []
        if campaign.execution_environment == "supervised":
            blockers.append("supervised_environment_not_claim_eligible")
        else:
            # Formal external usability claims require an operator-reviewed,
            # conflict-aware eligibility receipt. Self-attestation alone is not enough.
            blockers.append("operator_eligibility_adjudication_not_implemented")
        if campaign.status is not CampaignStatus.COMPLETE:
            blockers.append("pilot_not_complete")
        if campaign.frozen_at is None:
            blockers.append("protocol_not_frozen")
        if int(raw["eligible_participant_count"]) < 3:
            blockers.append("eligible_participant_count_below_minimum")
        if feedback_completed < 20 or any(v < 5 for v in completed_by_participant):
            blockers.append("completed_interaction_count_below_minimum")
        if max_contribution is None or max_contribution > 0.40:
            blockers.append("participant_concentration_too_high")
        dataset_counts = {
            str(key): int(value) for key, value in raw["dataset_counts"].items()
        }
        scenario_counts = {
            str(key): int(value) for key, value in raw["scenario_counts"].items()
        }
        if any(dataset_counts.get(name, 0) < 1 for name in _REQUIRED_PILOT_DATASETS):
            blockers.append("dataset_coverage_below_minimum")
        if any(scenario_counts.get(name, 0) < 1 for name in _REQUIRED_PILOT_SCENARIOS):
            blockers.append("scenario_coverage_below_minimum")
        if feedback_rate is None or feedback_rate < 0.95:
            blockers.append("feedback_coverage_incomplete")
        if int(raw["unresolved_incident_count"]) > 0:
            blockers.append("unresolved_safety_incident")
        if int(raw["confirmed_incident_count"]) > 0:
            blockers.append("confirmed_safety_incident")
        if not raw.get("audit_chain_valid") or not raw.get("task_pack_integrity_valid"):
            blockers.append("artifact_integrity_invalid")
        if campaign.deployment_git_sha is None or campaign.deployment_image_digest is None:
            blockers.append("mixed_build_or_candidate_versions")
        if int(raw["secret_or_personal_data_exposure_count"]) > 0:
            blockers.append("prohibited_data_detected")
        if not self._retention_schedule_confirmed:
            blockers.append("retention_not_configured")
        usability = {
            name: _metric(int(raw[f"{name}_yes_count"]), feedback_completed)
            for name in (
                "understandable",
                "useful",
                "missing_information",
                "obvious_problem",
                "needs_expert_review",
            )
        }
        usability["clarification_useful"] = _metric(
            int(raw["clarification_useful_yes_count"]),
            int(raw["clarification_feedback_count"]),
        )
        latencies = [int(v) for v in raw["provider_latencies_ms"]]
        review_times = [int(v) for v in raw["human_review_seconds"]]
        return {
            "schema_version": "external-pilot-summary/1.0",
            "campaign_id": campaign.campaign_id,
            "status": campaign.status.value,
            "execution_environment": campaign.execution_environment,
            "supervised_pretest": campaign.execution_environment == "supervised",
            "external_pilot": campaign.execution_environment != "supervised",
            "external_participant_pretest": campaign.execution_environment
            == "supervised",
            "evidence_status": (
                "supervised_external_user_pretest_only"
                if campaign.execution_environment == "supervised"
                else "external_researcher_usability_only"
            ),
            "claim_scope": CLAIM_SCOPE,
            "external_validation_claim_allowed": not blockers,
            "external_validation_claim_reason_codes": blockers,
            "pilot_success_criteria_met": (
                usability["understandable"]["rate"] is not None
                and usability["understandable"]["rate"] >= 0.8
                and usability["useful"]["rate"] is not None
                and usability["useful"]["rate"] >= 0.8
                and usability["missing_information"]["rate"] <= 0.2
                and usability["obvious_problem"]["rate"] <= 0.1
                and int(raw["confirmed_incident_count"]) == 0
            )
            if not blockers and feedback_completed
            else None,
            "commitments": {
                "protocol_sha256": campaign.protocol_sha256,
                "consent_document_sha256": campaign.consent_sha256,
                "task_pack_sha256": campaign.task_pack_sha256,
                "feedback_schema_sha256": campaign.feedback_schema_sha256,
                "candidate_commitment_sha256": campaign.candidate_commitment_sha256,
                "deployment_git_sha": campaign.deployment_git_sha,
                "deployment_image_digest": campaign.deployment_image_digest,
                "dataset_manifest_sha256": campaign.dataset_manifest_sha256,
                "provider_id": campaign.provider_id,
                "model_id": campaign.model_id,
                "transport_id": campaign.transport_id,
            },
            "cohort": {
                "eligible_external_participant_count": int(
                    raw["eligible_participant_count"]
                ),
                "started_participant_count": int(raw["started_participant_count"]),
                "completed_participant_count": int(raw["completed_participant_count"]),
                "withdrawn_participant_count": int(raw["withdrawn_participant_count"]),
            },
            "interactions": {
                "planned_count": int(raw["planned_interaction_count"]),
                "started_count": int(raw["started_interaction_count"]),
                "answer_displayed_count": int(raw["answer_displayed_count"]),
                "feedback_completed_count": feedback_completed,
                "technical_failure_count": int(raw["technical_failure_count"]),
                "seeded_count": feedback_completed,
                "exploratory_count": 0,
            },
            "coverage": {
                "dataset_count": int(raw["dataset_count"]),
                "dataset_counts": dataset_counts,
                "scenario_counts": scenario_counts,
                "feedback_rate": feedback_rate,
                "max_participant_contribution_rate": max_contribution,
            },
            "usability": usability,
            "machine_contract": {
                "assessed_count": 0,
                "passed_count": 0,
                "pass_rate": None,
                "model_planning_accuracy_claim_allowed": False,
            },
            "professional_correctness": {
                "assessed_count": 0,
                "qualified_reviewer_count": 0,
                "acceptance_rate": None,
                "claim_allowed": False,
            },
            "safety": {
                "user_reported_concern_count": int(
                    raw["user_reported_concern_count"]
                ),
                "unresolved_incident_count": int(raw["unresolved_incident_count"]),
                "confirmed_incident_count": int(raw["confirmed_incident_count"]),
                "approval_bypass_count": int(raw["approval_bypass_count"]),
                "secret_or_personal_data_exposure_count": int(
                    raw["secret_or_personal_data_exposure_count"]
                ),
            },
            "timing": {
                "provider_latency_ms_p50": _percentile(latencies, 0.50),
                "provider_latency_ms_p95": _percentile(latencies, 0.95),
                "human_review_seconds_p50": _percentile(review_times, 0.50),
                "human_review_seconds_p95": _percentile(review_times, 0.95),
                "latency_is_sla": False,
            },
            "retention_status": {
                "participant_level_max_days": self._retention_days,
                "withdrawal_deletion_sla_days": 7,
                "scheduled_purge_confirmed": self._retention_schedule_confirmed,
            },
            "boundaries": [
                "professional_correctness_not_assessed",
                "public_prepared_data_only",
                "not_private_holdout",
                "not_unknown_distribution_generalization",
                "not_production_sla",
            ],
        }

    def list_incidents(self, campaign_id: str) -> dict[str, Any]:
        campaign_id = _safe_id(campaign_id, "campaign_id")
        self._require_campaign(campaign_id)
        return {
            "campaign_id": campaign_id,
            "incidents": [dict(item) for item in self._store.list_incidents(campaign_id)],
            "participant_identifiers_included": False,
        }

    def _digest(self, namespace: str, token: str) -> str:
        return hmac.new(
            self._pepper,
            f"{namespace}:{token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _require_campaign(self, campaign_id: str) -> Campaign:
        campaign = self._store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFound("campaign 不存在。")
        return campaign

    def _campaign_matches_runtime(self, campaign: Campaign) -> bool:
        return (
            campaign.execution_environment == self._environment
            and campaign.deployment_git_sha == self._deployment_git_sha
            and campaign.deployment_image_digest == self._deployment_image_digest
            and campaign.candidate_commitment_sha256
            == LOCKED_CANDIDATE_COMMITMENT
        )

    @staticmethod
    def _require_consent(session: ParticipantSession) -> None:
        if session.consented_at is None or session.participant_status not in {
            ParticipantStatus.CONSENTED,
            ParticipantStatus.ACTIVE,
            ParticipantStatus.COMPLETED,
        }:
            raise ConsentRequired("需要先完成 consent。")


class PilotWorker:
    def __init__(
        self,
        *,
        store: PilotStore,
        executor: CandidateExecutor,
        worker_id: str,
        execution_environment: Literal["local", "staging", "supervised"] = "local",
        deployment_image_digest: str | None = None,
        lease_seconds: int = 300,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if execution_environment not in {"local", "staging", "supervised"}:
            raise ValueError("worker execution_environment 无效。")
        if execution_environment in {"staging", "supervised"} and (
            deployment_image_digest is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", deployment_image_digest) is None
        ):
            raise ValueError("staging/supervised worker 必须绑定 image digest。")
        self._store = store
        self._executor = executor
        self._worker_id = worker_id
        self._execution_environment = execution_environment
        self._deployment_image_digest = deployment_image_digest
        self._lease_seconds = lease_seconds
        self._clock = clock
        self.heartbeat()

    def heartbeat(self) -> None:
        self._store.record_worker_heartbeat(
            worker_id=self._worker_id,
            candidate_commitment_sha256=LOCKED_CANDIDATE_COMMITMENT,
            execution_environment=self._execution_environment,
            deployment_image_digest=self._deployment_image_digest,
            now=self._clock(),
        )

    def process_one(self) -> Attempt | None:
        self.heartbeat()
        attempt = self._store.claim_attempt(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            execution_environment=self._execution_environment,
            deployment_image_digest=self._deployment_image_digest,
            now=self._clock(),
        )
        if attempt is None:
            return None
        try:
            result = self._executor.execute(attempt.task)
            if result.error_code is not None or not result.final_output.strip():
                return self._store.fail_attempt(
                    attempt_id=attempt.attempt_id,
                    worker_id=self._worker_id,
                    error_code=result.error_code or "provider_output_incomplete",
                    withheld=False,
                    now=self._clock(),
                )
            _reject_prohibited_text(
                result.final_output, label="provider_output", row_dump_check=True
            )
            digest = hashlib.sha256(result.final_output.encode("utf-8")).hexdigest()
            return self._store.complete_attempt(
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
                result=result,
                safe_output=result.final_output,
                output_sha256=digest,
                now=self._clock(),
            )
        except ProhibitedDataDetected:
            return self._store.fail_attempt(
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
                error_code="pilot_output_safety_filter",
                withheld=True,
                now=self._clock(),
            )
        except Exception:
            return self._store.fail_attempt(
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
                error_code="provider_failed",
                withheld=False,
                now=self._clock(),
            )


def _parse_task(value: Any, sequence: int) -> PilotTask:
    item = _mapping(value, f"tasks[{sequence}]")
    _exact_keys(
        item,
        {
            "task_id",
            "source_task_id",
            "dataset_id",
            "scenario",
            "prompt_en",
            "prompt_zh",
            "context",
            "clarification_expected",
        },
        f"tasks[{sequence}]",
    )
    task_id = _bounded_string(item["task_id"], "task_id", 64)
    if _SAFE_TASK_ID.fullmatch(task_id) is None:
        raise InvalidRequest("task_id 格式无效。")
    source_task_id = _safe_id(item["source_task_id"], "source_task_id")
    dataset_id = _safe_id(item["dataset_id"], "dataset_id")
    scenario = _bounded_string(item["scenario"], "scenario", 64)
    if scenario not in _SCENARIOS:
        raise InvalidRequest("scenario 不在 pilot allowlist。")
    context = _mapping(item["context"], "context")
    if not set(context).issubset(_CONTEXT_KEYS):
        raise InvalidRequest("context 包含未授权字段。")
    normalized_context = {
        key: _safe_id(raw, f"context.{key}") for key, raw in context.items()
    }
    if normalized_context.get("dataset_id") != dataset_id:
        raise InvalidRequest("task dataset_id 与 context 授权不一致。")
    clarification = item["clarification_expected"]
    if not isinstance(clarification, bool) or clarification is not (
        scenario == "clarification_required"
    ):
        raise InvalidRequest("clarification_expected 与 scenario 不一致。")
    prompt_en = _bounded_string(item["prompt_en"], "prompt_en", 4000)
    prompt_zh = _bounded_string(item["prompt_zh"], "prompt_zh", 4000)
    _reject_prohibited_text(prompt_en, label="prompt_en", row_dump_check=False)
    _reject_prohibited_text(prompt_zh, label="prompt_zh", row_dump_check=False)
    return PilotTask(
        task_id=task_id,
        sequence=sequence,
        source_task_id=source_task_id,
        dataset_id=dataset_id,
        scenario=scenario,
        prompt_en=prompt_en,
        prompt_zh=prompt_zh,
        context=normalized_context,
        clarification_expected=clarification,
    )


def _validate_task_pack(tasks: Sequence[PilotTask], allowed_datasets: frozenset[str]) -> None:
    if not 6 <= len(tasks) <= 12:
        raise InvalidRequest("Pilot task pack 必须包含 6..12 题。")
    if len({task.task_id for task in tasks}) != len(tasks):
        raise InvalidRequest("Pilot task_id 必须唯一。")
    datasets = {task.dataset_id for task in tasks}
    if not datasets.issubset(allowed_datasets):
        raise InvalidRequest("Pilot task 使用未登记 dataset_id。")
    if len(datasets) < 3:
        raise InvalidRequest("Pilot task pack 必须覆盖至少 3 个数据集。")
    if len({task.scenario for task in tasks}) < 5:
        raise InvalidRequest("Pilot task pack 必须覆盖至少 5 类场景。")


def _parse_feedback(value: Mapping[str, Any], clarification_expected: bool) -> Feedback:
    required = {
        "understandable",
        "useful_for_next_step",
        "confidence",
        "needs_expert_review",
        "obvious_problem",
        "missing_information",
        "safety_concern",
        "notes",
    }
    if clarification_expected:
        required.add("clarification_useful")
    payload = _mapping(value, "feedback")
    _exact_keys(payload, required, "feedback")
    booleans = {}
    for name in required - {"confidence", "notes", "clarification_useful"}:
        if not isinstance(payload[name], bool):
            raise InvalidRequest(f"{name} 必须是 boolean。")
        booleans[name] = payload[name]
    confidence = payload["confidence"]
    if confidence not in _CONFIDENCE:
        raise InvalidRequest("confidence 必须是 low/medium/high。")
    clarification = payload.get("clarification_useful")
    if clarification_expected and not isinstance(clarification, bool):
        raise InvalidRequest("clarification_useful 必须是 boolean。")
    notes = _bounded_string(payload["notes"], "notes", 2000, allow_empty=True)
    if (confidence == "low" or booleans["needs_expert_review"]) and not notes:
        raise InvalidRequest("低 confidence 或需要专家复核时请简要说明。")
    return Feedback(
        understandable=booleans["understandable"],
        useful_for_next_step=booleans["useful_for_next_step"],
        confidence=confidence,
        needs_expert_review=booleans["needs_expert_review"],
        obvious_problem=booleans["obvious_problem"],
        missing_information=booleans["missing_information"],
        safety_concern=booleans["safety_concern"],
        clarification_useful=clarification,
        notes=notes,
    )


def _reject_prohibited_text(text: str, *, label: str, row_dump_check: bool) -> None:
    patterns = (_SECRET, _EMAIL, _WINDOWS_PATH, _UNIX_PATH, _SUBJECT_KEY)
    if any(pattern.search(text) for pattern in patterns):
        raise ProhibitedDataDetected(f"{label} 命中敏感信息过滤。")
    if row_dump_check:
        lines = [line for line in text.splitlines() if line.strip()]
        tabular = sum(line.count(",") >= 4 or line.count("\t") >= 4 for line in lines)
        if len(lines) >= 20 and tabular >= 10:
            raise ProhibitedDataDetected(f"{label} 疑似包含行级表格。")


def _task_pack_sha256(tasks: Sequence[PilotTask]) -> str:
    payload = [
        {
            "task_id": item.task_id,
            "sequence": item.sequence,
            "source_task_id": item.source_task_id,
            "dataset_id": item.dataset_id,
            "scenario": item.scenario,
            "prompt_en": item.prompt_en,
            "prompt_zh": item.prompt_zh,
            "context": dict(item.context),
            "clarification_expected": item.clarification_expected,
        }
        for item in tasks
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def task_pack_commitment_sha256(raw_tasks: Sequence[Mapping[str, Any]]) -> str:
    """Return the canonical campaign task commitment used by the service."""

    tasks = tuple(_parse_task(item, index) for index, item in enumerate(raw_tasks, 1))
    return _task_pack_sha256(tasks)


def _admin_campaign(campaign: Campaign, *, task_count: int | None = None) -> dict[str, Any]:
    return {
        "campaign_id": campaign.campaign_id,
        "title": campaign.title,
        "status": campaign.status.value,
        "execution_environment": campaign.execution_environment,
        "candidate_commitment_sha256": campaign.candidate_commitment_sha256,
        "task_pack_sha256": campaign.task_pack_sha256,
        "feedback_schema_sha256": campaign.feedback_schema_sha256,
        "deployment_git_sha": campaign.deployment_git_sha,
        "deployment_image_digest": campaign.deployment_image_digest,
        "provider": {
            "provider_id": campaign.provider_id,
            "model_id": campaign.model_id,
            "transport_id": campaign.transport_id,
        },
        "max_provider_runs": campaign.max_provider_runs,
        "target_participants": campaign.target_participants,
        "task_count": task_count,
        "created_at": campaign.created_at,
        "frozen_at": campaign.frozen_at,
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidRequest(f"{label} 必须是 object。")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise InvalidRequest(f"{label} 字段集合无效。")


def _bounded_string(
    value: Any, label: str, maximum: int, *, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(f"{label} 必须是 string。")
    normalized = value.strip()
    if (not allow_empty and not normalized) or len(normalized) > maximum:
        raise InvalidRequest(f"{label} 长度无效。")
    return normalized


def _safe_id(value: Any, label: str) -> str:
    normalized = _bounded_string(value, label, 128)
    if _SAFE_ID.fullmatch(normalized) is None:
        raise InvalidRequest(f"{label} 格式无效。")
    return normalized


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InvalidRequest(f"{label} 必须是小写 SHA-256。")
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRequest(f"{label} 必须在 {minimum}..{maximum}。")
    return value


def _optional_git_sha(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise InvalidRequest("deployment_git_sha 格式无效。")
    return value


def _optional_image_digest(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise InvalidRequest("deployment_image_digest 格式无效。")
    return value


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metric(yes_count: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "yes_count": yes_count,
        "denominator": denominator,
        "rate": _rate(yes_count, denominator),
    }


def _percentile(values: Sequence[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower))
