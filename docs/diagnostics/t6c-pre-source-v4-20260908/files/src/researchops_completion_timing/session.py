"""Pending timed Adapter/ledger bridge, exercised only by offline integration.

Public Provider factories intentionally reject this session. The independent
consumed-authority gateway, frozen first-live origin and full artifact/A/B path
are not implemented here. Passing ordinary runtime bindings must not enable it.
"""
from __future__ import annotations

import asyncio
import re

import httpx2

from researchops.audit import AuditLedger
from researchops.completion_telemetry_ledger import LedgerCompletionTelemetrySession
from researchops_external_closure.primitives import canonical_json_bytes
from .clock import TimingCaptureError, _TimingClock
from .contract import commitment, digest
from .transport import _TimedHTTPTransport


class _PendingTimedLedgerSession(LedgerCompletionTelemetrySession):
    def __init__(self, session, *, ledger, run_id, runtime_plan_binding, clock, timing_binding, sensitive_canaries=()):
        from researchops_completion_telemetry.sanitization import sanitize_completion_capture
        if (type(sensitive_canaries) is not tuple or len(sensitive_canaries) > 8
            or any(type(value) is not str or not value for value in sensitive_canaries)):
            raise TimingCaptureError('timing_session_canaries_invalid')
        sanitize_completion_capture({}, sensitive_canaries=sensitive_canaries)
        self._capture_canaries = sensitive_canaries
        if type(clock) is not _TimingClock or type(timing_binding) is not dict:
            raise TimingCaptureError("timing_session_input_invalid")
        if (set(timing_binding) != {"plan_commitment_sha256", "execution_binding_sha256", "clock_domain_id"}
            or any(type(value) is not str for value in timing_binding.values())
            or any(re.fullmatch(r"(?!0{64}$)[0-9a-f]{64}", timing_binding[name]) is None
                   for name in ("plan_commitment_sha256", "execution_binding_sha256"))
            or re.fullmatch(r"PCECLOCK-[A-F0-9]{32}", timing_binding["clock_domain_id"]) is None):
            raise TimingCaptureError("timing_binding_mismatch")
        self._timing_clock = clock
        self._timing_binding = dict(timing_binding)
        self._timing_handle = None
        self._current_transport = None
        self._segments = {}
        super().__init__(session, ledger=ledger, run_id=run_id, runtime_plan_binding=runtime_plan_binding)
        if (self.provider_id, self.api_surface, self.transport_id) != ("deepseek", "responses", "openai_compatible_responses"):
            raise TimingCaptureError("timing_session_binding_invalid")

    def assert_provider_telemetry_authority(self):
        # Do not substitute an ordinary write capability for consumption proof.
        raise TimingCaptureError("timing_admission_not_enabled")

    def _completion_sensitive_canaries(self):
        return self._capture_canaries

    def begin_attempt(self):
        self._require_active()
        if self._timing_handle is not None:
            self._timing_clock.abort_new_work()
            raise TimingCaptureError("timing_concurrency_overlap")
        self._timing_handle = self._timing_clock.begin_attempt()
        self._current_transport = None
        try:
            return super().begin_attempt()
        except BaseException:
            self._failed = True
            self._timing_clock.abort_new_work()
            raise

    def _transport_for_attempt(self, delegate):
        self._require_active()
        if self._timing_handle is None or self._current_transport is not None:
            self._timing_clock.abort_new_work()
            raise TimingCaptureError("timing_transport_reused")
        self._current_transport = _TimedHTTPTransport(delegate, clock=self._timing_clock, attempt_handle=self._timing_handle)
        return self._current_transport

    def transport_send_observed_for_attempt(self, handle):
        self._known_handle(handle)
        return self._current_transport is not None and self._current_transport.observation()["send_observed"]

    def _finalize(self, handle, terminal_kind, *, capture=None, error_code=None):
        requested_kind = terminal_kind
        sent = self.transport_send_observed_for_attempt(handle)
        if terminal_kind == "cancelled" and sent:
            terminal_kind, error_code = "outcome_unknown", "provider_completion_outcome_unknown"
        if (terminal_kind == "outcome_unknown" and self._current_transport is not None
            and self._current_transport.observation()["full_body_observed"]):
            # A complete local HTTP body followed by cleanup/SDK failure is a
            # rejected response, not absence of any observed response.
            terminal_kind, error_code = "response_rejected", "provider_completion_capture_failed"
        if terminal_kind == "response_accepted" and (
            self._current_transport is None or not self._current_transport.observation()["full_body_observed"]
        ):
            terminal_kind = "outcome_unknown" if sent else "no_response"
            error_code, capture = "provider_completion_capture_failed", None
        if terminal_kind == "response_accepted" and self._timing_clock.snapshot()["halted"]:
            terminal_kind, error_code, capture = "response_rejected", "provider_completion_capture_failed", None
        try:
            terminal = super()._finalize(handle, terminal_kind, capture=capture, error_code=error_code)
            # The Adapter ignores the returned terminal value. Merely recording
            # a rejection would still return the response to its planner.
            if requested_kind == "response_accepted" and terminal.terminal_kind != "response_accepted":
                raise TimingCaptureError("timing_response_not_accepted")
            return terminal
        except BaseException:
            self._failed = True
            self._timing_clock.abort_new_work()
            raise

    def _write(self, event_type, payload, *, handle, terminal):
        if terminal is None:
            return super()._write(event_type, payload, handle=handle, terminal=None)
        try:
            if self._current_transport is None or not self._current_transport.observation()["send_observed"]:
                self._timing_clock.transport_terminal(self._timing_handle)
                status = "not_applicable"
            else:
                status = self._current_transport.observation()["raw_cleanup_status"]
            self._timing_clock.finish_attempt(self._timing_handle, terminal_kind=terminal.terminal_kind, raw_cleanup_status=status)
            offsets = self._timing_clock.snapshot()["attempts"][-1]
            record = payload.get("completion_record")
            segment = dict(schema_version="provider-completion-timing-segment/1.0", document_type="completion_timing_segment",
                **self._timing_binding, **offsets, case_handle=terminal.case_id,
                attempt_index=terminal.attempt_index, case_attempt_index=terminal.case_attempt_index,
                request_index=terminal.attempt_index, response_index=terminal.response_index,
                completion_record_sha256=digest(canonical_json_bytes(record)) if record is not None else None,
                usage_sha256=digest(canonical_json_bytes(record["usage"])) if record is not None else None)
            segment["segment_commitment_sha256"] = commitment("segment", segment)
            segment_bytes = canonical_json_bytes(segment)
            self._segments[terminal.attempt_index] = segment_bytes
            event_hash = AuditLedger.append_timed_completion_telemetry_event(self._ledger, event_type, payload,
                capability=self._capability, attempt_handle=handle, terminal=terminal,
                segment_bytes=segment_bytes, expected_timing_binding=self._timing_binding)
            self._timing_handle = None
            return event_hash
        except BaseException:
            self._failed = True
            self._timing_clock.abort_new_work()
            raise

    def segment_bytes(self):
        return tuple(self._segments[index] for index in sorted(self._segments))


class _PendingTimedSessionTransport(httpx2.AsyncBaseTransport):
    """Internal client transport dispatcher; public factories do not accept it."""
    def __init__(self, delegate, session):
        if not isinstance(delegate, httpx2.AsyncBaseTransport) or type(session) is not _PendingTimedLedgerSession:
            raise TimingCaptureError("timing_session_transport_invalid")
        self._delegate, self._session, self._closed = delegate, session, False

    async def handle_async_request(self, request):
        if self._closed:
            raise TimingCaptureError("timing_session_transport_closed")
        transport = self._session._transport_for_attempt(self._delegate)
        return await transport.handle_async_request(request)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        delegate, self._delegate = self._delegate, None
        try:
            remaining = self._session._timing_clock.remaining_phase_ns()
            async with asyncio.timeout(min(remaining / 1_000_000_000, 5.0)):
                await delegate.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TimingCaptureError("timing_post_cleanup_failed") from None
