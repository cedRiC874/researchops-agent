"""Actual SDK loop over a sealed MockTransport-only decision protocol.

This is NOT a Provider adapter or a production-authorization capability.
"""
import asyncio
from copy import deepcopy
import json

import httpx
from agents import Agent, ModelResponse, ModelSettings, RunConfig, Runner, Usage, function_tool
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from .binding import canonical, digest, verify_snapshot
from .budget import StopRun
from .paths import FAKE_KEY, INSTRUCTION, safe


class MockDecisionTransport:
    def __init__(self, script, faults=None):
        if script.get("source_kind") != "synthetic_fixture" or script.get("response_origin") != "scripted_mock_response":
            raise ValueError("synthetic_script_required")
        self.script = deepcopy(script)
        self.faults = faults or {}
        self.index = 0
        self.hits = []

    async def handle(self, request):
        if request.url.host != "item6.invalid" or request.headers.get("authorization") != "Bearer " + FAKE_KEY:
            raise ValueError("invalid_offline_wire_scope")
        self.index += 1
        if self.faults.get("transport_timeout"):
            self.hits.append("transport_timeout")
            raise httpx.ReadTimeout("synthetic timeout")
        if self.faults.get("cancel"):
            self.hits.append("cancel")
            raise asyncio.CancelledError()
        if self.faults.get("delay"):
            self.hits.append("delay")
            await asyncio.sleep(self.faults["delay"])
        step = deepcopy(self.script["steps"][self.index - 1])
        if self.faults.get("missing_text") and step["kind"] == "final":
            self.hits.append("missing_text")
            step["text"] = None
        usage = {"input_tokens": 100, "output_tokens": 30}
        if self.faults.get("missing_usage"):
            self.hits.append("missing_usage")
            usage = None
        if self.faults.get("usage_overrun"):
            self.hits.append("usage_overrun")
            usage["output_tokens"] = 1001
        if self.faults.get("privacy_response"):
            self.hits.append("privacy_response")
            step = {"kind": "final", "text": FAKE_KEY, "status": "completed"}
        return httpx.Response(200, json={"protocol": "item6-offline-decision-fixture-v1", "step": step, "usage": usage})


def client_for(transport, key):
    # No arbitrary transport, real-key file, env lookup, endpoint or online switch.
    if type(transport) is not httpx.MockTransport or key != FAKE_KEY:
        raise ValueError("mock_transport_and_fake_key_only")
    return httpx.AsyncClient(transport=transport, base_url="https://item6.invalid", trust_env=False,
                             headers={"Authorization": "Bearer " + key})


class BudgetedMockModel(Model):
    def __init__(self, session, client):
        self.session, self.client = session, client

    async def get_response(self, *args, **kwargs):
        s = self.session
        verify_snapshot(s.snapshot)
        payload = {"protocol": "item6-offline-decision-fixture-v1",
                   "input": kwargs.get("input", args[1] if len(args) > 1 else None),
                   "instructions": INSTRUCTION,
                   "tools": [{"name": t.name, "schema": t.params_json_schema} for t in kwargs.get("tools", [])]}
        safe(payload)
        wire = canonical(payload)
        # UTF-8 bytes are only a conservative engineering test counter, not a verified native tokenizer.
        row = s.budget.reserve(s.key, len(wire), len(wire))
        row["input_counter_origin"] = "offline_utf8_byte_bound_not_validated_for_real_model"
        s.record["model_requests"].append({"index": row["index"], "request_sha256": digest(payload),
                                          "origin": "synthetic_mock_transport", "provider_observed": False})
        try:
            response = await asyncio.wait_for(self.client.post("/decision", content=wire), s.budget.limits["request_seconds"])
        except asyncio.CancelledError:
            s.budget.request_failed(row, "cancelled")
        except (httpx.TimeoutException, TimeoutError):
            s.completion = "timeout"
            s.budget.request_failed(row, "model_timeout")
        data = response.json()
        try:
            safe(data)
        except StopRun:
            s.budget.request_failed(row, "privacy_boundary")
        step = data["step"]
        if step["kind"] == "tool":
            s.plan(step["tool"], step["arguments"], "synthetic_mock_response")
            output = [ResponseFunctionToolCall(type="function_call", call_id=f"M{row['index']}",
                name=step["tool"], arguments=json.dumps(step["arguments"]))]
        else:
            s.text, s.status = step["text"], step["status"]
            s.completion = "complete"
            content = [] if s.text is None else [ResponseOutputText(type="output_text", text=s.text, annotations=[])]
            output = [ResponseOutputMessage(id=f"R{row['index']}", type="message", role="assistant", status="completed", content=content)]
        s.record["model_requests"][-1]["response_sha256"] = digest(data)
        s.budget.settle(row, data.get("usage"))
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise ValueError("offline_nonstreaming_only")


async def agent_path(session, script):
    wire = MockDecisionTransport(script, session.faults)
    async with client_for(httpx.MockTransport(wire.handle), FAKE_KEY) as client:
        model = BudgetedMockModel(session, client)

        @function_tool(failure_error_function=None)
        async def inspect_sources(scope_id: str) -> str:
            """Read public metadata for the authorized synthetic scope."""
            return json.dumps(await session.call("inspect_sources", {"scope_id": scope_id}), ensure_ascii=False)

        @function_tool(failure_error_function=None)
        async def read_aggregate(bundle_id: str) -> str:
            """Read actual aggregate facts in the authorized synthetic scope."""
            return json.dumps(await session.call("read_aggregate", {"bundle_id": bundle_id}), ensure_ascii=False)

        agent = Agent(name="Offline comparison engineering", model=model, instructions=INSTRUCTION,
                      tools=[inspect_sources, read_aggregate], model_settings=ModelSettings(parallel_tool_calls=False, max_tokens=1000))
        try:
            await Runner.run(agent, json.dumps(session.task, ensure_ascii=False),
                max_turns=session.budget.limits["requests_per_task"],
                run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False))
        finally:
            session.record["fault_hits"].extend(wire.hits)
    return session.finish()
