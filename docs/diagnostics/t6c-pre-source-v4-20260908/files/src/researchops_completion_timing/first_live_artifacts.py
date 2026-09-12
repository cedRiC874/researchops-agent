"""Eight-file first-live archive verification; no local execution or admission."""
from __future__ import annotations

from pathlib import Path

from researchops_external_closure.io import read_regular_file_no_follow, read_exact_artifact_directory, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from . import contract as core
from . import first_live_audit as audit
from . import first_live_control as control
from .artifacts import _json_file, _sha


PROFILE_SHA256 = "4e50cbfa2a92473f3ed56763fd4895dc5a2e1cf4871e31e364e64b11f0bda265"
_DIRECTORY = "evals/provider_completion_first_live_artifact_v1"
_DOMAIN = "researchops-provider-completion-first-live-artifact-bundle-v1"


def load_first_live_artifact_contract(root):
    payload = read_regular_file_no_follow(root / _DIRECTORY / "contract_v1.json", max_bytes=4096)
    if len(payload) != 3188 or core.digest(payload) != PROFILE_SHA256:
        core.fail("first_live_artifact_contract_invalid")
    profile = decode_strict_json_object(payload, max_bytes=4096)
    if (profile["first_live_audit_profile_sha256"] != audit.PROFILE_SHA256
        or profile["first_live_control_profile_sha256"] != control.PROFILE_SHA256
        or profile["bundle_domain"] != _DOMAIN):
        core.fail("first_live_artifact_contract_invalid")
    audit.load_first_live_audit_contract(root)
    control.load_control_contract(root)
    schemas = {}
    for item in profile["schemas"]:
        payload = read_regular_file_no_follow(root / _DIRECTORY / item["name"], max_bytes=8192)
        if len(payload) != item["bytes"] or core.digest(payload) != item["sha256"]:
            core.fail("first_live_artifact_contract_invalid")
        schemas[item["name"]] = decode_strict_json_object(payload, max_bytes=8192)
    return profile, schemas


def bundle_commitment(document):
    if type(document) is not dict:
        core.fail("first_live_bundle_invalid")
    body = dict(document); body.pop("bundle_commitment_sha256", None)
    return core.digest(_DOMAIN.encode() + b"\0" + raw(body))


def verify_first_live_artifact_bundle(root, directory, *, bundle_bytes,
        expected_bundle_commitment_sha256, expected_authorization_binding_sha256,
        verification_time_utc, sensitive_canaries=()):
    """Portable archive verification. Never inspect/reserve a local claim store.

    Independent digests must arrive through the external approval/review path;
    hashing caller-created data does not provide consent or a review signature.
    """
    try:
        if type(root) is not type(Path()) or type(directory) is not type(Path()):
            core.fail("first_live_artifact_path_invalid")
        _sha(expected_bundle_commitment_sha256); _sha(expected_authorization_binding_sha256)
        canaries = tuple(sensitive_canaries)
        profile, schemas = load_first_live_artifact_contract(root)
        bundle = _json_file(bundle_bytes, profile["max_envelope_bytes"], canaries, schemas["bundle_v1.schema.json"])
        if (bundle["artifact_contract_sha256"] != PROFILE_SHA256
            or bundle["bundle_commitment_sha256"] != bundle_commitment(bundle)
            or bundle["bundle_commitment_sha256"] != expected_bundle_commitment_sha256
            or bundle["authorization_binding_sha256"] != expected_authorization_binding_sha256):
            core.fail("first_live_bundle_commitment_mismatch")

        def snapshot():
            return read_exact_artifact_directory(directory, expected_names=tuple(profile["artifact_files"]),
                max_file_bytes=profile["max_file_bytes"], max_total_bytes=profile["max_total_bytes"])

        files = snapshot()
        scan_public_artifact_bytes(tuple(files.values()), sensitive_canaries=canaries)
        entries = {name: {"bytes": len(payload), "sha256": core.digest(payload)} for name, payload in files.items()}
        if raw(bundle["files"]) != raw(entries):
            core.fail("first_live_bundle_file_mismatch")
        # Check every JSON file canonically, not only the manifest. The underlying
        # control/timing validators apply their smaller field-specific limits.
        documents = {name: _json_file(payload, 4194304, canaries) for name, payload in files.items() if name.endswith(".json")}
        manifest = _json_file(files[profile["manifest_file"]], profile["max_envelope_bytes"], canaries, schemas["manifest_v1.schema.json"])
        if raw(manifest["files"]) != raw({name: entries[name] for name in profile["payload_files"]}):
            core.fail("first_live_manifest_payload_mismatch")
        for name in ("artifact_contract_sha256", "authorization_binding_sha256", "plan_commitment_sha256", "execution_binding_sha256"):
            if manifest[name] != bundle[name]:
                core.fail("first_live_manifest_binding_mismatch")
        plan, evidence = documents["completion_timing_plan.json"], documents["completion_timing_evidence.json"]
        if (plan["plan_commitment_sha256"] != bundle["plan_commitment_sha256"]
            or evidence["execution_binding_sha256"] != bundle["execution_binding_sha256"]):
            core.fail("first_live_manifest_binding_mismatch")
        records, events, head = audit._extract(root, directory / "audit.sqlite3",
            expected_database_sha256=entries["audit.sqlite3"]["sha256"], plan_bytes=files["completion_timing_plan.json"],
            evidence_bytes=files["completion_timing_evidence.json"], sensitive_canaries=canaries)
        checked = control.verify_first_live_controlled_timing(root, plan_bytes=files["completion_timing_plan.json"],
            authorization_bytes=files["authorization.json"], expected_authorization_binding_sha256=expected_authorization_binding_sha256,
            verification_time_utc=verification_time_utc, intent_bytes=files["claim_intent.json"],
            claim_receipt_bytes=files["claim_receipt.json"], expected_claim_receipt_sha256=entries["claim_receipt.json"]["sha256"],
            evidence_bytes=files["completion_timing_evidence.json"], record_bytes=records, terminal_projection_bytes=events,
            publication_bytes=files[profile["publication_file"]], expected_manifest_file_sha256=entries[profile["manifest_file"]]["sha256"],
            sensitive_canaries=canaries)
        if snapshot() != files:
            core.fail("first_live_artifact_snapshot_changed")
        return dict(checked, status="first_live_archive_verified", scope="archive_bytes_and_actual_audit_linkage_only",
            file_count=len(files), bundle_commitment_sha256=expected_bundle_commitment_sha256,
            manifest_sha256=entries[profile["manifest_file"]]["sha256"],
            publication_sha256=entries[profile["publication_file"]]["sha256"], audit_chain_head_sha256=head,
            authoritative_ledger_verified=True, local_store_entry_independently_verified=False,
            review_signature_verified=False, external_review_digest_verified=False,
            first_live_campaign_source_equivalence_verified=False)
    except core.TimingContractError:
        raise
    except Exception as error:
        if getattr(error, "code", None) == "external_closure_sensitive_content_detected":
            core.fail("timing_sensitive_content")
        core.fail("first_live_artifact_invalid")
