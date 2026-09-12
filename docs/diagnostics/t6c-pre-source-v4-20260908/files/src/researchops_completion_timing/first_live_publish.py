"""Create-once completed first-live archive, measured by its owned phase clock."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from researchops.audit import verify_audit_chain_rows
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.artifacts import _verify_audit_database
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from . import first_live, first_live_artifacts as archive, first_live_audit as audit, first_live_control as control
from .artifacts import load_artifact_contract
from .contract import CONTRACT_SHA256, digest
from .first_live_runtime import ROOT, _FirstLiveModelFactory
from .local_claim import _locked_chain


class FirstLivePublishError(ValueError):
    def __init__(self, code, *, directory_created, created_files):
        self.code = code
        self.directory_created = directory_created
        self.created_files = tuple(created_files)
        self.retry_authorized = False
        super().__init__(code)


def _write_exclusive(path, payload, created):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        created.append(path.name)  # Track even an empty/partial failed write.
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("first_live_publish_file_invalid")
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if type(count) is not int or not 0 < count <= len(payload) - offset:
                raise ValueError("first_live_publish_write_unknown")
            offset += count
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_nlink, after.st_size) != (before.st_dev, before.st_ino, 1, len(payload)):
            raise ValueError("first_live_publish_file_changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if read_regular_file_no_follow(path, max_bytes=max(1, len(payload))) != payload:
        raise ValueError("first_live_publish_file_changed")


def _publish_completed_first_live(factory, directory, *, authorization_bytes, sensitive_canaries=()):
    """Return the external bundle only after complete write/readback validation.

    Failed/partial output stays in place and is never a retry or success grant.
    The publication receipt cannot measure its own final write; outer checks
    still enforce the sealing and whole/auth deadlines before returning.
    """
    made = False; created = []
    try:
        if type(factory) is not _FirstLiveModelFactory or not isinstance(directory, Path):
            raise ValueError("first_live_publish_input_invalid")
        if not directory.is_absolute() or ".." in directory.parts or (os.name == "nt" and (len(directory.drive) != 2 or directory.drive[1] != ":")):
            raise ValueError("first_live_publish_destination_invalid")
        prepared = factory._prepared
        prepared._assert_process()
        state = prepared.clock.snapshot()
        if not factory._phase_finished or factory._failed or not state["closed"] or state["halted"] or len(state["attempts"]) != 2:
            raise ValueError("first_live_publish_phase_incomplete")
        prepared._budget.artifact_checkpoint(prepared._expires)
        profile, _ = archive.load_first_live_artifact_contract(ROOT)
        started = prepared.clock._sealing_offset()
        canaries = tuple(sensitive_canaries)
        def checkpoint():
            stamp, _deadline = prepared._budget.artifact_checkpoint(prepared._expires)
            observed = prepared.clock._sealing_offset()
            if observed - started > 30_000_000_000:
                raise ValueError("first_live_publish_timeout")
            return stamp, observed
        checkpoint()
        database = factory._ledger.database_path
        database_bytes = audit._database_snapshot(database, profile["max_file_bytes"])
        scan_public_artifact_bytes((database_bytes, authorization_bytes, prepared.intent_bytes, prepared.receipt_bytes, prepared.plan_bytes), sensitive_canaries=canaries)
        parent, _ = load_artifact_contract(ROOT)
        if _verify_audit_database(database, expected_payload=database_bytes).schema_commitment_sha256 != parent["expected_database_schema_sha256"]:
            raise ValueError("first_live_publish_audit_invalid")
        runs, rows, *_counts = _read_database_rows(database, expected_payload=database_bytes)
        chain = verify_audit_chain_rows(factory._run, rows)
        if not chain.valid or len(runs) != 1:
            raise ValueError("first_live_publish_audit_invalid")
        heads = [{"run_id": factory._run, "final_chain_head_sha256": chain.chain_head}]
        heads_hash = digest(parent["ordered_audit_heads_domain"].encode() + b"\0" + parent["ordered_audit_heads_tag"].encode() + b"\0" + raw(heads))
        segments = [decode_strict_json_object(value, max_bytes=65536) for session in factory._sessions for value in session.segment_bytes()]
        if len(segments) != 2 or any(any(segment.get(name) != value for name, value in attempt.items()) for segment, attempt in zip(segments, state["attempts"])):
            raise ValueError("first_live_publish_clock_mismatch")
        plan = decode_strict_json_object(prepared.plan_bytes, max_bytes=131072)
        # Authorization binds the semantic timing-plan commitment. Publish its
        # canonical bytes even when the approved input used harmless whitespace.
        plan_bytes = raw(plan)
        intent = decode_strict_json_object(prepared.intent_bytes, max_bytes=4096)
        binding = dict(plan["binding"], authorization_binding_sha256=intent["authorization_binding_sha256"], local_claim_receipt_sha256=digest(prepared.receipt_bytes))
        evidence = dict(schema_version="provider-completion-first-live-timing-evidence/1.0", document_type="completion_first_live_timing_evidence",
            timing_contract_sha256=CONTRACT_SHA256, first_live_profile_sha256=first_live.PROFILE_SHA256,
            plan_commitment_sha256=plan["plan_commitment_sha256"], binding=binding,
            execution_binding_sha256=first_live.commitment("execution_binding", binding), clock_domain_id=prepared.clock_domain_id,
            clock_source="time.monotonic_ns", clock_unit="relative_integer_nanoseconds", origin_offset_ns=0,
            record_provenance="live_monotonic_capture", segments=segments, phase=state["phase"], audit_chain_heads_sha256=heads_hash)
        evidence["evidence_commitment_sha256"] = first_live.commitment("evidence", evidence)
        evidence_bytes = raw(evidence)
        records, events, _head = audit._extract(ROOT, database, expected_database_sha256=digest(database_bytes),
            plan_bytes=plan_bytes, evidence_bytes=evidence_bytes, sensitive_canaries=canaries)
        checked_at, _ = checkpoint()
        check = control.verify_first_live_controlled_timing(ROOT, plan_bytes=plan_bytes, authorization_bytes=authorization_bytes,
            expected_authorization_binding_sha256=intent["authorization_binding_sha256"], verification_time_utc=checked_at,
            intent_bytes=prepared.intent_bytes, claim_receipt_bytes=prepared.receipt_bytes, expected_claim_receipt_sha256=digest(prepared.receipt_bytes),
            evidence_bytes=evidence_bytes, record_bytes=records, terminal_projection_bytes=events, sensitive_canaries=canaries)
        if check["incomplete_reasons"] != ["publication_unverified"]:
            raise ValueError("first_live_publish_evidence_incomplete")
        payloads = {"audit.sqlite3": database_bytes, "authorization.json": authorization_bytes, "claim_intent.json": prepared.intent_bytes,
            "claim_receipt.json": prepared.receipt_bytes, "completion_timing_plan.json": plan_bytes,
            "completion_timing_evidence.json": evidence_bytes}
        scan_public_artifact_bytes(tuple(payloads.values()), sensitive_canaries=canaries)
        if sum(map(len, payloads.values())) > profile["max_total_bytes"]:
            raise ValueError("first_live_publish_byte_limit")
        common = dict(artifact_contract_sha256=archive.PROFILE_SHA256, authorization_binding_sha256=intent["authorization_binding_sha256"],
            plan_commitment_sha256=plan["plan_commitment_sha256"], execution_binding_sha256=evidence["execution_binding_sha256"])
        entries = {name: {"bytes": len(value), "sha256": digest(value)} for name, value in payloads.items()}
        manifest = dict(schema_version="provider-completion-first-live-manifest/1.0", status="sealed_payloads", **common, files=entries)
        payloads["manifest.json"] = raw(manifest)
        if sum(map(len, payloads.values())) > profile["max_total_bytes"]:
            raise ValueError("first_live_publish_byte_limit")
        with _locked_chain(directory.parent):
            directory.mkdir(exist_ok=False); made = True
            with _locked_chain(directory):
                for name, value in payloads.items():
                    checkpoint(); _write_exclusive(directory / name, value, created)
                _stamp, finished = checkpoint()
                publication = dict(schema_version="provider-completion-first-live-timing-publication/1.0", document_type="completion_first_live_timing_publication",
                    timing_contract_sha256=CONTRACT_SHA256, first_live_profile_sha256=first_live.PROFILE_SHA256,
                    plan_commitment_sha256=common["plan_commitment_sha256"], execution_binding_sha256=common["execution_binding_sha256"],
                    clock_domain_id=prepared.clock_domain_id, timing_evidence_file_sha256=digest(evidence_bytes),
                    bundle_manifest_file_sha256=digest(payloads["manifest.json"]), sealing_started_ns=started, sealing_finished_ns=finished,
                    sealing_status="sealed", sealing_scope="bounded_artifact_set_before_publication_receipt", online_execution_authorized=False)
                publication["publication_commitment_sha256"] = first_live.commitment("publication", publication)
                payloads["completion_timing_publication.json"] = raw(publication)
                if sum(map(len, payloads.values())) > profile["max_total_bytes"]:
                    raise ValueError("first_live_publish_byte_limit")
                _write_exclusive(directory / "completion_timing_publication.json", raw(publication), created)
                entries = {name: {"bytes": len(value), "sha256": digest(value)} for name, value in payloads.items()}
                bundle = dict(schema_version="provider-completion-first-live-bundle/1.0", **common, files=entries)
                bundle["bundle_commitment_sha256"] = archive.bundle_commitment(bundle)
                checked_at, _ = checkpoint()
                result = archive.verify_first_live_artifact_bundle(ROOT, directory, bundle_bytes=raw(bundle),
                    expected_bundle_commitment_sha256=bundle["bundle_commitment_sha256"], expected_authorization_binding_sha256=common["authorization_binding_sha256"],
                    verification_time_utc=checked_at, sensitive_canaries=canaries)
                if not result["timing_gate_complete"] or audit._database_snapshot(database, profile["max_file_bytes"]) != database_bytes:
                    raise ValueError("first_live_publish_final_mismatch")
                checkpoint()
        checkpoint()
        return {"status": "first_live_archive_written_and_verified", "bundle_bytes": raw(bundle), "created_files": tuple(created),
            "sealing_started_ns": started,
            "publication_self_write_measured": False, "runtime_authority_granted": False, "first_live_success_verified": False}
    except BaseException as error:
        if type(factory) is _FirstLiveModelFactory:
            factory._failed = True; factory._prepared.abort()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        code = "first_live_publish_failed"
        if isinstance(error, FileExistsError): code = "first_live_publish_destination_exists"
        elif type(error) is ValueError and str(error).startswith("first_live_publish_"): code = str(error)
        raise FirstLivePublishError(code, directory_created=made, created_files=created) from None
