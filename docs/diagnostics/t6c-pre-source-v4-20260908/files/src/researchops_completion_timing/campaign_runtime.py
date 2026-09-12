"""Consumed campaign owner to timed SDK sessions; no public Key loader/publisher."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import httpx2
from agents import Agent, ModelSettings, RunConfig, Runner

from researchops.audit import AuditLedger, sha256_json
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops.model_providers import DeepSeekProvider
from researchops.deepseek_completion_first_live_validation import _environment_isolated, _network_logging_disabled
from researchops_completion_telemetry import surface_mapping as mapping
from researchops_completion_telemetry.capture import CompletionTelemetryCollector, verify_runtime_denominator_plan
from researchops_external_closure.io import scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .campaign_start import ROOT
from .campaign_tasks import _CampaignTaskOwner
from .campaign_budget import _budget_for_opened_owner
from .campaign_wire import _render_case_input, _verify_wire_case_input
from .session import _PendingTimedLedgerSession
from .campaign_usage import _project_case_usage, _usage_event


class CampaignRuntimeError(ValueError):
    def __init__(self, code):
        self.code = code; self.retry_authorized = False
        super().__init__(code)


def _native_transport():
    return httpx2.AsyncHTTPTransport(retries=0, trust_env=False)


class _CampaignTransport(httpx2.AsyncBaseTransport):
    def __init__(self, session):
        self._session, self._delegate, self._closed = session, _native_transport(), False
    async def handle_async_request(self, request):
        if self._closed: self._session._owner._stop('campaign_runtime_transport_closed')
        self._session._owner._before_send(self._session, request)
        # Prepare/validate the immutable write capability before the final clock
        # checkpoint. No SQLite work may intervene between that checkpoint and
        # the native delegate: a slow write must not invent an unsent request.
        transport = self._session._transport_for_attempt(self._delegate)
        self._session._owner._send_pending._mark_dispatch_boundary()
        try:
            response = await transport.handle_async_request(request)
        except BaseException:
            if transport.observation()['send_observed']:
                self._session._owner._record_send(self._session)
            raise
        try:
            self._session._owner._record_send(self._session)
        except BaseException:
            # HTTPX has not received the wrapped response yet. We still own its
            # bounded cleanup if the append fails, including uncertain writes.
            await response.aclose()
            raise
        return response
    async def aclose(self):
        if self._closed: return
        self._closed = True
        try:
            remaining = self._session._timing_clock.remaining_phase_ns()
            async with asyncio.timeout(min(5.0, remaining / 1_000_000_000)):
                await self._delegate.aclose()
        except BaseException:
            self._session._owner._abort(); raise


class _CampaignTimedSession(_PendingTimedLedgerSession):
    def __init__(self, owner, session, **kwargs):
        if type(owner) is not _CampaignModelFactory: raise TypeError('campaign owner required')
        self._owner, self._attempt_handle = owner, None
        super().__init__(session, **kwargs)
    def _required_runtime_authority_scope(self): return 'campaign_runtime'
    def _is_exact_supported_session_type(self): return type(self) is _CampaignTimedSession
    def assert_provider_telemetry_authority(self):
        self._owner._check_current()
        if self._owner._prepared.clock.snapshot()['phase']['key_loaded_ns'] is None:
            self._owner._stop('campaign_runtime_key_boundary_missing')
        LedgerCompletionTelemetrySession.assert_provider_telemetry_authority(self)
    def begin_attempt(self):
        self._owner._begin_attempt(self)
        try:
            self._attempt_handle = super().begin_attempt()
            return self._attempt_handle
        except BaseException:
            self._owner._abort(); raise
    def _create_campaign_timed_transport(self):
        self.assert_provider_telemetry_authority()
        return _CampaignTransport(self)
    def _write(self, event_type, payload, *, handle, terminal):
        result = super()._write(event_type, payload, handle=handle, terminal=terminal)
        if terminal is not None: self._owner._terminal(self, terminal, payload)
        return result


class _CampaignModelFactory:
    def __init__(self, task_owner, ledger):
        if type(task_owner) is not _CampaignTaskOwner or type(ledger) is not AuditLedger:
            raise CampaignRuntimeError('campaign_runtime_owner_required')
        self._task_owner, self._prepared, self._ledger = task_owner, task_owner._prepared, ledger
        self._failed = self._busy = False
        self._active = self._reservation = None
        self._send_pending = None; self._network_count = 0
        self._sessions = []; self._runs = []; self._records = []
        self._case_reconciliations = []
        self._phase_driver_taken = self._phase_finished = False
        self._sealed_denominator = None
        self._publication_started = self._publication_finished = False
        self._sealing_started_ns = self._sealing_timeout_ns = None
        try:
            self._budget = _budget_for_opened_owner(task_owner)
            self._opening = task_owner._opened
            self._envelope = self._prepared._verified_envelope()
            self._plan = self._envelope['runtime_plan']; self._execution = self._envelope['execution_binding']
            offline = mapping.load_and_select_surface_mapping(ROOT, 'deepseek', 'responses', 'openai_compatible_responses', purpose='offline_validation')
            selection = mapping.VerifiedSurfaceSelection._create(mapping._SELECTION_TOKEN, purpose='runtime_binding',
                telemetry_schema_sha256=offline.telemetry_schema_sha256, mapping=offline.mapping_snapshot(),
                entry=dict(adapter_version=offline.adapter_version, mapping_version=offline.mapping_version,
                    output_counter_comparability=offline.output_counter_comparability, output_counter_path=offline.output_counter_path,
                    runtime_binding_allowed=True))
            binding = mapping.create_runtime_completion_binding(selection)
            self._denominator = verify_runtime_denominator_plan(binding, self._plan['denominator_plan'],
                preregistration_commitment=self._plan['denominator_plan_commitment_sha256'])
            self._tracker = CompletionTelemetryCollector.for_runtime(self._denominator)
            self._timing_binding = dict(plan_commitment_sha256=self._prepared.proof.scope.timing.timing_plan_commitment_sha256,
                execution_binding_sha256=self._prepared.proof.scope.timing.timing_execution_binding_sha256,
                clock_domain_id=self._prepared.clock_domain_id)
            self._check_current()
        except BaseException:
            self._abort(); raise

    def _abort(self):
        self._failed = True; self._task_owner.abort()
    def _stop(self, code):
        self._abort(); raise CampaignRuntimeError(code)
    def _check_current(self):
        if self._failed: self._stop('campaign_runtime_halted')
        if (not _environment_isolated(os.environ) or not _network_logging_disabled()
            or any(logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in ('httpx2', 'httpcore2'))):
            self._stop('campaign_runtime_environment_not_isolated')
        self._task_owner._check_current()
    def before_key_load(self): self._check_current()
    def key_loaded(self):
        self._check_current(); self._prepared.clock.key_loaded()

    def _begin_attempt(self, session):
        self._check_current()
        if session is not self._active or self._reservation is not None:
            self._stop('campaign_runtime_attempt_mismatch')
        self._reservation = self._budget.reserve_request()
        self._send_pending = None

    def _before_send(self, session, request):
        self._check_current()
        if session is not self._active or self._reservation is None or self._send_pending is not None:
            self._stop('campaign_runtime_send_reused')
        if (request.method != 'POST' or request.url.scheme != 'https' or request.url.host != 'api.deepseek.com'
            or request.url.port not in (None, 443) or request.url.raw_path != b'/responses'
            or request.url.username or request.url.password or request.url.fragment):
            self._stop('campaign_runtime_origin_invalid')
        body = decode_strict_json_object(request.content, max_bytes=32768)
        required = {'model', 'input', 'include', 'tools', 'max_output_tokens', 'store'}
        if (not required <= set(body) or set(body) - required - {'stream', 'parallel_tool_calls'}
            or body['model'] != self._execution['model_id'] or body['include'] != [] or body['tools'] != []
            or body['store'] is not False or body.get('stream', False) is not False
            or body.get('parallel_tool_calls', False) is not False or type(body['max_output_tokens']) is not int
            or body['max_output_tokens'] != self._reservation.output_token_cap):
            self._stop('campaign_runtime_request_invalid')
        checked = _verify_wire_case_input(ROOT, self._opening, self._case_index, body['input'])
        handle = session._attempt_handle
        if handle is None or handle.attempt_index != self._reservation.request_index or handle.case_id != checked['case_handle']:
            self._stop('campaign_runtime_send_binding_invalid')
        payload = dict(schema_version='provider-completion-transport-send/1.0', case_id=handle.case_id,
            attempt_index=handle.attempt_index, case_attempt_index=handle.case_attempt_index, network_call_index=self._network_count,
            **{name: self._execution[name] for name in ('provider_id', 'api_surface', 'transport_id', 'adapter_version')},
            method='POST', origin=self._execution['api_origin'], path='/responses', requested_output_token_cap=body['max_output_tokens'],
            execution_input_commitment_sha256=checked['execution_input_commitment_sha256'], audit_request_sha256=self._request_sha,
            external_plan_binding_sha256=self._plan['external_plan_binding_sha256'],
            authorization_grant_sha256=self._prepared.proof.scope.timing.authorization_grant_sha256,
            headers_persisted=False, request_body_persisted=False, task_content_persisted=False)
        scan_public_artifact_bytes((raw(payload),), sensitive_canaries=(self._opening._canary_b64.encode(),))
        self._send_pending = self._ledger._create_transport_send_capability(payload,
            capability=session._capability, attempt_handle=handle,
            campaign_id=self._plan['campaign_topology']['campaign_id'], model_id=self._execution['model_id'],
            expected_output_token_cap=self._reservation.output_token_cap)

    def _record_send(self, session):
        if session is not self._active or self._send_pending is None: self._stop('campaign_runtime_send_binding_invalid')
        capability, self._send_pending = self._send_pending, None
        # This is a native dispatch count, not a count of successful DB writes.
        self._network_count += 1
        self._ledger.append_transport_send_event(capability)

    def _terminal(self, session, terminal, payload):
        reservation, self._reservation = self._reservation, None
        if session is not self._active or reservation is None: self._stop('campaign_runtime_terminal_mismatch')
        self._case_terminals.append(terminal)
        if terminal.terminal_kind != 'response_accepted':
            self._budget.unknown_outcome(reservation); self._stop('campaign_runtime_response_failed')
        record = payload['completion_record']; self._records.append(record)
        self._case_records.append(record)
        usage = record['usage']
        if not usage['complete'] or record['output_counter_comparability'] != 'comparable':
            self._budget.unknown_outcome(reservation); self._stop('campaign_runtime_usage_unknown')
        normalized = usage['normalized']
        self._budget.observe_usage(reservation, input_tokens=normalized['input_tokens'], output_tokens=normalized['output_tokens'], requests=normalized['requests'])
        if record['normalized_completion_state'] in ('unmapped', 'not_provided', 'not_persisted') or record['truncation_signal_source'] != 'native_status':
            self._stop('campaign_runtime_completion_unrecognized')

    def _write_case_usage(self, result):
        if self._usage_write_attempted: self._stop('campaign_runtime_usage_write_repeated')
        self._usage_write_attempted = True  # Never retry a partial ledger write.
        observed = sum(item.provider_response_observed for item in self._case_terminals)
        projected = _project_case_usage(result, self._case_records, observed_response_count=observed,
            terminal_attempt_count=len(self._case_terminals))
        elapsed = 0 if self._sdk_started_ns is None else (time.monotonic_ns() - self._sdk_started_ns) / 1_000_000
        rows = projected['rows']
        latency_share = elapsed / len(rows) if rows else 0
        for row in rows:
            self._ledger.record_model_call(self._run, provider=self._execution['provider_id'], model=self._execution['model_id'],
                started_at_utc=self._sdk_started_at, latency_ms=latency_share, input_tokens=row['input_tokens'],
                output_tokens=row['output_tokens'], cached_tokens=row['cached_tokens'], cost_usd=None,
                outcome='succeeded' if row['matched'] else 'failed', error_code=None if row['matched'] else 'pce_response_failed')
        event = _usage_event(projected, response_detail_count=observed, provider=self._execution['provider_id'], transport=self._execution['transport_id'])
        scan_public_artifact_bytes((raw(event),), sensitive_canaries=(self._opening._canary_b64.encode(),))
        self._ledger.append_event(self._run, 'model_run_usage_recorded', event, actor_kind='agent_sdk')
        case = self._opening._cases[self._case_index]['case_handle']
        reconciliation = self._tracker.seal_case(case, sdk_raw_response_count=projected['sdk_raw_response_count'],
            sdk_usage_request_count=projected['sdk_usage_request_count'], sdk_request_usage_indices_by_response=projected['indices_by_response'])
        self._case_reconciliations.append(reconciliation)
        return projected['matched'] and reconciliation.closure_eligible

    async def run_case(self, case_index, *, api_key):
        if self._busy: self._stop('campaign_runtime_concurrency')
        self._busy = True; run_started = False; result = None
        self._usage_write_attempted = False; self._sdk_started_ns = None; self._sdk_started_at = None
        self._case_records = []; self._case_terminals = []
        try:
            self._check_current()
            if self._prepared.clock.snapshot()['phase']['key_loaded_ns'] is None: self._stop('campaign_runtime_key_boundary_missing')
            self._budget.begin_case(case_index); self._case_index = case_index
            case = self._opening._cases[case_index]['case_handle']
            self._run = 'PCERUN-' + hashlib.sha256(b'researchops-provider-completion-audit-run-id-v1\0' + self._envelope['envelope_id'].encode()
                + b'\0' + case.encode()).hexdigest()[:32].upper()
            projection = dict(campaign_id=self._plan['campaign_topology']['campaign_id'], case_handle=case,
                execution_input_commitment_sha256=self._opening.execution_input_commitments[case_index],
                external_plan_binding_sha256=self._plan['external_plan_binding_sha256'],
                authorization_grant_sha256=self._prepared.proof.scope.timing.authorization_grant_sha256,
                **{name: self._execution[name] for name in ('provider_id', 'model_id', 'api_surface', 'transport_id')})
            self._request_sha = sha256_json(projection)
            self._ledger.start_run(run_id=self._run, mode='provider_completion_external_closure', request_summary=projection)
            run_started = True; self._runs.append(self._run)
            session = _CampaignTimedSession(self, self._tracker.bind_case(case), ledger=self._ledger, run_id=self._run,
                runtime_plan_binding=self._denominator, clock=self._prepared.clock, timing_binding=self._timing_binding,
                sensitive_canaries=(self._opening._canary_b64,))
            self._active = session; self._sessions.append(session)
            settings = ModelSettings(max_tokens=self._plan['budget_policy']['output_token_limit_per_request'], store=False)
            async with DeepSeekProvider().open_model(model_id=self._execution['model_id'], api_key=api_key,
                    timeout_seconds=self._plan['transport_limits']['request_timeout_seconds'], completion_telemetry_session=session) as provider:
                agent = Agent(name='ResearchOps completion telemetry', instructions=None, tools=[], model=provider.sdk_model, model_settings=settings)
                self._sdk_started_at = self._ledger._now().removesuffix('+00:00') + 'Z'
                self._sdk_started_ns = time.monotonic_ns()
                result = await Runner.run(agent, _render_case_input(ROOT, self._opening, case_index),
                    max_turns=self._plan['denominator_plan']['max_turns_per_case'], run_config=RunConfig(tracing_disabled=True, model_settings=settings))
            if not self._write_case_usage(result): self._stop('campaign_runtime_usage_reconciliation_failed')
            result = None
            self._budget.finish_case(); self._ledger.set_run_status(self._run, 'completed'); self._active = None
            return dict(case_index=case_index, status='completed', task_pass_produced=False)
        except BaseException as error:
            self._abort()
            if run_started:
                if not self._usage_write_attempted:
                    try: self._write_case_usage(result if result is not None else vars(error).get('run_data'))
                    except Exception: pass
                try:
                    cancelled = isinstance(error, asyncio.CancelledError)
                    self._ledger.set_run_status(self._run, 'cancelled' if cancelled else 'failed',
                        terminal_error_code='pce_response_cancelled' if cancelled else 'pce_response_failed')
                except Exception: pass
            raise
        finally:
            self._busy = False; result = None
