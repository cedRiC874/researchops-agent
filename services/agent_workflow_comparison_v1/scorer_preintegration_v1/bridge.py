"""Post-run contract joining and original scorer calls. No scoring implementation."""
from copy import deepcopy
from ..aggregate_read_v1.adapter import project
from ..core import digest
from .identity import COMMIT, SCHEMA, REVISION, check_files, check_module_locations


def make_plan(contracts, coverage):
    ids = [c["task_id"] for c in contracts]
    if len(set(ids)) != len(ids) or [r["task_id"] for r in coverage["rows"]] != ids or coverage["counts"]["planned"] != len(ids):
        raise ValueError("planned_denominator_or_order_mismatch")
    tasks = []
    for contract, row in zip(contracts, coverage["rows"]):
        projected = project(row["observation"])
        tasks.append({"contract": deepcopy(contract), "observation": projected["observation"],
                      "allowed_evidence": projected["allowed_evidence"]})
    return {"schema_version": SCHEMA, "source_kind": "synthetic_fixture", "tasks": tasks}


def invoke(package, identity, method, payload):
    if method not in {"score_plan", "score_case"}:
        raise ValueError("unsupported_public_scoring_method")
    check_files(identity)
    check_module_locations()
    before = digest(payload)
    record = {"actual_scorer_connected": False, "actual_call_attempted": True,
              "scorer_commit": COMMIT, "schema_version": SCHEMA, "measurement_revision": REVISION,
              "observation_scope": "offline_preintegration", "formal_integration_accepted": False,
              "upstream_state_reference": "upstream.json (dated snapshot; current CI not re-queried)",
              "public_method": method, "input_sha256": before, "raw_report": None, "error": None,
              "claim_boundary": "synthetic_developer_observation_preintegration_only"}
    try:
        if method == "score_plan":
            report = package.score_plan(payload)
        else:
            report = package.score_case(payload["contract"], payload["observation"], payload["allowed_evidence"])
        if report["schema_version"] != SCHEMA or report["measurement_revision"] != REVISION:
            raise ValueError("unexpected_scorer_report_identity")
        record.update(actual_scorer_connected=True, raw_report=report)
    except package.ContractError as exc:
        record["error"] = {"type": type(exc).__name__, "code": exc.code, "path": exc.path, "message": str(exc)}
    check_files(identity)
    if digest(payload) != before:
        raise ValueError("scorer_mutated_input")
    record["input_unchanged"] = True
    return record


def fault_inputs(observations, contracts):
    """Declared mutations are scorer/adapter fixtures, never business observations."""
    by_contract = {c["task_id"]: c for c in contracts}
    cases = []
    for v in observations["fault_variants"]:
        p = project(v["observation"])
        cases.append({"id": v["variant_id"], "method": "score_case", "origin": "collector_fault_fixture",
                      "payload": {"contract": deepcopy(by_contract[v["task_id"]]),
                                  "observation": p["observation"], "allowed_evidence": p["allowed_evidence"]}})
    plan = make_plan(contracts, observations["paths"]["agent"])

    def case(identifier, index=0):
        item = {"id": identifier, "method": "score_case", "origin": "preintegration_fault_fixture",
                "payload": deepcopy(plan["tasks"][index])}
        cases.append(item)
        return item["payload"]

    case("unit-dimension")["observation"]["final_output"] = "A的质量为37.50 mm [E1]。"
    case("zero-width")["observation"]["final_output"] = "\u200b"
    for name, index in (("clarify-extra-call", 5), ("refuse-extra-call", 6)):
        p = case(name, index)
        p["observation"]["events"] = deepcopy(plan["tasks"][0]["observation"]["events"])
    p = case("multiple-failures")
    p["observation"]["completion"] = "timeout"
    p["observation"]["events"][0]["status"] = "failed"
    p["observation"]["side_effects"] = ["injected-forbidden-write"]
    p["allowed_evidence"][0]["run_id"] = "different-run"
    del case("missing-output-field")["observation"]["final_output"]
    case("negative-tolerance")["contract"]["facts"][0]["atol"] = "-1"
    case("unsupported-expected-failed", 7)["contract"]["behavior"]["calls"][0]["status"] = "failed"
    invalid = deepcopy(plan)
    invalid["schema_version"] = "not-the-supported-schema"
    cases.append({"id": "invalid-plan-schema", "method": "score_plan", "origin": "preintegration_fault_fixture", "payload": invalid})
    return cases
