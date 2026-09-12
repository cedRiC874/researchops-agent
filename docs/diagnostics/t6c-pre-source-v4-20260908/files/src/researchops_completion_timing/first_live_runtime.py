"""Private two-scenario Provider factory; no public runner or artifact publisher."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx2
from agents import ModelSettings

from researchops.audit import AuditLedger, sha256_json
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops.model_providers import DeepSeekProvider
from researchops.deepseek_completion_first_live_validation import _validation_runtime_binding, _environment_isolated, _network_logging_disabled
from researchops_completion_telemetry.capture import CompletionTelemetryCollector, verify_runtime_denominator_plan
from researchops_external_closure.execution_binding import _current_file
from researchops_external_closure.execution_current_v3 import verify_current_timed_profile
from researchops_external_closure.execution_local_v3 import _head
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .first_live_start import _ClaimedFirstLiveStart, FirstLiveStartError
from .process_environment import verify_process_environment
from .session import _PendingTimedLedgerSession
from .contract import commitment, digest
from . import local_claim


ROOT = Path(__file__).resolve().parents[2]


def _native_transport():
    return httpx2.AsyncHTTPTransport(retries=0, trust_env=False)


class _FirstLiveTimedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, session):
        self._session, self._delegate, self._closed = session, _native_transport(), False

    async def handle_async_request(self, request):
        if self._closed:
            self._session._owner._stop("first_live_transport_closed")
        self._session._owner._before_send(self._session, request)
        transport = self._session._transport_for_attempt(self._delegate)
        return await transport.handle_async_request(request)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            remaining = self._session._timing_clock.remaining_phase_ns()
            async with asyncio.timeout(min(5.0, remaining / 1_000_000_000)):
                await self._delegate.aclose()
        except BaseException:
            self._session._owner._failed = True
            self._session._owner._prepared.abort()
            raise


class _FirstLiveTimedSession(_PendingTimedLedgerSession):
    def __init__(self, owner, session, **kwargs):
        if type(owner) is not _FirstLiveModelFactory:
            raise TypeError("first-live session requires its checked factory")
        self._owner = owner
        super().__init__(session, **kwargs)

    def _required_runtime_authority_scope(self):
        return "first_live_validation"

    def _is_exact_supported_session_type(self):
        return type(self) is _FirstLiveTimedSession

    def assert_provider_telemetry_authority(self):
        phase = self._owner._prepared.clock.snapshot()["phase"]
        if phase["task_released_ns"] is None or phase["key_loaded_ns"] is None:
            self._owner._stop("first_live_key_boundary_not_recorded")
        self._owner._check_current()
        LedgerCompletionTelemetrySession.assert_provider_telemetry_authority(self)

    def begin_attempt(self):
        self._owner._begin_case(self)
        return super().begin_attempt()

    def _create_first_live_timed_transport(self):
        self.assert_provider_telemetry_authority()
        return _FirstLiveTimedTransport(self)

    def _write(self, event_type, payload, *, handle, terminal):
        result = super()._write(event_type, payload, handle=handle, terminal=terminal)
        if terminal is not None:
            self._owner._terminal(self, terminal, payload)
        return result


class _FirstLiveModelFactory:
    """Own fixed inputs and limits; no caller-controlled prompts or tools.

    The private caller owns credential loading/release and artifact publication.
    Construction itself reads no Key and sends no request.
    """
    def __init__(self, prepared, ledger):
        if type(prepared) is not _ClaimedFirstLiveStart or prepared._environment is None or type(ledger) is not AuditLedger:
            raise FirstLiveStartError("first_live_factory_preparation_required", claim_consumed=True)
        self._prepared = prepared._take_for_runtime()
        self._ledger = ledger
        self._failed = False
        self._next = 0
        self._active = None
        self._sending = False
        self._records = []
        self._sessions = []
        self._inflight = False
        self._phase_finished = False
        try:
            self._initialize()
        except BaseException:
            self._failed = True
            self._prepared.abort()
            raise

    def _initialize(self):
        prepared = self._prepared
        self._plan = decode_strict_json_object(prepared.plan_bytes, max_bytes=131072)
        self._check_current()
        intent = decode_strict_json_object(prepared.intent_bytes, max_bytes=4096)
        binding = dict(self._plan["binding"], authorization_binding_sha256=intent["authorization_binding_sha256"],
            local_claim_receipt_sha256=digest(prepared.receipt_bytes))
        self._timing_binding = dict(plan_commitment_sha256=self._plan["plan_commitment_sha256"],
            execution_binding_sha256=commitment("execution_binding", binding), clock_domain_id=prepared.clock_domain_id)
        self._run = self._plan["binding"]["validation_run_id"]
        runtime = _validation_runtime_binding(ROOT)
        snapshot = runtime.runtime_snapshot()
        cases = self._plan["planned_case_handles"]
        denominator = {name: snapshot[name] for name in ("provider_id", "api_surface", "transport_id", "adapter_version",
            "telemetry_schema_sha256", "mapping_schema_version", "mapping_version", "mapping_sha256")}
        denominator.update(schema_version="provider-completion-runtime-denominator-plan/1.0", case_ids=cases,
            case_ids_sha256=sha256_json(cases), max_turns_per_case=1, total_model_request_cap=2,
            agents_sdk_retries=0, http_client_retries=0, denominator_algorithm="transport-response-finalization-v1",
            exact_response_count_preregistered=False)
        self._denominator = verify_runtime_denominator_plan(runtime, denominator, preregistration_commitment=sha256_json(denominator))
        self._tracker = CompletionTelemetryCollector.for_runtime(self._denominator)
        self._ledger.start_run(run_id=self._run, mode="deepseek_first_live_timed_validation",
            request_summary={name: self._timing_binding[name] for name in ("plan_commitment_sha256", "execution_binding_sha256")})

    def _stop(self, code):
        self._failed = True
        self._prepared.abort()
        raise FirstLiveStartError(code, claim_consumed=True)

    def _check_current(self):
        if self._failed:
            self._stop("first_live_factory_halted")
        try:
            # Check only variable presence, not unrelated credential values.
            if (not _environment_isolated(os.environ) or not _network_logging_disabled()
                or any(logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in ("httpx2", "httpcore2"))):
                self._stop("first_live_environment_not_isolated")
            self._prepared._assert_process()
            _now, deadline = self._prepared._budget.checkpoint(self._prepared._expires)
            self._prepared.clock._tighten_phase_deadline(deadline)
            expected = self._prepared.source_identity
            current = verify_current_timed_profile(ROOT, profile="first_live")
            if (current.source_integrity_commitment_sha256 != expected.source_integrity_commitment_sha256
                or current.implementation_commitment_sha256 != expected.source_manifest_commitment_sha256
                or _head(ROOT) != expected.execution_commit):
                self._stop("first_live_request_source_mismatch")
            verify_process_environment(ROOT, expected_dependency_lock_sha256=current.component_hashes["dependency_lock_sha256"],
                expected_pyproject_sha256=current.component_hashes["pyproject_sha256"])
            receipt = decode_strict_json_object(self._prepared.receipt_bytes, max_bytes=4096)
            request = {name: receipt[name] for name in local_claim._profile()["request_fields"]}
            request["schema_version"] = "provider-completion-local-claim-request/1.0"
            local_claim.read_reserved_local_claim(raw(request), expected_receipt_sha256=digest(self._prepared.receipt_bytes))
            _now, deadline = self._prepared._budget.checkpoint(self._prepared._expires)
            self._prepared.clock._tighten_phase_deadline(deadline)
        except BaseException:
            self._failed = True
            self._prepared.abort()
            raise

    def before_key_load(self):
        self._check_current()
        self._prepared.clock.task_released()

    def key_loaded(self):
        self._check_current()
        self._prepared.clock.key_loaded()

    def _scenario(self):
        design = decode_strict_json_object(_current_file(ROOT,
            "evals/provider_completion_first_live_validation_v1/deepseek_responses_adapter_validation_contract_v1.json"), max_bytes=32768)
        return design["frozen_inputs"]["scenarios"][self._next]

    def _begin_case(self, session):
        if self._active is not None or self._next >= 2 or session.case_id != self._plan["planned_case_handles"][self._next]:
            self._stop("first_live_request_sequence_invalid")
        self._check_current()
        self._active, self._sending = session, False

    def _before_send(self, session, request):
        if session is not self._active or self._sending:
            self._stop("first_live_request_reused")
        self._check_current()
        if request.method != "POST" or str(request.url) != "https://api.deepseek.com/responses":
            self._stop("first_live_request_origin_invalid")
        body = decode_strict_json_object(request.content, max_bytes=32768)
        scenario = self._scenario()
        if (body.get("model") != "deepseek-v4-flash" or type(body.get("max_output_tokens")) is not int
            or body["max_output_tokens"] != scenario["max_output_tokens"]
            or body.get("input") != [{"role": "user", "content": scenario["input"]}]
            or body.get("tools") != [] or body.get("stream", False) is not False
            or any(body.get(name) is not None for name in ("instructions", "previous_response_id", "conversation", "prompt"))):
            self._stop("first_live_request_contract_mismatch")
        self._sending = True

    def _terminal(self, session, terminal, payload):
        if session is not self._active or terminal.terminal_kind != "response_accepted":
            self._stop("first_live_response_not_accepted")
        record = payload["completion_record"]
        usage = record["usage"]["normalized"]
        scenario = self._scenario()
        if (record["normalized_completion_state"] != scenario["expected_normalized_completion_state"]
            or record["truncation_signal_source"] != "native_status" or not record["usage"]["complete"]
            or record["http_status"] != {"availability": "provided", "value": 200}
            or record["output_counter_comparability"] != "comparable"
            or record["output_token_cap"] != {"availability": "provided", "value": scenario["max_output_tokens"]}
            or usage["requests"] != 1
            or type(usage["input_tokens"]) is not int or not 0 <= usage["input_tokens"] <= 512
            or type(usage["output_tokens"]) is not int or not 0 <= usage["output_tokens"] <= scenario["max_output_tokens"]
            or (self._next == 1 and usage["output_tokens"] != 16)):
            self._stop("first_live_response_contract_mismatch")
        self._records.append(record)
        self._next += 1
        self._active = None

    async def run_next(self, *, api_key):
        if self._inflight or self._phase_finished:
            self._stop("first_live_request_sequence_invalid")
        self._inflight = True
        try:
            return await self._run_next(api_key=api_key)
        except BaseException:
            self._failed = True
            self._prepared.abort()
            raise
        finally:
            self._inflight = False

    def complete_phase_after_key_release(self):
        """Caller must first clear its owned Key reference; not a RAM wipe claim."""
        if self._failed or self._inflight or self._phase_finished or self._active is not None or self._next != 2:
            self._stop("first_live_phase_not_complete")
        try:
            self._check_current()
            self._prepared.clock.post_cleanup_terminal(status="completed")
            self._prepared.clock.key_reference_released(status="released")
            self._prepared.clock.phase_terminal()
            self._ledger.set_run_status(self._run, "completed")
            self._phase_finished = True
        except BaseException:
            self._failed = True
            self._prepared.abort()
            raise

    async def _run_next(self, *, api_key):
        if self._next >= 2 or self._active is not None:
            self._stop("first_live_request_sequence_invalid")
        self._check_current()
        if self._prepared.clock.snapshot()["phase"]["key_loaded_ns"] is None:
            self._stop("first_live_key_boundary_not_recorded")
        scenario = self._scenario()
        case = self._plan["planned_case_handles"][self._next]
        session = _FirstLiveTimedSession(self, self._tracker.bind_case(case), ledger=self._ledger, run_id=self._run,
            runtime_plan_binding=self._denominator, clock=self._prepared.clock, timing_binding=self._timing_binding)
        self._sessions.append(session)
        try:
            async with DeepSeekProvider().open_model(model_id="deepseek-v4-flash", api_key=api_key,
                    timeout_seconds=120, completion_telemetry_session=session) as provider:
                response = await provider.sdk_model._fetch_response(system_instructions=None, input=scenario["input"],
                    model_settings=ModelSettings(max_tokens=scenario["max_output_tokens"]), tools=[], output_schema=None, handoffs=[])
                del response
        except BaseException:
            self._failed = True
            self._prepared.abort()
            raise
        return {"response_index": self._next - 1, "completion_state": self._records[-1]["normalized_completion_state"]}
