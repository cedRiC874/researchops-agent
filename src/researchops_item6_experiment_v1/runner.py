"""Experiment entry through fresh owner, actual Provider Adapter and SDK tools."""
import asyncio
from copy import deepcopy
import json
import logging
import os
from pathlib import Path

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool, set_tracing_disabled
from agents.models.interface import Model
from openai.types.shared import Reasoning
from researchops.audit import AuditLedger
from researchops.model_providers import DeepSeekProvider
from researchops.deepseek_completion_first_live_validation import _environment_isolated, _network_logging_disabled
from services.agent_workflow_comparison_v1.controlled_comparison_v1.paths import INSTRUCTION, fixed_path
from . import contract as c
from .authority import validate_experiment, _claim_experiment
from .session import ExperimentFactory
from .observations import Case, safe_business, score_business, write_archive
from .finalization import FinalizationDeadline, record_failure


class _ObservedModel(Model):
    def __init__(self, delegate, case): self.delegate, self.case = delegate, case
    async def get_response(self, *args, **kwargs):
        case = self.case
        try:
            response = await self.delegate.get_response(*args, **kwargs)
        except BaseException:
            case.sdk_observation_complete = False
            raise
        entries = getattr(response.usage, "request_usage_entries", None)
        c.require(type(entries) is list, "sdk_usage_entries_unavailable")
        case.sdk_usage_indices[case.sdk_raw_count] = list(range(len(entries)))
        case.sdk_raw_count += 1
        requests = getattr(response.usage, "requests", None)
        c.require(type(requests) is int and requests >= 0, "sdk_usage_requests")
        case.sdk_requests += requests
        calls, texts = [], []
        message_count = 0
        for item in response.output:
            if item.type == "reasoning": raise c.ExperimentError("reasoning_not_allowed")
            if item.type == "function_call":
                c.require(getattr(item, "status", None) == "completed", "function_call_not_complete")
                c.require(type(item.call_id) is str and item.call_id not in case.record["native_call_ids"], "call_identity")
                c.require(item.name in {"inspect_sources", "read_aggregate"} and type(item.arguments) is str, "model_tool")
                arguments = c.decode(item.arguments.encode(), 8192)
                field = "scope_id" if item.name == "inspect_sources" else "bundle_id"
                c.require(set(arguments) == {field} and type(arguments[field]) is str, "model_arguments")
                safe_business(arguments, key=case.factory.canary)
                case.record["native_call_ids"].append(item.call_id)
                case.plan(item.name, arguments, "actual_adapter_response")
                calls.append(item)
            elif item.type == "message":
                message_count += 1
                c.require(item.role == "assistant" and item.status == "completed", "message_state")
                for part in item.content:
                    c.require(part.type == "output_text" and type(part.text) is str, "text_part")
                    safe_business(part.text, key=case.factory.canary); texts.append(part.text)
            else: raise c.ExperimentError("output_item")
        # item6-response-actions/1.1: one tool may carry one companion message.
        # Bound messages as well as text parts: the SDK selects the last message,
        # so multiple messages (including empty ones) must not change our answer.
        c.require(len(calls) <= 1 and message_count <= 1 and len(texts) <= 1
                  and bool(calls or texts), "response_actions")
        # Text beside a tool call is intermediate, never a completed answer or
        # evidence. Keep the original response/replay for the SDK's next turn.
        if texts and not calls:
            case.text = texts[0]; case.completion = "complete"
            case.status = {"请指定分析设计。": "clarification", "不能伪造数据。": "refusal"}.get(case.text, "completed")
        case.replay.extend(item.model_dump(mode="json", exclude_unset=True) for item in response.output)
        return response

    def stream_response(self, *args, **kwargs): raise c.ExperimentError("streaming_denied")


async def _agent(case, key):
    session = case.factory.model_session(key)
    async with DeepSeekProvider().open_model(model_id="deepseek-flash", api_key=key,
            timeout_seconds=case.factory.freeze["budget"]["request_seconds"], completion_telemetry_session=session) as provider:
        async def invoke(tool, arguments):
            result = await case.call(tool, arguments)
            output = json.dumps(result, ensure_ascii=False)
            case.replay.append({"type": "function_call_output", "call_id": case.record["native_call_ids"][-1], "output": output})
            return output
        @function_tool(failure_error_function=None)
        async def inspect_sources(scope_id: str) -> str:
            """Read metadata in the authorized synthetic scope."""
            return await invoke("inspect_sources", {"scope_id": scope_id})
        @function_tool(failure_error_function=None)
        async def read_aggregate(bundle_id: str) -> str:
            """Read the requested authorized aggregate result."""
            return await invoke("read_aggregate", {"bundle_id": bundle_id})
        agent = Agent(name="Item6 controlled experiment", instructions=INSTRUCTION,
            model=_ObservedModel(provider.sdk_model, case), tools=[inspect_sources, read_aggregate],
            model_settings=ModelSettings(max_tokens=case.factory.freeze["budget"]["output_per_request"],
                reasoning=Reasoning(effort="none"), store=False, parallel_tool_calls=False))
        await Runner.run(agent, json.dumps(case.task, ensure_ascii=False), max_turns=case.factory.freeze["budget"]["requests_per_task"],
                         run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False))
    return case.finish()


def _exclusive_directory(path):
    base = c.ROOT / "output"
    if base.exists(): c.require(base.is_dir() and not base.is_symlink() and not base.is_junction(), "output_path")
    else: base.mkdir()
    parent = c.ROOT / c.OUTPUT
    if parent.exists(): c.require(parent.is_dir() and not parent.is_symlink() and not parent.is_junction(), "output_path")
    else: parent.mkdir()
    c.require(path.parent == parent and not path.exists() and not path.is_symlink(), "output_exists")
    path.mkdir(exist_ok=False)


async def _run(*, freeze_bytes, approval_bytes, approved_digest, expected_mode):
    try:
        c.require(_environment_isolated(os.environ) and _network_logging_disabled(), "environment_not_isolated")
        prepared = validate_experiment(freeze_bytes=freeze_bytes, approval_bytes=approval_bytes,
                                       approved_digest=approved_digest, expected_mode=expected_mode)
        owner = _claim_experiment(prepared)
    except BaseException as error:
        try:
            frozen_tasks = c.decode(c.source.v2._git(c.ROOT, "show", c.BASE + ":" + c.TASKS))["tasks"]
            error.admission_observations = dict(scope="entry_rejection_not_runtime_authority", baseline_commit=c.BASE,
                runtime_authority_granted=False, planned=32, records=[dict(task_id=t["task_id"], path_kind=p,
                    run_id=None, execution_state="not_executed", final_output=None, model_dispatch_count=0)
                    for i,t in enumerate(frozen_tasks) for p in (("fixed_workflow","agent") if i%2==0 else ("agent","fixed_workflow"))])
        except BaseException:
            error.admission_observations = None
        raise
    try:
        return await _run_owned(owner)
    except BaseException as error:
        owner.stop(c.safe_error(error))
        error.claim_may_exist = True
        raise


async def _run_owned(owner):
    _exclusive_directory(owner.output)
    ledger = AuditLedger(owner.output / "audit.sqlite3", timestamp_format="utc_z")
    factory = ExperimentFactory(owner, ledger)
    owner.clock.task_released()
    key = None
    try:
        key = os.environ.get("DEEPSEEK_API_KEY")
        c.require(type(key) is str and bool(key), "key_unavailable")
        if owner.mode == "offline_test": c.require(key == "offline-fixture-key-item6", "test_key_required")
        else: c.require(key != "offline-fixture-key-item6", "live_test_key_rejected")
        owner.clock.key_loaded(); factory.canary = key
    except BaseException as error:
        key = None
        owner.stop(c.safe_error(error))
    set_tracing_disabled(True)
    tasks = {t["task_id"]: t for t in c.decode(c.read(c.TASKS))["tasks"]}
    records = {"fixed_workflow": {}, "agent": {}}
    for item in factory.freeze["business_plan"]:
        path, task_id = item["path_kind"], item["task_id"]
        case = Case(factory, tasks[task_id], path)
        if owner.stopped:
            case.record["known_failures"].append({"code": "not_executed", "reason": owner.stopped})
            records[path][task_id] = case.finish()
            continue
        admitted = False
        try:
            factory.open_case(case); admitted = True
            case.record["execution_state"] = "observed"
            record = await (fixed_path(case) if path == "fixed_workflow" else _agent(case, key))
        except BaseException as error:
            code = c.safe_error(error)
            if isinstance(error, asyncio.CancelledError): code = "item6_cancelled"
            if owner.stopped and owner.stopped != code:
                case.record["known_failures"].append({"code": owner.stopped})
            owner.stop(code); factory.budget.unknown(code)
            case.record["known_failures"].append({"code": code})
            if not admitted: case.record["execution_state"] = "not_executed"
            record = case.finish()
        finally:
            if admitted:
                try: factory.close_case(sdk_responses=case.sdk_raw_count if case.sdk_observation_complete else None,
                                        sdk_requests=case.sdk_requests if case.sdk_observation_complete else None,
                                        sdk_indices=case.sdk_usage_indices)
                except BaseException as error:
                    owner.stop(c.safe_error(error))
                    case.record["known_failures"].append({"code": c.safe_error(error)})
                    record = case.finish()
                    factory.current = factory.active = None
        records[path][task_id] = record
    denominator = None
    try: denominator = factory.tracker.seal_runtime().to_dict()
    except Exception as error: owner.stop(c.safe_error(error))
    segments = [c.decode(blob) for s in factory.sessions for blob in s.segment_bytes()]
    key = None; factory.canary = None
    for session in factory.sessions: session._capture_canaries = ()
    deadline = None
    try:
        deadline = FinalizationDeadline(owner)
        cleanup_errors = []
        unfinished = owner.clock.snapshot()["active_attempt"] is not None
        # Never retry the failed attempt, invent a handle or claim a phase terminal.
        if unfinished:
            c.require(owner.stopped is not None and bool(factory.attempt_failures), "unfinished_attempt_without_diagnostic")
            cleanup_errors.append("item6_attempt_terminal_unavailable")
        operations = () if unfinished else (lambda: owner.clock.post_cleanup_terminal(status="completed"),
                                            lambda: owner.clock.key_reference_released(status="released"))
        for operation in operations:
            try: operation()
            except BaseException as error:
                code = c.safe_error(error)
                cleanup_errors.append(code); owner.stop(code)
        deadline.check("scoring_before")
        scores = score_business(factory.freeze, records)
        deadline.check("scoring_after")
        status = "failed" if owner.stopped else "completed"
        ledger.set_run_status(owner.run_id, status, terminal_error_code=owner.stopped)
        audit = ledger.export_run(owner.run_id)
        deadline.check("export_after")
        if not unfinished:
            owner.clock.phase_terminal()
            deadline.check("phase_terminal_after")
        document = dict(schema_version="item6-experiment-artifact/1.1", mode=owner.mode, run_id=owner.run_id,
            status=status, authority=owner.snapshots(), business=records, scores=scores,
            binding=factory.binding.runtime_snapshot(), denominator_plan=factory.plan, denominator=denominator,
            budget=factory.budget.snapshot(), dispatches=factory.dispatches, audit=audit,
            segments=segments, phase=owner.clock.snapshot(), cleanup_errors=cleanup_errors, formal_comparison_result=False,
            provider_calls=0 if owner.mode == "offline_test" else factory.dispatches)
        if unfinished:
            from .failed_archive import write_failed_archive
            deadline.check("failure_evidence_before")
            receipt = write_failed_archive(owner.output, document, factory.attempt_failures)
            deadline.check("failure_evidence_after")
            owner.close()
            return dict(status="failed", archive_kind="unfinished_failure", execution_completed=False,
                archive_namespace=c.OUTPUT + "/" + owner.output.name, failure_receipt_sha256=receipt,
                mode=owner.mode, actual_exit_code=2, provider_calls=document["provider_calls"])
        deadline.check("seal_before")
        seal = write_archive(owner.output, document)
        deadline.check("seal_after")
        finalization_sha256 = deadline.finish(seal, status)
    except BaseException as error:
        error.primary_stop_reason = owner.stopped
        error.partial_observations = records
        error.archive_namespace = c.OUTPUT + "/" + owner.output.name
        owner.stop(c.safe_error(error))
        record_failure(owner, error, deadline)
        raise
    owner.close()
    return dict(status=status, archive_namespace=c.OUTPUT + "/" + owner.output.name, seal_sha256=seal,
                finalization_sha256=finalization_sha256,
                mode=owner.mode, actual_exit_code=0 if status == "completed" else 2, provider_calls=document["provider_calls"])


async def run_live(**kwargs):
    return await _run(**kwargs, expected_mode="live")


async def _run_offline_test(**kwargs):
    return await _run(**kwargs, expected_mode="offline_test")
