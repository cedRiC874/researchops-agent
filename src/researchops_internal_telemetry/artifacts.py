"""Exclusive Internal archive writer and locked workspace; no external claims."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

from researchops_completion_timing.local_claim import _locked_chain
from researchops_completion_timing.first_live_workspace import _new_database
from researchops_completion_timing.first_live_publish import _write_exclusive
from researchops_external_closure.io import scan_public_artifact_bytes
from .contract import ROOT, commitment, decode, digest, now, raw, read, require, utc

PAYLOADS = ("protocol.json", "internal_freeze.json", "internal_authorization.json", "local_claim_intent.json",
    "local_claim_receipt.json", "cases.json", "audit.sqlite3", "run_index.json", "completion_telemetry.json", "timing_evidence.json")
FILES = PAYLOADS + ("manifest.json", "publication.json")


@contextmanager
def owned_paths(owner):
    candidate = decode(owner.authorization_bytes)["candidate"]
    identifier = digest(candidate["authorization_id"].encode())
    output, parent = ROOT / "output", ROOT / "output/internal-telemetry-v1"
    work, archive, bundle = parent / (identifier + ".work"), parent / identifier, parent / (identifier + ".bundle.json")
    with _locked_chain(ROOT):
        output.mkdir(exist_ok=True)
        with _locked_chain(output):
            parent.mkdir(exist_ok=True)
            with _locked_chain(parent):
                require(not any(os.path.lexists(path) for path in (work, archive, bundle)), "output_exists")
                work.mkdir()
                with _locked_chain(work), _new_database(work / "audit.sqlite3") as database:
                    yield database, archive, bundle


def seal(factory, database, archive, bundle, *, run_status):
    start = time.monotonic_ns()
    expires = utc(decode(factory.owner.authorization_bytes)["candidate"]["expires_at_utc"])
    def checkpoint():
        require(0 <= time.monotonic_ns() - start <= 120_000_000_000 and now() < expires, "sealing_timeout")
    checkpoint()
    projection = factory.projections()
    index = dict(schema_version="provider-completion-internal-run-index/1.0", scope="internal_known_synthetic",
        run_id=factory.run_id, status=run_status, planned_case_count=30, case_pass_count=factory.passed,
        case_states=[dict(case_id=case["case_id"], case_index=i,
            status="passed" if i < factory.passed else "failed_or_unknown" if i < factory.model_requests else "not_started")
            for i, case in enumerate(factory.tasks["cases"])],
        model_requests=factory.model_requests, network_attempts=factory.network_attempts, transport_mode=factory.transport_mode,
        response_count=sum(item["response_index"] is not None for item in factory.terminals), budget=factory.budget.summary(),
        binding=factory.binding.runtime_snapshot(), denominator_plan=factory.plan,
        freeze_commitment_sha256=commitment("freeze", factory.freeze),
        source_commitment_sha256=factory.freeze["source_commitment_sha256"],
        external_validation_completed=False, status_closure_allowed=False, provider_bill=None)
    intent = dict(schema_version="provider-completion-internal-claim-intent/1.0", scope="internal_known_synthetic",
        authorization_sha256=digest(factory.owner.authorization_bytes), freeze_commitment_sha256=commitment("freeze", factory.freeze),
        clock_domain_id=factory.owner.clock_domain)
    payloads = {"protocol.json": raw(factory.freeze["protocol"]), "internal_freeze.json": factory.owner.freeze_bytes,
        "internal_authorization.json": factory.owner.authorization_bytes, "local_claim_intent.json": raw(intent),
        "local_claim_receipt.json": factory.owner.receipt_bytes, "cases.json": factory.task_bytes,
        "audit.sqlite3": read(database, 33554432), "run_index.json": raw(index),
        "completion_telemetry.json": raw(projection), "timing_evidence.json": raw(factory.owner.clock.snapshot())}
    require(set(payloads) == set(PAYLOADS), "payload_set")
    maxima = {"protocol.json":262144,"internal_freeze.json":262144,"internal_authorization.json":16384,
        "local_claim_intent.json":4096,"local_claim_receipt.json":4096,"cases.json":262144,
        "audit.sqlite3":33554432,"run_index.json":65536,"completion_telemetry.json":1048576,"timing_evidence.json":1048576}
    for name, payload in payloads.items():
        require(len(payload) <= maxima[name], "artifact_size")
        if name == "audit.sqlite3":
            scan_public_artifact_bytes((payload,))
        else:
            decode(payload, maxima[name])
    manifest = dict(schema_version="provider-completion-internal-manifest/1.0",
        files=[dict(path=name, bytes=len(payloads[name]), sha256=digest(payloads[name])) for name in PAYLOADS])
    payloads["manifest.json"] = raw(manifest)
    with _locked_chain(archive.parent):
        archive.mkdir(exist_ok=False)
        with _locked_chain(archive):
            for name in PAYLOADS + ("manifest.json",):
                checkpoint()
                _write_exclusive(archive / name, payloads[name], [])
            checkpoint()
            publication = dict(schema_version="provider-completion-internal-publication/1.0",
                manifest_sha256=digest(payloads["manifest.json"]), manifest_commitment_sha256=commitment("manifest", manifest),
                observation_scope="before_publication_write_not_process_exit", sealing_elapsed_ns=time.monotonic_ns()-start,
                status=run_status, external_validation_completed=False, status_closure_allowed=False,
                internal_acceptance_pending_independent_readback=True)
            payloads["publication.json"] = raw(publication)
            _write_exclusive(archive / "publication.json", payloads["publication.json"], [])
            checkpoint()
        body = dict(schema_version="provider-completion-internal-bundle/1.0",
            files=[dict(path=name, bytes=len(payloads[name]), sha256=digest(payloads[name])) for name in FILES])
        outer = dict(body, commitment_sha256=commitment("bundle", body))
        require(sum(map(len, payloads.values())) <= 67108864, "archive_size")
        checkpoint()
        _write_exclusive(bundle, raw(outer), [])
        checkpoint()
    return outer["commitment_sha256"], start
