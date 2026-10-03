"""Exact experiment-only Adapter session, checked dispatch and existing timed ledger."""
from copy import deepcopy
import asyncio
import json
import time

import httpx2
from researchops.audit import AuditLedger, sha256_json
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops_completion_telemetry.capture import CompletionTelemetryCollector, verify_runtime_denominator_plan
from researchops_completion_telemetry.surface_mapping import create_item6_experiment_binding
from researchops_completion_timing.session import _PendingTimedLedgerSession
from .authority import Owner
from .budget import Budget
from . import contract as c
from . import case_isolation as isolation


def _native_transport(mode):
    c.require(mode == "live", "offline_delegate_required")
    return httpx2.AsyncHTTPTransport(retries=0, trust_env=False)


class _LimitedStream(httpx2.AsyncByteStream):
    def __init__(self, stream, factory): self.stream, self.factory = stream, factory
    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > self.factory.freeze["policy"]["response_bytes"]:
                self.factory.owner.stop("item6_response_bytes")
                raise c.ExperimentError("response_bytes")
            yield chunk
    async def aclose(self): await self.stream.aclose()


class _CheckedDelegate(httpx2.AsyncBaseTransport):
    def __init__(self, delegate, session): self.delegate, self.session = delegate, session
    async def handle_async_request(self, request):
        factory = self.session.factory
        factory.budget.dispatch()
        factory.dispatches += 1
        response = await self.delegate.handle_async_request(request)
        response.stream = _LimitedStream(response.stream, factory)
        return response
    async def aclose(self): await self.delegate.aclose()


class _ExperimentTransport(httpx2.AsyncBaseTransport):
    def __init__(self, session):
        self.session = session
        delegate = _native_transport(session.factory.owner.mode)
        expected = httpx2.MockTransport if session.factory.owner.mode == "offline_test" else httpx2.AsyncHTTPTransport
        c.require(type(delegate) is expected, "transport_mode_mismatch")
        self.delegate = _CheckedDelegate(delegate, session)
        self.closed = False

    async def handle_async_request(self, request):
        c.require(not self.closed, "transport_closed")
        self.session.factory.before_send(self.session, request)
        timed = self.session._transport_for_attempt(self.delegate)
        self.session.factory.owner.dispatch_checkpoint()
        c.require(time.monotonic_ns() < self.session.factory.request_deadline, "request_deadline")
        return await timed.handle_async_request(request)

    async def aclose(self):
        if not self.closed:
            self.closed = True
            async with asyncio.timeout(5): await self.delegate.aclose()


class ExperimentSession(_PendingTimedLedgerSession):
    def __init__(self, factory, session, *, key):
        c.require(type(factory) is ExperimentFactory and factory.current is not None, "factory_required")
        factory.owner.check(source_check=True)
        self.factory = factory
        factory.owner.assert_case(factory, factory.current)
        super().__init__(session, ledger=factory.ledger, run_id=factory.owner.run_id,
            runtime_plan_binding=factory.plan_binding, clock=factory.owner.clock,
            timing_binding=factory.timing_binding, sensitive_canaries=(key,))

    def _required_runtime_authority_scope(self): return c.SCOPE
    def _is_exact_supported_session_type(self): return type(self) is ExperimentSession

    def assert_provider_telemetry_authority(self):
        self.factory.owner.check()
        self.factory.owner.assert_case(self.factory, self.factory.current)
        c.require(self.factory.active is self and self.factory.owner.clock.snapshot()["phase"]["key_loaded_ns"] is not None, "session_inactive")
        LedgerCompletionTelemetrySession.assert_provider_telemetry_authority(self)

    def assert_model_scope(self, model):
        self.assert_provider_telemetry_authority()
        c.require(model == self.factory.freeze["policy"]["model"] == "deepseek-flash", "model_scope")
        return model

    def _create_item6_timed_transport(self):
        self.assert_provider_telemetry_authority()
        return _ExperimentTransport(self)

    def begin_attempt(self):
        self.assert_provider_telemetry_authority()
        isolation.work_allowed(self.factory.current)
        self.factory.owner.check(source_check=True)
        self.factory.budget.reserve(self.case_id)
        self.factory.request_deadline = time.monotonic_ns() + self.factory.freeze["budget"]["request_seconds"] * 10**9
        return super().begin_attempt()

    def _write(self, event_type, payload, *, handle, terminal):
        event_hash = super()._write(event_type, payload, handle=handle, terminal=terminal)
        if terminal is not None:
            self.factory.terminals.append(dict(case_id=self.case_id, attempt_index=terminal.attempt_index,
                terminal_kind=terminal.terminal_kind, event_hash=event_hash))
            if "completion_record" in payload:
                record = deepcopy(payload["completion_record"])
                self.factory.records.append(record)
                if record["normalized_completion_state"] == "incomplete_length":
                    self.factory.current.completion = "truncated"
                try:
                    self.factory.budget.settle(record)
                    c.require(record["normalized_completion_state"] == "completed"
                              and record["truncation_signal_source"] == "native_status", "response_not_complete")
                except BaseException as error:
                    self.factory.owner.stop(c.safe_error(error))
                    raise
            else:
                self.factory.budget.unknown(terminal.terminal_kind)
        return event_hash

    def _finalize(self, handle, terminal_kind, *, capture=None, error_code=None):
        try:
            return super()._finalize(handle, terminal_kind, capture=capture, error_code=error_code)
        except BaseException as error:
            from .attempt_failures import observe
            observe(self, error)
            raise


class ExperimentFactory:
    def __init__(self, owner, ledger):
        c.require(type(owner) is Owner and type(ledger) is AuditLedger, "factory_owner")
        owner.take_factory(self)
        self.owner, self.ledger, self.freeze = owner, ledger, owner.freeze
        self.binding = create_item6_experiment_binding(owner)
        snapshot = self.binding.runtime_snapshot()
        names = ("provider_id", "api_surface", "transport_id", "adapter_version", "telemetry_schema_sha256",
                 "mapping_schema_version", "mapping_version", "mapping_sha256")
        plan = {n: snapshot[n] for n in names}
        cases = [self.freeze["model_case_handles"][tid] for tid in self.freeze["model_case_ids"]]
        plan.update(schema_version="provider-completion-runtime-denominator-plan/1.0", case_ids=cases,
            case_ids_sha256=sha256_json(cases), max_turns_per_case=self.freeze["budget"]["requests_per_task"],
            total_model_request_cap=self.freeze["budget"]["model_requests"], agents_sdk_retries=0, http_client_retries=0,
            denominator_algorithm="transport-response-finalization-v1", exact_response_count_preregistered=False)
        self.plan = plan
        self.plan_binding = verify_runtime_denominator_plan(self.binding, plan, preregistration_commitment=sha256_json(plan))
        self.tracker = CompletionTelemetryCollector.for_runtime(self.plan_binding)
        self.timing_binding = dict(plan_commitment_sha256=c.commit("timing-plan", self.freeze),
            execution_binding_sha256=c.commit("freeze", self.freeze), clock_domain_id=owner.clock_domain)
        self.budget = Budget(self.freeze)
        self.current = self.active = None
        self.canary = None
        self.records, self.terminals, self.sessions = [], [], []
        self.attempt_failures = []
        self.dispatches = self.case_index = 0
        self.request_deadline = 0
        ledger.start_run(run_id=owner.run_id, mode=c.SCOPE + "." + owner.mode,
            request_summary=dict(mode=owner.mode, freeze_sha256=c.commit("freeze", self.freeze), source_sha256=self.freeze["source_commitment_sha256"]))
        isolation.bind_factory(self)

    def open_case(self, case):
        self.owner.check(source_check=True)
        isolation.new_case(self)
        c.require(self.current is None and self.case_index < 32, "case_overlap")
        expected = self.freeze["business_plan"][self.case_index]
        c.require(expected == {"task_id": case.task["task_id"], "path_kind": case.path}, "case_order")
        self.owner.open_business_case(self, case)
        self.current = case
        self.case_index += 1
        isolation.started(case)

    def model_session(self, key):
        c.require(self.current is not None and self.current.path == "agent" and self.active is None, "model_case")
        session = ExperimentSession(self, self.tracker.bind_case(self.freeze["model_case_handles"][self.current.task["task_id"]]), key=key)
        self.active = session; self.sessions.append(session)
        return session

    def before_send(self, session, request):
        self.owner.check(source_check=True)
        self.owner.assert_case(self, self.current)
        c.require(self.active is session and self.current is not None, "session_identity")
        isolation.work_allowed(self.current)
        self.current.check_deadline()
        policy = self.freeze["policy"]
        c.require(request.method == "POST" and str(request.url) == policy["origin"] + "/responses", "request_origin")
        body = c.decode(request.content, policy["request_bytes"])
        c.require(set(body) <= {"model", "input", "instructions", "include", "tools", "max_output_tokens", "store", "stream", "parallel_tool_calls", "reasoning"}, "request_fields")
        c.require(body.get("model") == policy["model"] and type(body.get("max_output_tokens")) is int and body.get("max_output_tokens") == self.freeze["budget"]["output_per_request"]
            and body.get("store") is False and body.get("stream", False) is False and body.get("parallel_tool_calls") is False
            and body.get("reasoning") == {"effort": "none"} and body.get("include") == [], "request_policy")
        from .interface_v2 import INSTRUCTION
        c.require(body.get("instructions") == INSTRUCTION and body.get("tools") == policy["tools"]
                  and body.get("input") == self.current.replay, "request_replay")
        self.budget.wire(len(request.content))
        self.ledger.append_event(self.owner.run_id, "item6_send_intent_v1", dict(mode=self.owner.mode,
            task_id=self.current.task["task_id"], attempt_index=self.budget.pending["index"],
            model_case_id=session.case_id,
            source_sha256=self.freeze["source_commitment_sha256"], request_sha256=c.digest(request.content),
            policy_sha256=c.digest(c.raw(policy)), request_bytes=len(request.content), request_body_persisted=False, headers_persisted=False), actor_kind="system")

    def close_case(self, *, sdk_responses=None, sdk_requests=None, sdk_indices=None):
        case = self.current
        reconciliation = None
        if self.current.path == "agent":
            reconciliation = self.tracker.seal_case(self.freeze["model_case_handles"][self.current.task["task_id"]],
                sdk_raw_response_count=sdk_responses, sdk_usage_request_count=sdk_requests,
                sdk_request_usage_indices_by_response=sdk_indices)
        if self.owner.stopped is None:
            isolation.before_close(case, reconciliation)
        if self.active is not None: self.active._capture_canaries = ()
        self.owner.close_business_case(self, self.current)
        self.current = self.active = None
        if self.owner.stopped is None:
            isolation.closed(case, reconciliation)
        else:
            isolation.abort(case)
        return reconciliation
