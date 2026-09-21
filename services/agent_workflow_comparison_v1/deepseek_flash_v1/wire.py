"""Native Responses SDK requests over exact MockTransport; no live transport capability."""
import asyncio
from copy import deepcopy
import json

import httpx2
from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.models.interface import Model
from agents.models.openai_responses import OpenAIResponsesModel

from ..controlled_comparison_v1.budget import StopRun
from ..controlled_comparison_v1.binding import canonical, digest, verify_snapshot
from ..controlled_comparison_v1.paths import FAKE_KEY, INSTRUCTION, safe

MODEL = "deepseek-flash"
ORIGIN = "https://api.deepseek.com"


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StopRun("duplicate_response_field")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(StopRun("nonfinite_response")))


class ResponsesFixture:
    """Hand-authored decision steps serialized into native Responses wire objects."""
    def __init__(self, script, faults=None):
        if script.get("source_kind") != "synthetic_fixture" or script.get("response_origin") != "scripted_mock_response":
            raise ValueError("synthetic_script_required")
        self.steps = deepcopy(script["steps"])
        self.faults = faults or {}
        self.count = 0
        self.hits = []
        self.requests = []  # safe request bodies only; never header or credentials

    async def handle(self, request):
        self.count += 1
        if str(request.url) != ORIGIN + "/responses" or request.method != "POST":
            raise StopRun("fixture_request_scope")
        if request.headers.get("authorization") != "Bearer " + FAKE_KEY:
            raise StopRun("fixture_fake_key_required")
        self.requests.append(strict_json(request.content))
        if "http_status" in self.faults:
            self.hits.append("http_status")
            status = self.faults["http_status"]
            return httpx2.Response(status, headers={"location": "https://example.invalid/redirect", "retry-after": "0"},
                                   json={"error": {"message": FAKE_KEY, "type": "synthetic_error"}})
        if self.faults.get("timeout"):
            self.hits.append("timeout")
            raise httpx2.ReadTimeout("synthetic timeout", request=request)
        if self.faults.get("cancel"):
            self.hits.append("cancel")
            raise asyncio.CancelledError()
        if self.faults.get("delay"):
            self.hits.append("delay")
            await asyncio.sleep(self.faults["delay"])
        if self.faults.get("invalid_json"):
            self.hits.append("invalid_json")
            return httpx2.Response(200, content=b"{", headers={"content-type": "application/json"})
        if self.faults.get("duplicate_json"):
            self.hits.append("duplicate_json")
            return httpx2.Response(200, content=b'{"status":"completed","status":"failed"}', headers={"content-type": "application/json"})
        step = deepcopy(self.steps[self.count - 1])
        if step["kind"] == "tool":
            output = [{"type": "function_call", "id": f"fc_{self.count}", "call_id": f"call_{self.count}",
                       "name": step["tool"], "arguments": json.dumps(step["arguments"]), "status": "completed"}]
        else:
            output = [{"type": "message", "id": f"msg_{self.count}", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": step["text"], "annotations": []}]}]
        payload = {"id": f"fixture_response_{self.count}", "object": "response", "created_at": 0,
                   "model": MODEL, "status": "completed", "output": output, "error": None,
                   "incomplete_details": None, "parallel_tool_calls": False, "store": False,
                   "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                       "input_tokens_details": {"cached_tokens": 10}, "output_tokens_details": {"reasoning_tokens": 0}}}
        for name in self.faults:
            if name == "missing_usage": payload.pop("usage")
            elif name == "null_usage": payload["usage"] = None
            elif name == "usage_total_mismatch": payload["usage"]["total_tokens"] = 1
            elif name == "usage_boolean": payload["usage"]["input_tokens"] = True
            elif name == "usage_overrun": payload["usage"].update(output_tokens=1001, total_tokens=1101)
            elif name == "cache_overrun": payload["usage"]["input_tokens_details"]["cached_tokens"] = 101
            elif name == "missing_usage_details":
                payload["usage"].pop("input_tokens_details"); payload["usage"].pop("output_tokens_details")
            elif name == "invalid_usage_details": payload["usage"]["input_tokens_details"] = "invalid"
            elif name == "incomplete_call" and step["kind"] == "tool": output[0]["status"] = "incomplete"
            elif name == "incomplete_message": payload["output"] = [{"type": "message", "id": "partial", "role": "assistant", "status": "incomplete", "content": []}]
            elif name == "wrong_model": payload["model"] = "deepseek-v4-flash"
            elif name == "reasoning_output": payload["output"].insert(0, {"id": "rs_1", "type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "PRIVATE_REASONING_SENTINEL"}]})
            elif name == "truncated": payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            elif name == "failed_status": payload.update(status="failed", error={"code": "server_error", "message": FAKE_KEY})
            elif name == "in_progress": payload["status"] = "in_progress"
            elif name == "empty_output": payload["output"] = []
            elif name == "duplicate_call_id" and step["kind"] == "tool": output[0]["call_id"] = "call_1"
            elif name == "privacy_text": payload["output"] = [{"type": "message", "role": "assistant", "id": "leak", "status": "completed", "content": [{"type": "output_text", "text": FAKE_KEY, "annotations": []}]}]
            elif name == "invalid_arguments" and step["kind"] == "tool": output[0]["arguments"] = "{"
            elif name == "multiple_tools" and step["kind"] == "tool":
                second = deepcopy(output[0]); second.update(id="fc_extra", call_id="call_extra"); output.append(second)
            elif name == "unknown_tool" and step["kind"] == "tool": output[0]["name"] = "publish_results"
            elif name == "duplicate_arguments" and step["kind"] == "tool": output[0]["arguments"] = '{"bundle_id":"aggregate-01","bundle_id":"aggregate-02"}'
            elif name == "oversized_response": payload["unused"] = "x" * 100001
            else: continue
            self.hits.append(name)
        return httpx2.Response(200, json=payload)


class Capture:
    def __init__(self, session, wire_check):
        self.s = session
        self.wire_check = wire_check
        self.current = None
        self.send_count = 0
        self.response_projection = []
        self.call_ids = set()
        self.pending_call_id = None

    def fail(self, code):
        if self.current is not None and self.s.budget.inflight is self.current:
            self.s.budget.request_failed(self.current, code)
        self.s.budget.stop(code)

    async def request(self, request):
        self.wire_check()
        verify_snapshot(self.s.snapshot)
        if request.method != "POST" or str(request.url) != ORIGIN + "/responses":
            self.fail("request_origin_path_mismatch")
        body = strict_json(request.content)
        if body.get("model") != MODEL or body.get("stream", False) is not False or body.get("store") is not False:
            self.fail("wire_configuration_mismatch")
        if body.get("reasoning") != {"effort": "none"} or body.get("max_output_tokens") != 1000 or body.get("parallel_tool_calls") is not False:
            self.fail("wire_policy_mismatch")
        if any(body.get(k) is not None for k in ("previous_response_id", "conversation", "prompt", "background")):
            self.fail("stateful_or_background_request_rejected")
        safe(body)
        # Only fixture byte counts; never certify native DeepSeek token overhead.
        self.current = self.s.budget.reserve(self.s.key, len(request.content), len(request.content))
        self.current["input_counter_origin"] = "fixture_utf8_bytes_native_bound_unverified"
        self.send_count += 1
        self.s.record["model_requests"].append({"index": self.current["index"], "wire_request_sha256": digest(body),
            "source": "synthetic_fixture_native_responses_wire", "model_sent": body["model"],
            "api_observed": False, "transport": "httpx2.MockTransport"})

    async def response(self, response):
        if response.status_code != 200:
            self.fail("http_status_" + str(response.status_code))
        await response.aread()
        if len(response.content) > 100000:
            self.fail("response_byte_limit")
        try:
            data = strict_json(response.content)
        except (ValueError, StopRun):
            self.fail("invalid_response_json")
        if type(data) is not dict or data.get("model") != MODEL:
            self.fail("response_model_mismatch")
        status = data.get("status")
        if status not in {"completed", "incomplete", "failed"}:
            self.fail("response_status_unknown")
        if status == "failed":
            self.fail("response_failed")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if type(details) is dict else None
            reason = reason if reason in {"max_output_tokens", "content_filter"} else "unknown"
            self.s.completion = "truncated" if reason == "max_output_tokens" else "unknown"
            self.s.record["known_failures"].append({"code": "response_incomplete", "reason": reason})
        output = data.get("output")
        if not isinstance(output, list):
            self.fail("output_unobserved")
        calls, texts = [], []
        for item in output:
            if not isinstance(item, dict): self.fail("invalid_output_item")
            if item.get("type") == "reasoning":
                self.fail("unexpected_reasoning_not_retained")
            if item.get("type") == "function_call":
                if item.get("status") != "completed":
                    self.fail("incomplete_tool_call")
                calls.append(item)
            elif item.get("type") == "message":
                if item.get("role") != "assistant" or item.get("status") not in {"completed", "incomplete"}:
                    self.fail("invalid_message_state")
                if status == "completed" and item["status"] != "completed":
                    self.fail("inconsistent_message_completion")
                if type(item.get("content")) is not list:
                    self.fail("invalid_text_part")
                for content in item["content"]:
                    if type(content) is not dict or content.get("type") != "output_text" or type(content.get("text")) is not str:
                        self.fail("invalid_text_part")
                    safe(content["text"])
                    texts.append(content["text"])
            else:
                self.fail("unknown_output_item")
        if len(calls) > 1 or len(texts) > 1 or (calls and texts):
            self.fail("unsupported_multi_action_response")
        if texts:
            self.s.text = texts[0]  # No strip, concatenation, or falsy default.
            self.s.status = {"请指定分析设计。": "clarification", "不能伪造数据。": "refusal"}.get(self.s.text, "completed")
            if status == "completed":
                self.s.completion = "complete"
        for call in calls:
            if call.get("name") not in {"inspect_sources", "read_aggregate"} or not isinstance(call.get("call_id"), str) or not call["call_id"]:
                self.fail("tool_name_or_identity_invalid")
            if call["call_id"] in self.call_ids:
                self.fail("duplicate_tool_call_identity")
            safe(call["call_id"])
            self.call_ids.add(call["call_id"])
            self.pending_call_id = call["call_id"]
            try:
                arguments = strict_json(call["arguments"])
            except (ValueError, KeyError, TypeError, StopRun):
                self.fail("tool_arguments_invalid")
            field = "scope_id" if call["name"] == "inspect_sources" else "bundle_id"
            if type(arguments) is not dict or set(arguments) != {field} or type(arguments[field]) is not str:
                self.fail("tool_arguments_invalid")
            safe(arguments)
            self.s.plan(call["name"], arguments, "synthetic_native_responses")
        usage = data.get("usage")
        observed_usage = None
        if isinstance(usage, dict):
            names = ("input_tokens", "output_tokens", "total_tokens")
            if all(type(usage.get(n)) is int and usage[n] >= 0 for n in names):
                if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
                    self.fail("usage_total_mismatch")
                observed_usage = {n: usage[n] for n in names}
                for container, field, parent in (("input_tokens_details", "cached_tokens", "input_tokens"),
                                                 ("output_tokens_details", "reasoning_tokens", "output_tokens")):
                    details = usage.get(container)
                    if details is not None and type(details) is not dict:
                        self.fail("invalid_usage_details")
                    value = details.get(field) if isinstance(details, dict) else None
                    if value is not None and (type(value) is not int or not 0 <= value <= usage[parent]):
                        self.fail("usage_detail_out_of_range")
                    observed_usage[field] = value
        self.response_projection.append({"response_status": status, "model_observed": data["model"],
            "final_text_observation": "unobserved" if not texts else "observed",
            "usage": observed_usage, "usage_origin": "synthetic_native_responses_fixture",
            "reasoning_body_retained": False, "http_headers_retained": False})
        self.s.budget.settle(self.current, None if observed_usage is None else {
            "input_tokens": observed_usage["input_tokens"], "output_tokens": observed_usage["output_tokens"]})
        if status == "incomplete": self.fail("response_incomplete")
        if not calls and not texts: self.fail("final_output_unobserved")


def build_client(transport, capture, key):
    if type(transport) is not httpx2.MockTransport or key != FAKE_KEY:
        raise ValueError("offline_mock_and_explicit_fake_key_required")
    async def request_hook(request):
        try:
            await capture.request(request)
        except StopRun as exc:
            capture.fail(exc.code)  # Record before the SDK wraps hook exceptions.

    async def response_hook(response):
        try:
            await capture.response(response)
        except StopRun as exc:
            capture.fail(exc.code)

    http = httpx2.AsyncClient(transport=transport, timeout=30, follow_redirects=False, trust_env=False,
        event_hooks={"request": [request_hook], "response": [response_hook]})
    return AsyncOpenAI(api_key=key, admin_api_key="", organization="", project="", webhook_secret="",
        base_url=ORIGIN, max_retries=0, timeout=30, http_client=http, default_headers={})


class NativeModel(Model):
    def __init__(self, delegate, capture):
        self.delegate, self.capture = delegate, capture

    async def get_response(self, *args, **kwargs):
        try:
            return await asyncio.wait_for(self.delegate.get_response(*args, **kwargs), self.capture.s.budget.limits["request_seconds"])
        except asyncio.CancelledError:
            self.capture.fail("cancelled")
        except (TimeoutError, APITimeoutError):
            self.capture.s.completion = "timeout"
            self.capture.fail("model_timeout")
        except (APIConnectionError, APIStatusError):
            # Hooks may be wrapped by SDK connection errors: retain the original stop reason.
            self.capture.fail(self.capture.s.budget.stop_reason or "sdk_transport_error")
        except StopRun as exc:
            self.capture.fail(exc.code)

    def stream_response(self, *args, **kwargs):
        raise StopRun("streaming_not_approved")


async def run_agent(session, script, wire_check=lambda: None):
    fixture = ResponsesFixture(script, session.faults)
    capture = Capture(session, wire_check)
    async with build_client(httpx2.MockTransport(fixture.handle), capture, FAKE_KEY) as client:
        model = NativeModel(OpenAIResponsesModel(model=MODEL, openai_client=client), capture)

        async def invoke(tool, arguments):
            start = len(session.record["events"])
            try:
                return json.dumps(await session.call(tool, arguments), ensure_ascii=False)
            finally:
                if len(session.record["events"]) > start:
                    event = session.record["events"][-1]
                    session.record.setdefault("native_tool_links", []).append({
                        "sdk_call_id": capture.pending_call_id, "call_id": event["call_id"],
                        "run_id": session.record["run_id"], "artifact_ids": deepcopy(event["produced_artifacts"])})

        @function_tool(failure_error_function=None)
        async def inspect_sources(scope_id: str) -> str:
            """Read metadata in the authorized synthetic scope."""
            return await invoke("inspect_sources", {"scope_id": scope_id})

        @function_tool(failure_error_function=None)
        async def read_aggregate(bundle_id: str) -> str:
            """Read the requested authorized aggregate result."""
            return await invoke("read_aggregate", {"bundle_id": bundle_id})

        agent = Agent(name="DeepSeek Flash offline Responses compatibility", model=model, instructions=INSTRUCTION,
            tools=[inspect_sources, read_aggregate], model_settings=ModelSettings(max_tokens=1000, parallel_tool_calls=False,
                store=False, reasoning=Reasoning(effort="none")))
        try:
            await Runner.run(agent, json.dumps(session.task, ensure_ascii=False),
                max_turns=session.budget.limits["requests_per_task"],
                run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False))
        finally:
            session.record["fault_hits"].extend(fixture.hits)
            session.record["native_wire_observation"] = {"model_id": MODEL, "request_count": capture.send_count,
                "responses": deepcopy(capture.response_projection), "fixture_requests": deepcopy(fixture.requests),
                "actual_provider_calls": 0, "shared_production_adapter_used": False,
                "sdk": "OpenAIResponsesModel + AsyncOpenAI", "transport": "httpx2.MockTransport"}
    return session.finish()
