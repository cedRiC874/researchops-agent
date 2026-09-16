"""Lossless observation projection; intentionally no score or answer parser."""
from copy import deepcopy
from ..adapter import SCHEMA, REVISION, transfer_stub


def project(record):
    if record is None:
        return {"observation": None, "allowed_evidence": [],
                "mapping_blockers": ["no_confirmed_scorer_integration_commit"]}
    r = deepcopy(record)
    keys = ("task_id", "run_id", "execution_state", "final_output", "completion", "events_complete",
            "approval_interruptions", "side_effects", "side_effects_complete", "delivered_artifacts")
    observation = {k: r[k] for k in keys}
    observation["events"] = [{k: e[k] for k in ("call_id", "tool", "arguments", "status", "produced_artifacts")}
                             for e in r["events"]]
    evidence = [{k: e[k] for k in ("artifact_id", "run_id", "call_id", "facts")}
                for e in r["allowed_evidence"]]
    return {"observation": observation, "allowed_evidence": evidence,
            "mapping_blockers": ["no_confirmed_scorer_integration_commit"]}


def envelope(report):
    return {"interface": "comparison-observation-transfer-stub-v1", "target_schema": SCHEMA,
            "target_measurement_revision": REVISION, "source_kind": "synthetic_fixture",
            "planned": report["counts"]["planned"],
            "rows": [{"task_id": r["task_id"], **project(r["observation"])} for r in report["rows"]],
            "contracts_supplied": False, "actual_scorer_connected": False}
