"""Observation-only projection and a non-scoring transport stub. No gold accepted."""
from copy import deepcopy
from .core import digest

SCHEMA = "internal-behavior-eval-v1.0"
REVISION = "internal-behavior-eval-v1.1"


def project_observation(record):
    if record is None:
        return {"observation": None, "allowed_evidence": [],
                "mapping_blockers": ["entire_observation_missing"]}
    r = deepcopy(record)
    fields = ("task_id", "run_id", "execution_state", "final_output", "completion",
              "events_complete", "approval_interruptions", "side_effects",
              "side_effects_complete", "delivered_artifacts")
    observation = {k: r[k] for k in fields}
    observation["events"] = [{k: e[k] for k in (
        "call_id", "tool", "arguments", "status", "produced_artifacts")} for e in r["events"]]
    evidence = [{k: e[k] for k in ("artifact_id", "run_id", "call_id", "facts")}
                for e in r["allowed_evidence"]]
    blockers = ["no_main_confirmed_scorer_integration_commit"]
    if evidence:
        blockers.append("counts_and_method_semantics_not_supported_by_v1_1")
    if r["status"] == "waiting_approval":
        blockers.append("local_pause_is_not_an_actual_approval_interruption")
    if r["status"] in {"clarification", "refusal"}:
        blockers.append("general_stop_reason_may_be_outside_finite_scorer_vocabulary")
    return {"observation": observation, "allowed_evidence": evidence,
            "mapping_blockers": blockers}


def make_envelope(coverage_report):
    rows = [{"task_id": row["task_id"], **project_observation(row["observation"])}
            for row in coverage_report["rows"]]
    return {"interface": "comparison-observation-transfer-stub-v1", "target_schema": SCHEMA,
            "target_measurement_revision": REVISION, "source_kind": "synthetic_fixture",
            "planned": coverage_report["counts"]["planned"], "rows": rows,
            "contracts_supplied": False, "actual_scorer_connected": False}


def transfer_stub(envelope):
    """Checks packet transport only. Deliberately has no pass/verdict/score field."""
    required = {"interface", "target_schema", "target_measurement_revision", "source_kind",
                "planned", "rows", "contracts_supplied", "actual_scorer_connected"}
    if set(envelope) != required or envelope["interface"] != "comparison-observation-transfer-stub-v1":
        raise ValueError("stub_envelope_invalid")
    if (envelope["target_schema"] != SCHEMA or envelope["target_measurement_revision"] != REVISION
        or envelope["source_kind"] != "synthetic_fixture" or envelope["contracts_supplied"] is not False
        or envelope["actual_scorer_connected"] is not False):
        raise ValueError("stub_version_or_provenance_invalid")
    if not isinstance(envelope["rows"], list):
        raise ValueError("stub_rows_invalid")
    for row in envelope["rows"]:
        if not isinstance(row, dict) or set(row) != {
            "task_id", "observation", "allowed_evidence", "mapping_blockers"
        } or not isinstance(row["task_id"], str):
            raise ValueError("stub_row_invalid")
        obs = row["observation"]
        if obs is not None and (not isinstance(obs, dict) or "final_output" not in obs
                or obs.get("task_id") != row["task_id"]
                or (obs["final_output"] is not None and not isinstance(obs["final_output"], str))):
            raise ValueError("stub_observation_invalid")
    ids = [row["task_id"] for row in envelope["rows"]]
    if type(envelope["planned"]) is not int or envelope["planned"] <= 0 or (
        len(ids) != envelope["planned"] or len(set(ids)) != len(ids)
    ):
        raise ValueError("stub_fixed_denominator_invalid")
    return {"kind": "interface_stub_receipt", "transport_status": "received",
            "received_rows": len(ids), "payload_sha256": digest(envelope),
            "actual_scorer_connected": False, "score_status": "not_scored"}
