"""Bounded offline ledger. Synthetic rates/counters never claim real API costs."""
from decimal import Decimal
import time


class StopRun(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Clock:
    source = "local_monotonic"
    def now(self):
        return time.monotonic()


class Budget:
    def __init__(self, limits, clock=None):
        self.limits = dict(limits)
        self.clock = clock or Clock()
        self.started = self.clock.now()
        self.stop_reason = None
        self.requests = []
        self.task_requests = {}
        self.tool_attempts = {}
        self.task_starts = {}
        self.reserved_input = 0
        self.reserved_output = 0
        self.synthetic_cost = Decimal(0)
        self.inflight = None

    def stop(self, code):
        if self.stop_reason is None:
            self.stop_reason = code
        raise StopRun(self.stop_reason)

    def check(self, key=None):
        if self.stop_reason:
            raise StopRun(self.stop_reason)
        if self.clock.now() - self.started >= self.limits["batch_seconds"]:
            self.stop("batch_deadline")
        if key in self.task_starts and self.clock.now() - self.task_starts[key] >= self.limits["task_seconds"]:
            self.stop("task_deadline")

    def begin_task(self, key):
        self.check()
        if key in self.task_starts:
            self.stop("duplicate_task_execution")
        self.task_starts[key] = self.clock.now()

    def reserve(self, key, input_bound, wire_bytes):
        self.check(key)
        if self.inflight is not None:
            self.stop("concurrency_limit")
        if wire_bytes > self.limits["request_bytes"]:
            self.stop("request_byte_limit")
        if input_bound > self.limits["input_per_request"]:
            self.stop("input_bound_limit")
        if len(self.requests) >= self.limits["model_requests"] or self.task_requests.get(key, 0) >= self.limits["requests_per_task"]:
            self.stop("request_limit")
        output_bound = self.limits["output_per_request"]
        if self.reserved_input + input_bound > self.limits["input_total"] or self.reserved_output + output_bound > self.limits["output_total"]:
            self.stop("token_reservation_limit")
        # Deliberately synthetic prices: 1/2 currency units per million input/output tokens.
        reservation = (Decimal(input_bound) + 2 * Decimal(output_bound)) / Decimal(1000000)
        if self.synthetic_cost + reservation > Decimal(self.limits["synthetic_cost_limit"]):
            self.stop("synthetic_cost_pre_send_limit")
        row = {"index": len(self.requests) + 1, "key": key, "input_reserved": input_bound,
               "output_reserved": output_bound, "usage": None, "usage_origin": "synthetic_mock_response",
               "status": "reserved", "started": self.clock.now(), "elapsed": None,
               "clock_source": self.clock.source, "real_api_cost": None}
        self.requests.append(row)
        self.inflight = row
        self.task_requests[key] = self.task_requests.get(key, 0) + 1
        self.reserved_input += input_bound
        self.reserved_output += output_bound
        return row

    def settle(self, row, usage):
        if row is not self.inflight:
            self.stop("request_ownership_mismatch")
        row["elapsed"] = self.clock.now() - row["started"]
        self.inflight = None
        row["status"] = "response_observed"
        if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"} or any(
            type(n) is not int or n < 0 for n in usage.values()
        ):
            self.stop("usage_unavailable")
        row["usage"] = dict(usage)
        self.synthetic_cost += (Decimal(usage["input_tokens"]) + 2 * Decimal(usage["output_tokens"])) / Decimal(1000000)
        if usage["input_tokens"] > row["input_reserved"] or usage["output_tokens"] > row["output_reserved"]:
            self.stop("reported_usage_exceeds_reservation")
        if self.synthetic_cost > Decimal(self.limits["synthetic_cost_limit"]):
            self.stop("synthetic_cost_post_response_limit")
        if row["elapsed"] >= self.limits["request_seconds"]:
            self.stop("request_deadline")

    def request_failed(self, row, code):
        row.update(status=code, elapsed=self.clock.now() - row["started"])
        self.inflight = None
        self.stop(code)

    def tool(self, key):
        self.check(key)
        count = self.tool_attempts.get(key, 0)
        if count >= self.limits["tools_per_task"] or sum(self.tool_attempts.values()) >= self.limits["tool_total"]:
            self.stop("tool_budget")
        self.tool_attempts[key] = count + 1

    def snapshot(self):
        return {"stop_reason": self.stop_reason, "request_attempts": self.requests,
                "tool_attempts": self.tool_attempts, "input_reserved": self.reserved_input,
                "output_reserved": self.reserved_output,
                "synthetic_cost_units": None if any(r["usage"] is None for r in self.requests) else str(self.synthetic_cost),
                "known_synthetic_cost_subtotal": str(self.synthetic_cost),
                "pricing_origin": "synthetic_boundary_test_only", "real_provider_requests": 0,
                "real_api_cost": None, "unsettled_reservation_count": int(self.inflight is not None)}
