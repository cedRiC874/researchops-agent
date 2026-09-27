"""Bounded experiment diagnostics before the shared Adapter masks an exception.

Only exact trusted types and literal codes are projected. Never inspect exception
text, arguments, traceback, request, capture, or the caller-supplied handle.
"""
from researchops.audit import AuditError
from researchops_completion_timing.clock import TimingCaptureError
from researchops_completion_telemetry.capture import CompletionCaptureError
from researchops_completion_telemetry.sanitization import CompletionTelemetryError
from . import contract as c


TRUSTED_CODES = {
    AuditError: frozenset({"audit_completion_attempt_handle_invalid", "audit_completion_session_failed",
                           "audit_completion_capture_required", "audit_completion_terminal_invalid"}),
    TimingCaptureError: frozenset({"timing_lifecycle_invalid", "timing_attempt_handle_invalid",
                                   "timing_response_not_accepted", "timing_clock_order_invalid"}),
    CompletionCaptureError: frozenset({"completion_capture_attempt_handle_invalid",
                                      "completion_capture_attempt_already_finalized"}),
    CompletionTelemetryError: frozenset({"completion_telemetry_comparable_counter_invalid",
        "completion_telemetry_normalized_usage_invalid", "completion_telemetry_usage_invalid",
        "completion_telemetry_usage_missing_with_normalized_values"}),
    c.ExperimentError: frozenset({"item6_usage_unknown", "item6_usage_inconsistent", "item6_usage_detail",
        "item6_output_overshoot", "item6_input_overshoot", "item6_response_not_complete"}),
}
TYPE_NAMES = {kind: kind.__name__ for kind in TRUSTED_CODES}


def classify(error):
    kind = type(error)
    # Read .code only for exact trusted classes; never invoke arbitrary properties.
    code = error.__dict__.get("code") if kind in TRUSTED_CODES else None
    allowed = type(code) is str and code in TRUSTED_CODES.get(kind, ())
    return dict(exception_type=TYPE_NAMES.get(kind, "untrusted_exception"),
                code=code if allowed else None, code_status="allowlisted" if allowed else "unknown")


def observe(session, error):
    factory = session.factory
    c.require(len(factory.attempt_failures) < factory.freeze["budget"]["model_requests"], "attempt_diagnostic_limit")
    snapshot = factory.owner.clock.snapshot()
    commitment = session.event_commitment()
    # This ledger-owned list was populated by begin_attempt after persisting its
    # start event. Never infer identity from the handle that was just rejected.
    start = commitment["started"][-1] if commitment["started"] else None
    record = dict(stage="attempt_finalize", case_id=session.case_id, **classify(error),
        attempt_index=start["attempt_index"] if start is not None else None,
        request_start_event_hash=start["event_hash"] if start is not None else None,
        clock_active_attempt=snapshot["active_attempt"] is not None,
        clock_closed=snapshot["closed"], clock_halted=snapshot["halted"],
        session_failed=session.failed,
        persisted_terminal_count=len(commitment["terminals"]))
    factory.attempt_failures.append(record)
    # This is an observation of a refusal, not permission to retry/finalize.
    factory.owner.stop("item6_attempt_finalization_failed")


def validate(records):
    c.require(type(records) is list and 0 < len(records) <= 48, "attempt_diagnostics")
    for row in records:
        c.exact(row, "stage case_id attempt_index request_start_event_hash exception_type code code_status clock_active_attempt clock_closed clock_halted session_failed persisted_terminal_count")
        c.require(row["attempt_index"] is None or (type(row["attempt_index"]) is int and 0 <= row["attempt_index"] < 48), "attempt_diagnostics")
        c.require(row["request_start_event_hash"] is None or (type(row["request_start_event_hash"]) is str
                  and c.SHA.fullmatch(row["request_start_event_hash"]) is not None), "attempt_diagnostics")
        c.require(row["stage"] == "attempt_finalize" and row["exception_type"] in {*TYPE_NAMES.values(), "untrusted_exception"}, "attempt_diagnostics")
        codes = next((TRUSTED_CODES[t] for t,n in TYPE_NAMES.items() if n == row["exception_type"]), ())
        c.require((row["code_status"] == "allowlisted" and type(row["code"]) is str and row["code"] in codes)
                  or (row["code_status"] == "unknown" and row["code"] is None), "attempt_diagnostics")
        c.require(all(type(row[n]) is bool for n in ("clock_active_attempt", "clock_closed", "clock_halted", "session_failed")), "attempt_diagnostics")
        c.require(type(row["persisted_terminal_count"]) is int and 0 <= row["persisted_terminal_count"] <= 48, "attempt_diagnostics")
