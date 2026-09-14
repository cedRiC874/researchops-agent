"""Independent, read-only Internal evidence replay. Never mints runtime authority."""
from __future__ import annotations

from pathlib import Path

from researchops.audit import sha256_json, verify_audit_chain_rows
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_completion_telemetry.persisted_evidence import create_persisted_live_evidence_validation_context, validate_persisted_live_completion_record
from researchops_completion_timing.campaign_budget import _CampaignBudget
from researchops_completion_timing.ledger_event import validate_segment_link
from researchops_completion_timing import local_claim
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.io import read_exact_artifact_directory, scan_public_artifact_bytes
from .admission import validate_authorization, validate_freeze
from .artifacts import FILES, PAYLOADS
from .contract import ROOT, budget_plan, commitment, decode, digest, exact, raw, require, utc, validate_tasks


def _verify_listing(document, expected_names, payloads):
    require(type(document["files"]) is list and len(document["files"]) == len(expected_names), "file_set")
    for entry, name in zip(document["files"], expected_names):
        exact(entry, "path bytes sha256")
        require(entry == dict(path=name, bytes=len(payloads[name]), sha256=digest(payloads[name])), "artifact_hash")


def _check_timing(segment, phase):
    fields = ("attempt_started_ns", "send_started_ns", "transport_terminal_ns", "raw_cleanup_terminal_ns", "lifetime_ended_ns")
    values = [segment[name] for name in fields]
    require(all(type(value) is int and 0 <= value <= 5_400_000_000_000 for value in values), "timing_unknown")
    require(values == sorted(values) and values[-1] - values[0] <= 120_000_000_000, "timing_request_limit")
    require(values[3] - values[2] <= 5_000_000_000 and segment["raw_cleanup_status"] == "completed", "timing_cleanup")
    require(phase["key_loaded_ns"] <= values[0] <= values[-1] <= phase["post_cleanup_terminal_ns"], "timing_phase_order")


def verify_archive(directory, *, bundle_bytes, expected_bundle_commitment, expected_approved_digest, observed_exit_code=None):
    """Recompute semantics and require out-of-band expectations, not self-hashes."""
    directory = Path(directory)
    outer = decode(bundle_bytes, 32768)
    exact(outer, "schema_version files commitment_sha256")
    body = {name: outer[name] for name in ("schema_version", "files")}
    require(outer["schema_version"] == "provider-completion-internal-bundle/1.0" and
        commitment("bundle", body) == outer["commitment_sha256"] == expected_bundle_commitment, "bundle_commitment")
    payloads = read_exact_artifact_directory(directory, expected_names=FILES, max_file_bytes=33554432, max_total_bytes=67108864)
    scan_public_artifact_bytes((payloads["audit.sqlite3"],))
    _verify_listing(outer, FILES, payloads)
    docs = {name: decode(value, 1048576) for name, value in payloads.items() if name != "audit.sqlite3"}
    manifest = docs["manifest.json"]
    exact(manifest, "schema_version files")
    require(manifest["schema_version"] == "provider-completion-internal-manifest/1.0", "manifest_schema")
    _verify_listing(manifest, PAYLOADS, payloads)
    freeze = validate_freeze(docs["internal_freeze.json"])
    require(docs["protocol.json"] == freeze["protocol"], "protocol_mismatch")
    tasks = validate_tasks(docs["cases.json"], freeze["protocol"])
    require(digest(payloads["cases.json"]) == freeze["tasks_sha256"], "tasks_drift")
    receipt = docs["local_claim_receipt.json"]
    request_fields = local_claim._profile()["request_fields"]
    exact(receipt, " ".join(request_fields) + " status claimed_at_utc claim_contract_sha256 store_scope provider_actions_authorized")
    require(receipt["schema_version"] == "provider-completion-local-claim/1.0" and receipt["status"] == "reserved"
        and receipt["claim_contract_sha256"] == local_claim.CONTRACT_SHA256
        and receipt["store_scope"] == "single_host_fixed_store" and receipt["provider_actions_authorized"] is False, "claim_receipt")
    auth = docs["internal_authorization.json"]
    candidate = validate_authorization(auth, freeze, expected_approved_digest, at=utc(receipt["claimed_at_utc"]))
    require(receipt["authorization_grant_sha256"] == digest(payloads["internal_authorization.json"])
        and receipt["authorization_id_sha256"] == digest(candidate["authorization_id"].encode())
        and receipt["execution_environment_id"] == candidate["execution_environment_id"]
        and receipt["execution_commit"] == freeze["execution_commit"]
        and receipt["timing_plan_commitment_sha256"] == commitment("timing-plan", freeze), "claim_binding")
    intent = dict(schema_version="provider-completion-internal-claim-intent/1.0", scope="internal_known_synthetic",
        authorization_sha256=digest(payloads["internal_authorization.json"]), freeze_commitment_sha256=commitment("freeze", freeze),
        clock_domain_id=receipt["clock_domain_id"])
    require(docs["local_claim_intent.json"] == intent and receipt["consumption_entry_sha256"] == digest(raw(intent)), "claim_intent")
    publication = docs["publication.json"]
    exact(publication, "schema_version manifest_sha256 manifest_commitment_sha256 observation_scope sealing_elapsed_ns status external_validation_completed status_closure_allowed internal_acceptance_pending_independent_readback")
    require(publication["schema_version"] == "provider-completion-internal-publication/1.0"
        and publication["manifest_sha256"] == digest(payloads["manifest.json"])
        and publication["manifest_commitment_sha256"] == commitment("manifest", manifest)
        and publication["external_validation_completed"] is False and publication["status_closure_allowed"] is False
        and publication["internal_acceptance_pending_independent_readback"] is True
        and publication["observation_scope"] == "before_publication_write_not_process_exit"
        and type(publication["sealing_elapsed_ns"]) is int and 0 <= publication["sealing_elapsed_ns"] <= 120_000_000_000, "publication")
    index, projection, timing = (docs[name] for name in ("run_index.json", "completion_telemetry.json", "timing_evidence.json"))
    exact(index, "schema_version scope run_id status planned_case_count case_pass_count case_states model_requests network_attempts transport_mode response_count budget binding denominator_plan freeze_commitment_sha256 source_commitment_sha256 external_validation_completed status_closure_allowed provider_bill")
    exact(projection, "schema_version records terminals sdk_counts segments")
    require(index["schema_version"] == "provider-completion-internal-run-index/1.0" and index["scope"] == "internal_known_synthetic"
        and index["freeze_commitment_sha256"] == commitment("freeze", freeze)
        and index["source_commitment_sha256"] == freeze["source_commitment_sha256"]
        and index["external_validation_completed"] is False and index["status_closure_allowed"] is False
        and index["provider_bill"] is None and index["planned_case_count"] == 30, "index_binding")
    runs, events, model_rows, *tool_counts = _read_database_rows(directory / "audit.sqlite3", expected_payload=payloads["audit.sqlite3"])
    require(len(runs) == 1 and runs[0]["run_id"] == index["run_id"] and runs[0]["mode"] == "internal_known_synthetic"
        and not model_rows and tool_counts == [0, 0, 0], "audit_unexpected_rows")
    require(verify_audit_chain_rows(index["run_id"], events).valid, "audit_chain")
    for event in events:
        require(utc(candidate["not_before_utc"]) <= utc(event["occurred_at_utc"]) < utc(candidate["expires_at_utc"]), "audit_time")
    if index["status"] != "completed":
        return dict(eligible_before_exit_observation=False, internal_acceptance_passed=False, failure_container_verified=True,
            external_validation_completed=False, status_closure_allowed=False)
    require(runs[0]["status"] == "completed" and publication["status"] == "completed", "terminal_status")
    require(index["transport_mode"] in ("native_http_transport", "offline_mock_transport"), "transport_mode")
    require(all(type(index[name]) is int and index[name] == 30 for name in ("case_pass_count", "model_requests", "network_attempts", "response_count")), "complete_denominator")
    expected_states = [dict(case_id=case["case_id"], case_index=i, status="passed") for i,case in enumerate(tasks["cases"])]
    require(index["case_states"] == expected_states, "case_projection")
    require(all(type(projection[name]) is list and len(projection[name]) == 30 for name in ("records", "terminals", "sdk_counts", "segments")), "response_denominator")
    selection = load_and_select_surface_mapping(ROOT, "deepseek", "responses", "openai_compatible_responses", purpose="offline_validation")
    context = create_persisted_live_evidence_validation_context(selection, expected_binding=index["binding"],
        expected_denominator_plan=index["denominator_plan"], expected_denominator_plan_commitment_sha256=sha256_json(index["denominator_plan"]))
    phase = timing["phase"]
    phase_fields = ("task_released_ns", "key_loaded_ns", "post_cleanup_terminal_ns", "key_reference_released_ns", "phase_terminal_ns")
    phase_values = [phase[name] for name in phase_fields]
    require(all(type(value) is int and 0 <= value <= 5_400_000_000_000 for value in phase_values)
        and phase_values == sorted(phase_values) and phase["post_cleanup_status"] == "completed"
        and phase["key_reference_release_status"] == "released" and timing["closed"] is True
        and timing["halted"] is False and timing["clock_failed"] is False and timing["active_attempt"] is None, "phase_timing")
    require(len(timing["attempts"]) == 30, "timing_denominator")
    starts, sends, terminals, closes = [], [], [], []
    expected_events = ["run_started"] + [name for _ in range(30) for name in
        ("model_request_started", "internal_send_intent", "model_response_telemetry_recorded", "internal_case_closed")] + ["run_status_changed"]
    require([event["event_type"] for event in events] == expected_events, "event_lifecycle_order")
    allowed_other = {"run_started", "run_status_changed"}
    for event in events:
        payload = decode(event["safe_payload_json"].encode(), 65536)
        kind = event["event_type"]
        if kind == "model_request_started": starts.append(payload)
        elif kind == "internal_send_intent": sends.append(payload)
        elif kind == "model_response_telemetry_recorded": terminals.append((kind, payload))
        elif kind == "internal_case_closed": closes.append(payload)
        else: require(kind in allowed_other, "unexpected_event")
    require(len(starts) == len(sends) == len(terminals) == len(closes) == 30, "event_denominator")
    plan = index["denominator_plan"]
    ids = [case["case_id"] for case in tasks["cases"]]
    require(plan["case_ids"] == ids and plan["case_ids_sha256"] == sha256_json(ids)
        and plan["max_turns_per_case"] == 1 and plan["total_model_request_cap"] == 30
        and plan["agents_sdk_retries"] == plan["http_client_retries"] == 0
        and plan["exact_response_count_preregistered"] is False, "plan_task_projection")
    budget = _CampaignBudget(budget_plan(freeze["protocol"], tasks, freeze["pricing"]))
    timing_binding = dict(plan_commitment_sha256=commitment("timing-plan", freeze),
        execution_binding_sha256=commitment("freeze", freeze), clock_domain_id=receipt["clock_domain_id"])
    last_end = phase["key_loaded_ns"]
    for i, case in enumerate(tasks["cases"]):
        record, segment, sdk = (projection[name][i] for name in ("records", "segments", "sdk_counts"))
        validate_persisted_live_completion_record(record, context=context)
        require(record["request_index"] == record["response_index"] == i, "record_index")
        from .runtime import check_response
        usage = check_response(record)
        exact(sdk, "raw_response_count input_tokens output_tokens")
        require(sdk == dict(raw_response_count=1,input_tokens=usage["input_tokens"],output_tokens=usage["output_tokens"]), "sdk_projection")
        expected_terminal = dict(case_id=case["case_id"], attempt_index=i, response_index=i, terminal_kind="response_accepted")
        require(projection["terminals"][i] == expected_terminal, "terminal_projection")
        kind, payload = terminals[i]
        for member in (starts[i], sends[i], payload):
            require(member["case_id"] == case["case_id"] and member["attempt_index"] == i, "event_order")
        require(payload["completion_record"] == record and payload["response_index"] == i, "record_event_link")
        require(payload["binding"] == starts[i]["binding"] == index["binding"]
            and payload["case_attempt_index"] == starts[i]["case_attempt_index"] == 0, "event_binding")
        require(sends[i]["freeze_commitment_sha256"] == commitment("freeze", freeze) and sends[i]["tasks_sha256"] == freeze["tasks_sha256"]
            and sends[i]["source_commitment_sha256"] == freeze["source_commitment_sha256"] and sends[i]["cap"] == 512, "send_binding")
        reference = validate_segment_link(payload, event_type=kind, segment_bytes=raw(segment), expected_timing_binding=timing_binding)
        require(payload["timing"] == reference, "timing_event_link")
        _check_timing(segment, phase)
        close = closes[i]
        exact(close,"schema_version case_id case_index clock_domain_id request_started_ns request_completed_ns completion_record_sha256 sdk_counts")
        require(close["schema_version"] == "provider-completion-internal-case-close/1.0" and close["case_id"] == case["case_id"]
            and close["case_index"] == i and close["clock_domain_id"] == receipt["clock_domain_id"]
            and close["completion_record_sha256"] == digest(raw(record)) and close["sdk_counts"] == sdk, "case_close_binding")
        begin,end=close["request_started_ns"],close["request_completed_ns"]
        require(type(begin) is int and type(end) is int and
            last_end <= begin <= segment["attempt_started_ns"] <= segment["lifetime_ended_ns"] <= end <= phase["post_cleanup_terminal_ns"]
            and end-begin <= 120_000_000_000, "request_cleanup_timing")
        last_end = end
        require(all(segment[name] == value for name,value in timing["attempts"][i].items()), "timing_projection")
        budget.begin_case(i)
        reservation = budget.reserve_request()
        budget.observe_usage(reservation, input_tokens=usage["input_tokens"],output_tokens=usage["output_tokens"],requests=usage["requests"])
        budget.finish_case()
    require(index["budget"] == budget.summary(), "budget_projection")
    again = read_exact_artifact_directory(directory, expected_names=FILES,max_file_bytes=33554432,max_total_bytes=67108864)
    require(dict(again) == dict(payloads), "archive_changed")
    return dict(eligible_before_exit_observation=True,
        internal_acceptance_passed=type(observed_exit_code) is int and observed_exit_code == 0 and index["transport_mode"] == "native_http_transport",
        offline_engineering_acceptance_passed=type(observed_exit_code) is int and observed_exit_code == 0 and index["transport_mode"] == "offline_mock_transport",
        transport_mode=index["transport_mode"],
        response_count=30, case_pass_count=30, external_validation_completed=False, status_closure_allowed=False,
        provider_bill=None, evidence_scope="internal_known_synthetic")
