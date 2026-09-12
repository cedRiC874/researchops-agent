"""Pure success-only semantics for the versioned first-live artifact profile.

Failure/partial/unknown receipts cannot serve as admission evidence. They remain
historical facts handled by their original verifier. This module never imports
that verifier, creates an AuditLedger, or constructs any runtime authority.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction

from researchops.audit import verify_audit_chain_rows
from researchops_completion_telemetry.persisted_evidence import summarize_persisted_live_runtime_denominator_artifact

from .errors import ExternalClosurePrimitiveError
from .io import scan_public_artifact_bytes
from .primitives import canonical_json_bytes, decode_strict_json_object, parse_utc_timestamp


PROFILE_SHA256 = "573a3294662f28556585af225929e6b5a8f7cbae64b2e7eeb0429457a3ef7af0"
PROFILE_PATH = "evals/provider_completion_admission_link_v1/first_live_artifact_profile_v1.json"
_BINDING_FIELDS = ("telemetry_schema_sha256", "adapter_version", "mapping_schema_version", "mapping_version",
                   "mapping_sha256", "provider_id", "api_surface", "transport_id", "output_counter_comparability", "output_counter_path")
_PLAN_BINDING_FIELDS = tuple(name for name in _BINDING_FIELDS if not name.startswith("output_counter"))
_FALSE_TERMINAL = ("outcome_unknown", "provider_key_persisted", "raw_response_body_persisted", "message_content_persisted",
                   "exception_text_persisted", "input_content_repeated_in_receipt", "strict_provider_billing_hard_cap",
                   "closure_claim_allowed", "automatic_registry_promotion_allowed", "authorizes_retry", "authorizes_resume",
                   "authorizes_evaluation", "authorizes_provider_registration", "authorizes_model_quality_claim")


def _fail(code):
    raise ExternalClosurePrimitiveError("admission_first_live_" + code) from None


def _same(left, right):
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _sha(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def profile_document(raw: bytes) -> dict:
    if type(raw) is not bytes or len(raw) != 5309 or hashlib.sha256(raw).hexdigest() != PROFILE_SHA256:
        _fail("profile_invalid")
    return decode_strict_json_object(raw, max_bytes=8192)


def expected_first_live_plan(profile: dict, binding: dict) -> tuple[dict, dict]:
    case_ids = [profile["constants"]["VALIDATION_CASE_ID"]]
    plan = {"schema_version":"provider-completion-runtime-denominator-plan/1.0",
            **{name:binding[name] for name in _PLAN_BINDING_FIELDS},
            "case_ids":case_ids, "case_ids_sha256":_sha(case_ids), "max_turns_per_case":2,
            "total_model_request_cap":2, "agents_sdk_retries":0, "http_client_retries":0,
            "denominator_algorithm":"transport-response-finalization-v1", "exact_response_count_preregistered":False}
    persisted = {name:plan[name] for name in ("case_ids","case_ids_sha256","max_turns_per_case","total_model_request_cap",
                                            "denominator_algorithm","exact_response_count_preregistered")}
    persisted.update(preregistration_commitment=_sha(plan),binding=binding)
    return plan, persisted


def _utc(raw, *, audit=False):
    if type(raw) is not str or len(raw)>96:
        _fail("timeline_invalid")
    if audit and raw.endswith("+00:00"):
        raw=raw[:-6]+"Z"
    return parse_utc_timestamp(raw)


def _elapsed(start,end):
    delta=end.whole_second_utc-start.whole_second_utc
    def fraction(value):
        return Fraction(int(value.fraction_digits),10**len(value.fraction_digits)) if value.fraction_digits else Fraction(0)
    return Fraction(delta.days*86400+delta.seconds)+fraction(end)-fraction(start)


def _price(raw):
    if type(raw) is not str or re.fullmatch(r"[0-9]{1,4}\.[0-9]{6}",raw) is None:
        _fail("price_invalid")
    value=Decimal(raw)
    if not Decimal(0)<value<=Decimal(1000):
        _fail("price_invalid")
    return value


def _cost(input_tokens,output_tokens,input_price,output_price):
    with localcontext() as context:
        context.prec=50
        context.rounding=ROUND_HALF_EVEN
        return (Decimal(input_tokens)*input_price+Decimal(output_tokens)*output_price)/Decimal(1_000_000)


def _money(value):
    with localcontext() as context:
        context.prec=50
        context.rounding=ROUND_HALF_EVEN
        return format(value.quantize(Decimal("0.000001")),"f")


def recompute_authorization_binding(consumption: dict) -> str:
    fields=("authorization_id_sha256","authorization_expires_at_utc","contract_commitment_sha256",
            "implementation_commitment_sha256","execution_commit","source_integrity_commitment_sha256",
            "pricing_snapshot_date","pricing_source_url","input_price_per_million_cny","output_price_per_million_cny")
    value={"schema_version":"deepseek-first-live-authorization-binding/2.0",**{name:consumption[name] for name in fields},
           "locked_limits":{"network_attempts":2,"model_requests":2,"output_token_caps":[256,16],
                            "whole_process_wall_timeout_seconds":330,"local_observed_cost_stop_cny":"1.000000",
                            "retries":0,"resume":False,"fallback":False}}
    return hashlib.sha256(b"researchops-deepseek-first-live-authorization-binding-v2\0"+canonical_json_bytes(value)).hexdigest()


def verify_success_semantics(profile_bytes: bytes, *, bundle: dict, documents: dict, binding: dict,
                             context, run_rows: tuple, event_rows: tuple, model_call_rows: tuple,
                             tool_counts: tuple) -> dict:
    """Return derived counts/hashes, not source/review/registry/online authority."""
    profile=profile_document(profile_bytes)
    constants=profile["constants"]
    fields=profile["field_sets"]
    for name, key in (("consumption.json","_CONSUMPTION_FIELDS"),("terminal.json","_TERMINAL_FIELDS"),
                      ("manifest.json","_MANIFEST_FIELDS"),("audit_index.json","_AUDIT_INDEX_FIELDS"),
                      ("completion_telemetry.json","_SUCCESS_EVIDENCE_FIELDS")):
        if type(documents[name]) is not dict or set(documents[name])!=set(fields[key]):
            _fail("document_fields_invalid")
    c=documents["consumption.json"]; t=documents["terminal.json"]; e=documents["completion_telemetry.json"]
    d=documents["runtime_denominator.json"]; m=documents["manifest.json"]; a=documents["audit_index.json"]
    if set(binding)!=set(_BINDING_FIELDS):
        _fail("mapping_binding_invalid")
    if t["status"]!="success" or e["status"]!="validated" or m["status"]!="complete":
        _fail("not_success")
    common={"contract_id":constants["VALIDATION_ID"],"contract_commitment_sha256":bundle["first_live_design_commitment_sha256"],
            "implementation_commitment_sha256":bundle["subject"]["first_live_implementation_commitment_sha256"]}
    for obj in (c,t,e):
        if any(obj[name]!=value for name,value in common.items()):
            _fail("control_plane_binding_mismatch")
    if any(m[name]!=value for name,value in common.items() if name!="contract_id"):
        _fail("control_plane_binding_mismatch")
    for obj,name in ((c,"CONSUMPTION_SCHEMA_VERSION"),(t,"TERMINAL_SCHEMA_VERSION"),(e,"EVIDENCE_SCHEMA_VERSION"),(m,"MANIFEST_SCHEMA_VERSION")):
        if obj["schema_version"]!=constants[name]:
            _fail("document_version_invalid")
    linked={"authorization_id_sha256":bundle["authorization_id_sha256"],
            "authorization_binding_schema_version":"deepseek-first-live-authorization-binding/2.0",
            "authorization_binding_sha256":bundle["external_authorization_binding_sha256"],
            "execution_commit":bundle["subject"]["first_live_execution_commit"],
            "source_integrity_commitment_sha256":bundle["subject"]["first_live_source_integrity_commitment_sha256"]}
    if any(c[name]!=value or t[name]!=value for name,value in linked.items()):
        _fail("authorization_binding_mismatch")
    if recompute_authorization_binding(c)!=bundle["external_authorization_binding_sha256"]:
        _fail("authorization_binding_mismatch")
    if c["status"]!="consumed" or c["consume_before_key_load"] is not True or c["provider_key_loaded_at_consumption"] is not False:
        _fail("consumption_invalid")
    if any(type(c[name]) is not int or c[name]!=0 for name in ("network_attempts_at_consumption","model_requests_at_consumption")):
        _fail("consumption_invalid")
    if any(c[name] is not False for name in ("authorizes_retry","authorizes_resume","authorizes_evaluation","authorizes_status_closure")):
        _fail("consumption_invalid")
    if type(c["source_integrity_plan_id"]) is not str or re.fullmatch(r"phase6-deepseek-depth60-v[1-9][0-9]*",c["source_integrity_plan_id"]) is None:
        _fail("source_plan_invalid")
    if t["source_integrity_plan_id"]!=c["source_integrity_plan_id"] or t["authorization_expires_at_utc"]!=c["authorization_expires_at_utc"]:
        _fail("authorization_binding_mismatch")
    consumed=_utc(c["consumed_at_utc"]); started=_utc(t["started_at_utc"]); completed=_utc(t["completed_at_utc"]); expires=_utc(c["authorization_expires_at_utc"])
    if not consumed<=started<=completed<=expires or not 330<=_elapsed(consumed,expires)<=86400 or _elapsed(started,completed)>330:
        _fail("timeline_invalid")
    if bundle["first_live_completed_at_utc"]!=t["completed_at_utc"] or c["pricing_snapshot_date"]!=consumed.whole_second_utc.date().isoformat():
        _fail("timeline_invalid")
    if c["pricing_source_url"]!="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/":
        _fail("price_invalid")
    input_price=_price(c["input_price_per_million_cny"]); output_price=_price(c["output_price_per_million_cny"])
    if _cost(1024,272,input_price,output_price)>Decimal(1):
        _fail("cost_reservation_exceeded")
    if any(t[name] is not False for name in _FALSE_TERMINAL):
        _fail("terminal_invalid")
    if any(t[name] is not True for name in ("provider_key_loaded","raw_response_cleanup_complete","usage_complete","manifest_complete","network_call_observation_complete")):
        _fail("terminal_invalid")
    if t["error_code"] is not None or t["actual_provider_billed_cost_cny"] is not None or t["partial_artifacts"]!={} or t["ledger_run_status"]!="completed" or t["local_observed_usage_cost_stop_cny"]!="1.000000":
        _fail("terminal_invalid")
    for name,value in {"network_attempts":2,"network_calls":2,"model_requests":2,"network_attempt_limit":2,"model_request_limit":2,"input_token_limit":1024,"output_token_limit":272}.items():
        if type(t[name]) is not int or t[name]!=value:
            _fail("terminal_invalid")
    for obj,names in ((m,("raw_response_body_persisted","message_content_persisted","api_key_persisted")),
                      (e,("status_defect_closure_allowed","closure_claim_allowed","automatic_registry_promotion_allowed","raw_response_body_persisted","message_content_persisted","provider_key_persisted"))):
        if any(obj[name] is not False for name in names):
            _fail("claim_boundary_invalid")
    plan,persisted=expected_first_live_plan(profile,binding)
    summary=summarize_persisted_live_runtime_denominator_artifact(d,context=context)
    if not summary.inner_closure_claim_allowed or not _same(e["runtime_plan"],persisted) or not _same(e["runtime_denominator"],d):
        _fail("denominator_invalid")
    case_id=constants["VALIDATION_CASE_ID"]
    attempts=[{"case_id":case_id,"attempt_index":i,"case_attempt_index":i,"terminal_kind":"response_accepted","response_index":i,"error_code":None} for i in range(2)]
    if not _same(d["attempts"],attempts) or len(d["records"])!=2:
        _fail("denominator_invalid")
    # The generic denominator also supports partial SDK usage rows. This
    # fixed writer's successful path pins exactly one usage entry per reply
    # (source profile, tracker.seal_case); global counts alone are insufficient.
    expected_case={"case_id":case_id,"attempts_started":2,"attempts_terminal":2,"observed_response_count":2,
                   "accepted_response_count":2,"rejected_response_count":0,"sdk_raw_response_count":2,
                   "sdk_raw_response_reconciliation":"matched","sdk_usage_request_count":2,
                   "sdk_usage_request_reconciliation":"matched","closure_eligible":True,
                   "sdk_request_usage_indices_by_response":[{"response_index":i,"sdk_raw_response_index":i,
                                                             "sdk_request_usage_indices":[0]} for i in range(2)]}
    if not _same(d["cases"],[expected_case]):
        _fail("sdk_usage_coverage_invalid")
    records=d["records"]
    shapes=[]; input_tokens=0; output_tokens=0
    for i,(record,cap,state) in enumerate(zip(records,(256,16),("completed","incomplete_length"))):
        if record["normalized_completion_state"]!=state or record["truncation_signal_source"]!="native_status":
            _fail("completion_shape_invalid")
        if record["output_token_cap"]["availability"]!="provided" or record["output_token_cap"]["value"]!=cap:
            _fail("output_cap_invalid")
        usage=record["usage"]["normalized"]
        if type(usage["input_tokens"]) is not int or type(usage["output_tokens"]) is not int or not 0<=usage["input_tokens"]<=512 or not 0<=usage["output_tokens"]<=cap:
            _fail("usage_invalid")
        input_tokens+=usage["input_tokens"]; output_tokens+=usage["output_tokens"]
        shapes.append({"status_availability":record["native_status"]["availability"],"status_value":record["native_status"]["value"],
                       "incomplete_details_availability":record["native_incomplete_details"]["availability"],"incomplete_details_value":record["native_incomplete_details"]["value"]})
    if not _same(shapes,profile["expected_completion_shapes"]):
        _fail("completion_shape_invalid")
    cost=_cost(input_tokens,output_tokens,input_price,output_price)
    if cost>1 or e["validation_integrity_gate_passed"] is not True:
        _fail("usage_invalid")
    expected={"observed_states_in_order":["completed","incomplete_length"],"observed_signal_sources_in_order":["native_status"]*2,
              "observed_completion_shapes_in_order":shapes,"observed_input_tokens":input_tokens,"observed_output_tokens":output_tokens,"local_observed_cost_cny":_money(cost)}
    if any(not _same(e[name],value) for name,value in expected.items()) or type(t["input_tokens"]) is not int or type(t["output_tokens"]) is not int or (t["input_tokens"],t["output_tokens"],t["local_observed_usage_cost_cny"])!=(input_tokens,output_tokens,_money(cost)):
        _fail("usage_projection_mismatch")
    if len(run_rows)!=1 or len(event_rows)!=8 or model_call_rows!=() or tool_counts!=(0,0,0):
        _fail("audit_scope_invalid")
    run=dict(run_rows[0]); run_id="RUN-"+case_id
    request={"validation_id":constants["VALIDATION_ID"],"contract_commitment_sha256":common["contract_commitment_sha256"],
             "implementation_commitment_sha256":common["implementation_commitment_sha256"],"authorization_id_sha256":bundle["authorization_id_sha256"],
             "scenario_count":2,"provider":"deepseek","model":"deepseek-v4-flash"}
    expected_run={"run_id":run_id,"mode":"deepseek_completion_first_live_validation","status":"completed","request_sha256":_sha(request),
                  "dataset_sha256":None,"created_at_utc":run["created_at_utc"],"updated_at_utc":run["updated_at_utc"],"terminal_error_code":None}
    if not _same(run,expected_run) or not consumed<=_utc(run["created_at_utc"],audit=True)<=_utc(run["updated_at_utc"],audit=True)<=completed:
        _fail("audit_run_invalid")
    verification=verify_audit_chain_rows(run_id,event_rows)
    if not verification.valid or type(a["runs"]) is not list or len(a["runs"])!=1 or set(a["runs"][0])!=set(fields["_AUDIT_RUN_ENTRY_FIELDS"]):
        _fail("audit_chain_invalid")
    entry=a["runs"][0]
    if entry["case_id"]!=case_id or entry["run_id"]!=run_id or not _same(entry["chain_verification"],verification.to_dict()):
        _fail("audit_index_invalid")
    expected_events=[("run_started","system",{"mode":run["mode"],"request_sha256":run["request_sha256"],"dataset_sha256":None})]
    for i in range(2):
        core={"schema_version":"provider-completion-ledger-event/1.1","case_id":case_id,"attempt_index":i,"case_attempt_index":i,"binding":binding}
        expected_events.extend([
            ("model_request_started","provider_adapter",core),
            ("provider_transport_request_sent","provider_adapter",{"schema_version":"deepseek-first-live-transport-send/1.0","case_id":case_id,
                "attempt_index":i,"case_attempt_index":i,"network_call_index":i,"method":"POST","origin":"https://api.deepseek.com","path":"/responses"}),
            ("model_response_telemetry_recorded","provider_adapter",core|{"terminal_kind":"response_accepted","response_index":i,"error_code":None,"completion_record":records[i]}),
        ])
    expected_events.append(("run_status_changed","system",{"from":"running","to":"completed","error_code":None}))
    last=_utc(run["created_at_utc"],audit=True)
    for row,(kind,actor,payload) in zip(event_rows,expected_events):
        occurred=_utc(row["occurred_at_utc"],audit=True)
        decoded=decode_strict_json_object(row["safe_payload_json"].encode(),max_bytes=2_000_000)
        scan_public_artifact_bytes((canonical_json_bytes(decoded),))
        if row["event_type"]!=kind or row["actor_kind"]!=actor or not _same(decoded,payload) or not last<=occurred<=_utc(run["updated_at_utc"],audit=True):
            _fail("event_projection_invalid")
        last=occurred
    bridge={"schema_version":"provider-completion-ledger-bridge-commitment/1.0","case_id":case_id,"binding_sha256":_sha(binding),
            "started":[{"attempt_index":i,"event_hash":event_rows[1+3*i]["event_hash"]} for i in range(2)],
            "terminals":[{"attempt_index":i,"event_hash":event_rows[3+3*i]["event_hash"]} for i in range(2)],
            "all_started_attempts_terminal":True,"write_failed":False}
    bridge["commitment_sha256"]=_sha(bridge)
    if not _same(entry["completion_telemetry_event_commitment"],bridge):
        _fail("bridge_commitment_invalid")
    reconciliation={"all_chains_valid":True,"ledger_export_failed":False,"ledger_failure_observed":False,
                    "event_counts":dict(sorted(Counter(row["event_type"] for row in event_rows).items())),"reasons":[]}
    if not _same(e["ledger_reconciliation"],reconciliation):
        _fail("reconciliation_invalid")
    return {"accepted_response_count":2,"network_attempt_count":2,"event_count":8,"input_tokens":input_tokens,
            "output_tokens":output_tokens,"local_observed_cost_cny":_money(cost),"chain_head":verification.chain_head,
            "source_plan_id":c["source_integrity_plan_id"],"execution_identity_verified":False,
            "review_verified":False,"registry_admission_verified":False,"runtime_authority_granted":False,"closure_claim_allowed":False}


__all__ = ["PROFILE_PATH","PROFILE_SHA256","profile_document","expected_first_live_plan",
           "recompute_authorization_binding","verify_success_semantics"]
