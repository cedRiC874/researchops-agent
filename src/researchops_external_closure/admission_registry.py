"""Strict v3 registry carrier checks; documents alone never grant authority.

The immutable v2 mapping projection is not edited or reinterpreted. This v3
document is a separate evidence-link carrier; full composed verification is
required before it can become a runtime eligibility result.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import jsonschema

from .admission_bundle_bytes import admission_bundle_commitment
from .admission_contract import CONTRACT_SHA256, FrozenAdmissionLinkContract
from .errors import ExternalClosurePrimitiveError
from .io import read_regular_file_no_follow, scan_public_artifact_bytes
from .primitives import canonical_json_bytes, decode_strict_json_object
from .schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, only_local_schema_references


PROTOCOL_PATH="evals/provider_completion_runtime_registry_v3/protocol_v3.json"
PROTOCOL_SHA256="5866dfd68722b8579a34cea3f39bfac7f817813a91b584c3a0ea677583384905"
_SCHEMA_SHA256="c2112ab4cfbcc83ad9141018a4d52213da9ccc19bf5dcaf5ad662acd559b7162"


def _fail(code):
    raise ExternalClosurePrimitiveError("admission_registry_"+code) from None


def validate_registry_carrier(
    project_root: Path,contract: FrozenAdmissionLinkContract,*,registry_bytes: bytes,admission_bundle_bytes: bytes,
    expected_registry_commitment_sha256: str,expected_subject: dict,
    expected_first_live_evidence_commitment_sha256: str,expected_review_document_sha256: str,
    expected_reviewed_evidence_commit: str,expected_reviewed_evidence_tree: str,
) -> tuple[dict,dict]:
    raw=read_regular_file_no_follow(project_root/PROTOCOL_PATH,max_bytes=8192)
    if len(raw)!=1753 or hashlib.sha256(raw).hexdigest()!=PROTOCOL_SHA256:
        _fail("protocol_invalid")
    protocol=decode_strict_json_object(raw,max_bytes=8192)
    schema_raw=read_regular_file_no_follow(project_root/protocol["schema"]["path"],max_bytes=16384)
    if len(schema_raw)!=6706 or hashlib.sha256(schema_raw).hexdigest()!=_SCHEMA_SHA256:
        _fail("schema_invalid")
    schema=decode_strict_json_object(schema_raw,max_bytes=16384)
    registry=decode_strict_json_object(registry_bytes,max_bytes=32768)
    if not only_local_schema_references(schema):
        _fail("schema_invalid")
    try:
        jsonschema.Draft202012Validator(schema,registry=LOCAL_ONLY_SCHEMA_REGISTRY).validate(registry)
    except Exception:
        _fail("document_invalid")
    bundle=contract.validate("runtime_registry_admission_bundle_v1.schema.json",admission_bundle_bytes,max_bytes=32768)
    scan_public_artifact_bytes((registry_bytes,admission_bundle_bytes,canonical_json_bytes(registry),canonical_json_bytes(bundle)))
    if (type(expected_registry_commitment_sha256) is not str
        or admission_bundle_commitment(contract,bundle)!=expected_registry_commitment_sha256
        or bundle["commitment_sha256"]!=expected_registry_commitment_sha256
        or bundle["registry_document_sha256"]!=hashlib.sha256(registry_bytes).hexdigest()
        or bundle["admission_contract_sha256"]!=CONTRACT_SHA256
        or type(expected_subject) is not dict or canonical_json_bytes(bundle["subject"])!=canonical_json_bytes(expected_subject)):
        _fail("binding_mismatch")
    expected={"first_live_evidence_commitment_sha256":expected_first_live_evidence_commitment_sha256,
              "review_document_sha256":expected_review_document_sha256,"reviewed_evidence_commit":expected_reviewed_evidence_commit,
              "reviewed_evidence_tree":expected_reviewed_evidence_tree,"campaign_selected_mapping_sha256":expected_subject["selected_mapping_sha256"]}
    if any(type(value) is not str or bundle[name]!=value for name,value in expected.items()):
        _fail("binding_mismatch")
    entry=registry["entries"][0]
    for name,value in {"selected_mapping_sha256":expected_subject["selected_mapping_sha256"],
                       "first_live_evidence_commitment_sha256":expected_first_live_evidence_commitment_sha256,
                       "review_document_sha256":expected_review_document_sha256}.items():
        if entry[name]!=value:
            _fail("entry_binding_mismatch")
    return registry,bundle


__all__=["PROTOCOL_PATH","PROTOCOL_SHA256","validate_registry_carrier"]
