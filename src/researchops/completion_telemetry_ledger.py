from __future__ import annotations

from typing import Any

from researchops_completion_telemetry.capture import (
    RuntimeAttemptHandle,
    RuntimeAttemptTerminal,
    RuntimeCaseTelemetrySession,
    VerifiedRuntimeDenominatorPlanBinding,
)
from researchops_completion_telemetry.sanitization import (
    SanitizedCompletionCapture,
    build_completion_record,
)
from .audit import (
    COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
    COMPLETION_TELEMETRY_UNMAPPED_EVENT,
    AuditError,
    AuditLedger,
    sha256_json,
)


_TERMINAL_EVENT_TYPES = {
    "response_accepted": "model_response_telemetry_recorded",
    "response_rejected": "model_response_telemetry_rejected",
    "http_error": "model_request_http_error",
    "no_response": "model_request_no_response",
    "cancelled": "model_request_cancelled",
    "outcome_unknown": "model_request_outcome_unknown",
}


class LedgerCompletionTelemetrySession:
    """Write-ahead audit bridge for one runtime case telemetry session."""

    __slots__ = (
        "_binding",
        "_binding_summary",
        "_capability",
        "_failed",
        "_handles",
        "_ledger",
        "_session",
        "_start_event_hashes",
        "_terminal_attempts",
        "_terminal_event_hashes",
    )

    def __init__(
        self,
        session: RuntimeCaseTelemetrySession,
        *,
        ledger: AuditLedger,
        run_id: str,
        runtime_plan_binding: VerifiedRuntimeDenominatorPlanBinding,
    ) -> None:
        if type(session) is not RuntimeCaseTelemetrySession:
            raise AuditError(
                "audit_completion_case_session_required",
                "Ledger bridge 需要 runtime case telemetry session。",
            )
        if type(ledger) is not AuditLedger:
            raise AuditError(
                "audit_completion_ledger_required",
                "Ledger bridge 需要 AuditLedger。",
            )
        if type(runtime_plan_binding) is not VerifiedRuntimeDenominatorPlanBinding:
            raise AuditError(
                "audit_completion_runtime_plan_required",
                "Ledger bridge 需要 verified runtime denominator plan。",
            )
        try:
            runtime_plan_binding.assert_plan_authority()
            runtime_binding = runtime_plan_binding.runtime_binding()
            runtime_binding.assert_runtime_authority()
            binding = runtime_binding.runtime_snapshot()
        except Exception:
            raise AuditError(
                "audit_completion_runtime_plan_required",
                "Ledger bridge runtime plan/binding authority 无效。",
            ) from None
        if session.binding_snapshot() != binding:
            raise AuditError(
                "audit_completion_binding_mismatch",
                "Case session 与 runtime binding 不匹配。",
            )
        if not isinstance(run_id, str) or not run_id:
            raise AuditError(
                "audit_completion_run_id_invalid",
                "Ledger bridge run ID 无效。",
            )
        self._session = session
        self._ledger = ledger
        self._binding = runtime_binding
        self._binding_summary = dict(binding)
        self._capability = AuditLedger._create_completion_telemetry_write_capability(
            ledger,
            run_id,
            plan_binding=runtime_plan_binding,
            session=session,
        )
        self._failed = False
        self._handles: dict[int, RuntimeAttemptHandle] = {}
        self._terminal_attempts: set[int] = set()
        self._start_event_hashes: dict[int, str] = {}
        self._terminal_event_hashes: dict[int, str] = {}

    @property
    def case_id(self) -> str:
        return self._session.case_id

    @property
    def provider_id(self) -> str:
        return self._binding_summary["provider_id"]

    @property
    def api_surface(self) -> str:
        return self._binding_summary["api_surface"]

    @property
    def transport_id(self) -> str:
        return self._binding_summary["transport_id"]

    @property
    def adapter_version(self) -> str:
        return self._binding_summary["adapter_version"]

    def binding_snapshot(self) -> dict[str, str]:
        return dict(self._binding_summary)

    def event_commitment(self) -> dict[str, Any]:
        """Return a canonical runner-facing commitment to written events."""

        body: dict[str, Any] = {
            "schema_version": "provider-completion-ledger-bridge-commitment/1.0",
            "case_id": self.case_id,
            "binding_sha256": sha256_json(self._binding_summary),
            "started": [
                {"attempt_index": index, "event_hash": event_hash}
                for index, event_hash in sorted(self._start_event_hashes.items())
            ],
            "terminals": [
                {"attempt_index": index, "event_hash": event_hash}
                for index, event_hash in sorted(self._terminal_event_hashes.items())
            ],
            "all_started_attempts_terminal": (
                set(self._start_event_hashes) == set(self._terminal_event_hashes)
            ),
            "write_failed": self._failed,
        }
        return {**body, "commitment_sha256": sha256_json(body)}

    @property
    def failed(self) -> bool:
        return self._failed

    def assert_provider_telemetry_authority(self) -> None:
        """Revalidate the exact bridge, runtime plan and write capability."""

        if type(self) is not LedgerCompletionTelemetrySession or self._failed:
            raise AuditError(
                "audit_completion_session_failed",
                "Completion telemetry ledger bridge 无效或已失败。",
            )
        self._binding.assert_runtime_authority()
        self._capability._assert_authority(
            self._ledger._completion_capability_token
        )

    def _require_active(self) -> None:
        if self._failed:
            raise AuditError(
                "audit_completion_session_failed",
                "Completion telemetry ledger write 已失败；禁止重试。",
            )

    def _write(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        handle: RuntimeAttemptHandle,
        terminal: RuntimeAttemptTerminal | None,
    ) -> str:
        try:
            return AuditLedger.append_completion_telemetry_event(
                self._ledger,
                event_type,
                payload,
                capability=self._capability,
                attempt_handle=handle,
                terminal=terminal,
            )
        except Exception:
            self._failed = True
            raise

    def begin_attempt(self) -> RuntimeAttemptHandle:
        """Persist the request-start event before returning control to network code."""

        self._require_active()
        handle = self._session.begin_attempt()
        payload = {
            "schema_version": COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
            "case_id": handle.case_id,
            "attempt_index": handle.attempt_index,
            "case_attempt_index": handle.case_attempt_index,
            "binding": dict(self._binding_summary),
        }
        event_hash = self._write(
            "model_request_started",
            payload,
            handle=handle,
            terminal=None,
        )
        self._handles[handle.attempt_index] = handle
        self._start_event_hashes[handle.attempt_index] = event_hash
        return handle

    def _known_handle(self, handle: RuntimeAttemptHandle) -> RuntimeAttemptHandle:
        self._require_active()
        if (
            type(handle) is not RuntimeAttemptHandle
            or self._handles.get(handle.attempt_index) != handle
            or handle.attempt_index in self._terminal_attempts
        ):
            raise AuditError(
                "audit_completion_attempt_handle_invalid",
                "Ledger bridge attempt handle 无效或已终态。",
            )
        return handle

    def _finalize(
        self,
        handle: RuntimeAttemptHandle,
        terminal_kind: str,
        *,
        capture: SanitizedCompletionCapture | None = None,
        error_code: str | None = None,
    ) -> RuntimeAttemptTerminal:
        handle = self._known_handle(handle)
        safe_error_code: str | None = None
        if terminal_kind in {
            "response_rejected",
            "http_error",
            "no_response",
            "outcome_unknown",
        }:
            if not isinstance(error_code, str):
                raise AuditError(
                    "audit_completion_error_code_required",
                    "该 completion terminal 需要稳定 error code。",
                )
            safe_error_code = error_code
        if terminal_kind == "response_accepted":
            if type(capture) is not SanitizedCompletionCapture:
                raise AuditError(
                    "audit_completion_capture_required",
                    "Accepted response 需要 sanitized capture。",
                )
            terminal = self._session.finalize_response_accepted(handle, capture)
        elif terminal_kind == "response_rejected":
            terminal = self._session.finalize_response_rejected(
                handle, safe_error_code  # type: ignore[arg-type]
            )
        elif terminal_kind == "http_error":
            terminal = self._session.finalize_http_error(
                handle, safe_error_code  # type: ignore[arg-type]
            )
        elif terminal_kind == "no_response":
            terminal = self._session.finalize_no_response(
                handle, safe_error_code  # type: ignore[arg-type]
            )
        elif terminal_kind == "cancelled":
            terminal = self._session.finalize_cancelled(handle)
        elif terminal_kind == "outcome_unknown":
            terminal = self._session.finalize_outcome_unknown(
                handle, safe_error_code  # type: ignore[arg-type]
            )
        else:  # pragma: no cover - private callers use the fixed wrappers below
            raise AuditError(
                "audit_completion_terminal_invalid",
                "Ledger bridge terminal kind 无效。",
            )

        payload: dict[str, Any] = {
            "schema_version": COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION,
            "case_id": terminal.case_id,
            "attempt_index": terminal.attempt_index,
            "case_attempt_index": terminal.case_attempt_index,
            "terminal_kind": terminal.terminal_kind,
            "response_index": terminal.response_index,
            "error_code": terminal.error_code,
            "binding": dict(self._binding_summary),
        }
        if terminal_kind == "response_accepted":
            if terminal.response_index is None or terminal._capture is None:
                self._failed = True
                raise AuditError(
                    "audit_completion_terminal_invalid",
                    "Accepted terminal 丢失 response index/capture。",
                )
            try:
                payload["completion_record"] = build_completion_record(
                    terminal._capture._collector_snapshot(),
                    binding=self._binding,
                    response_index=terminal.response_index,
                    request_index=terminal.attempt_index,
                )
            except Exception:
                self._failed = True
                raise
        event_type = _TERMINAL_EVENT_TYPES[terminal_kind]
        if (
            terminal_kind == "response_accepted"
            and payload["completion_record"]["normalized_completion_state"]
            == "unmapped"
        ):
            event_type = COMPLETION_TELEMETRY_UNMAPPED_EVENT
        event_hash = self._write(
            event_type,
            payload,
            handle=handle,
            terminal=terminal,
        )
        self._terminal_attempts.add(handle.attempt_index)
        self._terminal_event_hashes[handle.attempt_index] = event_hash
        return terminal

    def finalize_response_accepted(
        self,
        handle: RuntimeAttemptHandle,
        capture: SanitizedCompletionCapture,
    ) -> RuntimeAttemptTerminal:
        return self._finalize(handle, "response_accepted", capture=capture)

    def finalize_response_rejected(
        self,
        handle: RuntimeAttemptHandle,
        error_code: str,
    ) -> RuntimeAttemptTerminal:
        return self._finalize(
            handle,
            "response_rejected",
            error_code=error_code,
        )

    def finalize_http_error(
        self,
        handle: RuntimeAttemptHandle,
        error_code: str,
    ) -> RuntimeAttemptTerminal:
        return self._finalize(handle, "http_error", error_code=error_code)

    def finalize_no_response(
        self,
        handle: RuntimeAttemptHandle,
        error_code: str,
    ) -> RuntimeAttemptTerminal:
        return self._finalize(handle, "no_response", error_code=error_code)

    def finalize_cancelled(
        self,
        handle: RuntimeAttemptHandle,
    ) -> RuntimeAttemptTerminal:
        return self._finalize(handle, "cancelled")

    def finalize_outcome_unknown(
        self,
        handle: RuntimeAttemptHandle,
        error_code: str,
    ) -> RuntimeAttemptTerminal:
        return self._finalize(handle, "outcome_unknown", error_code=error_code)


__all__ = ["LedgerCompletionTelemetrySession"]
