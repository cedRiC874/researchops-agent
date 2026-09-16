"""Actual SDK tool loop, with only a scripted in-memory Model implementation."""
from copy import deepcopy
import json

from agents import Agent, ModelResponse, ModelSettings, Runner, RunConfig, Usage, function_tool
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from .core import CONFIG, Session, digest


class ScriptedModel(Model):
    def __init__(self, session, script):
        if set(script) != {"source_kind", "response_origin", "steps"} or (
            script["source_kind"] != "synthetic_fixture" or
            script["response_origin"] != "scripted_model_response"
        ):
            raise ValueError("only explicit synthetic scripted_model_response accepted")
        self.session = session
        self.script = deepcopy(script)
        self.index = 0
        self.final = None

    async def get_response(self, *args, **kwargs):
        r = self.session.record
        if self.index >= CONFIG["max_model_requests"] or self.index >= len(self.script["steps"]):
            raise ValueError("script_exhausted_or_model_budget")
        step = deepcopy(self.script["steps"][self.index])
        self.index += 1
        # Hash actual SDK input (including prior tool results), not fictitious token counts.
        r["model_requests"].append({"index": self.index, "source": "scripted_model_response",
            "request_sha256": digest(kwargs.get("input", args[1] if len(args) > 1 else None)),
            "response_sha256": digest(step), "api_observed": False})
        if step["kind"] == "tool":
            if set(step) != {"kind", "tool", "arguments"}:
                raise ValueError("invalid scripted tool response")
            self.session.plan(step["tool"], step["arguments"], "scripted_model_response")
            output = [ResponseFunctionToolCall(type="function_call", call_id=f"S{self.index}",
                name=step["tool"], arguments=json.dumps(step["arguments"]))]
        elif step["kind"] == "final":
            if set(step) != {"kind", "text", "status"} or not isinstance(step["text"], str):
                raise ValueError("invalid scripted final response")
            self.final = step
            output = [ResponseOutputMessage(type="message", id=f"M{self.index}", role="assistant",
                status="completed", content=[ResponseOutputText(type="output_text", text=step["text"],
                                                               annotations=[])])]
        else:
            raise ValueError("unknown scripted response")
        # SDK demands Usage; these placeholders are NOT exported as measured usage.
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise RuntimeError("streaming not supported in isolated offline prototype")


async def agent_run(task, script, run_id):
    s = Session(task, "agent", run_id)
    model = ScriptedModel(s, script)
    s.record["model_response_origin"] = "scripted_model_response"
    s.record["binding"]["script_sha256"] = digest(script)

    @function_tool(failure_error_function=None)
    def inspect_dataset(dataset_id: str) -> str:
        """Inspect the selected synthetic dataset; only aggregate projection returned."""
        return json.dumps(s.execute("inspect_dataset", {"dataset_id": dataset_id}), ensure_ascii=False)

    @function_tool(failure_error_function=None)
    def recommend_statistical_method(dataset_id: str) -> str:
        """Recommend from the already inspected dataset and the user's explicit design."""
        return json.dumps(s.execute("recommend_statistical_method", {"dataset_id": dataset_id}), ensure_ascii=False)

    agent = Agent(name="Offline comparison fixture", model=model,
                  instructions=s.visible["instruction"],
                  tools=[inspect_dataset, recommend_statistical_method],
                  model_settings=ModelSettings(parallel_tool_calls=False))
    try:
        await Runner.run(agent, json.dumps(s.visible, ensure_ascii=False, sort_keys=True),
                         max_turns=CONFIG["max_model_requests"],
                         run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False))
        if model.final is None:
            raise ValueError("final_response_not_observed")
        # Read original captured response text; do not strip or apply SDK truthiness defaults.
        return s.finish(model.final["text"], model.final["status"], origin="scripted_model_response")
    except Exception as exc:
        s.record["known_failures"].append({"code": "sdk_or_script_failure", "type": type(exc).__name__})
        return s.finish(None, "failed", origin="collector", completion="unknown")
