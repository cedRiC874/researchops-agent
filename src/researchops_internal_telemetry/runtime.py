"""Claim-owned Internal SDK execution. No alternate public transport/Key hook."""
from __future__ import annotations

import asyncio
import os
import time

import httpx2
from agents import ModelSettings

from researchops.audit import AuditLedger, sha256_json
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops.model_providers import DeepSeekProvider
from researchops.deepseek_completion_first_live_validation import _environment_isolated, _network_logging_disabled
from researchops_completion_telemetry.capture import CompletionTelemetryCollector, verify_runtime_denominator_plan
from researchops_completion_timing.session import _PendingTimedLedgerSession
from researchops_completion_timing.campaign_budget import _CampaignBudget
from researchops_external_closure.io import scan_public_artifact_bytes
from .admission import _ClaimedInternal
from .binding import _binding_for_claim
from .contract import ROOT, DIRECTORY, RUNTIME_SCOPE, InternalError, budget_plan, commitment, decode, digest, now, raw, read, require, utc, validate_tasks


def _native_transport():
    return httpx2.AsyncHTTPTransport(retries=0, trust_env=False)


def check_response(record, cap=512):
    require(record["normalized_completion_state"] == "completed" and record["truncation_signal_source"] == "native_status", "unexpected_completion")
    require(record["http_status"] == {"availability": "provided", "value": 200}, "http_status")
    require(record["usage"]["complete"] is True and record["output_counter_comparability"] == "comparable", "usage_unknown")
    require(record["output_token_cap"] == {"availability": "provided", "value": cap}, "output_cap_binding")
    usage = record["usage"]["normalized"]
    for name in ("requests", "input_tokens", "output_tokens"):
        require(type(usage[name]) is int and usage[name] >= 0, "usage_unknown")
    require(usage["requests"] == 1, "usage_requests")
    # Overshoot is accounted by the budget before raising; do not drop usage.
    return usage


class _InternalTransport(httpx2.AsyncBaseTransport):
    def __init__(self, session):
        self.session, self.delegate, self.closed = session, _native_transport(), False
        mode = "offline_mock_transport" if type(self.delegate) is httpx2.MockTransport else "native_http_transport" if type(self.delegate) is httpx2.AsyncHTTPTransport else None
        require(mode is not None and session.owner.transport_mode in (None, mode), "transport_mode")
        session.owner.transport_mode = mode

    async def handle_async_request(self, request):
        require(not self.closed, "transport_closed")
        factory = self.session.owner
        factory.before_send(self.session, request)
        transport = self.session._transport_for_attempt(self.delegate)
        factory.dispatch_checkpoint()
        try:
            return await transport.handle_async_request(request)
        finally:
            if transport.observation()["send_observed"]:
                factory.network_attempts += 1

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        async with asyncio.timeout(min(5, self.session._timing_clock.remaining_phase_ns() / 1e9)):
            await self.delegate.aclose()


class _InternalSession(_PendingTimedLedgerSession):
    def __init__(self, owner, session, **kwargs):
        require(type(owner) is _InternalFactory, "factory_required")
        self.owner = owner
        super().__init__(session, **kwargs)

    def _required_runtime_authority_scope(self): return RUNTIME_SCOPE
    def _is_exact_supported_session_type(self): return type(self) is _InternalSession

    def assert_provider_telemetry_authority(self):
        self.owner.check()
        require(self.owner.owner.clock.snapshot()["phase"]["key_loaded_ns"] is not None, "key_boundary")
        LedgerCompletionTelemetrySession.assert_provider_telemetry_authority(self)

    def _create_internal_timed_transport(self):
        self.assert_provider_telemetry_authority()
        return _InternalTransport(self)

    def begin_attempt(self):
        require(self is self.owner.active and self.owner.reservation is None, "attempt_reused")
        self.owner.reservation = self.owner.budget.reserve_request()
        return super().begin_attempt()

    def _write(self, event_type, payload, *, handle, terminal):
        result = super()._write(event_type, payload, handle=handle, terminal=terminal)
        if terminal is not None:
            self.owner.terminals.append(dict(case_id=terminal.case_id, attempt_index=terminal.attempt_index,
                response_index=terminal.response_index, terminal_kind=terminal.terminal_kind))
            if "completion_record" in payload:
                self.owner.records.append(payload["completion_record"])
        return result


class _InternalFactory:
    def __init__(self, owner, ledger):
        require(type(owner) is _ClaimedInternal and type(ledger) is AuditLedger, "factory_owner")
        self.owner, self.ledger = owner.take(), ledger
        self.freeze = self.owner.check(source_check=True)
        self.task_bytes = read(ROOT / DIRECTORY / "cases_v1.json")
        self.tasks = validate_tasks(decode(self.task_bytes), self.freeze["protocol"])
        require(digest(self.task_bytes) == self.freeze["tasks_sha256"], "tasks_drift")
        self.owner.clock.task_released()
        self.binding = _binding_for_claim(self.owner)
        snapshot = self.binding.runtime_snapshot()
        self.plan = {name: snapshot[name] for name in ("provider_id", "api_surface", "transport_id", "adapter_version",
            "telemetry_schema_sha256", "mapping_schema_version", "mapping_version", "mapping_sha256")}
        ids = [case["case_id"] for case in self.tasks["cases"]]
        self.plan.update(schema_version="provider-completion-runtime-denominator-plan/1.0", case_ids=ids,
            case_ids_sha256=sha256_json(ids), max_turns_per_case=1, total_model_request_cap=30,
            agents_sdk_retries=0, http_client_retries=0, denominator_algorithm="transport-response-finalization-v1",
            exact_response_count_preregistered=False)
        self.denominator = verify_runtime_denominator_plan(self.binding, self.plan, preregistration_commitment=sha256_json(self.plan))
        self.tracker = CompletionTelemetryCollector.for_runtime(self.denominator)
        self.budget = _CampaignBudget(budget_plan(self.freeze["protocol"], self.tasks, self.freeze["pricing"]))
        self.run_id = "PCEINTERNAL-" + digest(owner.authorization_bytes)[:32].upper()
        self.timing_binding = dict(plan_commitment_sha256=commitment("timing-plan", self.freeze),
            execution_binding_sha256=commitment("freeze", self.freeze), clock_domain_id=owner.clock_domain)
        self.sessions, self.records, self.terminals, self.sdk_counts = [], [], [], []
        self.model_requests = self.network_attempts = self.passed = 0
        self.transport_mode = None
        self.active = self.reservation = None
        self._sent = self._busy = self._failed = False
        self.error_code = None
        ledger.start_run(run_id=self.run_id, mode="internal_known_synthetic",
            request_summary=dict(freeze_commitment_sha256=commitment("freeze", self.freeze), source_commitment_sha256=self.freeze["source_commitment_sha256"]))

    def check(self, *, source_check=False):
        require(not self._failed, "runtime_halted")
        self.owner.check(source_check=source_check)
        require(_environment_isolated(os.environ) and _network_logging_disabled(), "environment_not_isolated")
        require(self.freeze == decode(self.owner.freeze_bytes) and digest(self.task_bytes) == self.freeze["tasks_sha256"]
            and self.tasks == decode(self.task_bytes), "runtime_snapshot_changed")

    def before_send(self, session, request):
        self.check(source_check=True)
        require(session is self.active and self.reservation is not None and not self._sent and self.network_attempts < 30, "send_reused")
        require(request.method == "POST" and str(request.url) == "https://api.deepseek.com/responses", "origin_invalid")
        body = decode(request.content, 32768)
        allowed = {"model", "input", "include", "tools", "max_output_tokens", "store", "stream", "parallel_tool_calls"}
        require(set(body) <= allowed and body.get("model") == "deepseek-v4-flash" and body.get("tools") == []
            and body.get("store") is False and body.get("include") == [] and body.get("stream", False) is False
            and body.get("parallel_tool_calls", False) is False and type(body.get("max_output_tokens")) is int
            and body["max_output_tokens"] == 512, "request_contract")
        case = self.tasks["cases"][self.passed]
        require(body.get("input") == [{"role": "user", "content": case["input"]}], "request_input")
        payload = dict(schema_version="provider-completion-internal-send-intent/1.0", case_id=case["case_id"],
            case_index=self.passed, attempt_index=self.reservation.request_index,
            source_commitment_sha256=self.freeze["source_commitment_sha256"], tasks_sha256=self.freeze["tasks_sha256"],
            freeze_commitment_sha256=commitment("freeze", self.freeze), cap=512, method="POST", path="/responses",
            origin="https://api.deepseek.com", request_body_persisted=False, headers_persisted=False)
        scan_public_artifact_bytes((raw(payload),))
        self.ledger.append_event(self.run_id, "internal_send_intent", payload, actor_kind="system")
        self._sent = True

    def dispatch_checkpoint(self):
        # No database or filesystem work may intervene after this checkpoint.
        # asyncio.timeout alone cannot notice a deadline while sync IO blocks.
        require(time.monotonic_ns() < self._request_deadline_ns, "request_timeout")
        require(now() < utc(decode(self.owner.authorization_bytes)["candidate"]["expires_at_utc"]), "authorization_expired")

    async def run_case(self, key):
        require(not self._busy and self.passed < 30, "case_order")
        self._busy, self._sent = True, False
        self.check()
        require(self.owner.clock.remaining_phase_ns() >= 120_000_000_000, "request_window_short")
        self.budget.begin_case(self.passed)
        self.reservation = None
        case = self.tasks["cases"][self.passed]
        session = _InternalSession(self, self.tracker.bind_case(case["case_id"]), ledger=self.ledger,
            run_id=self.run_id, runtime_plan_binding=self.denominator, clock=self.owner.clock,
            timing_binding=self.timing_binding, sensitive_canaries=(key,))
        self.active = session
        self.sessions.append(session)
        started = time.monotonic_ns()
        self._request_deadline_ns = started + 120_000_000_000
        try:
            async with asyncio.timeout(120):
                async with DeepSeekProvider().open_model(model_id="deepseek-v4-flash", api_key=key,
                        timeout_seconds=120, completion_telemetry_session=session) as provider:
                    self.model_requests += 1
                    response = await provider.sdk_model._fetch_response(system_instructions=None, input=case["input"],
                        model_settings=ModelSettings(max_tokens=512, store=False, parallel_tool_calls=False), tools=[], output_schema=None, handoffs=[])
                    # Read only metadata; no response content copied to evidence.
                    sdk_usage = response.usage
                    sdk_counts = dict(raw_response_count=1, input_tokens=getattr(sdk_usage, "input_tokens", None),
                        output_tokens=getattr(sdk_usage, "output_tokens", None))
                    del response, sdk_usage
            completed = time.monotonic_ns()
            require(completed - started <= 120_000_000_000, "request_timeout")
            require(len(self.records) == self.passed + 1 and len(self.terminals) == self.passed + 1
                and self.terminals[-1]["terminal_kind"] == "response_accepted", "response_missing")
            record = self.records[-1]
            usage = record["usage"]["normalized"]
            reservation = self.reservation
            self.reservation = None
            self.budget.observe_usage(reservation, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], requests=usage["requests"])
            check_response(record)
            require(sdk_counts["input_tokens"] == usage["input_tokens"] and sdk_counts["output_tokens"] == usage["output_tokens"], "sdk_usage_mismatch")
            self.sdk_counts.append(sdk_counts)
            reconciliation = self.tracker.seal_case(case["case_id"], sdk_raw_response_count=sdk_counts["raw_response_count"], sdk_usage_request_count=usage["requests"])
            require(reconciliation.closure_eligible, "denominator_mismatch")
            self.check(source_check=True)
            self.budget.finish_case()
            close_payload = dict(schema_version="provider-completion-internal-case-close/1.0",case_id=case["case_id"],
                case_index=self.passed,clock_domain_id=self.owner.clock_domain,
                request_started_ns=started-self.owner.clock._origin,request_completed_ns=completed-self.owner.clock._origin,
                completion_record_sha256=digest(raw(record)),sdk_counts=sdk_counts)
            scan_public_artifact_bytes((raw(close_payload),))
            self.ledger.append_event(self.run_id,"internal_case_closed",close_payload,actor_kind="system")
            self.passed += 1
        except BaseException:
            if self.reservation is not None:
                self.budget.unknown_outcome(self.reservation)
                self.reservation = None
            self._failed = True
            self.owner.abort()
            raise
        finally:
            session._capture_canaries = ()
            self.active, self._busy = None, False

    def projections(self):
        segments = [decode(payload) for session in self.sessions for payload in session.segment_bytes()]
        return dict(schema_version="provider-completion-internal-projection/1.0", records=self.records,
            terminals=self.terminals, sdk_counts=self.sdk_counts, segments=segments)
