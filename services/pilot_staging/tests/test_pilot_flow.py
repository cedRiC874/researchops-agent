from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pilot_staging.api import PilotContainer, create_app
from pilot_staging.application import (
    PilotApplication,
    PilotWorker,
    task_pack_commitment_sha256,
)
from pilot_staging.domain import (
    LOCKED_CANDIDATE_COMMITMENT,
    CandidateResult,
    CampaignStatus,
    Feedback,
    ProhibitedDataDetected,
    ProviderUnavailable,
)
from pilot_staging.memory import InMemoryPilotStore, StaticDatasetCatalog


ADMIN_TOKEN = "admin-token-with-more-than-24-characters"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
DATASETS = (
    "palmer_penguins_v0_1_0",
    "uci_parkinsons_telemonitoring_189",
    "uci_heart_disease_cleveland_45",
)
CONSENT_DOCUMENT = "# Test consent\nAll pilot terms shown here."
COMMITMENTS = {
    "protocol_sha256": "1" * 64,
    "consent_sha256": hashlib.sha256(CONSENT_DOCUMENT.encode("utf-8")).hexdigest(),
    "feedback_schema_sha256": "4" * 64,
    "dataset_manifest_sha256": "3" * 64,
}


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeExecutor:
    def __init__(
        self,
        output: str = "## English\nSafe aggregate answer.\n\n## 中文\n安全的聚合回答。",
        *,
        forced_outcome: str | None = None,
        error_code: str | None = None,
        model_call_count: int | None = 1,
        model_requested_tool_call_count: int | None = 1,
        backend_executed_tool_call_count: int = 1,
    ) -> None:
        self.output = output
        self.forced_outcome = forced_outcome
        self.error_code = error_code
        self.model_call_count = model_call_count
        self.model_requested_tool_call_count = model_requested_tool_call_count
        self.backend_executed_tool_call_count = backend_executed_tool_call_count
        self.calls = 0

    def execute(self, task):
        self.calls += 1
        return CandidateResult(
            final_output=self.output,
            outcome=self.forced_outcome or (
                "clarification_required"
                if task.clarification_expected
                else "completed"
            ),
            provider_latency_ms=1250,
            model_call_count=self.model_call_count,
            model_requested_tool_call_count=self.model_requested_tool_call_count,
            backend_executed_tool_call_count=self.backend_executed_tool_call_count,
            error_code=self.error_code,
        )


class RaisingExecutor:
    def execute(self, task):
        raise RuntimeError("SECRET_RAW_PROVIDER_BODY must never be persisted")


class MalformedExecutor:
    def execute(self, task):
        return None


class BlockingExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, task):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("blocking test timed out")
        return CandidateResult(
            final_output="## English\nSafe aggregate answer.\n\n## 中文\n安全聚合回答。",
            outcome="completed",
            provider_latency_ms=900,
            model_call_count=2,
            model_requested_tool_call_count=1,
            backend_executed_tool_call_count=1,
        )


def campaign_payload(*, target: int = 3, budget: int = 18):
    rows = [
        ("002", DATASETS[0], "standard_analysis", False),
        ("016", DATASETS[1], "clarification_required", True),
        ("030", DATASETS[2], "safe_refusal", False),
        ("018", DATASETS[1], "unauthorized_resource", False),
        ("032", DATASETS[2], "prompt_injection", False),
        ("010", DATASETS[0], "approval_pause", False),
    ]
    tasks = []
    for number, dataset_id, scenario, clarification in rows:
        context = {"dataset_id": dataset_id}
        if scenario == "approval_pause":
            context.update(bundle_id="palmer_summary", release_name="palmer-public-regression")
        tasks.append(
            {
                "task_id": f"PILOT-TASK-{number}",
                "source_task_id": f"V2-PUB-{number}",
                "dataset_id": dataset_id,
                "scenario": scenario,
                "prompt_en": f"Prepared public task {number}.",
                "prompt_zh": f"准备好的公开任务 {number}。",
                "context": context,
                "clarification_expected": clarification,
            }
        )
    return {
        "title": "External researcher pilot 01",
        **COMMITMENTS,
        "deployment_git_sha": None,
        "deployment_image_digest": None,
        "candidate_commitment_sha256": LOCKED_CANDIDATE_COMMITMENT,
        "provider": {
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "transport_id": "openai_compatible_responses",
        },
        "target_participants": target,
        "max_provider_runs": budget,
        "tasks": tasks,
    }


def supervised_test_task_pack_sha256() -> str:
    return task_pack_commitment_sha256(
        campaign_payload(target=2, budget=12)["tasks"]
    )


def consent_payload():
    return {
        "adult_and_voluntary": True,
        "experimental_system_understood": True,
        "public_data_only": True,
        "provider_transfer_understood": True,
        "pseudonymous_recording_agreed": True,
        "withdrawal_understood": True,
        "external_researcher_eligible": True,
    }


def feedback_payload(*, clarification: bool = False):
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


def build_system(
    *,
    online: bool = True,
    output: str | None = None,
    forced_outcome: str | None = None,
    error_code: str | None = None,
    model_call_count: int | None = 1,
    model_requested_tool_call_count: int | None = 1,
    backend_executed_tool_call_count: int = 1,
):
    clock = Clock()
    store = InMemoryPilotStore()
    app = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        provider_execution_enabled=online,
        clock=clock,
    )
    executor = FakeExecutor(
        output or "## English\nSafe answer.\n\n## 中文\n安全回答。",
        forced_outcome=forced_outcome,
        error_code=error_code,
        model_call_count=model_call_count,
        model_requested_tool_call_count=model_requested_tool_call_count,
        backend_executed_tool_call_count=backend_executed_tool_call_count,
    )
    worker = PilotWorker(
        store=store,
        executor=executor,
        worker_id="worker-test",
        clock=clock,
    )
    web = create_app(
        PilotContainer(
            application=app,
            admin_token=ADMIN_TOKEN,
            allowed_hosts=("testserver",),
            secure_cookies=False,
            ready_checks=(store.healthcheck,),
        )
    )
    return app, store, worker, executor, clock, TestClient(web)


def create_frozen_campaign(application: PilotApplication) -> str:
    campaign = application.create_campaign(campaign_payload())
    application.freeze_campaign(campaign["campaign_id"])
    return campaign["campaign_id"]


def exchange_and_consent(application: PilotApplication, campaign_id: str):
    invite = application.create_invite(campaign_id)
    auth = application.exchange_invite(invite["invite_token"])
    session = application.authenticate(
        auth["session_token"], csrf_token=auth["csrf_token"]
    )
    application.record_consent(session, consent_payload())
    return auth, application.authenticate(
        auth["session_token"], csrf_token=auth["csrf_token"]
    )


def test_application_flow_is_one_run_and_server_timed() -> None:
    app, store, worker, executor, clock, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    auth, session = exchange_and_consent(app, campaign_id)
    state = app.state(session)
    attempt_id = state["attempt"]["attempt_id"]
    queued = app.reveal(session, attempt_id)
    assert queued["status"] == "pending"
    assert app.reveal(session, attempt_id)["status"] == "pending"
    assert executor.calls == 0
    completed = worker.process_one()
    assert completed is not None
    assert executor.calls == 1
    revealed = app.reveal(session, attempt_id)
    assert revealed["attempt"]["agent_output"].startswith("## English")
    assert revealed["attempt"]["provider_latency_ms"] == 1250
    clock.advance(47)
    result = app.record_feedback(session, attempt_id, feedback_payload())
    assert result["status"] == "completed"
    assert store.feedback[attempt_id][1] == 47
    assert executor.calls == 1
    assert result["next"]["attempt"]["sequence"] == 2
    assert "agent_output" not in result["next"]["attempt"]


def test_invite_is_one_time_and_consent_is_required() -> None:
    app, _, _, _, _, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    invite = app.create_invite(campaign_id)
    first = app.exchange_invite(invite["invite_token"])
    with pytest.raises(Exception) as repeated:
        app.exchange_invite(invite["invite_token"])
    assert getattr(repeated.value, "code", None) == "invite_invalid"
    session = app.authenticate(first["session_token"], csrf_token=first["csrf_token"])
    with pytest.raises(Exception) as no_consent:
        app.reveal(session, "not-an-attempt")
    assert getattr(no_consent.value, "code", None) == "consent_required"


def test_participant_cap_is_lifetime_even_after_projection_row_is_removed() -> None:
    app, store, _, _, _, _ = build_system()
    first_campaign = app.create_campaign(campaign_payload(target=3, budget=18))[
        "campaign_id"
    ]
    app.freeze_campaign(first_campaign)
    auth_rows = [
        app.exchange_invite(app.create_invite(first_campaign)["invite_token"])
        for _ in range(3)
    ]
    first_auth = auth_rows[0]
    participant_id = next(
        participant_id
        for participant_id, session in store.participants.items()
        if session.session_instance_id == first_auth["session_instance_id"]
    )
    session_digest = next(
        digest for digest, value in store.sessions.items() if value == participant_id
    )
    del store.sessions[session_digest]
    del store.participants[participant_id]

    replacement_invite = app.create_invite(first_campaign)
    with pytest.raises(Exception) as cap_reached:
        app.exchange_invite(replacement_invite["invite_token"])
    assert getattr(cap_reached.value, "code", None) == "campaign_not_available"
    assert store.redeemed_invite_counts[first_campaign] == 3

    new_campaign = app.create_campaign(campaign_payload(target=3, budget=18))[
        "campaign_id"
    ]
    app.freeze_campaign(new_campaign)
    replacement = app.exchange_invite(
        app.create_invite(new_campaign)["invite_token"]
    )
    assert replacement["campaign"]["execution_environment"] == "local"


def test_online_kill_switch_blocks_before_queue() -> None:
    app, store, _, _, _, _ = build_system(online=False)
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    with pytest.raises(ProviderUnavailable):
        app.reveal(session, attempt_id)
    assert store.attempts[attempt_id].status.value == "assigned"


def test_online_mode_requires_recent_worker_preflight() -> None:
    app, store, _, _, _, _ = build_system(online=True)
    store.worker_heartbeats.clear()
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    with pytest.raises(ProviderUnavailable):
        app.reveal(session, attempt_id)
    assert store.attempts[attempt_id].status.value == "assigned"


def test_output_dlp_withholds_and_pauses_campaign() -> None:
    app, store, worker, _, _, _ = build_system(output="Contact researcher@example.org")
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    withheld = worker.process_one()
    assert withheld is not None and withheld.status.value == "withheld"
    assert store.campaigns[campaign_id].status is CampaignStatus.PAUSED
    assert withheld.safe_output is None
    assert withheld.output_sha256 is None
    assert withheld.provider_latency_ms == 1250
    assert withheld.model_call_count == 1
    assert withheld.model_requested_tool_call_count == 1
    assert withheld.backend_executed_tool_call_count == 1
    telemetry = app.summary(campaign_id)["provider_execution_telemetry"]
    assert telemetry["safety_withheld_attempt_count"] == 1
    assert app.summary(campaign_id)["interactions"]["technical_failure_count"] == 0
    assert telemetry["executor_model_call_count"]["observed_sum"] == 1
    assert telemetry["telemetry_coverage_status"] == "complete"
    serialized = json.dumps(
        {
            "attempt": repr(withheld),
            "incidents": store.incidents,
            "summary": app.summary(campaign_id),
        },
        default=str,
    )
    assert "researcher@example.org" not in serialized
    assert store.incidents[0]["status"] == "unresolved"
    incidents = app.list_incidents(campaign_id)
    assert len(incidents["incidents"]) == 1
    assert "participant_id" not in incidents["incidents"][0]
    with pytest.raises(Exception) as error:
        app.reveal(session, attempt_id)
    assert getattr(error.value, "code", None) == "output_withheld"


def test_summary_is_fail_closed_and_never_claims_correctness() -> None:
    app, _, _, _, _, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    summary = app.summary(campaign_id)
    assert summary["external_validation_claim_allowed"] is False
    assert "pilot_not_complete" in summary["external_validation_claim_reason_codes"]
    assert "eligible_participant_count_below_minimum" in summary["external_validation_claim_reason_codes"]
    assert summary["professional_correctness"]["claim_allowed"] is False
    assert summary["machine_contract"]["model_planning_accuracy_claim_allowed"] is False
    telemetry = summary["provider_execution_telemetry"]
    assert telemetry["worker_started_attempt_count"] == 0
    assert telemetry["telemetry_coverage_status"] == "no_attempts"
    assert telemetry["executor_model_call_count"]["observed_sum"] is None
    assert telemetry["executor_model_call_count"]["coverage_rate"] is None
    assert telemetry["upstream_api_request_total_claim_allowed"] is False
    assert summary["timing"]["latency_is_sla"] is False


def test_api_cookie_csrf_isolation_and_markdown_ui() -> None:
    app, _, worker, _, _, client = build_system()
    response = client.get("/pilot")
    assert response.status_code == 200
    assert "type=\"password\"" in response.text
    assert 'id="api-key"' not in response.text
    script = client.get("/pilot/app.js").text
    assert "innerHTML=markdown" in script
    assert "researchops_pilot_csrf" in script
    assert "pollAttempt" in script
    assert "监督式预试运行" in script

    created = client.post("/v1/admin/campaigns", headers=ADMIN, json=campaign_payload())
    assert created.status_code == 200
    campaign_id = created.json()["campaign_id"]
    assert client.post(
        f"/v1/admin/campaigns/{campaign_id}/freeze", headers=ADMIN
    ).status_code == 200

    invite1 = client.post(
        f"/v1/admin/campaigns/{campaign_id}/invites", headers=ADMIN, json={}
    ).json()
    invite2 = client.post(
        f"/v1/admin/campaigns/{campaign_id}/invites", headers=ADMIN, json={}
    ).json()
    user1, user2 = TestClient(client.app), TestClient(client.app)
    auth1 = user1.post("/v1/pilot/auth/session", json={"invite_token": invite1["invite_token"]})
    auth2 = user2.post("/v1/pilot/auth/session", json={"invite_token": invite2["invite_token"]})
    csrf1, csrf2 = auth1.json()["csrf_token"], auth2.json()["csrf_token"]
    assert auth1.json()["consent_document"] == CONSENT_DOCUMENT
    assert "HttpOnly" in auth1.headers["set-cookie"]
    assert user1.post("/v1/pilot/consent", json=consent_payload()).status_code == 401
    assert user1.post(
        "/v1/pilot/consent", headers={"X-CSRF-Token": csrf1}, json=consent_payload()
    ).status_code == 200
    assert user2.post(
        "/v1/pilot/consent", headers={"X-CSRF-Token": csrf2}, json=consent_payload()
    ).status_code == 200
    attempt1 = user1.get("/v1/pilot/state").json()["attempt"]["attempt_id"]
    attempt2 = user2.get("/v1/pilot/state").json()["attempt"]["attempt_id"]
    assert attempt1 != attempt2
    cross = user2.post(
        f"/v1/pilot/attempts/{attempt1}/reveal",
        headers={"X-CSRF-Token": csrf2},
        json={},
    )
    assert cross.status_code == 404
    queued = user1.post(
        f"/v1/pilot/attempts/{attempt1}/reveal",
        headers={"X-CSRF-Token": csrf1},
        json={},
    )
    assert queued.status_code == 200 and queued.json()["status"] == "pending"
    worker.process_one()
    revealed = user1.post(
        f"/v1/pilot/attempts/{attempt1}/reveal",
        headers={"X-CSRF-Token": csrf1},
        json={},
    )
    assert revealed.json()["status"] == "revealed"
    assert "agent_output" not in user2.get("/v1/pilot/state").text


def test_streamed_request_body_limit_is_enforced_without_length_header() -> None:
    _, _, _, _, _, client = build_system()

    def oversized():
        for _ in range(70):
            yield b"x" * 1024

    response = client.post(
        "/v1/pilot/auth/session",
        content=oversized(),
        headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error_code"] == "request_too_large"


def test_invalid_invites_share_a_global_rate_bucket() -> None:
    _, _, _, _, _, client = build_system()
    statuses = []
    for index in range(31):
        token = f"invalid-invite-token-{index:03d}-xxxxxxxxxxxxxxxx"
        statuses.append(
            client.post(
                "/v1/pilot/auth/session", json={"invite_token": token}
            ).status_code
        )
    assert statuses[:30] == [401] * 30
    assert statuses[30] == 429


def test_feedback_clarification_field_and_notes_contract() -> None:
    app, _, worker, _, _, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    first = app.state(session)["attempt"]
    app.reveal(session, first["attempt_id"])
    worker.process_one()
    app.reveal(session, first["attempt_id"])
    bad = feedback_payload()
    bad["clarification_useful"] = True
    with pytest.raises(Exception):
        app.record_feedback(session, first["attempt_id"], bad)
    low = feedback_payload()
    low["confidence"] = "low"
    with pytest.raises(Exception):
        app.record_feedback(session, first["attempt_id"], low)


def test_withdraw_revokes_session_and_blocks_queued_work() -> None:
    app, store, worker, executor, _, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    auth, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    result = app.withdraw(session, auth["session_token"])
    assert result["further_provider_calls_blocked"] is True
    assert worker.process_one() is None
    assert executor.calls == 0
    with pytest.raises(Exception) as expired:
        app.authenticate(auth["session_token"])
    assert getattr(expired.value, "code", None) == "session_expired"
    assert store.attempts[attempt_id].error_code == "participant_withdrew"


def test_withdrawn_feedback_never_qualifies_for_claim_gate() -> None:
    app, _, worker, _, _, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    auth, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    worker.process_one()
    app.reveal(session, attempt_id)
    app.record_feedback(session, attempt_id, feedback_payload())
    app.withdraw(session, auth["session_token"])
    summary = app.summary(campaign_id)
    assert summary["cohort"]["eligible_external_participant_count"] == 0
    assert summary["cohort"]["withdrawn_participant_count"] == 1
    assert summary["interactions"]["feedback_completed_count"] == 0
    telemetry = summary["provider_execution_telemetry"]
    assert telemetry["withdrawn_participant_attempts_included"] is False
    assert telemetry["worker_started_attempt_count"] == 0
    assert telemetry["executor_model_call_count"]["observed_sum"] is None
    assert summary["external_validation_claim_allowed"] is False


def test_withdrawal_during_execution_discards_late_telemetry() -> None:
    app, store, _, _, clock, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    auth, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    executor = BlockingExecutor()
    worker = PilotWorker(
        store=store,
        executor=executor,
        worker_id="worker-late-withdrawal",
        clock=clock,
    )
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(worker.process_one()))
    thread.start()
    assert executor.started.wait(timeout=2)
    app.withdraw(session, auth["session_token"])
    executor.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(result) == 1
    discarded = store.attempts[attempt_id]
    assert discarded.error_code == "participant_withdrew"
    assert discarded.safe_output is None
    assert discarded.output_sha256 is None
    assert discarded.provider_latency_ms is None
    assert discarded.outcome is None
    assert discarded.model_call_count is None
    assert discarded.model_requested_tool_call_count is None
    assert discarded.backend_executed_tool_call_count is None
    telemetry = app.summary(campaign_id)["provider_execution_telemetry"]
    assert telemetry["worker_started_attempt_count"] == 0


def test_withdrawal_wins_over_late_feedback_and_skip() -> None:
    app, store, worker, _, clock, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    auth, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    worker.process_one()
    app.reveal(session, attempt_id)
    app.withdraw(session, auth["session_token"])

    late_feedback = Feedback(
        understandable=True,
        useful_for_next_step=True,
        confidence="high",
        needs_expert_review=False,
        obvious_problem=False,
        missing_information=False,
        safety_concern=False,
        clarification_useful=None,
        notes="",
    )
    with pytest.raises(Exception):
        store.record_feedback(
            participant_id=session.participant_id,
            attempt_id=attempt_id,
            feedback=late_feedback,
            human_review_seconds=1,
            now=clock(),
        )
    with pytest.raises(Exception):
        store.skip_attempt(
            participant_id=session.participant_id,
            attempt_id=attempt_id,
            now=clock(),
        )
    participant = store.participants[session.participant_id]
    assert participant.participant_status.value == "withdrawn"
    assert participant.withdrawn_at is not None
    assert app.summary(campaign_id)["interactions"]["feedback_completed_count"] == 0


def test_actual_outcome_controls_clarification_feedback() -> None:
    app, _, worker, _, _, _ = build_system(forced_outcome="clarification_required")
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    worker.process_one()
    revealed = app.reveal(session, attempt_id)
    assert revealed["attempt"]["clarification_feedback_required"] is True
    with pytest.raises(Exception):
        app.record_feedback(session, attempt_id, feedback_payload())
    app.record_feedback(session, attempt_id, feedback_payload(clarification=True))


def test_provider_failure_can_be_excluded_without_rerun() -> None:
    app, store, worker, executor, _, _ = build_system(
        output="", error_code="provider_timeout"
    )
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    failed = worker.process_one()
    assert failed is not None and failed.status.value == "failed"
    assert failed.safe_output is None
    assert failed.output_sha256 is None
    assert failed.provider_latency_ms == 1250
    assert failed.outcome == "completed"
    assert failed.model_call_count == 1
    assert failed.model_requested_tool_call_count == 1
    assert failed.backend_executed_tool_call_count == 1
    excluded = app.exclude_failed_attempt(session, attempt_id)
    assert excluded["status"] == "excluded"
    assert excluded["next"]["attempt"]["sequence"] == 2
    assert executor.calls == 1
    assert store.campaign_summary_data(campaign_id)["technical_failure_count"] == 1
    persisted = store.attempts[attempt_id]
    assert persisted.model_call_count == 1
    telemetry = app.summary(campaign_id)["provider_execution_telemetry"]
    assert telemetry["worker_started_attempt_count"] == 1
    assert telemetry["technical_failure_attempt_count"] == 1
    assert telemetry["failure_reason_counts"] == [
        {"reason_code": "provider_timeout", "attempt_count": 1}
    ]
    for name in (
        "executor_model_call_count",
        "model_requested_tool_call_count",
        "backend_executed_tool_call_count",
    ):
        assert telemetry[name] == {
            "observed_sum": 1,
            "observed_attempt_count": 1,
            "unknown_attempt_count": 0,
            "coverage_rate": 1.0,
        }


def test_exception_and_explicit_zero_are_not_conflated() -> None:
    app, store, _, _, clock, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)
    attempt_id = app.state(session)["attempt"]["attempt_id"]
    app.reveal(session, attempt_id)
    raising_worker = PilotWorker(
        store=store,
        executor=RaisingExecutor(),
        worker_id="worker-raising",
        clock=clock,
    )
    failed = raising_worker.process_one()
    assert failed is not None and failed.error_code == "provider_failed"
    assert failed.provider_latency_ms is None
    assert failed.outcome is None
    assert failed.model_call_count is None
    assert failed.model_requested_tool_call_count is None
    assert failed.backend_executed_tool_call_count is None
    assert "SECRET_RAW_PROVIDER_BODY" not in repr(failed)
    telemetry = app.summary(campaign_id)["provider_execution_telemetry"]
    assert telemetry["telemetry_coverage_status"] == "partial"
    for name in (
        "executor_model_call_count",
        "model_requested_tool_call_count",
        "backend_executed_tool_call_count",
    ):
        assert telemetry[name]["observed_sum"] is None
        assert telemetry[name]["observed_attempt_count"] == 0
        assert telemetry[name]["unknown_attempt_count"] == 1
        assert telemetry[name]["coverage_rate"] == 0.0

    app2, _, worker2, _, _, _ = build_system(
        output="",
        error_code="provider_output_incomplete",
        model_call_count=None,
        model_requested_tool_call_count=None,
        backend_executed_tool_call_count=0,
    )
    campaign2 = create_frozen_campaign(app2)
    _, session2 = exchange_and_consent(app2, campaign2)
    second_id = app2.state(session2)["attempt"]["attempt_id"]
    app2.reveal(session2, second_id)
    partial = worker2.process_one()
    assert partial is not None
    assert partial.model_call_count is None
    assert partial.model_requested_tool_call_count is None
    assert partial.backend_executed_tool_call_count == 0
    partial_summary = app2.summary(campaign2)["provider_execution_telemetry"]
    assert partial_summary["backend_executed_tool_call_count"] == {
        "observed_sum": 0,
        "observed_attempt_count": 1,
        "unknown_attempt_count": 0,
        "coverage_rate": 1.0,
    }

    app3, _, worker3, _, _, _ = build_system(model_call_count=-1)
    campaign3 = create_frozen_campaign(app3)
    _, session3 = exchange_and_consent(app3, campaign3)
    third_id = app3.state(session3)["attempt"]["attempt_id"]
    app3.reveal(session3, third_id)
    invalid_success = worker3.process_one()
    assert invalid_success is not None
    assert invalid_success.status.value == "failed"
    assert invalid_success.error_code == "provider_failed"
    assert invalid_success.model_call_count is None

    app4, store4, worker4, _, _, _ = build_system(
        output="Contact second@example.org", model_call_count=-1
    )
    campaign4 = create_frozen_campaign(app4)
    _, session4 = exchange_and_consent(app4, campaign4)
    fourth_id = app4.state(session4)["attempt"]["attempt_id"]
    app4.reveal(session4, fourth_id)
    invalid_dlp = worker4.process_one()
    assert invalid_dlp is not None
    assert invalid_dlp.status.value == "withheld"
    assert invalid_dlp.model_call_count is None
    assert store4.campaigns[campaign4].status is CampaignStatus.PAUSED

    app5, _, worker5, _, _, _ = build_system(
        output="", error_code="unsafe error SECRET_RAW_PROVIDER_BODY"
    )
    campaign5 = create_frozen_campaign(app5)
    _, session5 = exchange_and_consent(app5, campaign5)
    fifth_id = app5.state(session5)["attempt"]["attempt_id"]
    app5.reveal(session5, fifth_id)
    sanitized_error = worker5.process_one()
    assert sanitized_error is not None
    assert sanitized_error.error_code == "provider_failed"
    assert "SECRET_RAW_PROVIDER_BODY" not in repr(sanitized_error)

    app6, store6, _, _, clock6, _ = build_system()
    campaign6 = create_frozen_campaign(app6)
    _, session6 = exchange_and_consent(app6, campaign6)
    sixth_id = app6.state(session6)["attempt"]["attempt_id"]
    app6.reveal(session6, sixth_id)
    malformed_worker = PilotWorker(
        store=store6,
        executor=MalformedExecutor(),
        worker_id="worker-malformed-result",
        clock=clock6,
    )
    malformed = malformed_worker.process_one()
    assert malformed is not None
    assert malformed.error_code == "provider_failed"
    assert malformed.model_call_count is None


def test_participant_can_skip_before_or_after_reveal_without_a_rerun() -> None:
    app, store, worker, executor, _, _ = build_system()
    campaign_id = create_frozen_campaign(app)
    _, session = exchange_and_consent(app, campaign_id)

    first = app.state(session)["attempt"]
    skipped_before = app.skip_attempt(session, first["attempt_id"])
    assert skipped_before["status"] == "excluded"
    assert skipped_before["next"]["attempt"]["sequence"] == 2
    assert executor.calls == 0

    second = skipped_before["next"]["attempt"]
    app.reveal(session, second["attempt_id"])
    worker.process_one()
    app.reveal(session, second["attempt_id"])
    skipped_after = app.skip_attempt(session, second["attempt_id"])
    assert skipped_after["status"] == "excluded"
    assert skipped_after["next"]["attempt"]["sequence"] == 3
    assert executor.calls == 1

    raw = store.campaign_summary_data(campaign_id)
    assert raw["technical_failure_count"] == 0
    assert raw["feedback_completed_count"] == 0
    assert raw["answer_displayed_count"] == 1

    for _ in range(4):
        attempt = app.state(session)["attempt"]
        app.reveal(session, attempt["attempt_id"])
        worker.process_one()
        revealed = app.reveal(session, attempt["attempt_id"])["attempt"]
        app.record_feedback(
            session,
            attempt["attempt_id"],
            feedback_payload(
                clarification=revealed["clarification_feedback_required"]
            ),
        )
    assert app.state(session)["status"] == "complete"
    assert store.participants[session.participant_id].participant_status.value == "completed"


def test_paused_campaign_stops_other_queued_attempts() -> None:
    app, _, worker, executor, _, _ = build_system(output="Contact researcher@example.org")
    campaign_id = create_frozen_campaign(app)
    _, first = exchange_and_consent(app, campaign_id)
    _, second = exchange_and_consent(app, campaign_id)
    first_id = app.state(first)["attempt"]["attempt_id"]
    second_id = app.state(second)["attempt"]["attempt_id"]
    app.reveal(first, first_id)
    app.reveal(second, second_id)
    worker.process_one()
    assert executor.calls == 1
    assert worker.process_one() is None


def test_cohort_size_is_precommitted_to_three_to_five() -> None:
    app, _, _, _, _, _ = build_system()
    for invalid in (2, 6):
        with pytest.raises(Exception):
            app.create_campaign(campaign_payload(target=invalid, budget=max(18, invalid * 6)))


def test_supervised_campaign_hard_limits_six_tasks_and_exact_budget() -> None:
    clock = Clock()
    git_sha = "a" * 40
    image_digest = "sha256:" + "b" * 64
    application = PilotApplication(
        store=InMemoryPilotStore(),
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha=git_sha,
        deployment_image_digest=image_digest,
        supervised_task_pack_sha256=supervised_test_task_pack_sha256(),
        retention_schedule_confirmed=True,
        environment="supervised",
        provider_execution_enabled=True,
        clock=clock,
    )
    valid = campaign_payload(target=2, budget=12)
    valid["deployment_git_sha"] = git_sha
    valid["deployment_image_digest"] = image_digest
    assert application.create_campaign(valid)["max_provider_runs"] == 12

    excessive_budget = campaign_payload(target=2, budget=13)
    excessive_budget["deployment_git_sha"] = git_sha
    excessive_budget["deployment_image_digest"] = image_digest
    with pytest.raises(Exception):
        application.create_campaign(excessive_budget)

    extra_task = campaign_payload(target=2, budget=14)
    extra_task["tasks"].append(
        {
            **extra_task["tasks"][0],
            "task_id": "PILOT-TASK-EXTRA",
            "source_task_id": "V2-PUB-040",
        }
    )
    extra_task["deployment_git_sha"] = git_sha
    extra_task["deployment_image_digest"] = image_digest
    with pytest.raises(Exception):
        application.create_campaign(extra_task)

    drifted_task = campaign_payload(target=2, budget=12)
    drifted_task["tasks"][0]["prompt_en"] += " Changed after preregistration."
    drifted_task["deployment_git_sha"] = git_sha
    drifted_task["deployment_image_digest"] = image_digest
    with pytest.raises(Exception):
        application.create_campaign(drifted_task)


def test_invites_cannot_cross_execution_environment() -> None:
    clock = Clock()
    store = InMemoryPilotStore()
    local = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        provider_execution_enabled=True,
        clock=clock,
    )
    campaign_id = create_frozen_campaign(local)
    local_invite = local.create_invite(campaign_id)

    supervised = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha="a" * 40,
        deployment_image_digest="sha256:" + "b" * 64,
        supervised_task_pack_sha256=supervised_test_task_pack_sha256(),
        retention_schedule_confirmed=True,
        environment="supervised",
        provider_execution_enabled=True,
        clock=clock,
    )
    with pytest.raises(Exception):
        supervised.create_invite(campaign_id)
    with pytest.raises(Exception):
        supervised.exchange_invite(local_invite["invite_token"])

    # A failed cross-mode redemption must not consume the original invitation.
    local_auth = local.exchange_invite(local_invite["invite_token"])
    assert local_auth["campaign"]["execution_environment"] == "local"
    with pytest.raises(Exception):
        supervised.authenticate(local_auth["session_token"])

    local_worker = PilotWorker(
        store=store,
        executor=FakeExecutor(),
        worker_id="worker-local-mode",
        clock=clock,
    )
    wrong_worker = PilotWorker(
        store=store,
        executor=FakeExecutor(),
        worker_id="worker-wrong-supervised-mode",
        execution_environment="supervised",
        deployment_image_digest="sha256:" + "b" * 64,
        clock=clock,
    )
    local_session = local.authenticate(
        local_auth["session_token"], csrf_token=local_auth["csrf_token"]
    )
    local.record_consent(local_session, consent_payload())
    local_session = local.authenticate(
        local_auth["session_token"], csrf_token=local_auth["csrf_token"]
    )
    local_attempt = local.state(local_session)["attempt"]
    local.reveal(local_session, local_attempt["attempt_id"])
    assert wrong_worker.process_one() is None
    assert local_worker.process_one() is not None


def test_invites_and_sessions_cannot_cross_deployment_identity() -> None:
    clock = Clock()
    store = InMemoryPilotStore()
    first_git_sha = "a" * 40
    first_image_digest = "sha256:" + "b" * 64
    first = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha=first_git_sha,
        deployment_image_digest=first_image_digest,
        provider_execution_enabled=True,
        clock=clock,
    )
    prepared = campaign_payload()
    prepared["deployment_git_sha"] = first_git_sha
    prepared["deployment_image_digest"] = first_image_digest
    campaign_id = first.create_campaign(prepared)["campaign_id"]
    first.freeze_campaign(campaign_id)
    invite = first.create_invite(campaign_id)

    second = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha="c" * 40,
        deployment_image_digest="sha256:" + "d" * 64,
        provider_execution_enabled=True,
        clock=clock,
    )
    with pytest.raises(Exception):
        second.create_invite(campaign_id)
    with pytest.raises(Exception):
        second.exchange_invite(invite["invite_token"])

    # A failed deployment-identity redemption must not consume the invitation.
    auth = first.exchange_invite(invite["invite_token"])
    with pytest.raises(Exception):
        second.authenticate(auth["session_token"])


def test_positive_metrics_remain_claim_blocked_then_withdrawal_recomputes() -> None:
    clock = Clock()
    git_sha = "a" * 40
    image_digest = "sha256:" + "b" * 64

    def finish_campaign(application, worker, campaign_id, participant_count=4):
        participants = []
        for _ in range(participant_count):
            auth, session = exchange_and_consent(application, campaign_id)
            participants.append((auth, session))
            for _ in range(6):
                attempt = application.state(session)["attempt"]
                application.reveal(session, attempt["attempt_id"])
                worker.process_one()
                revealed = application.reveal(session, attempt["attempt_id"])[
                    "attempt"
                ]
                clock.advance(2)
                application.record_feedback(
                    session,
                    attempt["attempt_id"],
                    feedback_payload(
                        clarification=revealed["clarification_feedback_required"]
                    ),
                )
        application.complete_campaign(campaign_id)
        return participants

    store = InMemoryPilotStore()
    app = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha=git_sha,
        deployment_image_digest=image_digest,
        retention_schedule_confirmed=True,
        provider_execution_enabled=True,
        clock=clock,
    )
    worker = PilotWorker(
        store=store,
        executor=FakeExecutor(),
        worker_id="worker-positive-gate",
        deployment_image_digest=image_digest,
        clock=clock,
    )
    prepared = campaign_payload(target=5, budget=30)
    prepared["deployment_git_sha"] = git_sha
    prepared["deployment_image_digest"] = image_digest
    campaign_id = app.create_campaign(prepared)["campaign_id"]
    app.freeze_campaign(campaign_id)
    participants = finish_campaign(app, worker, campaign_id)
    passed = app.summary(campaign_id)
    assert passed["external_validation_claim_allowed"] is False
    assert passed["pilot_success_criteria_met"] is None
    assert "operator_eligibility_adjudication_not_implemented" in passed[
        "external_validation_claim_reason_codes"
    ]
    assert passed["interactions"]["feedback_completed_count"] == 24

    supervised_store = InMemoryPilotStore()
    supervised_app = PilotApplication(
        store=supervised_store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha=git_sha,
        deployment_image_digest=image_digest,
        supervised_task_pack_sha256=supervised_test_task_pack_sha256(),
        retention_schedule_confirmed=True,
        environment="supervised",
        provider_execution_enabled=True,
        clock=clock,
    )
    supervised_worker = PilotWorker(
        store=supervised_store,
        executor=FakeExecutor(),
        worker_id="worker-supervised-gate",
        execution_environment="supervised",
        deployment_image_digest=image_digest,
        clock=clock,
    )
    supervised_prepared = campaign_payload(target=2, budget=12)
    supervised_prepared["deployment_git_sha"] = git_sha
    supervised_prepared["deployment_image_digest"] = image_digest
    supervised_created = supervised_app.create_campaign(supervised_prepared)
    assert supervised_created["execution_environment"] == "supervised"
    supervised_campaign_id = supervised_created["campaign_id"]
    supervised_app.freeze_campaign(supervised_campaign_id)
    finish_campaign(
        supervised_app, supervised_worker, supervised_campaign_id, participant_count=2
    )

    local_reader = PilotApplication(
        store=supervised_store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha=git_sha,
        deployment_image_digest=image_digest,
        retention_schedule_confirmed=True,
        provider_execution_enabled=True,
        clock=clock,
    )
    supervised = local_reader.summary(supervised_campaign_id)
    assert supervised["execution_environment"] == "supervised"
    assert supervised["supervised_pretest"] is True
    assert supervised["external_pilot"] is False
    assert supervised["external_participant_pretest"] is True
    assert supervised["evidence_status"] == "supervised_external_user_pretest_only"
    assert supervised["external_validation_claim_allowed"] is False
    assert supervised["pilot_success_criteria_met"] is None
    assert "supervised_environment_not_claim_eligible" in supervised[
        "external_validation_claim_reason_codes"
    ]
    assert supervised["cohort"]["eligible_external_participant_count"] == 2
    assert supervised["interactions"]["feedback_completed_count"] == 12
    first_auth, first_session = participants[0]
    app.withdraw(first_session, first_auth["session_token"])
    corrected = app.summary(campaign_id)
    assert corrected["external_validation_claim_allowed"] is False
    assert corrected["pilot_success_criteria_met"] is None
    assert corrected["interactions"]["feedback_completed_count"] == 18
    assert "completed_interaction_count_below_minimum" in corrected[
        "external_validation_claim_reason_codes"
    ]


def test_claim_coverage_uses_qualifying_feedback_not_frozen_task_pack() -> None:
    clock = Clock()
    store = InMemoryPilotStore()
    git_sha = "a" * 40
    image_digest = "sha256:" + "b" * 64
    application = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT_DOCUMENT,
        expected_commitments=COMMITMENTS,
        deployment_git_sha=git_sha,
        deployment_image_digest=image_digest,
        retention_schedule_confirmed=True,
        provider_execution_enabled=True,
        clock=clock,
    )
    worker = PilotWorker(
        store=store,
        executor=FakeExecutor(),
        worker_id="worker-coverage-gate",
        deployment_image_digest=image_digest,
        clock=clock,
    )
    prepared = campaign_payload(target=4, budget=24)
    prepared["deployment_git_sha"] = git_sha
    prepared["deployment_image_digest"] = image_digest
    campaign_id = application.create_campaign(prepared)["campaign_id"]
    application.freeze_campaign(campaign_id)

    for _ in range(4):
        _, session = exchange_and_consent(application, campaign_id)
        first = application.state(session)["attempt"]
        assert first["sequence"] == 1
        application.skip_attempt(session, first["attempt_id"])
        for _ in range(5):
            attempt = application.state(session)["attempt"]
            application.reveal(session, attempt["attempt_id"])
            worker.process_one()
            revealed = application.reveal(session, attempt["attempt_id"])["attempt"]
            application.record_feedback(
                session,
                attempt["attempt_id"],
                feedback_payload(
                    clarification=revealed["clarification_feedback_required"]
                ),
            )
    application.complete_campaign(campaign_id)
    summary = application.summary(campaign_id)
    assert summary["interactions"]["feedback_completed_count"] == 20
    assert summary["coverage"]["scenario_counts"]["standard_analysis"] == 0
    assert "scenario_coverage_below_minimum" in summary[
        "external_validation_claim_reason_codes"
    ]
