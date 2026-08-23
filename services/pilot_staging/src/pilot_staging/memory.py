from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

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
from .telemetry import summarize_provider_execution_telemetry


class StaticDatasetCatalog:
    def __init__(self, dataset_ids: Sequence[str]) -> None:
        self._ids = frozenset(dataset_ids)

    def dataset_ids(self) -> frozenset[str]:
        return self._ids


class InMemoryPilotStore:
    """Deterministic test adapter; production staging uses PostgreSQL."""

    def __init__(self) -> None:
        self.campaigns: dict[str, Campaign] = {}
        self.tasks: dict[str, tuple[PilotTask, ...]] = {}
        self.invites: dict[str, dict[str, Any]] = {}
        self.redeemed_invite_counts: dict[str, int] = {}
        self.participants: dict[str, ParticipantSession] = {}
        self.sessions: dict[str, str] = {}
        self.attempts: dict[str, Attempt] = {}
        self.attempt_by_participant_task: dict[tuple[str, str], str] = {}
        self.feedback: dict[str, tuple[Feedback, int]] = {}
        self.leases: dict[str, tuple[str, datetime]] = {}
        self.incidents: list[dict[str, Any]] = []
        self.rate_buckets: dict[tuple[str, str, int], int] = {}
        self.worker_heartbeats: dict[
            str, tuple[str, str, str | None, datetime]
        ] = {}
        self.healthy = True
        self._lock = threading.RLock()

    def healthcheck(self) -> bool:
        return self.healthy

    def create_campaign(
        self, campaign: Campaign, tasks: Sequence[PilotTask], *, now: datetime
    ) -> Campaign:
        with self._lock:
            if campaign.campaign_id in self.campaigns:
                raise InvalidTransition("campaign 已存在。")
            self.campaigns[campaign.campaign_id] = campaign
            self.tasks[campaign.campaign_id] = tuple(tasks)
            return campaign

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        return self.campaigns.get(campaign_id)

    def freeze_campaign(self, campaign_id: str, *, now: datetime) -> Campaign:
        with self._lock:
            campaign = self.campaigns[campaign_id]
            if campaign.status is CampaignStatus.FROZEN:
                return campaign
            if campaign.status is not CampaignStatus.DRAFT:
                raise InvalidTransition("只有 draft campaign 可以 freeze。")
            frozen = replace(campaign, status=CampaignStatus.FROZEN, frozen_at=now)
            self.campaigns[campaign_id] = frozen
            return frozen

    def create_invite(
        self,
        *,
        invite_id: str,
        campaign_id: str,
        token_digest: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        with self._lock:
            if token_digest in self.invites:
                raise InvalidTransition("invite digest 已存在。")
            self.invites[token_digest] = {
                "invite_id": invite_id,
                "campaign_id": campaign_id,
                "expires_at": expires_at,
                "used_at": None,
            }

    def complete_campaign(self, campaign_id: str, *, now: datetime) -> Campaign:
        with self._lock:
            campaign = self.campaigns[campaign_id]
            if any(
                item["campaign_id"] == campaign_id and item["status"] == "unresolved"
                for item in self.incidents
            ):
                raise InvalidTransition("仍有未解决安全事件。")
            cohort = [p for p in self.participants.values() if p.campaign_id == campaign_id]
            if not cohort or any(
                p.consented_at is not None
                and p.participant_status not in {ParticipantStatus.COMPLETED, ParticipantStatus.WITHDRAWN}
                for p in cohort
            ):
                raise InvalidTransition("仍有未完成 participant。")
            completed = replace(campaign, status=CampaignStatus.COMPLETE)
            self.campaigns[campaign_id] = completed
            return completed

    def resolve_incident(
        self, *, campaign_id: str, incident_id: str, resolution: str, now: datetime
    ) -> None:
        with self._lock:
            for incident in self.incidents:
                if incident["incident_id"] == incident_id and incident["campaign_id"] == campaign_id:
                    if incident["status"] != "unresolved":
                        raise InvalidTransition("incident 已处理。")
                    incident["status"] = resolution
                    if not any(
                        item["campaign_id"] == campaign_id and item["status"] == "unresolved"
                        for item in self.incidents
                    ):
                        campaign = self.campaigns[campaign_id]
                        self.campaigns[campaign_id] = replace(
                            campaign, status=CampaignStatus.RUNNING
                        )
                    return
            raise InvalidTransition("incident 不存在。")

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
        with self._lock:
            invite = self.invites.get(token_digest)
            if (
                invite is None
                or invite["used_at"] is not None
                or invite["expires_at"] <= now
            ):
                raise InviteInvalid("邀请无效、已使用或已过期。")
            campaign = self.campaigns[invite["campaign_id"]]
            if campaign.execution_environment != expected_execution_environment:
                raise CampaignNotAvailable("campaign 与当前 execution environment 不匹配。")
            if (
                campaign.deployment_git_sha != expected_deployment_git_sha
                or campaign.deployment_image_digest
                != expected_deployment_image_digest
                or campaign.candidate_commitment_sha256
                != expected_candidate_commitment_sha256
            ):
                raise CampaignNotAvailable("campaign 与当前部署 identity 不匹配。")
            if campaign.status not in {CampaignStatus.FROZEN, CampaignStatus.RUNNING}:
                raise CampaignNotAvailable("campaign 当前不可加入。")
            if (
                self.redeemed_invite_counts.get(campaign.campaign_id, 0)
                >= campaign.target_participants
            ):
                raise CampaignNotAvailable("campaign 参与者名额已满。")
            invite["used_at"] = now
            session = ParticipantSession(
                participant_id=participant_id,
                campaign_id=campaign.campaign_id,
                participant_status=ParticipantStatus.INVITED,
                session_instance_id=session_instance_id,
                session_expires_at=session_expires_at,
                csrf_digest=csrf_digest,
                consented_at=None,
                withdrawn_at=None,
            )
            self.participants[participant_id] = session
            self.sessions[session_digest] = participant_id
            self.redeemed_invite_counts[campaign.campaign_id] = (
                self.redeemed_invite_counts.get(campaign.campaign_id, 0) + 1
            )
            return session

    def get_session(
        self, *, session_digest: str, now: datetime
    ) -> ParticipantSession | None:
        participant_id = self.sessions.get(session_digest)
        if participant_id is None:
            return None
        session = self.participants[participant_id]
        if session.session_expires_at <= now or session.withdrawn_at is not None:
            return None
        return session

    def revoke_session(self, *, session_digest: str, now: datetime) -> None:
        with self._lock:
            self.sessions.pop(session_digest, None)

    def record_consent(
        self,
        *,
        participant_id: str,
        consent_sha256: str,
        eligibility_confirmed: bool,
        now: datetime,
    ) -> ParticipantSession:
        with self._lock:
            current = self.participants[participant_id]
            if current.consented_at is not None:
                return current
            if current.withdrawn_at is not None or not eligibility_confirmed:
                raise InvalidTransition("participant 不可 consent。")
            if self.campaigns[current.campaign_id].status not in {
                CampaignStatus.FROZEN,
                CampaignStatus.RUNNING,
            }:
                raise InvalidTransition("campaign 当前不接受新 consent。")
            updated = replace(
                current,
                participant_status=ParticipantStatus.CONSENTED,
                consented_at=now,
            )
            self.participants[participant_id] = updated
            return updated

    def current_attempt(self, *, participant_id: str, now: datetime) -> Attempt | None:
        with self._lock:
            participant = self.participants[participant_id]
            if participant.consented_at is None or participant.withdrawn_at is not None:
                return None
            for task in self.tasks[participant.campaign_id]:
                key = (participant_id, task.task_id)
                attempt_id = self.attempt_by_participant_task.get(key)
                if attempt_id is None:
                    attempt = Attempt(
                        attempt_id=str(uuid.uuid4()),
                        participant_id=participant_id,
                        campaign_id=participant.campaign_id,
                        task=task,
                        status=AttemptStatus.ASSIGNED,
                        queued_at=None,
                        started_at=None,
                        provider_completed_at=None,
                        revealed_at=None,
                        completed_at=None,
                        safe_output=None,
                        output_sha256=None,
                        provider_latency_ms=None,
                        outcome=None,
                        error_code=None,
                    )
                    self.attempts[attempt.attempt_id] = attempt
                    self.attempt_by_participant_task[key] = attempt.attempt_id
                    return attempt
                attempt = self.attempts[attempt_id]
                if attempt.status not in {AttemptStatus.COMPLETED, AttemptStatus.EXCLUDED}:
                    return attempt
            return None

    def queue_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self._lock:
            participant = self.participants[participant_id]
            if participant.withdrawn_at is not None or participant.participant_status is ParticipantStatus.WITHDRAWN:
                raise InvalidTransition("participant 已撤回。")
            attempt = self.attempts[attempt_id]
            if attempt.participant_id != participant_id or attempt.status is not AttemptStatus.ASSIGNED:
                raise InvalidTransition("attempt 不可 queue。")
            campaign = self.campaigns[attempt.campaign_id]
            if campaign.status not in {CampaignStatus.FROZEN, CampaignStatus.RUNNING}:
                raise CampaignNotAvailable("campaign 已暂停或终止。")
            used = sum(
                value.campaign_id == campaign.campaign_id
                and (
                    value.status is AttemptStatus.QUEUED
                    or value.started_at is not None
                )
                for value in self.attempts.values()
            )
            if used >= campaign.max_provider_runs:
                raise ProviderBudgetExhausted("Provider run budget 已用尽。")
            queued = replace(attempt, status=AttemptStatus.QUEUED, queued_at=now)
            self.attempts[attempt_id] = queued
            if campaign.status is CampaignStatus.FROZEN:
                self.campaigns[campaign.campaign_id] = replace(
                    campaign, status=CampaignStatus.RUNNING
                )
            participant = self.participants[participant_id]
            if participant.participant_status is ParticipantStatus.CONSENTED:
                self.participants[participant_id] = replace(
                    participant, participant_status=ParticipantStatus.ACTIVE
                )
            return queued

    def claim_attempt(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
    ) -> Attempt | None:
        with self._lock:
            eligible = sorted(
                (
                    attempt
                    for attempt in self.attempts.values()
                    if self.participants[attempt.participant_id].withdrawn_at is None
                    and self.campaigns[attempt.campaign_id].status is CampaignStatus.RUNNING
                    and self.campaigns[attempt.campaign_id].execution_environment
                    == execution_environment
                    and self.campaigns[attempt.campaign_id].deployment_image_digest
                    == deployment_image_digest
                    and (
                    attempt.status is AttemptStatus.QUEUED
                    or (
                        attempt.status is AttemptStatus.RUNNING
                        and attempt.attempt_id in self.leases
                        and self.leases[attempt.attempt_id][1] <= now
                    )
                    )
                ),
                key=lambda item: item.queued_at or now,
            )
            if not eligible:
                return None
            current = eligible[0]
            if current.status is AttemptStatus.RUNNING:
                failed = replace(
                    current,
                    status=AttemptStatus.FAILED,
                    provider_completed_at=now,
                    error_code="provider_lease_expired",
                )
                self.attempts[current.attempt_id] = failed
                self.leases.pop(current.attempt_id, None)
                return None
            claimed = replace(current, status=AttemptStatus.RUNNING, started_at=now)
            self.attempts[current.attempt_id] = claimed
            self.leases[current.attempt_id] = (
                worker_id,
                now + timedelta(seconds=lease_seconds),
            )
            return claimed

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
        with self._lock:
            current = self._require_lease(attempt_id, worker_id, now)
            if self.participants[current.participant_id].withdrawn_at is not None:
                discarded = replace(
                    current,
                    status=AttemptStatus.FAILED,
                    provider_completed_at=now,
                    safe_output=None,
                    output_sha256=None,
                    provider_latency_ms=None,
                    outcome=None,
                    error_code="participant_withdrew",
                    model_call_count=None,
                    model_requested_tool_call_count=None,
                    backend_executed_tool_call_count=None,
                    completion_failure_source=None,
                )
                self.attempts[attempt_id] = discarded
                self.leases.pop(attempt_id, None)
                return discarded
            completed = replace(
                current,
                status=AttemptStatus.SUCCEEDED,
                provider_completed_at=now,
                safe_output=safe_output,
                output_sha256=output_sha256,
                provider_latency_ms=result.provider_latency_ms,
                outcome=result.outcome,
                error_code=None,
                model_call_count=result.model_call_count,
                model_requested_tool_call_count=result.model_requested_tool_call_count,
                backend_executed_tool_call_count=result.backend_executed_tool_call_count,
                completion_failure_source=result.completion_failure_source,
            )
            self.attempts[attempt_id] = completed
            self.leases.pop(attempt_id, None)
            return completed

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
        with self._lock:
            current = self._require_lease(attempt_id, worker_id, now)
            participant = self.participants[current.participant_id]
            if participant.withdrawn_at is not None or participant.participant_status is ParticipantStatus.WITHDRAWN:
                failed = replace(
                    current,
                    status=AttemptStatus.FAILED,
                    provider_completed_at=now,
                    safe_output=None,
                    output_sha256=None,
                    provider_latency_ms=None,
                    outcome=None,
                    error_code="participant_withdrew",
                    model_call_count=None,
                    model_requested_tool_call_count=None,
                    backend_executed_tool_call_count=None,
                    completion_failure_source=None,
                )
                self.attempts[attempt_id] = failed
                self.leases.pop(attempt_id, None)
                return failed
            failed = replace(
                current,
                status=AttemptStatus.WITHHELD if withheld else AttemptStatus.FAILED,
                provider_completed_at=now,
                safe_output=None,
                output_sha256=None,
                provider_latency_ms=(
                    telemetry.provider_latency_ms if telemetry is not None else None
                ),
                outcome=telemetry.outcome if telemetry is not None else None,
                error_code=error_code,
                model_call_count=(
                    telemetry.model_call_count if telemetry is not None else None
                ),
                model_requested_tool_call_count=(
                    telemetry.model_requested_tool_call_count
                    if telemetry is not None
                    else None
                ),
                backend_executed_tool_call_count=(
                    telemetry.backend_executed_tool_call_count
                    if telemetry is not None
                    else None
                ),
                completion_failure_source=(
                    telemetry.completion_failure_source
                    if telemetry is not None
                    else None
                ),
            )
            self.attempts[attempt_id] = failed
            self.leases.pop(attempt_id, None)
            if withheld:
                self._pause_for_incident(
                    current.campaign_id,
                    current.participant_id,
                    current.attempt_id,
                    "secret_or_personal_data_exposure",
                    now,
                )
            return failed

    def reveal_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self._lock:
            participant = self.participants[participant_id]
            if participant.withdrawn_at is not None or participant.participant_status is ParticipantStatus.WITHDRAWN:
                raise InvalidTransition("participant 已撤回。")
            current = self.attempts[attempt_id]
            if current.participant_id != participant_id or current.status not in {
                AttemptStatus.SUCCEEDED,
                AttemptStatus.COMPLETED,
            }:
                raise InvalidTransition("attempt 不可 reveal。")
            if current.revealed_at is not None:
                return current
            revealed = replace(current, revealed_at=now)
            self.attempts[attempt_id] = revealed
            return revealed

    def record_feedback(
        self,
        *,
        participant_id: str,
        attempt_id: str,
        feedback: Feedback,
        human_review_seconds: int,
        now: datetime,
    ) -> Attempt:
        with self._lock:
            participant = self.participants[participant_id]
            if participant.withdrawn_at is not None or participant.participant_status is ParticipantStatus.WITHDRAWN:
                raise InvalidTransition("participant 已撤回。")
            current = self.attempts[attempt_id]
            if (
                current.participant_id != participant_id
                or current.status is not AttemptStatus.SUCCEEDED
                or current.revealed_at is None
                or attempt_id in self.feedback
            ):
                raise InvalidTransition("feedback 不可记录或已存在。")
            self.feedback[attempt_id] = (feedback, human_review_seconds)
            completed = replace(
                current,
                status=AttemptStatus.COMPLETED,
                completed_at=now,
            )
            self.attempts[attempt_id] = completed
            if feedback.safety_concern:
                self._pause_for_incident(
                    current.campaign_id,
                    participant_id,
                    attempt_id,
                    "user_reported_safety_concern",
                    now,
                )
            terminal = {AttemptStatus.COMPLETED, AttemptStatus.EXCLUDED}
            all_done = all(
                (attempt_key := self.attempt_by_participant_task.get((participant_id, task.task_id)))
                is not None
                and self.attempts[attempt_key].status in terminal
                for task in self.tasks[current.campaign_id]
            )
            if all_done:
                self.participants[participant_id] = replace(
                    participant, participant_status=ParticipantStatus.COMPLETED
                )
            return completed

    def exclude_failed_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self._lock:
            participant = self.participants[participant_id]
            if participant.withdrawn_at is not None or participant.participant_status is ParticipantStatus.WITHDRAWN:
                raise InvalidTransition("participant 已撤回。")
            current = self.attempts[attempt_id]
            if current.participant_id != participant_id or current.status not in {
                AttemptStatus.FAILED,
                AttemptStatus.WITHHELD,
            }:
                raise InvalidTransition("只有失败或被拦截的当前任务可以排除。")
            excluded = replace(current, status=AttemptStatus.EXCLUDED, completed_at=now)
            self.attempts[attempt_id] = excluded
            terminal = {
                AttemptStatus.COMPLETED,
                AttemptStatus.EXCLUDED,
            }
            all_done = all(
                (attempt_key := self.attempt_by_participant_task.get((participant_id, task.task_id)))
                is not None
                and self.attempts[attempt_key].status in terminal
                for task in self.tasks[current.campaign_id]
            )
            if all_done:
                self.participants[participant_id] = replace(
                    participant, participant_status=ParticipantStatus.COMPLETED
                )
            return excluded

    def skip_attempt(
        self, *, participant_id: str, attempt_id: str, now: datetime
    ) -> Attempt:
        with self._lock:
            participant = self.participants[participant_id]
            if participant.withdrawn_at is not None or participant.participant_status is ParticipantStatus.WITHDRAWN:
                raise InvalidTransition("participant 已撤回。")
            current = self.attempts[attempt_id]
            if current.participant_id != participant_id or current.status not in {
                AttemptStatus.ASSIGNED,
                AttemptStatus.SUCCEEDED,
            }:
                raise InvalidTransition("当前任务不可跳过。")
            skipped = replace(
                current,
                status=AttemptStatus.EXCLUDED,
                completed_at=now,
                error_code="participant_skipped",
            )
            self.attempts[attempt_id] = skipped
            terminal = {AttemptStatus.COMPLETED, AttemptStatus.EXCLUDED}
            all_done = all(
                (attempt_key := self.attempt_by_participant_task.get((participant_id, task.task_id)))
                is not None
                and self.attempts[attempt_key].status in terminal
                for task in self.tasks[current.campaign_id]
            )
            if all_done:
                self.participants[participant_id] = replace(
                    participant, participant_status=ParticipantStatus.COMPLETED
                )
            return skipped

    def withdraw(self, *, participant_id: str, now: datetime) -> None:
        with self._lock:
            current = self.participants[participant_id]
            self.participants[participant_id] = replace(
                current,
                participant_status=ParticipantStatus.WITHDRAWN,
                withdrawn_at=now,
            )
            for attempt_id, attempt in tuple(self.attempts.items()):
                if attempt.participant_id == participant_id and attempt.status in {
                    AttemptStatus.ASSIGNED,
                    AttemptStatus.QUEUED,
                }:
                    self.attempts[attempt_id] = replace(
                        attempt,
                        status=AttemptStatus.FAILED,
                        error_code="participant_withdrew",
                    )

    def consume_rate_limit(
        self,
        *,
        principal_key: str,
        route_key: str,
        window_seconds: int,
        limit: int,
        now: datetime,
    ) -> bool:
        window = int(now.timestamp()) // window_seconds
        key = (principal_key, route_key, window)
        with self._lock:
            count = self.rate_buckets.get(key, 0) + 1
            self.rate_buckets[key] = count
            return count <= limit

    def campaign_summary_data(self, campaign_id: str) -> Mapping[str, Any]:
        participants = [
            item for item in self.participants.values() if item.campaign_id == campaign_id
        ]
        tasks = self.tasks[campaign_id]
        attempts = [
            item for item in self.attempts.values() if item.campaign_id == campaign_id
        ]
        qualifying_participants = {
            item.participant_id
            for item in participants
            if item.consented_at is not None
            and item.withdrawn_at is None
            and item.participant_status is not ParticipantStatus.WITHDRAWN
        }
        feedback_rows = [
            (self.attempts[attempt_id], value[0], value[1])
            for attempt_id, value in self.feedback.items()
            if self.attempts[attempt_id].campaign_id == campaign_id
            and self.attempts[attempt_id].participant_id in qualifying_participants
        ]
        completed_by_participant = [
            sum(attempt.participant_id == participant.participant_id for attempt, _, _ in feedback_rows)
            for participant in participants
            if participant.participant_id in qualifying_participants
        ]
        incidents = [item for item in self.incidents if item["campaign_id"] == campaign_id]
        qualifying_attempts = [
            attempt
            for attempt in attempts
            if attempt.participant_id in qualifying_participants
        ]
        provider_execution_telemetry = summarize_provider_execution_telemetry(
            [
                {
                    "started_at": attempt.started_at,
                    "status": attempt.status.value,
                    "error_code": attempt.error_code,
                    "outcome": attempt.outcome,
                    "model_call_count": attempt.model_call_count,
                    "model_requested_tool_call_count": attempt.model_requested_tool_call_count,
                    "backend_executed_tool_call_count": attempt.backend_executed_tool_call_count,
                    "completion_failure_source": attempt.completion_failure_source,
                }
                for attempt in qualifying_attempts
            ]
        )
        provider_execution_telemetry["append_only_event_binding_status"] = (
            "not_applicable"
        )
        return {
            "task_count": len(tasks),
            "eligible_participant_count": len(qualifying_participants),
            "started_participant_count": sum(
                p.participant_status
                in {ParticipantStatus.ACTIVE, ParticipantStatus.COMPLETED}
                for p in participants
            ),
            "completed_participant_count": sum(
                p.participant_status is ParticipantStatus.COMPLETED for p in participants
                if p.withdrawn_at is None
            ),
            "withdrawn_participant_count": sum(
                p.participant_status is ParticipantStatus.WITHDRAWN for p in participants
            ),
            "planned_interaction_count": len(qualifying_participants) * len(tasks),
            "started_interaction_count": sum(
                a.participant_id in qualifying_participants
                and a.status is not AttemptStatus.ASSIGNED
                for a in attempts
            ),
            "answer_displayed_count": sum(
                a.participant_id in qualifying_participants and a.revealed_at is not None
                for a in attempts
            ),
            "feedback_completed_count": len(feedback_rows),
            "technical_failure_count": sum(
                a.participant_id in qualifying_participants
                and a.status
                in {AttemptStatus.FAILED, AttemptStatus.WITHHELD, AttemptStatus.EXCLUDED}
                and a.error_code is not None
                and a.error_code
                not in {"participant_skipped", "pilot_output_safety_filter"}
                for a in attempts
            ),
            "provider_execution_telemetry": provider_execution_telemetry,
            "telemetry_integrity_valid": True,
            "participant_projection_integrity_valid": True,
            "participant_projection_binding_status": "not_applicable",
            "dataset_count": len(
                {attempt.task.dataset_id for attempt, _, _ in feedback_rows}
            ),
            "scenario_count": len(
                {attempt.task.scenario for attempt, _, _ in feedback_rows}
            ),
            "dataset_counts": {
                dataset_id: sum(
                    attempt.task.dataset_id == dataset_id
                    for attempt, _, _ in feedback_rows
                )
                for dataset_id in sorted({task.dataset_id for task in tasks})
            },
            "scenario_counts": {
                scenario: sum(attempt.task.scenario == scenario for attempt, _, _ in feedback_rows)
                for scenario in sorted({task.scenario for task in tasks})
            },
            "completed_by_participant": completed_by_participant,
            "understandable_yes_count": sum(f.understandable for _, f, _ in feedback_rows),
            "useful_yes_count": sum(f.useful_for_next_step for _, f, _ in feedback_rows),
            "missing_information_yes_count": sum(f.missing_information for _, f, _ in feedback_rows),
            "obvious_problem_yes_count": sum(f.obvious_problem for _, f, _ in feedback_rows),
            "needs_expert_review_yes_count": sum(f.needs_expert_review for _, f, _ in feedback_rows),
            "clarification_useful_yes_count": sum(
                f.clarification_useful is True for _, f, _ in feedback_rows
            ),
            "clarification_feedback_count": sum(
                f.clarification_useful is not None for _, f, _ in feedback_rows
            ),
            "user_reported_concern_count": sum(f.safety_concern for _, f, _ in feedback_rows),
            "unresolved_incident_count": sum(i["status"] == "unresolved" for i in incidents),
            "confirmed_incident_count": sum(i["status"] == "confirmed" for i in incidents),
            "approval_bypass_count": sum(i["type"] == "approval_bypass" for i in incidents),
            "secret_or_personal_data_exposure_count": sum(
                i["type"] == "secret_or_personal_data_exposure" for i in incidents
            ),
            "provider_latencies_ms": [
                attempt.provider_latency_ms
                for attempt, _, _ in feedback_rows
                if attempt.provider_latency_ms is not None
            ],
            "human_review_seconds": [seconds for _, _, seconds in feedback_rows],
            "audit_chain_valid": True,
            "task_pack_integrity_valid": True,
        }

    def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        candidate_commitment_sha256: str,
        execution_environment: str,
        deployment_image_digest: str | None,
        now: datetime,
    ) -> None:
        self.worker_heartbeats[worker_id] = (
            candidate_commitment_sha256,
            execution_environment,
            deployment_image_digest,
            now,
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
        return any(
            commitment == candidate_commitment_sha256
            and environment == execution_environment
            and image_digest == deployment_image_digest
            and (now - last_seen).total_seconds() <= max_age_seconds
            for commitment, environment, image_digest, last_seen in self.worker_heartbeats.values()
        )

    def list_incidents(self, campaign_id: str) -> Sequence[Mapping[str, Any]]:
        return tuple(
            {
                "incident_id": item["incident_id"],
                "incident_type": item["type"],
                "status": item["status"],
                "created_at": item["created_at"],
            }
            for item in self.incidents
            if item["campaign_id"] == campaign_id
        )

    def _require_lease(self, attempt_id: str, worker_id: str, now: datetime) -> Attempt:
        current = self.attempts[attempt_id]
        lease = self.leases.get(attempt_id)
        if (
            current.status is not AttemptStatus.RUNNING
            or lease is None
            or lease[0] != worker_id
            or lease[1] <= now
        ):
            raise InvalidTransition("attempt lease 已失效。")
        return current

    def _pause_for_incident(
        self,
        campaign_id: str,
        participant_id: str,
        attempt_id: str,
        incident_type: str,
        now: datetime,
    ) -> None:
        campaign = self.campaigns[campaign_id]
        self.campaigns[campaign_id] = replace(campaign, status=CampaignStatus.PAUSED)
        self.incidents.append(
            {
                "incident_id": str(uuid.uuid4()),
                "campaign_id": campaign_id,
                "participant_id": participant_id,
                "attempt_id": attempt_id,
                "type": incident_type,
                "status": "unresolved",
                "created_at": now,
            }
        )
