"""Failure evidence for an attempt that could not reach a legal clock terminal.

This format is deliberately separate from sealed.json/finalization.json. A hash
receipt proves bytes, never lifecycle completion, deadline compliance or authority.
"""
from . import contract as c
from .attempt_failures import validate
from .observations import safe_directory, _check_archive_entries, _verify_document


FILES = {"failed-experiment.json", "audit.sqlite3"}


def _verify_attempt_links(artifact, diagnostics):
    events = artifact["audit"]["events"]
    starts = [e for e in events if e["event_type"] == "model_request_started"]
    terminals = [e for e in events if "terminal_kind" in e["safe_payload"]]
    indices = [e["safe_payload"]["attempt_index"] for e in starts]
    c.require(bool(starts) and all(type(i) is int for i in indices)
              and indices == list(range(len(starts))), "failed_archive_attempt_binding")
    ended = [e["safe_payload"]["attempt_index"] for e in terminals]
    c.require(all(type(i) is int for i in ended) and len(set(ended)) == len(ended)
              and set(ended) == set(indices[:-1]), "failed_archive_attempt_binding")
    # The experiment is serial and stops after refusal: there must be exactly
    # one unmatched start, the last request, and one corresponding active clock.
    start = starts[-1]
    payload = start["safe_payload"]
    c.require(type(artifact["phase"]["attempts"]) is list
              and type(artifact["phase"]["active_attempt"]) is dict
              and len(artifact["phase"]["attempts"]) == len(terminals), "failed_archive_attempt_binding")
    for row in diagnostics:
        c.require(row["clock_active_attempt"] is True and row["clock_closed"] is False
                  and row["attempt_index"] == payload["attempt_index"]
                  and row["request_start_event_hash"] == start["event_hash"]
                  and row["case_id"] == payload["case_id"], "failed_archive_attempt_binding")
        count = sum(e["safe_payload"]["case_id"] == row["case_id"] for e in terminals)
        c.require(row["persisted_terminal_count"] == count, "failed_archive_terminal_count")


def _validate(document):
    c.exact(document, "schema_version status execution_completed runtime_authority_granted artifact attempt_failures")
    c.require(document["schema_version"] == "item6-unfinished-failure/1.1"
              and document["status"] == "failed" and document["execution_completed"] is False
              and document["runtime_authority_granted"] is False, "failed_archive_state")
    artifact = c.schema(document["artifact"], "artifact")
    phase = artifact["phase"]
    c.require(artifact["status"] == "failed" and artifact["authority"]["stopped"] is not None
              and phase["closed"] is False and phase["halted"] is True
              and phase["active_attempt"] is not None and phase["phase"]["phase_terminal_ns"] is None,
              "failed_archive_lifecycle")
    validate(document["attempt_failures"])
    c.require(all(r["case_id"] in artifact["denominator_plan"]["case_ids"] for r in document["attempt_failures"]), "failed_archive_case")
    c.require(any(r["clock_active_attempt"] for r in document["attempt_failures"]), "failed_archive_lifecycle")
    _verify_attempt_links(artifact, document["attempt_failures"])
    return artifact


def write_failed_archive(directory, artifact, attempt_failures):
    document = dict(schema_version="item6-unfinished-failure/1.1", status="failed", execution_completed=False,
                    runtime_authority_granted=False, artifact=artifact, attempt_failures=attempt_failures)
    _validate(document)
    data = c.raw(document)
    database = c.read_regular_file_no_follow(directory / "audit.sqlite3", max_bytes=c.policy()["archive_bytes"])
    c.require(len(data) + len(database) <= c.policy()["archive_bytes"], "archive_size")
    c.scan_public_artifact_bytes((data, database))
    _check_archive_entries(directory, {"audit.sqlite3"})
    with (directory / "failed-experiment.json").open("xb") as f: f.write(data)
    _check_archive_entries(directory, FILES)
    receipt = dict(schema_version="item6-unfinished-receipt/1.0", run_id=artifact["run_id"], mode=artifact["mode"],
                   files={"failed-experiment.json": dict(bytes=len(data), sha256=c.digest(data)),
                          "audit.sqlite3": dict(bytes=len(database), sha256=c.digest(database))})
    raw = c.raw(receipt)
    with (directory / "failure-receipt.json").open("xb") as f: f.write(raw)
    return c.digest(raw)


def verify_failed_archive(directory, *, expected_failure_sha256):
    directory = safe_directory(directory)
    _check_archive_entries(directory, FILES | {"failure-receipt.json"})
    receipt_bytes = c.read_regular_file_no_follow(directory / "failure-receipt.json", max_bytes=8192)
    c.require(c.digest(receipt_bytes) == expected_failure_sha256, "failed_archive_receipt")
    receipt = c.decode(receipt_bytes, 8192)
    c.exact(receipt, "schema_version run_id mode files")
    c.require(receipt["schema_version"] == "item6-unfinished-receipt/1.0" and set(receipt["files"]) == FILES, "failed_archive_files")
    payloads = {name: c.read_regular_file_no_follow(directory / name, max_bytes=c.policy()["archive_bytes"]) for name in FILES}
    c.require(sum(map(len, payloads.values())) <= c.policy()["archive_bytes"], "archive_size")
    c.scan_public_artifact_bytes(tuple(payloads.values()))
    for name, raw in payloads.items():
        c.require(receipt["files"][name] == dict(bytes=len(raw), sha256=c.digest(raw)), "failed_archive_hash")
    document = c.decode(payloads["failed-experiment.json"], c.policy()["archive_bytes"])
    artifact = _validate(document)
    c.require(artifact["run_id"] == receipt["run_id"] and artifact["mode"] == receipt["mode"], "archive_identity")
    _verify_document(directory, artifact, payloads["audit.sqlite3"])
    return dict(failure_archive_verified=True, execution_completed=False, phase_completed=False,
        mode=artifact["mode"], business_denominator=32, runtime_authority_granted=False,
        formal_comparison_result=False, attempt_failures=document["attempt_failures"])
