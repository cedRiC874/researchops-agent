"""Hand-built synthetic conformance data, never a live capture or runtime grant.

Uses the public offline record builder and a temporary audit schema. No runtime
binding, tracker, validation session, Provider or first-live runner is created.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from pathlib import Path

from researchops.audit import AuditLedger, _event_hash, verify_audit_chain_rows
from researchops_completion_telemetry.sanitization import OfflineCompletionRecordBinding, build_offline_completion_record, sanitize_completion_capture
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping
from researchops_external_closure.admission_bundle_bytes import admission_bundle_commitment
from researchops_external_closure.admission_contract import load_admission_link_contract
from researchops_external_closure.first_live_success_semantics import PROFILE_PATH, expected_first_live_plan, profile_document, recompute_authorization_binding
from researchops_external_closure.primitives import canonical_json_bytes


ROOT=Path(__file__).resolve().parents[1]


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def raw(document):
    return canonical_json_bytes(document)+b"\n"


class SyntheticFirstLiveArtifacts:
    def __init__(self,directory: Path):
        self.directory=directory
        directory.mkdir()
        self.contract=load_admission_link_contract(ROOT)
        self.profile=profile_document((ROOT/PROFILE_PATH).read_bytes())
        selection=load_and_select_surface_mapping(ROOT,"deepseek","responses","openai_compatible_responses",purpose="offline_validation")
        self.binding={name:getattr(selection,name) for name in ("telemetry_schema_sha256","adapter_version","mapping_schema_version","mapping_version",
                     "mapping_sha256","provider_id","api_surface","transport_id","output_counter_comparability","output_counter_path")}
        plan,self.persisted=expected_first_live_plan(self.profile,self.binding)
        self.case_id=self.profile["constants"]["VALIDATION_CASE_ID"]
        self.run_id="RUN-"+self.case_id
        self.subject={"provider_id":"deepseek","model_id":"deepseek-v4-flash","api_origin":"https://api.deepseek.com",
                      "api_surface":"responses","transport_id":"openai_compatible_responses","adapter_version":selection.adapter_version,
                      "first_live_execution_commit":"1"*40,"first_live_execution_tree":"2"*40,
                      "first_live_source_integrity_commitment_sha256":h("source plan"),"first_live_implementation_commitment_sha256":h("implementation"),
                      "source_byte_inventory_sha256":h("source inventory"),"dependency_lock_sha256":h("lock"),"pyproject_sha256":h("pyproject"),
                      "selected_mapping_sha256":selection.mapping_sha256}
        self.records=[]
        offline=OfflineCompletionRecordBinding(**{name:self.binding[name] for name in ("telemetry_schema_sha256","adapter_version","mapping_schema_version",
                    "mapping_version","mapping_sha256","provider_id","api_surface","transport_id")})
        for i,cap in enumerate((256,16)):
            output=5 if i==0 else 16
            capture=sanitize_completion_capture({"status":"completed" if i==0 else "incomplete",
                "incomplete_details":None if i==0 else {"reason":"max_output_tokens"},
                "usage":{"input_tokens":10,"output_tokens":output,"total_tokens":10+output},
                "provider_request_id":"synthetic-request", "http_status":200,"requested_output_token_cap":cap},
                normalized_usage={"requests":1,"input_tokens":10,"output_tokens":output,"total_tokens":10+output,
                                  "cached_input_tokens":0,"cache_write_tokens":None,"reasoning_tokens":None})
            record=build_offline_completion_record(capture,binding=offline,response_index=i,request_index=i,
                mapping_resolver=lambda projection,*args:selection.resolve_mapping(projection),
                output_counter_comparability=selection.output_counter_comparability,output_counter_path=selection.output_counter_path)
            # Conformance input models persisted data, not provenance evidence.
            record["record_provenance"]="live_adapter_write"
            self.records.append(record)
        counts={"attempts_started":2,"attempts_terminal":2,"observed_response_count":2,"accepted_response_count":2,"rejected_response_count":0}
        self.attempts=[{"case_id":self.case_id,"attempt_index":i,"case_attempt_index":i,"terminal_kind":"response_accepted","response_index":i,"error_code":None} for i in range(2)]
        self.denominator={"schema_version":"provider-completion-runtime-denominator-artifact/1.0",
            "denominator_algorithm":"transport-response-finalization-v1","exact_response_count_preregistered":False,"derived_after_run":True,
            "preregistration_commitment":self.persisted["preregistration_commitment"],"planned_case_ids":[self.case_id],
            "max_turns_per_case":2,"total_model_request_cap":2,**counts,
            "terminal_kind_counts":{"response_accepted":2,"response_rejected":0,"http_error":0,"no_response":0,"cancelled":0,"outcome_unknown":0},
            "attempts":self.attempts,"not_finalized_case_ids":[],"records":self.records,
            "cases":[{"case_id":self.case_id,**counts,"sdk_raw_response_count":2,"sdk_raw_response_reconciliation":"matched",
                      "sdk_usage_request_count":2,"sdk_usage_request_reconciliation":"matched","closure_eligible":True,
                      "sdk_request_usage_indices_by_response":[{"response_index":i,"sdk_raw_response_index":i,"sdk_request_usage_indices":[0]} for i in range(2)]}]}
        common={"contract_id":self.profile["constants"]["VALIDATION_ID"],"contract_commitment_sha256":h("design"),
                "implementation_commitment_sha256":self.subject["first_live_implementation_commitment_sha256"]}
        linked={"authorization_id_sha256":h("authorization"),"authorization_binding_schema_version":"deepseek-first-live-authorization-binding/2.0",
                "authorization_expires_at_utc":"2026-09-05T01:10:00.000Z","execution_commit":self.subject["first_live_execution_commit"],
                "source_integrity_commitment_sha256":self.subject["first_live_source_integrity_commitment_sha256"],"source_integrity_plan_id":"phase6-deepseek-depth60-v5"}
        c={name:False for name in self.profile["field_sets"]["_CONSUMPTION_FIELDS"]}
        c.update(common|linked|{"schema_version":"deepseek-first-live-consumption/1.1","status":"consumed",
            "consumed_at_utc":"2026-09-05T01:00:00.000Z","pricing_snapshot_date":"2026-09-05",
            "pricing_source_url":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing/","input_price_per_million_cny":"1.000000","output_price_per_million_cny":"2.000000",
            "consume_before_key_load":True,"network_attempts_at_consumption":0,"model_requests_at_consumption":0})
        self.auth=recompute_authorization_binding(c)
        c["authorization_binding_sha256"]=self.auth
        t={name:False for name in self.profile["field_sets"]["_TERMINAL_FIELDS"]}
        t.update(common|linked|{"schema_version":"deepseek-first-live-terminal/1.1","status":"success","error_code":None,
            "authorization_binding_sha256":self.auth,"started_at_utc":"2026-09-05T01:00:01.000Z","completed_at_utc":"2026-09-05T01:00:10.000Z",
            "actual_provider_billed_cost_cny":None,"input_tokens":20,"output_tokens":21,"local_observed_usage_cost_cny":"0.000062",
            "local_observed_usage_cost_stop_cny":"1.000000","ledger_run_status":"completed","partial_artifacts":{},
            "network_attempts":2,"network_calls":2,"model_requests":2,"network_attempt_limit":2,"model_request_limit":2,
            "input_token_limit":1024,"output_token_limit":272,"provider_key_loaded":True,"raw_response_cleanup_complete":True,
            "usage_complete":True,"manifest_complete":True,"network_call_observation_complete":True})
        e={name:False for name in self.profile["field_sets"]["_SUCCESS_EVIDENCE_FIELDS"]}
        e.update(common|{"schema_version":"deepseek-first-live-evidence/1.0","status":"validated","runtime_plan":self.persisted,
            "runtime_denominator":self.denominator,"validation_integrity_gate_passed":True,"observed_states_in_order":["completed","incomplete_length"],
            "observed_signal_sources_in_order":["native_status","native_status"],"observed_completion_shapes_in_order":self.profile["expected_completion_shapes"],
            "observed_input_tokens":20,"observed_output_tokens":21,"local_observed_cost_cny":"0.000062"})
        request={"validation_id":common["contract_id"],"contract_commitment_sha256":common["contract_commitment_sha256"],
                 "implementation_commitment_sha256":common["implementation_commitment_sha256"],"authorization_id_sha256":linked["authorization_id_sha256"],
                 "scenario_count":2,"provider":"deepseek","model":"deepseek-v4-flash"}
        self.request_hash=hashlib.sha256(canonical_json_bytes(request)).hexdigest()
        event_values=[("run_started","system",{"mode":"deepseek_completion_first_live_validation","request_sha256":self.request_hash,"dataset_sha256":None})]
        for i in range(2):
            core={"schema_version":"provider-completion-ledger-event/1.1","case_id":self.case_id,"attempt_index":i,"case_attempt_index":i,"binding":self.binding}
            event_values.extend([("model_request_started","provider_adapter",core),
                ("provider_transport_request_sent","provider_adapter",{"schema_version":"deepseek-first-live-transport-send/1.0","case_id":self.case_id,"attempt_index":i,"case_attempt_index":i,
                    "network_call_index":i,"method":"POST","origin":"https://api.deepseek.com","path":"/responses"}),
                ("model_response_telemetry_recorded","provider_adapter",core|{"terminal_kind":"response_accepted","response_index":i,"error_code":None,"completion_record":self.records[i]})])
        event_values.append(("run_status_changed","system",{"from":"running","to":"completed","error_code":None}))
        self.events=[]
        for i,(kind,actor,payload) in enumerate(event_values):
            self.events.append({"event_id":i+1,"run_id":self.run_id,"sequence":i+1,"event_type":kind,"actor_kind":actor,
                                "occurred_at_utc":f"2026-09-05T01:00:{i+2:02}.000Z","safe_payload_json":canonical_json_bytes(payload).decode(),"prev_hash":"","event_hash":""})
        self.documents={"consumption.json":c,"terminal.json":t,"runtime_denominator.json":self.denominator,"completion_telemetry.json":e,
            "manifest.json":{"schema_version":"deepseek-first-live-manifest/1.0","status":"complete","contract_commitment_sha256":common["contract_commitment_sha256"],
                "implementation_commitment_sha256":common["implementation_commitment_sha256"],"files":{},"raw_response_body_persisted":False,"message_content_persisted":False,"api_key_persisted":False}}
        self.bundle={"schema_version":"provider-completion-first-live-evidence-bundle/1.0","document_type":"completion_telemetry_first_live_evidence_bundle",
            "subject":self.subject,"authorization_id_sha256":linked["authorization_id_sha256"],"external_authorization_binding_sha256":self.auth,
            "first_live_completed_at_utc":t["completed_at_utc"],"artifact_files":[],"first_live_design_commitment_sha256":common["contract_commitment_sha256"],
            "first_live_implementation_contract_sha256":h("raw implementation contract"),"complete_artifact_set_required":True,"raw_body_included":False,
            "online_authority_granted":False,"commitment_sha256":"0"*64}
        self.rechain()
        self.refresh()

    def rechain(self):
        previous="0"*64
        for event in self.events:
            event["prev_hash"]=previous
            event["event_hash"]=_event_hash(**{key:event[key] for key in ("run_id","sequence","event_type","occurred_at_utc","actor_kind","safe_payload_json","prev_hash")})
            previous=event["event_hash"]
        # Rebuild this owned TemporaryDirectory fixture as an adversary would;
        # never disable the production append-only triggers to alter rows.
        database=self.directory/"audit.sqlite3"
        if database.exists():
            database.unlink()
        AuditLedger(database)
        connection=sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("INSERT INTO runs(run_id,mode,status,request_sha256,dataset_sha256,created_at_utc,updated_at_utc,terminal_error_code) VALUES(?,?,?,?,?,?,?,?)",
                (self.run_id,"deepseek_completion_first_live_validation","completed",self.request_hash,None,self.events[0]["occurred_at_utc"],self.events[-1]["occurred_at_utc"],None))
            names=tuple(self.events[0])
            for event in self.events:
                connection.execute("INSERT INTO audit_events("+",".join(names)+") VALUES("+",".join("?" for _ in names)+")",tuple(event[name] for name in names))
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()
        bridge={"schema_version":"provider-completion-ledger-bridge-commitment/1.0","case_id":self.case_id,"binding_sha256":hashlib.sha256(canonical_json_bytes(self.binding)).hexdigest(),
                "started":[{"attempt_index":i,"event_hash":self.events[1+3*i]["event_hash"]} for i in range(2)],
                "terminals":[{"attempt_index":i,"event_hash":self.events[3+3*i]["event_hash"]} for i in range(2)],"all_started_attempts_terminal":True,"write_failed":False}
        bridge["commitment_sha256"]=hashlib.sha256(canonical_json_bytes(bridge)).hexdigest()
        self.documents["audit_index.json"]={"runs":[{"case_id":self.case_id,"run_id":self.run_id,
            "chain_verification":verify_audit_chain_rows(self.run_id,self.events).to_dict(),"completion_telemetry_event_commitment":bridge}]}
        self.documents["completion_telemetry.json"]["ledger_reconciliation"]={"all_chains_valid":True,"ledger_export_failed":False,"ledger_failure_observed":False,
            "event_counts":dict(sorted(Counter(event["event_type"] for event in self.events).items())),"reasons":[]}

    def refresh(self):
        for name,value in self.documents.items():
            if name not in {"manifest.json","terminal.json"}:
                (self.directory/name).write_bytes(raw(value))
        files={name:(self.directory/name).read_bytes() for name in self.contract.document()["evidence_file_order"] if name not in {"manifest.json","terminal.json"}}
        self.documents["manifest.json"]["files"]={name:{"bytes":len(value),"sha256":hashlib.sha256(value).hexdigest()} for name,value in files.items()}
        (self.directory/"manifest.json").write_bytes(raw(self.documents["manifest.json"]))
        t=self.documents["terminal.json"]
        t["manifest_sha256"]=hashlib.sha256((self.directory/"manifest.json").read_bytes()).hexdigest()
        t["consumption_receipt_sha256"]=hashlib.sha256(files["consumption.json"]).hexdigest()
        (self.directory/"terminal.json").write_bytes(raw(t))
        self.bundle["artifact_files"]=[]
        for name in self.contract.document()["evidence_file_order"]:
            value=(self.directory/name).read_bytes()
            self.bundle["artifact_files"].append({"name":name,"bytes":len(value),"sha256":hashlib.sha256(value).hexdigest()})
        self.bundle["commitment_sha256"]=admission_bundle_commitment(self.contract,self.bundle)

    def arguments(self):
        return {"artifact_directory":self.directory,"bundle_bytes":raw(self.bundle),"expected_evidence_commitment_sha256":self.bundle["commitment_sha256"],
                "expected_authorization_binding_sha256":self.auth,"expected_subject":self.subject}
