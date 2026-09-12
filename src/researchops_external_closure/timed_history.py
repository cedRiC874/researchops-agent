"""Verify timed pre/post history and operational facts, without artifact access.

This is preparation for timed A/B. It neither signs nor verifies a final witness,
reads a task/Key/store, or establishes artifact/source/runtime authority.
"""
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from researchops_completion_timing.execution_scope import VerifiedSingleHostScope, verify_single_host_pre_execution
from . import timed_contract as wire, documents as inherited
from .evaluator import _postrun_pre_projection_error
from .errors import ExternalClosurePrimitiveError
from .primitives import canonical_json_bytes as raw, decode_strict_json_object, parse_utc_timestamp
from .types import PreReceiptDocumentBytes


def _fail(code):
    raise ExternalClosurePrimitiveError('timed_history_' + code) from None


@dataclass(frozen=True, slots=True)
class TimedHistory:
    scope: VerifiedSingleHostScope
    _projection: Mapping[str, str] = field(repr=False)
    observation_mode: str
    pre_execution_observation_sha256: str
    postrun_observation_sha256: str
    final_witness_verified: bool = field(default=False, init=False)
    oob_origin_independently_proved: bool = field(default=False, init=False)
    artifact_evidence_verified: bool = field(default=False, init=False)
    runtime_authority_granted: bool = field(default=False, init=False)
    closure_claim_allowed: bool = field(default=False, init=False)

    def __getattr__(self, name):
        values = object.__getattribute__(self, '_projection')
        if name in values:
            return values[name]
        raise AttributeError(name)


@dataclass(frozen=True, slots=True)
class TimedPostrunFacts:
    document_sha256: str
    _values: Mapping[str, object] = field(repr=False)

    def __getattr__(self, name):
        values = object.__getattribute__(self, '_values')
        if name in values:
            return values[name]
        raise AttributeError(name)


@dataclass(frozen=True, slots=True)
class TimedPreReceiptInputs:
    history: TimedHistory
    facts: TimedPostrunFacts
    runtime_authority_granted: bool = field(default=False, init=False)
    closure_claim_allowed: bool = field(default=False, init=False)


def verify_timed_history(project_root, documents, *, pre_execution_observation,
        postrun_observation, timing_plan_bytes, observation_mode='pre_receipt'):
    if (not isinstance(project_root, Path) or type(documents) is not PreReceiptDocumentBytes
            or type(observation_mode) is not str or observation_mode not in ('pre_receipt', 'final')):
        _fail('input_invalid')
    after = wire.validate_document('observation', postrun_observation, root=project_root)
    if after['mode'] != observation_mode:
        _fail('observation_mode_invalid')
    before = decode_strict_json_object(pre_execution_observation, max_bytes=2_000_000)
    try:
        as_of = before['authorization_consumed_anchor']['observed_at_utc']
    except (KeyError, TypeError):
        _fail('consumed_anchor_missing')
    # As-of is derived from the independent observation, not a caller clock.
    # The real verifier must still validate that observation and all signatures.
    scope = verify_single_host_pre_execution(project_root, documents,
        external_observation_bundle=pre_execution_observation, timing_plan_bytes=timing_plan_bytes,
        verification_time_utc=as_of)
    common = {key: value for key, value in before.items() if key not in {'schema_version', 'mode'}}
    if raw({key: after.get(key) for key in common}) != raw(common):
        _fail('observation_history_mismatch')
    decoded = {name: decode_strict_json_object(getattr(documents, name), max_bytes=2_000_000)
               for name in inherited._SCHEMA_BY_DOCUMENT}
    # Same immutable documents, already verified under v3; use the unchanged
    # timeline predicates without relabelling signed documents as v1.
    inherited._verify_timeline(decoded, after)
    projection = {}
    for target, source, key in (
        ('trust_manifest_sha256', 'trust_manifest', 'document_sha256'),
        ('candidate_freeze_receipt_sha256', 'candidate_freeze_receipt', 'document_sha256'),
        ('candidate_frozen_ledger_entry_sha256', 'candidate_frozen_ledger_entry', 'entry_sha256'),
        ('seen_case_exclusion_manifest_sha256', 'seen_case_exclusion_manifest', 'document_sha256'),
        ('preregistration_envelope_sha256', 'preregistration_envelope', 'document_sha256'),
        ('preregistration_frozen_ledger_entry_sha256', 'preregistration_frozen_ledger_entry', 'entry_sha256'),
        ('authorization_grant_sha256', 'authorization_grant', 'document_sha256'),
        ('consumption_receipt_sha256', 'consumption_receipt', 'document_sha256'),
        ('authorization_consumed_ledger_entry_sha256', 'authorization_consumed_ledger_entry', 'entry_sha256'),
        ('authorization_consumed_ledger_head_sha256', 'authorization_consumed_ledger_entry', 'resulting_head_sha256'),
        ('grant_expires_at_utc', 'authorization_grant', 'expires_at_utc'),
        ('envelope_valid_until_utc', 'preregistration_envelope', 'valid_until_utc')):
        projection[target] = decoded[source][key]
    for target, source in (('trust_observed_at_utc', 'trust_manifest_observation'),
                           ('envelope_observed_at_utc', 'preregistration_envelope_observation'),
                           ('authorization_observed_at_utc', 'user_authorization_observation'),
                           ('authorization_consumed_observed_at_utc', 'authorization_consumed_anchor'),
                           ('manifest_observed_at_utc', 'manifest_observation')):
        projection[target] = after[source]['observed_at_utc']
    return TimedHistory(scope, MappingProxyType(projection), observation_mode,
        inherited._sha256(pre_execution_observation), inherited._sha256(postrun_observation))


def verify_timed_pre_receipt_inputs(project_root, documents, *, pre_execution_observation,
        postrun_observation, timing_plan_bytes, postrun_attested_facts, observation_mode='pre_receipt'):
    """Raw-input composition; no accepted successful-history object or callback."""
    history = verify_timed_history(project_root, documents,
        pre_execution_observation=pre_execution_observation, postrun_observation=postrun_observation,
        timing_plan_bytes=timing_plan_bytes, observation_mode=observation_mode)
    value = wire.validate_document('facts', postrun_attested_facts, root=project_root)
    profile, _ = wire.load_contract(project_root)
    identifier = wire.receipt_id(dict(campaign_id=value['campaign_id'],
        authorization_grant_sha256=history.authorization_grant_sha256,
        authorization_consumption_entry_sha256=history.authorization_consumed_ledger_entry_sha256), profile)
    if value['receipt_id'] != identifier:
        _fail('receipt_id_mismatch')
    if not parse_utc_timestamp(value['completed_at_utc']) < parse_utc_timestamp(value['receipt_issued_at_utc']):
        _fail('facts_timeline_invalid')
    facts = TimedPostrunFacts(inherited._sha256(postrun_attested_facts), MappingProxyType(value))
    envelope = decode_strict_json_object(documents.preregistration_envelope, max_bytes=2_000_000)
    consumed = decode_strict_json_object(documents.authorization_consumed_ledger_entry, max_bytes=2_000_000)
    trust = decode_strict_json_object(documents.trust_manifest, max_bytes=2_000_000)
    after = decode_strict_json_object(postrun_observation, max_bytes=2_000_000)
    error = _postrun_pre_projection_error(facts, pre=history, envelope=envelope,
        consumed_entry=consumed, observation=after, trust=trust)
    if error is not None:
        raise ExternalClosurePrimitiveError(error)
    if after['manifest_observation']['last_completed_artifact_write_stage'] != facts.last_completed_artifact_write_stage:
        _fail('manifest_stage_mismatch')
    return TimedPreReceiptInputs(history, facts)
