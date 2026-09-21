"""Offline-only batch entry: no production capability, real Provider or key lookup."""
from copy import deepcopy
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

from .binding import ROOT, REPO, EXECUTION_BASE, SCORER, sha, digest, source_snapshot, verify_snapshot, reserve_output, seal, check_seal
from .budget import Budget, StopRun
from .human_record import unobserved_review


def fixed_dependency():
    from ..controlled_publication_v1.binding import verify_identity
    return verify_identity()


# Git identity first; then irreversible per-process network/child-process denial.
IDENTITY = fixed_dependency()
LOCKED = dict(line.split("==", 1) for line in (REPO / "requirements.lock").read_text(encoding="utf-8").splitlines()
              if "==" in line and not line.startswith("#"))
DEPENDENCIES = {name: importlib.metadata.version(name) for name in ("openai-agents", "openai", "httpx", "pydantic", "pydantic_core", "griffelib")}
if any(LOCKED[name] != version for name, version in DEPENDENCIES.items()):
    raise ValueError("runtime_dependency_version_drift")
LOOP = asyncio.new_event_loop()
from ..core import deny_network
deny_network()
sys.path.insert(0, str(REPO / "src"))
SCORING = importlib.import_module("researchops_behavior_eval_v1")
if Path(SCORING.__file__).resolve() != REPO / "src/researchops_behavior_eval_v1/__init__.py":
    raise ValueError("foreign_scorer_import")
from .paths import Session, fixed_path, read_json, public_task
from .mock_model import agent_path


def observation_for_score(record):
    if record is None:
        return None, []
    fields = ("task_id", "run_id", "execution_state", "final_output", "completion", "events_complete",
              "approval_interruptions", "side_effects", "side_effects_complete", "delivered_artifacts")
    observation = {k: deepcopy(record[k]) for k in fields}
    observation["events"] = [{k: deepcopy(e[k]) for k in ("call_id", "tool", "arguments", "status", "produced_artifacts")}
                             for e in record["events"]]
    evidence = [{k: deepcopy(e[k]) for k in ("artifact_id", "run_id", "call_id", "facts")} for e in record["allowed_evidence"]]
    return observation, evidence


def score_records(tasks, records, contract_hash):
    contract_path = ROOT / "scoring/contracts.json"
    if sha(contract_path) != contract_hash:
        raise StopRun("scoring_contract_drift")
    contracts = read_json(contract_path)["contracts"]
    result = {}
    for path in ("fixed_workflow", "agent"):
        rows = []
        for task in tasks:
            obs, evidence = observation_for_score(records[path][task["task_id"]])
            rows.append({"contract": deepcopy(contracts[task["task_id"]]), "observation": obs, "allowed_evidence": evidence})
        plan = {"schema_version": "internal-behavior-eval-v1.0", "source_kind": "internal/developer-known", "tasks": rows}
        try:
            report = SCORING.score_plan(plan)
            result[path] = {"input": plan, "raw_report": report, "error": None}
        except SCORING.ContractError as exc:
            result[path] = {"input": plan, "raw_report": None, "error": {"code": exc.code, "path": exc.path, "message": str(exc)}}
    return result


async def execute_batch(run_id, *, mode="offline_mock", faults=None, limits_override=None, tasks_override=None, clock=None):
    # Never accept an online authorization, real key, origin or injected live client here.
    if mode != "offline_mock":
        raise StopRun("real_execution_entry_not_approved")
    spec = read_json(ROOT / "experiment_spec.json")
    tasks = deepcopy(tasks_override) if tasks_override is not None else read_json(ROOT / "tasks/frozen/tasks.json")["tasks"]
    # Validate the complete plan before starting either path, including later rows.
    tasks = [public_task(task) for task in tasks]
    ids = [t["task_id"] for t in tasks]
    if not tasks or len(ids) != len(set(ids)):
        raise ValueError("invalid_plan_identity")
    scripts = read_json(ROOT / "fixtures/development/mock_responses.json")
    snapshot = source_snapshot()
    # The launcher binds gold bytes; neither path receives or parses them.
    contract_hash = sha(ROOT / "scoring/contracts.json")
    limits = {**spec["limits"], **(limits_override or {})}
    budget = Budget(limits, clock)
    records = {"fixed_workflow": {}, "agent": {}}
    ordered = []
    for index, task in enumerate(tasks):
        ordered.extend((task, p) for p in (("fixed_workflow", "agent") if index % 2 == 0 else ("agent", "fixed_workflow")))
    for task, path in ordered:
        session = Session(task, path, run_id + "-" + path + "-" + task["task_id"], budget, snapshot,
                          (faults or {}).get(task["task_id"], {}))
        if budget.stop_reason:
            session.record["execution_state"] = "not_executed"
            session.record["known_failures"].append({"code": "not_executed", "batch_stop_reason": budget.stop_reason})
            records[path][task["task_id"]] = session.finish(None, "failed", "unknown")
            continue
        admitted = False
        try:
            verify_snapshot(snapshot)
            budget.begin_task(session.key)
            admitted = True
            if session.faults.get("cancel_before_start"):
                session.record["fault_hits"].append("cancel_before_start")
                budget.stop("cancelled")
            record = await (fixed_path(session) if path == "fixed_workflow" else agent_path(session, scripts[task["task_id"]]))
        except (StopRun, asyncio.CancelledError) as exc:
            code = getattr(exc, "code", "cancelled")
            budget.stop_reason = budget.stop_reason or code
            session.record["known_failures"].append({"code": code})
            if not admitted:
                session.record["execution_state"] = "not_executed"
            record = session.finish()
        except Exception as exc:
            budget.stop_reason = budget.stop_reason or "execution_exception"
            session.record["known_failures"].append({"code": budget.stop_reason, "exception_type": type(exc).__name__})
            record = session.finish()
        records[path][task["task_id"]] = record
    source_stable = True
    try:
        verify_snapshot(snapshot)
        scores = score_records(tasks, records, contract_hash)
    except StopRun as exc:
        source_stable = exc.code != "source_drift"
        budget.stop_reason = budget.stop_reason or exc.code
        scores = {p: {"input": None, "raw_report": None, "error": {"code": exc.code}} for p in records}
    return {"observation_scope": "offline_engineering_mock", "formal_comparison_result": False,
            "identity": IDENTITY, "runtime_environment": {"python": sys.version, "dependencies": DEPENDENCIES},
            "runtime_input_binding": snapshot, "scoring_contract_sha256": contract_hash,
            "planned_tasks": ids, "execution_order": [{"task_id": t["task_id"], "path": p} for t, p in ordered],
            "records": records, "budget": budget.snapshot(), "scores": scores, "source_stable": source_stable,
            "post_run_human_reviews": {path: [unobserved_review(tid) for tid in ids] for path in records},
            "online_execution_authorized": False, "real_provider_calls": 0,
            "limitations": ["MockTransport protocol is not a target Provider adapter", "model/rates/native token bound unselected",
                            "legacy Phase6 prompt/tool/telemetry contract cannot be silently replaced",
                            "no new live sending entry approved or enabled"]}


def run_to_new_directory(run_id):
    before = source_snapshot()
    directory = reserve_output(ROOT / "outputs" / run_id, run_id, before)
    result = LOOP.run_until_complete(execute_batch(run_id))
    if source_snapshot() != before:
        result["source_stable"] = False
        result["budget"]["stop_reason"] = result["budget"]["stop_reason"] or "source_drift"
    with (directory / "observations.json").open("x", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
    sealed = seal(directory, ["offline-owner.json", "observations.json"])
    check_seal(directory, sealed["manifest_sha256"])
    return result, directory, sealed
