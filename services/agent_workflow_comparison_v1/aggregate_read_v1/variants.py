"""Explicit collector-fault fixtures, never fed into either business path."""
from copy import deepcopy
from .core import digest, text_state


def apply_variant(record, spec):
    r = deepcopy(record)
    kind = spec["kind"]
    if kind == "entire_observation_missing":
        r = None
    elif kind == "text":
        r.update(final_output=spec["value"], text_observation=text_state(spec["value"]))
        if spec["value"] is None:
            r["missing_observations"].append("final_output")
    elif kind == "timeout_null":
        r.update(final_output=None, text_observation="unobserved", completion="timeout",
                 status="failed", status_source="collector_fault_fixture")
        r["known_failures"].append({"code": "timeout"})
        r["missing_observations"].append("final_output")
    elif kind == "other_run_evidence":
        r["allowed_evidence"][0]["run_id"] = "synthetic-other-run"
    elif kind == "not_produced":
        r["events"][0]["produced_artifacts"] = []
    elif kind == "failed_event":
        r["events"][0]["status"] = "failed"
        r["known_failures"].append({"code": "tool_failed", "call_id": r["events"][0]["call_id"]})
        r.update(status="failed", status_source="collector_fault_fixture")
    elif kind == "missing_evidence_content":
        r["allowed_evidence"][0].update(facts=None, availability="unobserved", projection_sha256=None)
    elif kind == "incomplete_observation":
        r.update(events_complete=False, side_effects_complete=False)
    elif kind == "not_executed":
        r.update(execution_state="not_executed", status="failed", status_source="collector_fault_fixture",
                 completion="unknown", final_output=None, text_observation="unobserved", events=[],
                 allowed_evidence=[], delivered_artifacts=[], approval_interruptions=[], side_effects=[],
                 model_requests=[], model_response_origin="not_applicable",
                 missing_observations=["final_output"], known_failures=[{"code": "not_executed"}])
        for p in r["plan"]:
            p.update(execution="not_executed", call_id=None)
        for key in ("model_request_count", "tool_attempt_count"):
            r["metrics"][key].update(value=0, source="collector_fault_fixture_not_executed")
        r["metrics"]["local_elapsed"].update(value=None, availability="unavailable", source="not_executed_fixture")
    else:
        raise ValueError("unknown_fault_variant")
    return {"variant_id": spec["id"], "task_id": record["task_id"],
            "source_kind": "synthetic_fixture", "origin": "collector_fault_fixture",
            "base_observation_sha256": digest(record), "spec": deepcopy(spec),
            "observation": r, "score_status": "not_scored",
            "warning": "This is injected observation material, not an actual path result or scorer verdict."}
