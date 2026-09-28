"""Value-free, producer-bound rejection labels; never continuation authority."""
import re
from . import contract as c
from . import case_isolation as isolation

VERSION = "item6-business-privacy-diagnostic/1.0"
EVENT = "item6_business_privacy_rejected_v1"
# The union, order and flags are byte-for-byte equivalent to the previous
# business matcher. Opaque IDs prevent diagnostic labels from matching it.
RULES = (
    ("P01", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("P02", r"[A-Za-z]:[\\/]"), ("P03", r"/(?:Users|home)/"),
    ("P04", r"Traceback"), ("P05", r"Authorization"), ("P06", r"reasoning_text"),
)
PATTERN = "|".join(pattern for _, pattern in RULES)
LOCATIONS = ("sdk_text", "sdk_tool_args")
FIELDS = ("schema_version", "rule_id", "scan_location", "error_code", "stop_code",
          "sdk_response_index", "response_index", "attempt_index", "response_event_hash", "completion_record_sha256")


def business_rule(text):
    match = re.search(PATTERN, text, re.I)
    if match is None: return None
    # No matched text, offset, length or digest leaves this function.
    return next(rule for rule, pattern in RULES if re.fullmatch(pattern, match.group(0), re.I))


def notify(callback, rule, error):
    if callback is None: return
    try:
        callback(rule, error)
    except BaseException:
        # The original scanner exception is unconditionally raised by its
        # caller. Evidence failure must never replace it or permit continuation.
        # Null/unacknowledged diagnostics are explicitly incomplete evidence.
        pass


def _validate_fields(value):
    c.exact(value, " ".join(FIELDS))
    c.require(value["schema_version"] == VERSION and value["rule_id"] in {"P00", *[r for r, _ in RULES]}
              and value["scan_location"] in LOCATIONS, "privacy_diagnostic_fields")
    public = value["rule_id"] == "P00"
    c.require(value["error_code"] == ("external_closure_sensitive_content_detected" if public else "item6_business_privacy")
              and value["stop_code"] == ("item6_execution_failed" if public else "item6_business_privacy"), "privacy_diagnostic_code")
    for name, maximum in (("sdk_response_index", 2), ("response_index", 47), ("attempt_index", 47)):
        c.require(type(value[name]) is int and 0 <= value[name] <= maximum, "privacy_diagnostic_index")
    for name in ("response_event_hash", "completion_record_sha256"):
        c.require(type(value[name]) is str and c.SHA.fullmatch(value[name]), "privacy_diagnostic_hash")


def _responses(events, handle):
    return [e for e in events if e["event_type"] == "model_response_telemetry_recorded"
            and e["safe_payload"].get("case_id") == handle and "completion_record" in e["safe_payload"]]


def _check_response(value, events, handle):
    responses = _responses(events, handle)
    c.require(len(responses) == value["sdk_response_index"] + 1, "privacy_diagnostic_response")
    terminal = responses[-1]; payload = terminal["safe_payload"]; record = payload["completion_record"]
    c.require(terminal["event_hash"] == value["response_event_hash"] and payload["terminal_kind"] == "response_accepted"
              and payload["attempt_index"] == value["attempt_index"] and record["response_index"] == value["response_index"]
              and c.digest(c.raw(record)) == value["completion_record_sha256"], "privacy_diagnostic_response")
    return terminal


def record_model_failure(case, rule, error, *, location):
    from .observations import Case, safe_business
    from .session import ExperimentFactory
    from researchops_external_closure.errors import ExternalClosurePrimitiveError
    c.require(type(case) is Case and type(case.factory) is ExperimentFactory and case.path == "agent",
              "privacy_diagnostic_case")
    factory = case.factory
    factory.owner.assert_case(factory, case)
    c.require(factory.current is case and case.record["privacy_diagnostic"] is None
              and case.record["execution_state"] == "observed", "privacy_diagnostic_case")
    c.require((rule == "P00" and type(error) is ExternalClosurePrimitiveError
               and error.code == "external_closure_sensitive_content_detected")
              or (rule in {r for r, _ in RULES} and type(error) is c.ExperimentError
                  and error.code == "item6_business_privacy"), "privacy_diagnostic_error")
    handle = factory.freeze["model_case_handles"][case.task["task_id"]]
    exported = factory.ledger.export_run(factory.owner.run_id)
    c.require(exported["chain_verification"]["valid"], "privacy_diagnostic_audit")
    responses = _responses(exported["events"], handle)
    c.require(responses and case.sdk_raw_count == len(responses), "privacy_diagnostic_response")
    terminal = responses[-1]; payload = terminal["safe_payload"]; record = payload["completion_record"]
    value = dict(schema_version=VERSION, rule_id=rule, scan_location=location, error_code=error.code,
        stop_code=c.safe_error(error), sdk_response_index=case.sdk_raw_count-1,
        response_index=record["response_index"], attempt_index=payload["attempt_index"],
        response_event_hash=terminal["event_hash"], completion_record_sha256=c.digest(c.raw(record)))
    _validate_fields(value); _check_response(value, exported["events"], handle)
    event_payload = {**isolation.common(factory.freeze, factory.owner.run_id, case.record), **value}
    safe_business(event_payload, key=factory.canary)
    diagnostic = dict(value, state="unacknowledged", event_hash=None)
    case.record["privacy_diagnostic"] = diagnostic
    event_hash = factory.ledger.append_event(factory.owner.run_id, EVENT, event_payload, actor_kind="system")
    checked = factory.ledger.export_run(factory.owner.run_id)
    matches = [e for e in checked["events"] if e["event_hash"] == event_hash]
    c.require(checked["chain_verification"]["valid"] and len(matches) == 1 and matches[0]["event_type"] == EVENT
              and matches[0]["actor_kind"] == "system" and c.raw(matches[0]["safe_payload"]) == c.raw(event_payload)
              and matches[0]["sequence"] > terminal["sequence"], "privacy_diagnostic_ack")
    diagnostic.update(state="recorded", event_hash=event_hash)


def verify_document(doc, events):
    relevant = [e for e in events if e["event_type"] == EVENT]
    c.require(len(relevant) <= 1, "privacy_diagnostic_count")
    used = set()
    for path, rows in doc["business"].items():
        for row in rows.values():
            item = row["privacy_diagnostic"]
            matches = [e for e in relevant if e["safe_payload"].get("case_run_id") == row["run_id"]]
            if item is None:
                c.require(not matches, "privacy_diagnostic_missing")
                continue
            c.exact(item, " ".join(FIELDS) + " state event_hash")
            value = {k: item[k] for k in FIELDS}; _validate_fields(value)
            c.require(path == "agent" and row["execution_state"] == "observed" and row["status"] == "failed"
                      and row["final_output"] is None and row["case_rejection"] is None
                      and doc["status"] == "failed" and doc["collection_status"] == "stopped"
                      and doc["authority"]["stopped"] == value["stop_code"]
                      and any(f.get("code") == value["stop_code"] for f in row["known_failures"]), "privacy_diagnostic_state")
            handle = doc["authority"]["freeze"]["model_case_handles"][row["task_id"]]
            terminal = _check_response(value, events, handle)
            c.require(item["state"] in {"recorded", "unacknowledged"} and len(matches) <= 1, "privacy_diagnostic_ack")
            if item["state"] == "recorded":
                c.require(len(matches) == 1 and item["event_hash"] == matches[0]["event_hash"], "privacy_diagnostic_ack")
            else:
                c.require(item["event_hash"] is None, "privacy_diagnostic_ack")
            if matches:
                event = matches[0]
                expected = {**isolation.common(doc["authority"]["freeze"], doc["run_id"], row), **value}
                c.require(event["actor_kind"] == "system" and c.raw(event["safe_payload"]) == c.raw(expected)
                          and terminal["sequence"] < event["sequence"], "privacy_diagnostic_projection")
                used.add(event["event_hash"])
            # A privacy stop cannot be followed by another request, tool or case.
            c.require(not any(e["sequence"] > terminal["sequence"] and e["event_type"] in {
                "model_request_started", "item6_send_intent_v1", "item6_tool_started_v1", isolation.START}
                for e in events), "privacy_diagnostic_post_stop_work")
    c.require(used == {e["event_hash"] for e in relevant}, "privacy_diagnostic_unbound_event")


def unacknowledged_events(doc):
    acknowledged = {row["privacy_diagnostic"]["event_hash"] for rows in doc["business"].values()
                    for row in rows.values() if row["privacy_diagnostic"] is not None}
    return [dict(event_type=e["event_type"], case_run_id=e["safe_payload"]["case_run_id"], event_hash=e["event_hash"])
            for e in doc["audit"]["events"] if e["event_type"] == EVENT and e["event_hash"] not in acknowledged]
