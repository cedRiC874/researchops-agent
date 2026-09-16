"""Actual scoring of synthetic observations using one fixed local candidate."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2]))

import argparse
import json
from services.agent_workflow_comparison_v1.scorer_preintegration_v1 import bootstrap
from services.agent_workflow_comparison_v1.scorer_preintegration_v1.bridge import make_plan, invoke, fault_inputs
from services.agent_workflow_comparison_v1.aggregate_read_v1.core import load_json


def collect_and_score():
    # Gold/contract/expectation files are not opened until BOTH paths are finished.
    observations = bootstrap.aggregate_run.LOOP.run_until_complete(bootstrap.aggregate_run.run_all())
    contracts = load_json(ROOT / "contracts.json")["contracts"]
    result = {"description": "实际评分器对SDK＋FakeModel等合成开发观察的离线预集成验证。本结果不自动证明上游CI、正式集成或发布验收完成。",
              "identity": bootstrap.IDENTITY, "environment": bootstrap.ENVIRONMENT,
              "upstream": load_json(ROOT / "upstream.json"), "observations": observations,
              "business": {}, "fault_validation": [], "unknown_summary": {}, "blockers": []}
    for path, coverage in observations["paths"].items():
        plan = make_plan(contracts, coverage)
        call = invoke(bootstrap.PACKAGE, bootstrap.IDENTITY, "score_plan", plan)
        result["business"][path] = {"input_plan": plan, "scorer_call": call}
        if call["error"]:
            result["blockers"].append({"path_kind": path, "error": call["error"]})
        else:
            report = call["raw_report"]
            result["unknown_summary"][path] = {
                "task_unknown_fraction": {"numerator": report["counts"]["unknown"], "denominator": report["counts"]["planned"]},
                "unparsed_content_task_ids": [t["task_id"] for t in report["tasks"] if any(
                    c["code"] == "unparsed_content" for d in t["dimensions"].values() for c in d["checks"])],
                "interpretation": "Parser coverage is not a capability ranking; fixed templates have a format advantage."}
    for item in fault_inputs(observations, contracts):
        result["fault_validation"].append({**item,
            "scorer_call": invoke(bootstrap.PACKAGE, bootstrap.IDENTITY, item["method"], item["payload"])})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    target = Path(args.output).resolve()
    if not target.is_relative_to(ROOT) or target.suffix != ".json" or not target.parent.is_dir():
        raise ValueError("output must be new JSON under scorer_preintegration_v1")
    with target.open("x", encoding="utf-8", newline="\n") as f:
        result = collect_and_score()
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    # Rejection remains visible and makes this integration run non-successful.
    code = 2 if result["blockers"] else 0
    print(json.dumps({"output": str(target), "actual_exit_code": code,
                      "formal_integration_accepted": False}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        bootstrap.aggregate_run.LOOP.close()
