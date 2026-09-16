import json
from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from ..sdk_path import ScriptedModel
from .core import CONFIG, INSTRUCTION, Session, digest


async def agent_run(task, script, run_id):
    session = Session(task, "agent", run_id)
    model = ScriptedModel(session, script)
    session.record["model_response_origin"] = "scripted_model_response"
    session.record["binding"]["script_sha256"] = digest(script)

    @function_tool(failure_error_function=None)
    def read_aggregate_fixture(bundle_id: str) -> str:
        """Read an allowlisted synthetic aggregate bundle. No writes or analysis."""
        return json.dumps(session.execute(bundle_id), ensure_ascii=False)

    agent = Agent(name="Offline aggregate fixture", model=model, instructions=INSTRUCTION,
                  tools=[read_aggregate_fixture], model_settings=ModelSettings(parallel_tool_calls=False))
    try:
        await Runner.run(agent, json.dumps(session.visible, ensure_ascii=False, sort_keys=True),
                         max_turns=CONFIG["max_model_requests"],
                         run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False))
        if model.final is None:
            raise ValueError("final_response_missing")
        return session.finish(model.final["text"], model.final["status"], origin="scripted_model_response")
    except Exception as exc:
        session.record["known_failures"].append({"code": "sdk_or_script_failure", "type": type(exc).__name__})
        return session.finish(None, "failed", origin="collector", completion="unknown")
