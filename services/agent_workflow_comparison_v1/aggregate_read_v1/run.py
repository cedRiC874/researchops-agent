"""Offline-only aggregate CLI. All output files must be new and in this subdirectory."""
from pathlib import Path
import sys
import asyncio

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
# Windows' local wakeup socket pair is infrastructure only, before task code.
LOOP = asyncio.new_event_loop()
from services.agent_workflow_comparison_v1.core import deny_network
deny_network()

import argparse
import importlib.metadata
import json
import time
from services.agent_workflow_comparison_v1.aggregate_read_v1.core import (
    coverage, digest, fixed_run, load_json, metric)
from services.agent_workflow_comparison_v1.aggregate_read_v1.paths import agent_run
from services.agent_workflow_comparison_v1.aggregate_read_v1.adapter import envelope, project, transfer_stub
from services.agent_workflow_comparison_v1.aggregate_read_v1.variants import apply_variant


async def run_all():
    tasks = load_json(HERE / "fixtures/tasks.json")["tasks"]
    scripts = load_json(HERE / "fixtures/scripts.json")
    metadata = load_json(HERE / "fixtures/scenarios.json")["scenarios"]
    variants = load_json(HERE / "fixtures/variants.json")["variants"]
    ids = [t["task_id"] for t in tasks]
    result = {"source_kind": "synthetic_fixture", "score_status": "not_scored",
              "actual_scorer_connected": False, "runtime": {"python": sys.version,
              "openai-agents": importlib.metadata.version("openai-agents")},
              "paths": {}, "stub_transfers": {}, "fault_variants": [],
              "disclosures": ["FakeModel and stub provide no official Agent scores",
                  "damaged source is an explicit fault-handling scenario, not automatically a model defect",
                  "fixed templates fit finite grammar; free-expression unknown must not be ranked as inferior ability"]}
    for path in ("fixed_workflow", "agent"):
        records = []
        for t in tasks:
            rid = f"aggregate-synthetic-{path}-{t['task_id']}"
            started = time.perf_counter()
            r = fixed_run(t, rid) if path == "fixed_workflow" else await agent_run(t, scripts[t["task_id"]], rid)
            r["metrics"]["local_elapsed"] = metric(time.perf_counter() - started, "observed", "local_perf_counter", "second")
            # Metadata is attached only AFTER execution, and cannot alter status.
            r["scenario_metadata"] = metadata[t["task_id"]]
            records.append(r)
        report = coverage(ids, records)
        result["paths"][path] = report
        packet = envelope(report)
        result["stub_transfers"][path] = {"packet": packet, "receipt": transfer_stub(packet)}
    by_id = {r["task_id"]: r["observation"] for r in result["paths"]["agent"]["rows"]}
    for spec in variants:
        variant = apply_variant(by_id[spec["base_task_id"]], spec)
        variant["projected"] = project(variant["observation"])
        result["fault_variants"].append(variant)
    # Keep the original eight-task denominator in this separate collector diagnostic.
    missing = [r for k, r in by_id.items() if k not in {"AGR-01", "AGR-02"}]
    not_run = next(v["observation"] for v in result["fault_variants"] if v["variant_id"] == "FV-not-executed")
    result["coverage_diagnostic"] = {"origin": "collector_fault_fixture", **coverage(ids, [not_run, *missing])}
    result["input_pairing"] = [{"task_id": tid, "same_business_context":
        result["paths"]["fixed_workflow"]["rows"][i]["observation"]["binding"]["input_sha256"] ==
        result["paths"]["agent"]["rows"][i]["observation"]["binding"]["input_sha256"]} for i, tid in enumerate(ids)]
    return result


def output_path(path):
    target = Path(path).resolve()
    if not target.is_relative_to(HERE) or target.suffix != ".json" or not target.parent.is_dir():
        raise ValueError("output must be a new JSON within aggregate_read_v1")
    return target


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    args = p.parse_args(argv)
    try:
        target = output_path(args.output)
        with target.open("x", encoding="utf-8", newline="\n") as f:
            result = LOOP.run_until_complete(run_all())
            json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        print(json.dumps({"output": str(target), "score_status": "not_scored"}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"aggregate offline error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        LOOP.close()
