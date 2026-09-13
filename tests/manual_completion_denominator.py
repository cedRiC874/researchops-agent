"""Synthetic persisted-data construction with no runtime factory or tracker."""

from __future__ import annotations

import hashlib

from researchops_completion_telemetry.sanitization import (
    OfflineCompletionRecordBinding, build_offline_completion_record, sanitize_completion_capture,
)
from researchops_external_closure.primitives import canonical_json_bytes


def manual_denominator(selection,binding,case_ids,*,observed_cases=2,incomplete_usage=False,input_tokens=10,rejected_responses=False):
    plan={"schema_version":"provider-completion-runtime-denominator-plan/1.0",
          **{key:binding[key] for key in ("provider_id","api_surface","transport_id","adapter_version","telemetry_schema_sha256","mapping_schema_version","mapping_version","mapping_sha256")},
          "case_ids":list(case_ids),"case_ids_sha256":hashlib.sha256(canonical_json_bytes(list(case_ids))).hexdigest(),
          "max_turns_per_case":2,"total_model_request_cap":2,"agents_sdk_retries":0,"http_client_retries":0,
          "denominator_algorithm":"transport-response-finalization-v1","exact_response_count_preregistered":False}
    commitment=hashlib.sha256(canonical_json_bytes(plan)).hexdigest()
    offline=OfflineCompletionRecordBinding(**{key:binding[key] for key in ("provider_id","api_surface","transport_id","adapter_version",
             "telemetry_schema_sha256","mapping_schema_version","mapping_version","mapping_sha256")})
    attempts=[];cases=[];records=[]
    kind="response_rejected" if rejected_responses else "response_accepted"
    for ordinal,case_id in enumerate(case_ids[:observed_cases]):
        attempts.append({"case_id":case_id,"attempt_index":ordinal,"case_attempt_index":0,"terminal_kind":kind,
                         "response_index":ordinal,"error_code":"provider_completion_capture_failed" if rejected_responses else None})
        if not rejected_responses:
            usage={"output_tokens":2} if incomplete_usage else {"input_tokens":input_tokens,"output_tokens":2,"total_tokens":input_tokens+2}
            capture=sanitize_completion_capture({"status":"completed","incomplete_details":None,"usage":usage,"requested_output_token_cap":2000},
                normalized_usage={"requests":None if incomplete_usage else 1,"input_tokens":None if incomplete_usage else input_tokens,
                                  "output_tokens":2,"total_tokens":None if incomplete_usage else input_tokens+2,
                                  "cached_input_tokens":None if incomplete_usage else 0,"cache_write_tokens":None,"reasoning_tokens":None})
            record=build_offline_completion_record(capture,binding=offline,response_index=ordinal,request_index=ordinal,
                mapping_resolver=lambda projection,*args:selection.resolve_mapping(projection),
                output_counter_comparability=binding["output_counter_comparability"],output_counter_path=binding["output_counter_path"])
            # Conformance data models an on-disk record; it is not live evidence.
            record["record_provenance"]="live_adapter_write"
            records.append(record)
        cases.append({"case_id":case_id,"attempts_started":1,"attempts_terminal":1,"observed_response_count":1,
                      "accepted_response_count":0 if rejected_responses else 1,"rejected_response_count":1 if rejected_responses else 0,
                      "sdk_raw_response_count":1,"sdk_raw_response_reconciliation":"matched","sdk_usage_request_count":1,
                      "sdk_usage_request_reconciliation":"matched","closure_eligible":not rejected_responses,
                      "sdk_request_usage_indices_by_response":[{"response_index":ordinal,"sdk_raw_response_index":0,"sdk_request_usage_indices":[0]}]})
    artifact={"schema_version":"provider-completion-runtime-denominator-artifact/1.0","denominator_algorithm":"transport-response-finalization-v1",
              "exact_response_count_preregistered":False,"derived_after_run":True,"preregistration_commitment":commitment,
              "planned_case_ids":list(case_ids),"max_turns_per_case":2,"total_model_request_cap":2,
              "attempts_started":observed_cases,"attempts_terminal":observed_cases,"observed_response_count":observed_cases,
              "accepted_response_count":0 if rejected_responses else observed_cases,"rejected_response_count":observed_cases if rejected_responses else 0,
              "terminal_kind_counts":{name:observed_cases if name==kind else 0 for name in ("response_accepted","response_rejected","http_error","no_response","cancelled","outcome_unknown")},
              "attempts":attempts,"cases":cases,"not_finalized_case_ids":list(case_ids[observed_cases:]),"records":records}
    return plan,artifact
