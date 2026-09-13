"""Read-only success evidence validation, separate from execution/review/admission.

Only the seven declared first-live artifacts are read. SQLite is opened through
immutable read-only connections whose serialized image must equal the scanned
bytes. No live writer, first-live runner, Adapter or runtime factory is called.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from researchops_completion_telemetry.persisted_evidence import create_persisted_live_evidence_validation_context
from researchops_completion_telemetry.surface_mapping import load_and_select_surface_mapping

from .admission_bundle_bytes import validate_first_live_bundle_envelope, verify_first_live_bundle_bytes
from .admission_contract import load_admission_link_contract
from .artifact_inputs import _read_database_rows
from .artifacts import _verify_audit_database
from .errors import ExternalClosurePrimitiveError
from .first_live_success_semantics import PROFILE_PATH, expected_first_live_plan, profile_document, verify_success_semantics
from .io import decode_canonical_json_file, read_exact_artifact_directory, read_regular_file_no_follow, scan_public_artifact_bytes
from .primitives import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class FirstLiveArtifactSemanticsResult:
    evidence_commitment_sha256: str
    manifest_sha256: str
    terminal_sha256: str
    chain_head: str
    recorded_response_count: int
    recorded_network_attempt_count: int
    recorded_input_tokens: int
    recorded_output_tokens: int
    recorded_cost_cny: str
    source_plan_id: str
    artifact_structure_and_semantics_verified: bool = True
    execution_identity_verified: bool = False
    source_equivalence_verified: bool = False
    review_verified: bool = False
    registry_admission_verified: bool = False
    runtime_authority_granted: bool = False
    closure_claim_allowed: bool = False
    verification_provider_calls: int = 0
    verification_key_loads: int = 0


def verify_first_live_success_bundle(
    project_root: Path, *, artifact_directory: Path, bundle_bytes: bytes,
    expected_evidence_commitment_sha256: str, expected_authorization_binding_sha256: str,
    expected_subject: dict,
) -> FirstLiveArtifactSemanticsResult:
    if not isinstance(project_root,Path) or not isinstance(artifact_directory,Path):
        raise ExternalClosurePrimitiveError("admission_first_live_path_invalid")
    try:
        contract=load_admission_link_contract(project_root)
        profile_bytes=read_regular_file_no_follow(project_root/PROFILE_PATH,max_bytes=8192)
        profile=profile_document(profile_bytes)
        validate_first_live_bundle_envelope(contract,bundle_bytes,
            expected_commitment_sha256=expected_evidence_commitment_sha256,
            expected_authorization_binding_sha256=expected_authorization_binding_sha256,
            expected_subject=expected_subject)
        names=tuple(contract.document()["evidence_file_order"])
        limit=contract.document()["limits"]["artifact_total_bytes"]
        files=read_exact_artifact_directory(artifact_directory,expected_names=names,max_file_bytes=limit,max_total_bytes=limit)
        bundle=verify_first_live_bundle_bytes(contract,bundle_bytes,files,expected_commitment_sha256=expected_evidence_commitment_sha256,
                                              expected_authorization_binding_sha256=expected_authorization_binding_sha256,
                                              expected_subject=expected_subject)
        documents={name:decode_canonical_json_file(raw,max_bytes=2_097_152) for name,raw in files.items() if name.endswith(".json")}
        scan_public_artifact_bytes(tuple(canonical_json_bytes(value) for value in documents.values()))
        manifest=documents["manifest.json"]; terminal=documents["terminal.json"]
        committed={name:{"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()} for name,raw in files.items() if name not in {"manifest.json","terminal.json"}}
        if canonical_json_bytes(manifest.get("files"))!=canonical_json_bytes(committed):
            raise ExternalClosurePrimitiveError("admission_first_live_manifest_mismatch")
        manifest_hash=hashlib.sha256(files["manifest.json"]).hexdigest()
        if terminal.get("manifest_sha256")!=manifest_hash or terminal.get("consumption_receipt_sha256")!=hashlib.sha256(files["consumption.json"]).hexdigest():
            raise ExternalClosurePrimitiveError("admission_first_live_receipt_hash_mismatch")
        selection=load_and_select_surface_mapping(project_root,"deepseek","responses","openai_compatible_responses",purpose="offline_validation")
        binding={name:getattr(selection,name) for name in ("telemetry_schema_sha256","adapter_version","mapping_schema_version","mapping_version",
                 "mapping_sha256","provider_id","api_surface","transport_id","output_counter_comparability","output_counter_path")}
        if binding["mapping_sha256"]!=bundle["subject"]["selected_mapping_sha256"] or binding["adapter_version"]!=bundle["subject"]["adapter_version"]:
            raise ExternalClosurePrimitiveError("admission_first_live_mapping_binding_mismatch")
        plan,persisted=expected_first_live_plan(profile,binding)
        context=create_persisted_live_evidence_validation_context(selection,expected_binding=binding,expected_denominator_plan=plan,
                     expected_denominator_plan_commitment_sha256=persisted["preregistration_commitment"])
        _verify_audit_database(artifact_directory/"audit.sqlite3",expected_payload=files["audit.sqlite3"])
        runs,events,model_calls,*tool_counts=_read_database_rows(artifact_directory/"audit.sqlite3",expected_payload=files["audit.sqlite3"])
        second=read_exact_artifact_directory(artifact_directory,expected_names=names,max_file_bytes=limit,max_total_bytes=limit)
        if files!=second:
            raise ExternalClosurePrimitiveError("admission_first_live_snapshot_changed")
        result=verify_success_semantics(profile_bytes,bundle=bundle,documents=documents,binding=binding,context=context,
                  run_rows=runs,event_rows=events,model_call_rows=model_calls,tool_counts=tuple(tool_counts))
        return FirstLiveArtifactSemanticsResult(expected_evidence_commitment_sha256,manifest_hash,
               hashlib.sha256(files["terminal.json"]).hexdigest(),result["chain_head"],result["accepted_response_count"],
               result["network_attempt_count"],result["input_tokens"],result["output_tokens"],result["local_observed_cost_cny"],result["source_plan_id"])
    except ExternalClosurePrimitiveError:
        raise
    except Exception:
        raise ExternalClosurePrimitiveError("admission_first_live_evidence_invalid") from None


__all__ = ["FirstLiveArtifactSemanticsResult","verify_first_live_success_bundle"]
