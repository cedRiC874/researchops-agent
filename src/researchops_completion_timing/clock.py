"""Internal monotonic-clock primitive, not an admission or artifact API.

No production caller is wired yet. A future consumed-authority gate must create
this clock before task release/Key load. Constructing it alone proves neither
that ordering nor authorization. Snapshots contain offsets only, never provenance,
binding claims, raw clock epochs, payloads or exception text.
"""
from __future__ import annotations

import threading
import time
from copy import deepcopy


MAX_OFFSET_NS = 9_007_199_254_740_991


class TimingCaptureError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _AttemptHandle:
    __slots__ = ()


class _TimingClock:
    """Thread-serialized lifecycle measurements; does not invoke any transport.

    Local deadline checks cannot interrupt an in-flight await: the eventual
    transport integration must also bound the await by the remaining budget.
    Terminal/cleanup observations are retained even when over budget.
    """

    def __init__(self, *, request_timeout_ns: int, phase_timeout_ns: int):
        for value, cap in ((request_timeout_ns, 300_000_000_000),
                           (phase_timeout_ns, 7_200_000_000_000)):
            if type(value) is not int or not 0 < value <= cap:
                raise TimingCaptureError("timing_clock_limits_invalid")
        self._lock = threading.Lock()
        self._request_cap = request_timeout_ns
        self._phase_cap = phase_timeout_ns
        self._last = 0
        self._clock_failed = False
        self._halted = False
        self._active = None
        self._attempt = None
        self._cleanup_recorded = False
        self._completed = []
        self._phase = dict(task_released_ns=None, key_loaded_ns=None,
                           post_cleanup_terminal_ns=None, post_cleanup_status="unknown",
                           key_reference_released_ns=None, key_reference_release_status="unknown",
                           phase_terminal_ns=None)
        self._post_recorded = False
        self._key_recorded = False
        self._closed = False
        self._origin = self._read_native()

    def _tighten_phase_deadline(self, deadline_ns):
        """Internal deadline composition; can only shorten the current cap.

        The owning startup gate supplies its private process-local deadline.
        No absolute epoch is persisted or returned in clock snapshots.
        """
        with self._lock:
            if self._halted or self._closed:
                self._reject("timing_capture_halted")
            if type(deadline_ns) is not int or deadline_ns < 0:
                self._reject("timing_clock_limits_invalid")
            now = self._sample()
            cap = min(self._phase_cap, deadline_ns - self._origin)
            if cap <= now:
                self._reject("timing_phase_timeout_exceeded")
            self._phase_cap = cap

    def _read_native(self):
        try:
            value = time.monotonic_ns()
        except Exception:
            self._clock_failed = self._halted = True
            raise TimingCaptureError("timing_clock_unavailable") from None
        if type(value) is not int or value < 0:
            self._clock_failed = self._halted = True
            raise TimingCaptureError("timing_clock_invalid") from None
        return value

    def _sample(self):
        if self._clock_failed:
            raise TimingCaptureError("timing_clock_unavailable")
        offset = self._read_native() - self._origin
        if not self._last <= offset <= MAX_OFFSET_NS:
            self._clock_failed = self._halted = True
            raise TimingCaptureError("timing_clock_order_invalid")
        self._last = offset
        if offset > self._phase_cap:
            self._halted = True
        return offset

    def _reject(self, code="timing_lifecycle_invalid"):
        self._halted = True
        raise TimingCaptureError(code)

    def _new_work(self):
        if self._halted or self._closed or self._post_recorded or self._key_recorded:
            self._reject("timing_capture_halted")
        value = self._sample()
        if value >= self._phase_cap:
            self._reject("timing_phase_timeout_exceeded")
        return value

    def task_released(self):
        with self._lock:
            if self._phase["task_released_ns"] is not None:
                self._reject()
            self._phase["task_released_ns"] = self._new_work()

    def key_loaded(self):
        with self._lock:
            if self._phase["task_released_ns"] is None or self._phase["key_loaded_ns"] is not None:
                self._reject()
            self._phase["key_loaded_ns"] = self._new_work()

    def begin_attempt(self):
        with self._lock:
            if self._active is not None:
                self._reject("timing_concurrency_overlap")
            if self._phase["key_loaded_ns"] is None:
                self._reject()
            if len(self._completed) >= 800:
                self._reject("timing_attempt_limit_exceeded")
            start = self._new_work()
            handle = _AttemptHandle()
            self._active = handle
            self._cleanup_recorded = False
            self._attempt = dict(attempt_started_ns=start, send_started_ns=None,
                                 transport_terminal_ns=None, raw_cleanup_terminal_ns=None,
                                 raw_cleanup_status="unknown", lifetime_ended_ns=None)
            return handle

    def _require_attempt(self, handle):
        if type(handle) is not _AttemptHandle or handle is not self._active:
            self._reject("timing_attempt_handle_invalid")

    def send_started(self, handle):
        with self._lock:
            self._require_attempt(handle)
            if self._attempt["send_started_ns"] is not None or self._attempt["transport_terminal_ns"] is not None:
                self._reject()
            started = self._new_work()
            self._attempt["send_started_ns"] = started
            # One checked dispatch checkpoint supplies both the observation and
            # its strictly positive budget. No second pre-dispatch sample can
            # expire after recording a send that never reached the delegate.
            return min(self._request_cap, self._phase_cap - started)

    def remaining_request_ns(self, handle):
        """Budget for the actual transport await; never a retry authorization."""
        with self._lock:
            self._require_attempt(handle)
            send = self._attempt["send_started_ns"]
            if send is None or self._attempt["transport_terminal_ns"] is not None:
                self._reject()
            if self._halted:
                self._reject("timing_capture_halted")
            now = self._sample()
            remaining = min(self._phase_cap - now, self._request_cap - (now - send))
            if remaining <= 0:
                self._halted = True
            return max(0, remaining)

    def transport_terminal(self, handle):
        with self._lock:
            self._require_attempt(handle)
            if self._attempt["transport_terminal_ns"] is not None:
                self._reject()
            terminal = self._sample()
            self._attempt["transport_terminal_ns"] = terminal
            send = self._attempt["send_started_ns"]
            if send is not None and terminal - send > self._request_cap:
                self._halted = True

    def remaining_phase_ns(self):
        """Cleanup budget, even after request failure; no new-work permission."""
        with self._lock:
            if self._closed:
                self._reject("timing_capture_halted")
            return max(0, self._phase_cap - self._sample())

    def abort_new_work(self):
        """Poison future attempts while permitting bounded terminal cleanup."""
        with self._lock:
            self._halted = True

    def _observe_cleanup(self, raw_cleanup_status):
        if raw_cleanup_status not in {"completed", "not_applicable", "failed", "timed_out", "cancelled", "unknown"}:
            self._reject()
        if self._cleanup_recorded:
            self._reject()
        terminal = self._attempt["transport_terminal_ns"]
        if terminal is None and raw_cleanup_status != "unknown":
            self._reject()
        end = None
        if raw_cleanup_status == "not_applicable":
            end = terminal
        elif raw_cleanup_status != "unknown":
            end = self._sample()
            self._attempt["raw_cleanup_terminal_ns"] = end
        self._attempt.update(raw_cleanup_status=raw_cleanup_status, lifetime_ended_ns=end)
        self._cleanup_recorded = True
        if raw_cleanup_status not in {"completed", "not_applicable"}:
            self._halted = True

    def raw_cleanup_terminal(self, handle, *, status: str):
        """Observe actual cleanup now; later parse/ledger work cannot retime it."""
        with self._lock:
            self._require_attempt(handle)
            self._observe_cleanup(status)

    def finish_attempt(self, handle, *, terminal_kind: str, raw_cleanup_status: str):
        with self._lock:
            self._require_attempt(handle)
            if terminal_kind not in {"response_accepted", "response_rejected", "http_error",
                                     "no_response", "cancelled", "outcome_unknown"}:
                self._reject()
            if raw_cleanup_status not in {"completed", "not_applicable", "failed", "timed_out", "cancelled", "unknown"}:
                self._reject()
            sent = self._attempt["send_started_ns"] is not None
            if sent != (terminal_kind in {"response_accepted", "response_rejected", "http_error", "outcome_unknown"}):
                self._reject()
            if self._cleanup_recorded:
                if self._attempt["raw_cleanup_status"] != raw_cleanup_status:
                    self._reject()
            else:
                self._observe_cleanup(raw_cleanup_status)
            self._attempt["terminal_kind"] = terminal_kind
            if terminal_kind != "response_accepted" or raw_cleanup_status != "completed":
                self._halted = True
            self._completed.append(self._attempt)
            self._active = self._attempt = None

    def post_cleanup_terminal(self, *, status: str):
        with self._lock:
            if self._active is not None or self._post_recorded or self._closed:
                self._reject()
            if status not in {"completed", "failed", "timed_out", "cancelled", "unknown"}:
                self._reject()
            value = None if status == "unknown" else self._sample()
            self._phase.update(post_cleanup_terminal_ns=value, post_cleanup_status=status)
            self._post_recorded = True
            if status != "completed":
                self._halted = True

    def key_reference_released(self, *, status: str):
        with self._lock:
            if self._active is not None or self._key_recorded or self._closed:
                self._reject()
            if status not in {"released", "unknown"}:
                self._reject()
            value = None if status == "unknown" else self._sample()
            self._phase.update(key_reference_released_ns=value, key_reference_release_status=status)
            self._key_recorded = True
            if status != "released":
                self._halted = True

    def phase_terminal(self):
        with self._lock:
            if self._active is not None or not self._post_recorded or not self._key_recorded or self._closed:
                self._reject()
            self._phase["phase_terminal_ns"] = self._sample()
            self._closed = True

    def snapshot(self):
        """Detached diagnostic offsets only; not signed or ledger-bound evidence."""
        with self._lock:
            return deepcopy(dict(phase=self._phase, attempts=self._completed,
                                 active_attempt=self._attempt, halted=self._halted,
                                 clock_failed=self._clock_failed, closed=self._closed))

    def _sealing_offset(self):
        """Same private origin, after a successful phase; no new model work."""
        with self._lock:
            if not self._closed or self._halted or self._clock_failed:
                self._reject("timing_sealing_not_ready")
            offset = self._read_native() - self._origin
            if not self._last <= offset <= MAX_OFFSET_NS:
                self._reject("timing_clock_order_invalid")
            self._last = offset
            return offset
