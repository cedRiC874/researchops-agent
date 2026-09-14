"""Offline readback/reporting only; never grants runtime or Provider authority.

The fixed production verifier remains the acceptance authority. This auxiliary
tool adds bounded immutable SQLite statistics and checks the directory again
AFTER those statistics, which the failed local helper did not safely preserve.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from researchops.audit import verify_audit_chain_rows
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.io import read_exact_artifact_directory, read_regular_file_no_follow
from researchops_internal_telemetry import contract as c, source
from researchops_internal_telemetry.artifacts import FILES
from researchops_internal_telemetry.verify import verify_archive


def read(path, limit=33554432):
    return read_regular_file_no_follow(Path(path), max_bytes=limit)


def snapshot(directory, names=FILES):
    return dict(read_exact_artifact_directory(Path(directory), expected_names=names,
        max_file_bytes=33554432, max_total_bytes=67108864))


def directory_identities(directory, names):
    paths = [Path(directory), *(Path(directory) / name for name in names)]
    return [(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
        for info in (path.lstat() for path in paths)]


def immutable_audit_counts(database, expected_payload):
    """Use the existing byte-bound immutable/query-only reader, never connect()."""
    runs, events, model_rows, *counts = _read_database_rows(Path(database), expected_payload=expected_payload)
    c.require(len(runs) == 1, "audit_run_count")
    c.require(verify_audit_chain_rows(runs[0]["run_id"], events).valid, "audit_chain")
    return dict(event_count=len(events), event_counts=dict(Counter(row["event_type"] for row in events)),
        model_body_rows=len(model_rows), tool_calls=counts[0], tool_attempts=counts[1], approval_decisions=counts[2])


def immutable_audit_summary(directory, *, expected_names=FILES):
    before = snapshot(directory, expected_names)
    identities = directory_identities(directory, expected_names)
    counts = immutable_audit_counts(Path(directory) / "audit.sqlite3", before["audit.sqlite3"])
    c.require(snapshot(directory, expected_names) == before, "auxiliary_read_changed_archive")
    c.require(directory_identities(directory, expected_names) == identities, "auxiliary_read_changed_identity")
    return counts


def verified_readback(directory, *, bundle_bytes, expected_bundle, approved_digest, observed_exit_code):
    before = snapshot(directory)
    verdict = verify_archive(Path(directory), bundle_bytes=bundle_bytes,
        expected_bundle_commitment=expected_bundle, expected_approved_digest=approved_digest,
        observed_exit_code=observed_exit_code)
    c.require(verdict.get("internal_acceptance_passed") is True, "copy_not_accepted")
    counts = immutable_audit_summary(directory)
    after = snapshot(directory)
    c.require(after == before, "auxiliary_read_changed_archive")
    return verdict, before, counts


def project_observation(payloads, verdict, counts):
    """An explicit allowlist: never copy full authorization/claim/input records."""
    index = c.decode(payloads["run_index.json"])
    telemetry = c.decode(payloads["completion_telemetry.json"], 1048576)
    timing = c.decode(payloads["timing_evidence.json"], 1048576)
    publication = c.decode(payloads["publication.json"])
    rows = []
    for record in telemetry["records"]:
        usage = record["usage"]["normalized"]
        rows.append(dict(request_index=record["request_index"], response_index=record["response_index"],
            native_status=record["native_status"]["value"], normalized_state=record["normalized_completion_state"],
            signal_source=record["truncation_signal_source"], http_status=record["http_status"]["value"],
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            reasoning_tokens=usage["reasoning_tokens"], cached_input_tokens=usage["cached_input_tokens"],
            cache_write_tokens=usage["cache_write_tokens"], usage_complete=record["usage"]["complete"]))
    value = dict(schema_version="internal30-public-observation/1.0", scope="developer_known_synthetic",
        provider="deepseek", requested_model="deepseek-v4-flash", live_transport=verdict["transport_mode"],
        planned_cases=30, executed_cases=index["model_requests"], runtime_accepted_cases=index["case_pass_count"],
        model_requests=index["model_requests"], network_attempts=index["network_attempts"],
        failed_or_unknown_cases=sum(row["status"] == "failed_or_unknown" for row in index["case_states"]),
        unexecuted_cases=sum(row["status"] == "not_started" for row in index["case_states"]),
        input_tokens=index["budget"]["input_tokens"], output_tokens=index["budget"]["output_tokens"],
        local_observed_cost_cny=index["budget"]["observed_cost_cny"], provider_bill_cny=None,
        cost_rates_cny_per_million=dict(input="2.000000", output="8.000000", cache_discount_assumed=False),
        phase_terminal_ns=timing["phase"]["phase_terminal_ns"],
        publication_sealing_elapsed_ns=publication["sealing_elapsed_ns"],
        sealing_scope=publication["observation_scope"], audit=counts, responses=rows,
        original_directory_unmodified_claim_allowed=False, reconstructed_copy_accepted=True,
        external_validation_completed=False, status_t7_closed=False, model_quality_claim_allowed=False,
        depth60_result="20/60")
    c.scan_json(value)
    validate_public_observation(value)
    return value


def validate_public_observation(value):
    # Full archive verification requires private local inputs. This public check
    # checks published arithmetic/projection, not authenticity or live dispatch.
    c.scan_json(value)
    c.exact(value, "schema_version scope provider requested_model live_transport planned_cases executed_cases runtime_accepted_cases model_requests network_attempts failed_or_unknown_cases unexecuted_cases input_tokens output_tokens local_observed_cost_cny provider_bill_cny cost_rates_cny_per_million phase_terminal_ns publication_sealing_elapsed_ns sealing_scope audit responses original_directory_unmodified_claim_allowed reconstructed_copy_accepted external_validation_completed status_t7_closed model_quality_claim_allowed depth60_result")
    c.require(value["schema_version"] == "internal30-public-observation/1.0"
        and value["scope"] == "developer_known_synthetic" and value["provider"] == "deepseek"
        and value["requested_model"] == "deepseek-v4-flash" and value["live_transport"] == "native_http_transport"
        and value["reconstructed_copy_accepted"] is True, "public_scope")
    rows = value["responses"]
    c.require(type(rows) is list and len(rows) == 30, "public_denominator")
    for i, row in enumerate(rows):
        c.exact(row, "request_index response_index native_status normalized_state signal_source http_status input_tokens output_tokens reasoning_tokens cached_input_tokens cache_write_tokens usage_complete")
        c.require(type(row["request_index"]) is type(row["response_index"]) is int
            and row["request_index"] == row["response_index"] == i, "public_indices")
        c.require(row["native_status"] == row["normalized_state"] == "completed" and row["signal_source"] == "native_status"
            and type(row["http_status"]) is int and row["http_status"] == 200 and row["usage_complete"] is True, "public_state")
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens"):
            c.require(type(row[name]) is int and row[name] >= 0, "public_usage")
        c.require(row["cache_write_tokens"] is None and row["reasoning_tokens"] <= row["output_tokens"] <= 512, "public_usage")
    for name in ("input_tokens", "output_tokens"):
        c.require(value[name] == sum(row[name] for row in rows), "public_totals")
    rates = value["cost_rates_cny_per_million"]
    c.require(rates == dict(input="2.000000", output="8.000000", cache_discount_assumed=False), "public_rates")
    cost = (Decimal(value["input_tokens"])*Decimal(rates["input"])+Decimal(value["output_tokens"])*Decimal(rates["output"])) / Decimal(1000000)
    c.require(cost == Decimal(value["local_observed_cost_cny"]) and cost <= Decimal("1")
        and value["input_tokens"] <= 61440 and value["output_tokens"] <= 15360, "public_cost")
    c.require(all(type(value[key]) is int and value[key] == 30 for key in
        ("planned_cases", "executed_cases", "runtime_accepted_cases", "model_requests", "network_attempts")), "public_denominator")
    c.require(value["failed_or_unknown_cases"] == value["unexecuted_cases"] == 0, "public_denominator")
    c.require(type(value["phase_terminal_ns"]) is int and 0 <= value["phase_terminal_ns"] <= 5400000000000
        and type(value["publication_sealing_elapsed_ns"]) is int and 0 <= value["publication_sealing_elapsed_ns"] <= 120000000000
        and value["sealing_scope"] == "before_publication_write_not_process_exit", "public_timing")
    audit = value["audit"]
    c.exact(audit, "event_count event_counts model_body_rows tool_calls tool_attempts approval_decisions")
    expected_events = dict(run_started=1, model_request_started=30, internal_send_intent=30,
        model_response_telemetry_recorded=30, internal_case_closed=30, run_status_changed=1)
    c.require(audit["event_counts"] == expected_events and audit["event_count"] == 122
        and all(type(audit[name]) is int and audit[name] == 0 for name in
            ("model_body_rows", "tool_calls", "tool_attempts", "approval_decisions")), "public_audit")
    c.require(value["provider_bill_cny"] is None and value["external_validation_completed"] is False
        and value["status_t7_closed"] is False and value["model_quality_claim_allowed"] is False
        and value["original_directory_unmodified_claim_allowed"] is False and value["depth60_result"] == "20/60", "public_boundaries")


def build_public_pack(*, original, copy, bundle, exit_observation, incident, copy_receipt, faulty_helper):
    original, copy = Path(original), Path(copy)
    inputs = {name: read(path) for name, path in dict(bundle=bundle, exit_observation=exit_observation,
        incident=incident, copy_receipt=copy_receipt, faulty_helper=faulty_helper).items()}
    record = json.loads(inputs["copy_receipt"])
    accident = json.loads(inputs["incident"])
    observed_exit = json.loads(inputs["exit_observation"])
    c.require(record["schema_version"] == "internal30-original-12-copy-verification/1.0"
        and record["status"] == "verified_reconstruction_copy_only", "copy_receipt")
    c.require(c.digest(inputs["bundle"]) == record["original_bundle_file_sha256"]
        and c.digest(inputs["exit_observation"]) == record["original_execution_exit_observation_sha256"], "local_inputs_changed")
    c.require(observed_exit["actual_exit_code"] == 0 and observed_exit["process_has_exited"] is True, "exit_unknown")
    originals = snapshot(original, (*FILES, "audit.sqlite3-shm", "audit.sqlite3-wal"))
    c.require(accident["current_archive_verifier_failure_code"] == "external_closure_directory_file_set_invalid"
        and accident["final_internal_acceptance_passed"] is False, "incident_not_preserved")
    for name in ("audit.sqlite3-shm", "audit.sqlite3-wal"):
        c.require(accident["unexpected_files"][name] == dict(bytes=len(originals[name]), sha256=c.digest(originals[name])), "sidecar_changed")
    # Prove the unchanged verifier still rejects the ORIGINAL directory; never
    # hide the two files via a monkeypatched listing or relaxed validator.
    try:
        verify_archive(original, bundle_bytes=inputs["bundle"], expected_bundle_commitment=record["original_bundle_commitment"],
            expected_approved_digest=record["approved_candidate_digest"], observed_exit_code=0)
    except Exception as error:
        c.require(getattr(error, "code", None) == "external_closure_directory_file_set_invalid", "unexpected_original_failure")
    else:
        raise c.InternalError("internal_original_incident_not_reproduced")
    verdict, payloads, counts = verified_readback(copy, bundle_bytes=inputs["bundle"],
        expected_bundle=record["original_bundle_commitment"], approved_digest=record["approved_candidate_digest"], observed_exit_code=0)
    c.require(all(originals[name] == payloads[name] for name in FILES), "copy_bytes_differ")
    observation = project_observation(payloads, verdict, counts)
    source_record = source.verify_source(ROOT, expected=record["source_commitment_before"])
    lines = inputs["faulty_helper"].decode("utf-8").splitlines()
    faulty_lines = [i for i, line in enumerate(lines, 1) if "sqlite3.connect(" in line and "mode=ro" in line and "immutable=1" not in line]
    c.require(len(faulty_lines) == 1, "faulty_helper_source")
    commitments = dict(schema_version="internal30-public-evidence-provenance/1.0",
        execution_commit=record["execution_commit"], execution_tree=record["execution_tree"],
        source_commitment=source_record["commitment_sha256"], bundle_commitment=record["original_bundle_commitment"],
        public_observation_sha256=c.digest(c.raw(observation)),
        retained_local_files={name: dict(bytes=len(blob), sha256=c.digest(blob)) for name, blob in inputs.items()},
        original_payloads=[dict(path=name, bytes=len(payloads[name]), sha256=c.digest(payloads[name])) for name in FILES],
        helper_script_sha256=c.digest(read(Path(__file__))),
        incident=dict(original_files=14, original_expected_files=12, sidecars=accident["unexpected_files"],
            failed_auxiliary_line=faulty_lines[0], failed_auxiliary_exit=1, original_directory_still_rejected=True,
            original_payloads_unchanged=True, incident_retracted=False, sidecars_deleted=False),
        reconstruction=dict(copy_files=12, unchanged_production_verifier_passed=True, copy_stable_after_auxiliary_counts=True),
        public_default_verification_scope="published_hashes_and_arithmetic_only_not_full_archive_authentication",
        full_recomputation_requires_local_originals=True, provider_calls_during_pack_build=0,
        online_run_repeated=False, external_validation_completed=False, status_t7_closed=False)
    c.scan_json(commitments)
    c.require(snapshot(original, (*FILES, "audit.sqlite3-shm", "audit.sqlite3-wal")) == originals, "original_scene_changed")
    c.require(snapshot(copy) == payloads, "copy_changed_after_export")
    for name, path in dict(bundle=bundle, exit_observation=exit_observation, incident=incident,
            copy_receipt=copy_receipt, faulty_helper=faulty_helper).items():
        c.require(read(path) == inputs[name], "local_inputs_changed")
    return {"public_observation.json": c.raw(observation), "artifact_commitments.json": c.raw(commitments)}


def verify_public_pack(directory):
    observation_bytes = read(Path(directory) / "public_observation.json", 262144)
    commitments_bytes = read(Path(directory) / "artifact_commitments.json", 262144)
    observation, commitments = c.decode(observation_bytes), c.decode(commitments_bytes)
    c.require(c.raw(observation) == observation_bytes and c.raw(commitments) == commitments_bytes, "public_canonical")
    c.require(c.digest(observation_bytes) == commitments["public_observation_sha256"], "public_hash")
    c.require(c.digest(read(Path(__file__))) == commitments["helper_script_sha256"], "public_generator_changed")
    validate_public_observation(observation)
    c.require(commitments["incident"]["original_directory_still_rejected"] is True
        and commitments["incident"]["incident_retracted"] is False
        and commitments["reconstruction"]["copy_files"] == 12, "public_incident")
    return dict(status="valid_public_projection_only", full_archive_reverified=False, provider_calls=0)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_arguments")


def validate_output_location(output, original, copy):
    target = Path(os.path.abspath(output))
    for value in (original, copy):
        parent = Path(os.path.abspath(value))
        c.require(target != parent and parent not in target.parents, "output_inside_input_archive")
    c.require(not os.path.lexists(target), "output_exists")
    for parent in target.parents:
        info = parent.lstat()
        c.require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400, "output_parent_link")
    return target


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--output", type=Path)
    for name in ("original", "copy", "bundle", "exit-observation", "incident", "copy-receipt", "faulty-helper"):
        parser.add_argument("--" + name, type=Path)
    try:
        args = parser.parse_args(argv)
        values = {key: getattr(args, key) for key in ("original", "copy", "bundle", "exit_observation", "incident", "copy_receipt", "faulty_helper")}
        if args.verify:
            c.require(args.output is None and not any(values.values()), "mode_conflict")
            result = verify_public_pack(args.verify)
        else:
            c.require(args.output is not None and all(values.values()), "arguments")
            target = validate_output_location(args.output, args.original, args.copy)
            pack = build_public_pack(**values)
            # New directory only: output cannot alter either input archive.
            target.mkdir(parents=False, exist_ok=False)
            for name, blob in pack.items():
                with (target / name).open("xb") as stream:
                    stream.write(blob)
            result = dict(status="public_projection_created", files=list(pack), provider_calls=0)
        print(c.raw(result).decode())
        return 0
    except (Exception, KeyboardInterrupt):
        # No exception/argument echo, which could contain paths or fake secrets.
        print('{"status":"failed","error_code":"internal_readback_rejected","provider_calls":0}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
