"""Experiment-local, one-shot rejection association; persisted data grants nothing."""
from copy import deepcopy
import os
import weakref

from . import contract as c

START = "item6_business_case_started_v1"
CLOSE = "item6_business_case_closed_v1"
REJECT = "item6_case_tool_rejected_v1"
SEALED = "item6_case_isolation_sealed_v1"
CODE = "item6_tool_before_design_or_refusal"
_factories = weakref.WeakKeyDictionary()
_rejections = weakref.WeakKeyDictionary()


def bind_factory(factory):
    from .session import ExperimentFactory
    from .authority import Owner
    c.require(type(factory) is ExperimentFactory and type(factory.owner) is Owner, "isolation_factory")
    factory.owner.assert_factory(factory)
    c.require(factory not in _factories, "isolation_factory_rebound")
    _factories[factory] = dict(pid=os.getpid(), owner=factory.owner, tracker=factory.tracker,
        ledger=factory.ledger, budget=factory.budget, pending=None)


def _factory(factory):
    from .session import ExperimentFactory
    c.require(type(factory) is ExperimentFactory and factory in _factories, "isolation_factory")
    state = _factories[factory]
    c.require(state["pid"] == os.getpid() and all(getattr(factory, key) is state[key]
        for key in ("owner", "tracker", "ledger", "budget")), "isolation_factory_identity")
    factory.owner.assert_factory(factory)
    return state


def _case(case):
    from .observations import Case
    c.require(type(case) is Case, "isolation_case")
    factory = case.factory
    state = _factory(factory)
    factory.owner.check(source_check=True)
    case.check_deadline()
    factory.owner.assert_case(factory, case)
    c.require(factory.current is case, "isolation_case_identity")
    return state


def new_case(factory):
    c.require(_factory(factory)["pending"] is None, "isolation_pending")


def work_allowed(case):
    c.require(_factory(case.factory)["pending"] is None and case not in _rejections, "isolation_case_work_denied")


def common(freeze, run_id, record):
    item = dict(task_id=record["task_id"], path_kind=record["path_kind"])
    index = freeze["business_plan"].index(item)
    return dict(protocol_revision=c.REJECTION_REVISION, policy_sha256=freeze["case_rejection_policy_sha256"],
        freeze_sha256=c.commit("freeze", freeze), execution_commit=freeze["execution_commit"],
        source_commitment_sha256=freeze["source_commitment_sha256"], mode=freeze["mode"], run_id=run_id,
        business_index=index, case_run_id=record["run_id"], task_id=record["task_id"],
        path_kind=record["path_kind"], input_sha256=record["input_sha256"],
        model_case_id=freeze["model_case_handles"][record["task_id"]] if record["path_kind"] == "agent" else None)


def _append(case, kind, payload):
    from .observations import safe_business
    safe_business(payload, key=case.factory.canary)
    ledger, run_id = case.factory.ledger, case.factory.owner.run_id
    event_hash = ledger.append_event(run_id, kind, payload, actor_kind="system")
    c.require(type(event_hash) is str and c.SHA.fullmatch(event_hash), "isolation_audit_ack")
    exported = ledger.export_run(run_id)
    matches = [e for e in exported["events"] if e["event_hash"] == event_hash]
    c.require(exported["chain_verification"]["valid"] and len(matches) == 1
              and matches[0]["event_type"] == kind and matches[0]["actor_kind"] == "system"
              and c.raw(matches[0]["safe_payload"]) == c.raw(payload), "isolation_audit_ack")
    return event_hash


def started(case):
    _case(case)
    case.record["case_lifecycle"]["started_event_hash"] = _append(case, START,
        common(case.factory.freeze, case.factory.owner.run_id, case.record))


def reason(task, tool, arguments, evidence):
    """Pure frozen-metadata checks. Never invokes a tool or returns tool data."""
    from services.agent_workflow_comparison_v1.controlled_comparison_v1.paths import refusal
    if refusal(task) or len(task["design_requests"]) == 1:
        return None
    catalog = evidence["catalog"].get(task["scope_id"], [])
    if tool == "inspect_sources":
        allowed = arguments == {"scope_id": task["scope_id"]}
    elif tool == "read_aggregate":
        # With zero prior tools, the original catalog precondition requires a
        # provided bundle. Do not inspect a source to manufacture eligibility.
        allowed = (task["bundle_id"] is not None and arguments == {"bundle_id": task["bundle_id"]}
                   and task["bundle_id"] in {row["bundle_id"] for row in catalog})
    else:
        allowed = False
    return ("missing_design" if not task["design_requests"] else "conflicting_design") if allowed else None


def _zero_tools(record, budget):
    return (not record["events"] and not record["allowed_evidence"] and not record["side_effects"]
            and not record["delivered_artifacts"] and record["side_effects_complete"] is True
            and budget.get("tools", {}).get(record["task_id"] + ":" + record["path_kind"], 0) == 0
            and record.get("final_output") is None)


def response_links(events, segments, budget, handle):
    starts = [e for e in events if e["event_type"] == "model_request_started" and e["safe_payload"]["case_id"] == handle]
    c.require(0 < len(starts) <= 3, "isolation_responses")
    links = []
    for start in starts:
        index = start["safe_payload"]["attempt_index"]
        sends = [e for e in events if e["event_type"] == "item6_send_intent_v1" and e["safe_payload"]["attempt_index"] == index]
        ends = [e for e in events if "terminal_kind" in e["safe_payload"] and e["safe_payload"]["attempt_index"] == index]
        parts = [s for s in segments if s["attempt_index"] == index]
        c.require(len(sends) == len(ends) == len(parts) == 1, "isolation_response_links")
        terminal, segment = ends[0], parts[0]
        payload = terminal["safe_payload"]
        c.require(payload["case_id"] == sends[0]["safe_payload"]["model_case_id"] == segment["case_handle"] == handle
                  and start["sequence"] < sends[0]["sequence"] < terminal["sequence"]
                  and payload["terminal_kind"] == segment["terminal_kind"] == "response_accepted", "isolation_response_terminal")
        record = payload.get("completion_record")
        c.require(type(record) is dict and record["usage"]["complete"] is True
                  and record["native_status"]["value"] == "completed"
                  and record["normalized_completion_state"] == "completed"
                  and record["truncation_signal_source"] == "native_status", "isolation_native_usage")
        request = budget["requests"][index]
        c.require(request["case_id"] == handle and request["dispatches"] == 1 and request["cost_settled"] is True
                  and request["usage"] == record["usage"]["normalized"], "isolation_settlement")
        c.require(segment["raw_cleanup_status"] == "completed" and segment["lifetime_ended_ns"] is not None
                  and segment["raw_cleanup_terminal_ns"] is not None
                  and segment["completion_record_sha256"] == c.digest(c.raw(record))
                  and payload["timing"]["segment_commitment_sha256"] == segment["segment_commitment_sha256"], "isolation_raw_cleanup")
        links.append(dict(attempt_index=index, response_index=record["response_index"],
            request_start_event_hash=start["event_hash"], send_intent_event_hash=sends[0]["event_hash"],
            terminal_event_hash=terminal["event_hash"], completion_record_sha256=c.digest(c.raw(record)),
            segment_commitment_sha256=segment["segment_commitment_sha256"]))
    return links


def _live_links(case):
    factory = case.factory
    phase = factory.owner.clock.snapshot()
    c.require(factory.owner.stopped is None and not phase["halted"] and not phase["clock_failed"]
              and phase["active_attempt"] is None and not phase["closed"], "isolation_clock")
    c.require(factory.budget.pending is None and not factory.attempt_failures, "isolation_unfinished")
    exported = factory.ledger.export_run(factory.owner.run_id)
    c.require(exported["chain_verification"]["valid"], "isolation_audit_chain")
    if case in _rejections or case.record["case_rejection"] is not None:
        c.require(not any(e["event_type"] in {"item6_tool_started_v1", "item6_tool_finished_v1"}
                          and e["safe_payload"].get("case_run_id") == case.record["run_id"]
                          for e in exported["events"]), "isolation_audit_tool_executed")
    segments = [c.decode(blob) for session in factory.sessions for blob in session.segment_bytes()]
    return response_links(exported["events"], segments, factory.budget.snapshot(),
                          factory.freeze["model_case_handles"][case.task["task_id"]])


def register_rejection(case, error, tool, arguments):
    """Called only AFTER the original design gate refused, never to authorize a tool."""
    _case(case)
    c.require(type(error) is c.ExperimentError and error.code == CODE, "isolation_error")
    if case.path != "agent": return False
    kind = reason(case.task, tool, arguments, c.decode(c.read(c.EVIDENCE)))
    if kind is None or case.text is not None or not _zero_tools(case.record, case.factory.budget.snapshot()): return False
    state = _factory(case.factory)
    c.require(state["pending"] is None and case not in _rejections and case.record["execution_state"] == "observed", "isolation_duplicate")
    c.require(case.record["case_lifecycle"]["started_event_hash"] is not None and case.sdk_observation_complete,
              "isolation_unobserved")
    plan_index = len(case.record["plan"]) - 1
    c.require(0 <= plan_index < 3 and len(case.record["native_call_ids"]) == len(case.record["plan"]), "isolation_plan")
    plan = case.record["plan"][plan_index]
    c.require(plan == dict(tool=tool, arguments=arguments, source="actual_adapter_response", execution="not_executed", call_id=None), "isolation_plan")
    links = _live_links(case)
    exported = case.factory.ledger.export_run(case.factory.owner.run_id)
    c.require(not any(e["event_type"] in {"item6_tool_started_v1", "item6_tool_finished_v1"}
                      and e["safe_payload"].get("case_run_id") == case.record["run_id"]
                      for e in exported["events"]), "isolation_audit_tool_executed")
    item = dict(schema_version="item6-case-rejection/1.0", code=CODE, reason_kind=kind, plan_index=plan_index,
        tool=tool, arguments_sha256=c.digest(c.raw(arguments)),
        native_call_id_sha256=c.digest(case.record["native_call_ids"][plan_index].encode()), response_links=links)
    base = common(case.factory.freeze, case.factory.owner.run_id, case.record)
    item["rejection_id"] = c.commit("case-rejection-v1", {**base, **item})
    payload = {**base, **item, "tool_executions": 0, "tool_events": 0, "allowed_evidence": 0}
    # No durable success claim is installed if append fails or has unknown outcome.
    event_hash = _append(case, REJECT, payload)
    item.update(rejected_event_hash=event_hash, provider_closed=False, reconciliation=None, sealed_event_hash=None, state="rejected")
    case.record["case_rejection"] = item
    case.record["known_failures"].append({"code": CODE})
    _rejections[case] = dict(error=error, factory=case.factory, owner=case.factory.owner, pid=os.getpid(),
        immutable=c.raw(payload), captured=False, provider_closed=False, ready=False)
    state["pending"] = case
    return True


def capture(case, error):
    proof = _rejections.get(case)
    if proof is None: return False
    from agents.exceptions import UserError
    matched = error is proof["error"] or (type(error) is UserError and error.__cause__ is proof["error"])
    if not matched: return False
    _case(case)
    c.require(proof["pid"] == os.getpid() and proof["factory"] is case.factory and proof["owner"] is case.factory.owner
              and not proof["captured"] and _factory(case.factory)["pending"] is case, "isolation_association")
    proof["captured"] = True
    return True


def provider_closed(case):
    proof = _rejections.get(case)
    c.require(proof is not None and proof["captured"] and not proof["provider_closed"], "isolation_provider_close")
    _case(case)
    c.require(_live_links(case) == case.record["case_rejection"]["response_links"], "isolation_response_changed")
    proof["provider_closed"] = True
    case.record["case_rejection"]["provider_closed"] = True


def before_close(case, reconciliation):
    _case(case)
    if case.path == "agent":
        from researchops_completion_telemetry.capture import RuntimeCaseReconciliation
        handle = case.factory.freeze["model_case_handles"][case.task["task_id"]]
        c.require(type(reconciliation) is RuntimeCaseReconciliation
                  and case.factory.tracker._case_reconciliations.get(handle) is reconciliation
                  and reconciliation.case_id == handle and reconciliation.closure_eligible is True,
                  "isolation_case_reconciliation")
        _reconciliation(reconciliation.to_dict(), handle, _live_links(case))
    proof = _rejections.get(case)
    if proof is None: return
    c.require(proof["captured"] and proof["provider_closed"] and not proof["ready"], "isolation_not_closed")
    item = case.record["case_rejection"]
    c.require(_zero_tools(case.record, case.factory.budget.snapshot()) and case.text is None
              and _live_links(case) == item["response_links"], "isolation_close_changed")
    base = common(case.factory.freeze, case.factory.owner.run_id, case.record)
    original = c.decode(proof["immutable"])
    candidate = {**base, **{key: item[key] for key in original if key in item},
                 "tool_executions": 0, "tool_events": 0, "allowed_evidence": 0}
    c.require(candidate == original, "isolation_proof_changed")
    item["reconciliation"] = reconciliation.to_dict()
    proof["ready"] = True


def projection(record):
    # Mutable failure diagnostics/time and event back-references are excluded;
    # rejection status/known failures are checked independently in replay.
    excluded = {"case_lifecycle", "case_rejection", "elapsed_seconds", "status", "known_failures"}
    return {key: value for key, value in record.items() if key not in excluded}


def closed(case, reconciliation):
    factory = case.factory
    state = _factory(factory)
    factory.owner.check(source_check=True); case.check_deadline()
    c.require(factory.current is None and factory.active is None and factory.owner._state()["case"] is None,
              "isolation_business_not_closed")
    payload = common(factory.freeze, factory.owner.run_id, case.record)
    payload.update(started_event_hash=case.record["case_lifecycle"]["started_event_hash"],
        observation_sha256=c.digest(c.raw(projection(case.record))),
        reconciliation=reconciliation.to_dict() if reconciliation is not None else None,
        reconciliation_sha256=c.digest(c.raw(reconciliation.to_dict())) if reconciliation is not None else None)
    case.record["case_lifecycle"]["closed_event_hash"] = _append(case, CLOSE, payload)
    proof = _rejections.get(case)
    if proof is None:
        c.require(state["pending"] is None, "isolation_pending")
        return
    c.require(state["pending"] is case and proof["ready"], "isolation_seal_state")
    item = case.record["case_rejection"]
    sealing = common(factory.freeze, factory.owner.run_id, case.record)
    sealing.update(rejection_id=item["rejection_id"], rejected_event_hash=item["rejected_event_hash"],
        closed_event_hash=case.record["case_lifecycle"]["closed_event_hash"],
        reconciliation_sha256=c.digest(c.raw(item["reconciliation"])), provider_closed=True)
    event_hash = _append(case, SEALED, sealing)
    # Never start subsequent work before the durable, validated sealing event.
    factory.owner.check(source_check=True); case.check_deadline()
    item.update(sealed_event_hash=event_hash, state="isolated")
    state["pending"] = None
    del _rejections[case]


def abort(case):
    c.require(case.factory.owner.stopped is not None, "isolation_abort_without_stop")
    state = _factories.get(case.factory)
    if state is not None and state["pending"] is case: state["pending"] = None
    _rejections.pop(case, None)


def result_fields(records, scores, stopped):
    rows = [row for path in records.values() for row in path.values()]
    observed = [row for row in rows if row["execution_state"] == "observed"]
    isolated = sum(row["case_rejection"] is not None and row["case_rejection"]["state"] == "isolated" for row in rows)
    summary = dict(planned=len(rows), observed=len(observed), not_executed=len(rows)-len(observed),
        operational_complete=sum(row["completion"] == "complete" and row["status"] != "failed" for row in observed),
        failed_after_start=sum(row["status"] == "failed" for row in observed), isolated_case_rejections=isolated)
    complete = stopped is None and len(observed) == len(rows) == 32
    status = "complete_with_case_rejections" if complete and isolated else "complete" if complete else "stopped"
    all_passed = (complete and summary["failed_after_start"] == 0 and isolated == 0 and all(
        score["error"] is None and score["raw_report"] is not None
        and len(score["raw_report"]["tasks"]) == 16
        and all(row["task_verdict"] == "pass" for row in score["raw_report"]["tasks"]) for score in scores.values()))
    return dict(collection_status=status, collection_summary=summary, all_business_passed=all_passed)


def _reconciliation(value, handle, links):
    c.exact(value, "case_id attempts_started attempts_terminal observed_response_count accepted_response_count rejected_response_count "
                  "sdk_raw_response_count sdk_raw_response_reconciliation sdk_usage_request_count sdk_usage_request_reconciliation "
                  "sdk_request_usage_indices_by_response closure_eligible")
    c.require(value["case_id"] == handle and value["closure_eligible"] is True
              and value["sdk_raw_response_reconciliation"] == value["sdk_usage_request_reconciliation"] == "matched",
              "isolation_reconciliation")
    for key in ("attempts_started", "attempts_terminal", "observed_response_count", "accepted_response_count",
                "sdk_raw_response_count", "sdk_usage_request_count"):
        c.require(type(value[key]) is int and value[key] == len(links), "isolation_reconciliation")
    c.require(type(value["rejected_response_count"]) is int and value["rejected_response_count"] == 0, "isolation_reconciliation")
    nested = value["sdk_request_usage_indices_by_response"]
    c.require(type(nested) is list and len(nested) == len(links), "isolation_reconciliation")
    for index, (row, link) in enumerate(zip(nested, links)):
        c.exact(row, "response_index sdk_raw_response_index sdk_request_usage_indices")
        indices = row["sdk_request_usage_indices"]
        c.require(type(row["response_index"]) is int and row["response_index"] == link["response_index"]
                  and type(row["sdk_raw_response_index"]) is int and row["sdk_raw_response_index"] == index
                  and type(indices) is list and len(indices) <= 3 and all(type(i) is int for i in indices)
                  and indices == list(range(len(indices))), "isolation_reconciliation")


def verify_document(doc, events):
    """Replay the new case protocol, including failures; no capability is recreated."""
    freeze = doc["authority"]["freeze"]
    c.require({key: doc[key] for key in ("collection_status", "collection_summary", "all_business_passed")}
              == result_fields(doc["business"], doc["scores"], doc["authority"]["stopped"]), "isolation_collection_projection")
    c.require((doc["status"] == "completed") == (doc["collection_status"] != "stopped"), "isolation_collection_state")
    relevant = [e for e in events if e["event_type"] in {START, CLOSE, REJECT, SEALED}]
    c.require(len(relevant) <= 96 and all(e["actor_kind"] == "system" for e in relevant), "isolation_event_limit")
    used = set()
    previous_end = 0
    gap = False
    evidence = c.decode(c.read(c.EVIDENCE))
    reconciliations = {row["case_id"]: row for row in doc["denominator"]["cases"]} if doc["denominator"] is not None else {}
    for expected in freeze["business_plan"]:
        row = doc["business"][expected["path_kind"]][expected["task_id"]]
        c.require(row["task_id"] == expected["task_id"] and row["path_kind"] == expected["path_kind"], "isolation_case_identity")
        base = common(freeze, doc["run_id"], row)
        c.require(row["run_id"] == f"{doc['run_id']}-{row['path_kind']}-{row['task_id']}", "isolation_case_identity")
        group = [e for e in relevant if e["safe_payload"].get("case_run_id") == row["run_id"]]
        started_events = [e for e in group if e["event_type"] == START]
        closed_events = [e for e in group if e["event_type"] == CLOSE]
        rejected_events = [e for e in group if e["event_type"] == REJECT]
        sealed_events = [e for e in group if e["event_type"] == SEALED]
        c.require(all(len(values) <= 1 for values in (started_events, closed_events, rejected_events, sealed_events)), "isolation_event_duplicates")
        used.update(e["event_hash"] for e in group)
        lifecycle, rejection = row["case_lifecycle"], row["case_rejection"]
        c.exact(lifecycle, "started_event_hash closed_event_hash")
        row_work = [e for e in events if e["event_type"] in {"item6_tool_started_v1", "item6_tool_finished_v1"}
                    and e["safe_payload"].get("case_run_id") == row["run_id"]]
        if row["path_kind"] == "agent":
            row_work += [e for e in events if (e["event_type"] in {"model_request_started", "item6_send_intent_v1"}
                                               or "terminal_kind" in e["safe_payload"])
                         and e["safe_payload"].get("case_id", e["safe_payload"].get("model_case_id")) == base["model_case_id"]]
        if row["execution_state"] == "not_executed":
            c.require(not row_work and (not gap or not group), "isolation_events_after_stop")
            if started_events:
                c.require(started_events[0]["sequence"] > previous_end, "isolation_case_order")
            gap = True
            # A failed start write can have unknown acknowledgement. It cannot
            # authorize work or be called a completed/observed case.
            c.require(doc["status"] == "failed" and rejection is None and not closed_events
                      and not rejected_events and not sealed_events and lifecycle["closed_event_hash"] is None,
                      "isolation_unexecuted")
            if started_events:
                c.require(lifecycle["started_event_hash"] in (None, started_events[0]["event_hash"])
                          and c.raw(started_events[0]["safe_payload"]) == c.raw(base), "isolation_failed_start")
            else: c.require(lifecycle["started_event_hash"] is None, "isolation_failed_start")
            continue
        c.require(not gap and len(started_events) == 1, "isolation_case_order")
        begin = started_events[0]
        c.require(begin["sequence"] > previous_end and lifecycle["started_event_hash"] == begin["event_hash"]
                  and c.raw(begin["safe_payload"]) == c.raw(base), "isolation_case_start")
        case_work = row_work
        c.require(all(e["sequence"] > begin["sequence"] for e in case_work), "isolation_case_work_order")
        if closed_events:
            end = closed_events[0]
            payload = end["safe_payload"]
            reconciliation = payload.get("reconciliation")
            expected_close = {**base, "started_event_hash": begin["event_hash"],
                "observation_sha256": c.digest(c.raw(projection(row))), "reconciliation": reconciliation,
                "reconciliation_sha256": c.digest(c.raw(reconciliation)) if reconciliation is not None else None}
            close_ack = lifecycle["closed_event_hash"] == end["event_hash"]
            c.require((close_ack or (doc["status"] == "failed" and lifecycle["closed_event_hash"] is None))
                      and end["sequence"] > begin["sequence"]
                      and all(e["sequence"] < end["sequence"] for e in case_work)
                      and c.raw(payload) == c.raw(expected_close), "isolation_case_close")
            if row["path_kind"] == "agent":
                links = response_links(events, doc["segments"], doc["budget"], base["model_case_id"])
                _reconciliation(reconciliation, base["model_case_id"], links)
                if base["model_case_id"] in reconciliations:
                    c.require(reconciliation == reconciliations[base["model_case_id"]], "isolation_case_reconciliation")
            else: c.require(reconciliation is None, "isolation_fixed_reconciliation")
            previous_end = end["sequence"]
            if not close_ack:
                c.require(not sealed_events, "isolation_unacknowledged_close")
                gap = True
        else:
            c.require(doc["status"] == "failed" and lifecycle["closed_event_hash"] is None, "isolation_unclosed")
            gap = True
            previous_end = max([begin["sequence"], *[e["sequence"] for e in case_work]])
        rejection_ack = rejection is not None
        if rejection is None and not rejected_events:
            c.require(not sealed_events, "isolation_missing_rejection")
            continue
        if rejection is None:
            # Rejection append committed but the caller never obtained a valid
            # acknowledgement. Validate the existing row, without filling any
            # missing artifact field or granting isolation/continuation.
            c.require(doc["status"] == "failed" and not closed_events and not sealed_events, "isolation_unacknowledged_rejection")
            payload = rejected_events[0]["safe_payload"]
            keys = ("schema_version", "rejection_id", "code", "reason_kind", "plan_index", "tool",
                    "arguments_sha256", "native_call_id_sha256", "response_links")
            rejection = {key: payload.get(key) for key in keys}
            rejection.update(rejected_event_hash=rejected_events[0]["event_hash"], provider_closed=False,
                             reconciliation=None, sealed_event_hash=None, state="rejected")
            gap = True
        c.require(row["path_kind"] == "agent" and len(rejected_events) == 1 and row["status"] == "failed"
                  and row["completion"] == "unknown" and _zero_tools(row, doc["budget"]), "isolation_rejected_case")
        c.require(not any(e["event_type"] in {"item6_tool_started_v1", "item6_tool_finished_v1"}
                          for e in case_work), "isolation_audit_tool_executed")
        index = rejection["plan_index"]
        c.require(type(index) is int and 0 <= index < len(row["plan"]) == len(row["native_call_ids"]), "isolation_plan")
        plan = row["plan"][index]
        c.require(plan["source"] == "actual_adapter_response" and plan["execution"] == "not_executed" and plan["call_id"] is None
                  and rejection["tool"] == plan["tool"]
                  and rejection["arguments_sha256"] == c.digest(c.raw(plan["arguments"]))
                  and rejection["native_call_id_sha256"] == c.digest(row["native_call_ids"][index].encode()), "isolation_plan")
        why = reason(row["task_input"], plan["tool"], plan["arguments"], evidence)
        c.require(why is not None and why == rejection["reason_kind"] and rejection["code"] == CODE
                  and rejection["schema_version"] == "item6-case-rejection/1.0"
                  and (not rejection_ack or any(f.get("code") == CODE for f in row["known_failures"])), "isolation_reason")
        links = response_links(events, doc["segments"], doc["budget"], base["model_case_id"])
        c.require(rejection["response_links"] == links, "isolation_response_links")
        initial = {key: rejection[key] for key in ("schema_version", "code", "reason_kind", "plan_index", "tool",
                                                  "arguments_sha256", "native_call_id_sha256", "response_links")}
        c.require(rejection["rejection_id"] == c.commit("case-rejection-v1", {**base, **initial}), "isolation_rejection_id")
        rejected = rejected_events[0]
        expected_reject = {**base, **initial, "rejection_id": rejection["rejection_id"],
                           "tool_executions": 0, "tool_events": 0, "allowed_evidence": 0}
        by_hash = {e["event_hash"]: e for e in events}
        c.require(rejection["rejected_event_hash"] == rejected["event_hash"]
                  and c.raw(rejected["safe_payload"]) == c.raw(expected_reject)
                  and all(by_hash[link["terminal_event_hash"]]["sequence"] < rejected["sequence"] for link in links)
                  and (not closed_events or rejected["sequence"] < closed_events[0]["sequence"]), "isolation_rejection_event")
        if rejection["state"] == "isolated" or sealed_events:
            c.require(len(closed_events) == len(sealed_events) == 1 and rejection["provider_closed"] is True,
                      "isolation_unsealed")
            _reconciliation(rejection["reconciliation"], base["model_case_id"], links)
            c.require(rejection["reconciliation"] == closed_events[0]["safe_payload"]["reconciliation"], "isolation_reconciliation")
            sealed = sealed_events[0]
            wanted = {**base, "rejection_id": rejection["rejection_id"], "rejected_event_hash": rejected["event_hash"],
                "closed_event_hash": closed_events[0]["event_hash"], "provider_closed": True,
                "reconciliation_sha256": c.digest(c.raw(rejection["reconciliation"]))}
            seal_ack = rejection["state"] == "isolated" and rejection["sealed_event_hash"] == sealed["event_hash"]
            c.require((seal_ack or (doc["status"] == "failed" and rejection["state"] == "rejected" and rejection["sealed_event_hash"] is None))
                      and sealed["sequence"] > closed_events[0]["sequence"]
                      and c.raw(sealed["safe_payload"]) == c.raw(wanted), "isolation_seal_event")
            previous_end = sealed["sequence"]
            if not seal_ack: gap = True
        else:
            c.require(doc["status"] == "failed" and rejection["state"] == "rejected" and not sealed_events
                      and rejection["sealed_event_hash"] is None, "isolation_unsealed")
            gap = True
    c.require(used == {e["event_hash"] for e in relevant}, "isolation_unbound_event")


def unacknowledged_events(doc):
    acknowledged = set()
    for path in doc["business"].values():
        for row in path.values():
            acknowledged.update(v for v in row["case_lifecycle"].values() if v is not None)
            if row["case_rejection"] is not None:
                acknowledged.update(row["case_rejection"][k] for k in ("rejected_event_hash", "sealed_event_hash")
                                    if row["case_rejection"][k] is not None)
    return [dict(event_type=e["event_type"], case_run_id=e["safe_payload"]["case_run_id"], event_hash=e["event_hash"])
            for e in doc["audit"]["events"] if e["event_type"] in {START, CLOSE, REJECT, SEALED} and e["event_hash"] not in acknowledged]
