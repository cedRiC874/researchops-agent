"""IO-free projection of recorded layers. Does not score or authenticate input."""
from collections import Counter
from copy import deepcopy
from . import contract as c

VERSION = "item6-outcome-layers/1.0"
VERDICTS = frozenset(("pass", "fail", "unknown"))


def project(document):
    """Preserve all planned rows and original scores; never infer text semantics."""
    c.require(type(document) is dict and type(document.get("business")) is dict
              and set(document["business"]) == {"agent", "fixed_workflow"}, "outcome_layers_input")
    plan = document["authority"]["freeze"]["business_plan"]
    pairs = [(row["path_kind"], row["task_id"]) for row in plan]
    c.require(len(pairs) == 32 and len(set(pairs)) == 32
              and all(path in document["business"] for path, _ in pairs), "outcome_layers_denominator")
    scoring = {}
    for path, rows in document["business"].items():
        wanted = {task for kind, task in pairs if kind == path}
        c.require(type(rows) is dict and set(rows) == wanted and len(rows) == 16, "outcome_layers_denominator")
        report = document["scores"][path]["raw_report"]
        c.require(type(report) is dict and type(report.get("tasks")) is list, "outcome_layers_score_missing")
        scored = report["tasks"]
        c.require(len(scored) == len(wanted) and {r["task_id"] for r in scored} == wanted,
                  "outcome_layers_score_identity")
        scoring[path] = {r["task_id"]: r for r in scored}
    result = []
    for path, task_id in pairs:
        row = document["business"][path][task_id]; score = scoring[path][task_id]
        c.require(row["task_id"] == task_id and row["path_kind"] == path
                  and score["run_id"] == row["run_id"] and score["task_verdict"] in VERDICTS,
                  "outcome_layers_score_identity")
        c.require(type(row["plan"]) is list and type(row["events"]) is list
                  and row["execution_state"] in {"observed", "not_executed"}, "outcome_layers_input")
        rejection = row["case_rejection"]
        if rejection is not None:
            c.require(path == "agent" and row["status"] == "failed"
                      and row["final_output"] is None and not row["events"]
                      and rejection["state"] in {"isolated", "rejected"}, "outcome_layers_rejection")
        result.append(dict(path=path, task_id=task_id, case_run_id=row["run_id"],
            operation=dict(execution_state=row["execution_state"], status=row["status"],
                completion=row["completion"], final_output_observed=row["final_output"] is not None,
                proposed_tool_calls=len(row["plan"]), actual_tool_events=len(row["events"]),
                unexecuted_proposals=sum(p["execution"] == "not_executed" for p in row["plan"]),
                known_failure_codes=[f["code"] for f in row["known_failures"]]),
            rejection=None if rejection is None else dict(state=rejection["state"],
                reason_kind=rejection["reason_kind"], code=rejection["code"]),
            original_scoring=deepcopy(score)))
    return dict(schema_version=VERSION, run_id=document["run_id"],
        observation_kind="projection_of_supplied_recorded_observations", archive_authenticity_verified=False,
        rescored=False, semantic_manual_review_performed=False, planned=32, rows=result,
        observed=sum(r["operation"]["execution_state"] == "observed" for r in result),
        not_executed=sum(r["operation"]["execution_state"] == "not_executed" for r in result),
        original_verdict_counts={path: dict(Counter(r["original_scoring"]["task_verdict"]
            for r in result if r["path"] == path)) for path in document["business"]})
