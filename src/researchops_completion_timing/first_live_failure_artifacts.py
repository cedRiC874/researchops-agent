"""Bounded local failure receipts linked to optional verified database bytes."""
from __future__ import annotations

from researchops.audit import sha256_json, verify_audit_chain_rows
from researchops_external_closure.artifact_inputs import _read_database_rows
from researchops_external_closure.artifacts import _verify_audit_database
from researchops_external_closure.io import read_regular_file_no_follow, scan_public_artifact_bytes
from researchops_external_closure.primitives import canonical_json_bytes as raw, decode_strict_json_object
from .artifacts import load_artifact_contract
from .contract import _decode, digest, fail
from .first_live_audit import _database_snapshot
from .first_live_failure import validate_failure_receipt
from .first_live_publish import _write_exclusive
from .first_live_runtime import _FirstLiveModelFactory, _FirstLiveModelFactoryV5
from .first_live_start import _StartBudget
from .local_claim import _locked_chain

SCHEMA_PATH = 'evals/provider_completion_first_live_failure_artifact_v1/manifest_v1.schema.json'
SCHEMA_SHA256 = '554d9e48acebe76a5193c1b5030b659778040d733fac1d474eadaeba55cb26b0'
DOMAIN = b'researchops-provider-completion-first-live-failure-manifest-v1\0'
MAX_DATABASE_BYTES = 8_388_608
_BINDING = ('authorization_id_sha256', 'validation_run_id', 'plan_commitment_sha256', 'execution_binding_sha256')


def load_failure_artifact_schema(root):
    payload = read_regular_file_no_follow(root / SCHEMA_PATH, max_bytes=8192)
    if digest(payload) != SCHEMA_SHA256:
        fail('first_live_failure_artifact_schema_invalid')
    return decode_strict_json_object(payload, max_bytes=8192)


def manifest_commitment(value):
    body = dict(value); body.pop('manifest_commitment_sha256', None)
    return digest(DOMAIN + raw(body))


def _validate_manifest(root, payload):
    value = _decode(payload, load_failure_artifact_schema(root), 8192, ())
    if raw(value) != payload or manifest_commitment(value) != value['manifest_commitment_sha256']:
        fail('first_live_failure_artifact_commitment_mismatch')
    snapshot = value['audit_snapshot']
    missing = [snapshot[name] is None for name in ('bytes', 'sha256', 'event_count', 'chain_head_sha256')]
    if (snapshot['status'] == 'unavailable' and not all(missing)) or (snapshot['status'] == 'verified_chain' and any(missing)):
        fail('first_live_failure_artifact_snapshot_invalid')
    return value


def _inspect_database(root, directory, binding):
    database = directory / 'audit.sqlite3'
    payload = _database_snapshot(database, MAX_DATABASE_BYTES)
    scan_public_artifact_bytes((payload,))  # Before parsing or hashing any database.
    profile, _ = load_artifact_contract(root)
    if _verify_audit_database(database, expected_payload=payload).schema_commitment_sha256 != profile['expected_database_schema_sha256']:
        fail('first_live_failure_artifact_database_schema')
    runs, events, models, tools, attempts, approvals = _read_database_rows(database, expected_payload=payload)
    if len(runs) != 1 or not 1 <= len(events) <= 6 or models or tools or attempts or approvals:
        fail('first_live_failure_artifact_inventory')
    run = runs[0]
    expected_request = sha256_json({name: binding[name] for name in ('plan_commitment_sha256', 'execution_binding_sha256')})
    if (run['run_id'] != binding['validation_run_id'] or run['mode'] != 'deepseek_first_live_timed_validation'
        or run['request_sha256'] != expected_request or run['dataset_sha256'] is not None
        or run['status'] not in ('running', 'failed', 'cancelled', 'completed')
        or any(row['run_id'] != run['run_id'] for row in events)):
        fail('first_live_failure_artifact_run_binding')
    chain = verify_audit_chain_rows(run['run_id'], events)
    if not chain.valid or _database_snapshot(database, MAX_DATABASE_BYTES) != payload:
        fail('first_live_failure_artifact_database_changed')
    return dict(name='audit.sqlite3', status='verified_chain', bytes=len(payload), sha256=digest(payload),
                event_count=len(events), chain_head_sha256=chain.chain_head)


def verify_failure_artifacts(root, directory, *, expected_manifest_commitment_sha256):
    """Read-only diagnostic binding; no source, consent, count or timing proof."""
    manifest_bytes = read_regular_file_no_follow(directory / 'failure_manifest.json', max_bytes=8192)
    value = _validate_manifest(root, manifest_bytes)
    if value['manifest_commitment_sha256'] != expected_manifest_commitment_sha256:
        fail('first_live_failure_artifact_expected_commitment')
    receipt_bytes = read_regular_file_no_follow(directory / 'failure_receipt.json', max_bytes=4096)
    receipt = validate_failure_receipt(root, receipt_bytes)
    if receipt['claim_consumed'] is not True or value['receipt'] != dict(name='failure_receipt.json', bytes=len(receipt_bytes), sha256=digest(receipt_bytes)):
        fail('first_live_failure_artifact_receipt_mismatch')
    available = value['audit_snapshot']['status'] == 'verified_chain'
    if available and _inspect_database(root, directory, value) != value['audit_snapshot']:
        fail('first_live_failure_artifact_database_mismatch')
    if (read_regular_file_no_follow(directory / 'failure_manifest.json', max_bytes=8192) != manifest_bytes or
        read_regular_file_no_follow(directory / 'failure_receipt.json', max_bytes=4096) != receipt_bytes):
        fail('first_live_failure_artifact_changed')
    if available and digest(_database_snapshot(directory / 'audit.sqlite3', MAX_DATABASE_BYTES)) != value['audit_snapshot']['sha256']:
        fail('first_live_failure_artifact_database_changed')
    return dict(status='failure_artifacts_verified', audit_chain_bound=available, diagnostic_only=True,
                receipt_counts_independently_verified=False, runtime_authority_granted=False, closure_claim_allowed=False,
                manifest_commitment_sha256=value['manifest_commitment_sha256'])


def _write_failure_package(root, directory, receipt_bytes, *, binding, budget, expires):
    if type(budget) is not _StartBudget or type(binding) is not dict or set(binding) != set(_BINDING):
        fail('first_live_failure_artifact_input_invalid')
    budget.artifact_checkpoint(expires)
    def checkpoint():
        budget.artifact_checkpoint(expires)
    value = validate_failure_receipt(root, receipt_bytes)
    if value['claim_consumed'] is not True:
        fail('first_live_failure_artifact_claim_unknown')
    with _locked_chain(directory):
        checkpoint()
        try:
            snapshot = _inspect_database(root, directory, binding)
        except Exception:
            snapshot = dict(name='audit.sqlite3', status='unavailable', bytes=None, sha256=None,
                            event_count=None, chain_head_sha256=None)
        manifest = dict(schema_version='provider-completion-first-live-failure-manifest/1.0', **binding,
            receipt=dict(name='failure_receipt.json', bytes=len(receipt_bytes), sha256=digest(receipt_bytes)),
            audit_snapshot=snapshot, diagnostic_only=True, receipt_counts_independently_verified=False,
            runtime_authority_granted=False, closure_claim_allowed=False, retry_authorized=False)
        manifest['manifest_commitment_sha256'] = manifest_commitment(manifest)
        payload = raw(manifest)
        _validate_manifest(root, payload)
        checkpoint()
        _write_exclusive(directory / 'failure_receipt.json', receipt_bytes, [])
        checkpoint()
        _write_exclusive(directory / 'failure_manifest.json', payload, [])
        checkpoint()
        result = verify_failure_artifacts(root, directory, expected_manifest_commitment_sha256=manifest['manifest_commitment_sha256'])
        checkpoint()
    checkpoint()
    return result


def _persist_owned_failure(factory, receipt_bytes):
    if type(factory) not in (_FirstLiveModelFactory, _FirstLiveModelFactoryV5):
        fail('first_live_failure_artifact_factory_required')
    prepared = factory._prepared
    prepared._assert_process()
    if factory._inflight:
        fail('first_live_failure_artifact_inflight')
    binding = dict(authorization_id_sha256=factory._plan['binding']['authorization_id_sha256'],
        validation_run_id=factory._run,
        **{name: factory._timing_binding[name] for name in ('plan_commitment_sha256', 'execution_binding_sha256')})
    # Invoked only inside the runner's still-held owned workspace context.
    from .first_live_runtime import ROOT
    return _write_failure_package(ROOT, factory._ledger.database_path.parent, receipt_bytes,
        binding=binding, budget=prepared._budget, expires=prepared._expires)
