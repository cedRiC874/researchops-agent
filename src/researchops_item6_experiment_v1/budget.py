"""Item6 request/tool accounting; no claim, timing, SDK or ledger implementation."""
from decimal import Decimal
from copy import deepcopy
from . import contract as c


class Budget:
    def __init__(self, freeze):
        self.limits = deepcopy(freeze["budget"])
        self.pricing = deepcopy(freeze["pricing"])
        self.mode = freeze["mode"]
        self.requests, self.tools, self.cost = [], {}, Decimal(0)
        self.pending = None

    def reserve(self, case_id):
        c.require(self.pending is None, "request_overlap")
        c.require(len(self.requests) < self.limits["model_requests"] and sum(r["case_id"] == case_id for r in self.requests) < self.limits["requests_per_task"], "request_budget")
        cap = self.limits["output_per_request"]
        c.require((len(self.requests) + 1) * cap <= self.limits["output_total"], "output_reservation")
        minimum = Decimal(cap) * Decimal(self.pricing["output_per_million"]) / 1000000
        c.require(self.cost + minimum <= Decimal(self.pricing["cost_limit"]), "cost_reservation")
        row = dict(index=len(self.requests), case_id=case_id, output_reserved=cap,
                   usage=None, cost_settled=False, outcome="reserved", dispatches=0, request_bytes=None)
        self.requests.append(row); self.pending = row
        return row

    def wire(self, size):
        c.require(self.pending is not None and type(size) is int and 0 < size <= self.limits["request_bytes"], "request_bytes")
        self.pending["request_bytes"] = size

    def dispatch(self):
        c.require(self.pending is not None and self.pending["dispatches"] == 0, "repeat_dispatch")
        self.pending["dispatches"] = 1

    def settle(self, record):
        row = self.pending
        c.require(row is not None, "reservation_missing")
        self.pending = None
        row["outcome"] = "response_observed"
        usage = record["usage"]["normalized"]
        row["usage"] = deepcopy(usage)
        names = ("requests", "input_tokens", "output_tokens", "total_tokens")
        c.require(record["usage"]["complete"] is True and all(type(usage.get(k)) is int and usage[k] >= 0 for k in names), "usage_unknown")
        c.require(usage["requests"] == 1 and usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"], "usage_inconsistent")
        for name, parent in (("cached_input_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")):
            value = usage.get(name)
            c.require(value is None or (type(value) is int and 0 <= value <= usage[parent]), "usage_detail")
        # No cache discount is assumed: full input rate is explicit in the approved pricing policy.
        self.cost += (Decimal(usage["input_tokens"]) * Decimal(self.pricing["input_per_million"])
                      + Decimal(usage["output_tokens"]) * Decimal(self.pricing["output_per_million"])) / 1000000
        # A valid observed charge remains known even if it subsequently exceeds a cap.
        row["cost_settled"] = True
        c.require(usage["output_tokens"] <= row["output_reserved"], "output_overshoot")
        c.require(usage["input_tokens"] <= self.limits["input_per_request"] and
                  sum(r["usage"]["input_tokens"] for r in self.requests if r["usage"] is not None) <= self.limits["input_total"], "observed_input_limit")
        c.require(self.cost <= Decimal(self.pricing["cost_limit"]), "observed_cost_limit")

    def unknown(self, code):
        if self.pending is not None:
            self.pending["outcome"] = code
            self.pending = None

    def tool(self, case_id):
        count = self.tools.get(case_id, 0)
        c.require(count < self.limits["tools_per_task"] and sum(self.tools.values()) < self.limits["tool_total"], "tool_budget")
        self.tools[case_id] = count + 1

    def snapshot(self):
        return deepcopy(dict(requests=self.requests, tools=self.tools,
            cost_total=None if any(not r["cost_settled"] for r in self.requests) else str(self.cost),
            known_cost_subtotal=str(self.cost), cost_origin=self.pricing["kind"],
            provider_bill=None, provider_bill_hard_cap=False, mode=self.mode,
            pending_reservation=self.pending is not None))
