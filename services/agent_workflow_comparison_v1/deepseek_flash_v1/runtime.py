"""Isolated native Responses wire experiment. All transports are synthetic."""
from ..controlled_comparison_v1 import runtime as base  # identity, dependencies, network denial
from copy import deepcopy
import asyncio
import importlib.metadata
from pathlib import Path
import re

from ..controlled_comparison_v1.binding import ROOT as OLD, REPO, sha, digest, source_snapshot, verify_snapshot
from ..controlled_comparison_v1.budget import Budget, StopRun
from ..controlled_comparison_v1.paths import Session, fixed_path, public_task, read_json
from .wire import run_agent

ROOT = Path(__file__).resolve().parent
DEPENDENCIES = {**base.DEPENDENCIES, "httpx2": importlib.metadata.version("httpx2")}
if DEPENDENCIES["httpx2"] != base.LOCKED["httpx2"]:
    raise ValueError("httpx2_version_drift")


def snapshot():
    files = [p for p in ROOT.iterdir() if p.suffix in {".py", ".json", ".md"} and p.name != "delivery-manifest.json"]
    return {**source_snapshot(), **{p.relative_to(REPO).as_posix(): sha(p) for p in sorted(files)}}


def preserved():
    from ..controlled_publication_v1.binding import read_manifest
    manifest = read_manifest()
    return {"carried_source_files": {name: sha(REPO / name) == value for name, value in manifest["files"].items()}}


def check_snapshot(expected):
    if snapshot() != expected:
        raise StopRun("native_source_drift")


async def execute_batch(run_id, *, faults=None, limits_override=None, scripts_override=None, mode="offline_mock"):
    if mode != "offline_mock":
        raise StopRun("real_execution_entry_not_approved")
    tasks = [public_task(t) for t in read_json(OLD / "tasks/frozen/tasks.json")["tasks"]]
    scripts = deepcopy(scripts_override) if scripts_override is not None else read_json(OLD / "fixtures/development/mock_responses.json")
    old_snapshot, native_snapshot = source_snapshot(), snapshot()
    gold_hash = sha(OLD / "scoring/contracts.json")
    budget = Budget({**read_json(OLD / "experiment_spec.json")["limits"], **(limits_override or {})})
    records = {"fixed_workflow": {}, "agent": {}}
    ordered = [(task, path) for i, task in enumerate(tasks) for path in
               (("fixed_workflow", "agent") if i % 2 == 0 else ("agent", "fixed_workflow"))]
    for task, path in ordered:
        s = Session(task, path, run_id + "-" + path + "-" + task["task_id"], budget, old_snapshot,
                    (faults or {}).get(task["task_id"], {}) if path == "agent" else {})
        if budget.stop_reason:
            s.record["execution_state"] = "not_executed"
            s.record["known_failures"].append({"code": "not_executed", "batch_stop_reason": budget.stop_reason})
            record = s.finish(None, "failed", "unknown")
        else:
            admitted = False
            try:
                check_snapshot(native_snapshot)
                verify_snapshot(old_snapshot)
                budget.begin_task(s.key)
                admitted = True
                record = await (fixed_path(s) if path == "fixed_workflow" else
                                run_agent(s, scripts[task["task_id"]], lambda: check_snapshot(native_snapshot)))
            except (StopRun, asyncio.CancelledError) as exc:
                code = getattr(exc, "code", "cancelled")
                budget.stop_reason = budget.stop_reason or code
                s.record["known_failures"].append({"code": code})
                if not admitted:
                    s.record["execution_state"] = "not_executed"
                record = s.finish()
            except Exception as exc:
                budget.stop_reason = budget.stop_reason or "execution_exception"
                s.record["known_failures"].append({"code": budget.stop_reason, "exception_type": type(exc).__name__})
                if not admitted:
                    s.record["execution_state"] = "not_executed"
                record = s.finish()
        records[path][task["task_id"]] = record
    stable = snapshot() == native_snapshot
    if not stable:
        budget.stop_reason = budget.stop_reason or "native_source_drift"
    try:
        if not stable:
            raise StopRun("native_source_drift")
        scores = base.score_records(tasks, records, gold_hash)
    except StopRun as exc:
        budget.stop_reason = budget.stop_reason or exc.code
        scores = {p: {"raw_report": None, "input": None, "error": {"code": exc.code}} for p in records}
    return {"kind": "synthetic_native_responses_offline_validation", "formal_comparison_result": False,
            "identity": base.IDENTITY, "dependencies": DEPENDENCIES,
            "selection": read_json(ROOT / "selection.json"), "source_binding": native_snapshot,
            "source_binding_sha256": digest(native_snapshot), "scoring_contract_sha256": gold_hash,
            "planned_tasks": [t["task_id"] for t in tasks], "execution_order": [
                {"task_id": t["task_id"], "path_kind": p} for t, p in ordered],
            "records": records, "budget": budget.snapshot(), "scores": scores,
            "actual_scorer_connected": all(x["error"] is None for x in scores.values()),
            "schema_version": "internal-behavior-eval-v1.0", "measurement_revision": "internal-behavior-eval-v1.1",
            "text_parse_observation": {p: {"planned": len(tasks), "unresolved_text_count": sum(
                bool(base.SCORING.parse_answer(rec["final_output"])["unresolved"])
                for rec in rows.values() if type(rec["final_output"]) is str),
                "missing_text_count": sum(rec["final_output"] is None for rec in rows.values())}
                for p, rows in records.items()},
            "source_stable": stable, "real_provider_calls": 0, "online_authorized": False,
            "human_review_time": None, "human_review_time_origin": "unobserved"}


def new_directory(run_id):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("isolated_run_id_required")
    path = ROOT / "outputs" / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(exist_ok=False)
    return path
