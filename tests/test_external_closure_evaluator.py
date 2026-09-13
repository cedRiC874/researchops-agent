from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

from researchops.audit import AuditLedger
import researchops_external_closure.evaluator as evaluator_module
from researchops_external_closure.artifacts import validate_postrun_attested_facts
from researchops_external_closure.evaluator import evaluate_closure_bundle
from researchops_external_closure.final import verify_closed_campaign
from researchops_external_closure.primitives import (
    canonical_json_bytes,
    compute_document_sha256,
    compute_ledger_entry_sha256,
    compute_ledger_resulting_head,
)
from researchops_external_closure.types import (
    FinalDocumentBytes,
    PreReceiptDocumentBytes,
    PreReceiptRejected,
    ReceiptProjectionReady,
)
from tests.test_external_closure_artifacts import _write_prefix
from tests.test_external_closure_documents import (
    AUDIT_SET_DOMAIN,
    CASE_ORDER_DOMAIN,
    CASE_SET_DOMAIN,
    RUNTIME_PLAN_DOMAIN,
    USER_BINDING_DOMAIN,
    SyntheticPreReceipt,
    _domain_hash,
)
from tests import test_external_closure_semantics as _semantic_helpers


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_external_closure" / "evaluator.py"
SCHEMA_ROOT = (
    ROOT / "evals" / "provider_completion_external_preregistration_v1" / "schemas"
)


def _postrun(
    fixture: SyntheticPreReceipt,
    *,
    stage: str = "manifest_written",
    canary_scan: str = "passed",
    public_scan: str = "passed",
    database_origin: str = "fresh_path_confirmed",
    database_mutation: str = "normative_only",
) -> dict:
    envelope = fixture.documents["preregistration_envelope"]
    custody = envelope["task_custody"]
    campaign_id = envelope["runtime_plan"]["campaign_topology"]["campaign_id"]
    artifact_error = {
        "not_started": "pce_artifact_not_started_failed",
        "audit_database_written": "pce_artifact_audit_index_failed",
        "audit_index_written": "pce_artifact_completion_telemetry_failed",
        "completion_telemetry_written": "pce_artifact_manifest_failed",
        "manifest_written": None,
    }[stage]
    return {
        "schema_version": "provider-completion-postrun-attested-facts/1.0",
        "campaign_id": campaign_id,
        "receipt_id": "PCECLOSE-0123456789ABCDEF0123456789ABCDEF",
        "completed_at_utc": "2026-09-04T00:00:19Z",
        "receipt_issued_at_utc": "2026-09-04T00:00:21Z",
        "terminal_stage": "none",
        "local_terminal_outcome": "succeeded",
        "terminal_provider_outcome_state": "response_observed",
        "terminal_error_code": None,
        "last_completed_artifact_write_stage": stage,
        "artifact_error_code": artifact_error,
        "task_released": True,
        "task_released_at_utc": "2026-09-04T00:00:17Z",
        "provider_key_loaded": True,
        "provider_key_loaded_at_utc": "2026-09-04T00:00:18Z",
        "released_task_bundle_commitment_sha256": custody[
            "task_bundle_commitment_sha256"
        ],
        "released_case_handle_order_commitment_sha256": custody[
            "case_order_commitment_sha256"
        ],
        "released_execution_input_order_commitment_sha256": custody[
            "execution_input_order_commitment_sha256"
        ],
        "task_bundle_opening_verified_in_memory": True,
        "task_bundle_opening_persisted": False,
        "custodian_private_leak_canary_scan_status": canary_scan,
        "public_generic_privacy_scan_status": public_scan,
        "canary_injection_verified_for_every_send": True,
        "database_origin_status": database_origin,
        "database_mutation_status": database_mutation,
        "network_attempt_count_observed": 2,
        "model_request_count_observed": 2,
        "raw_response_cleanup_status": "completed",
        "post_request_cleanup_status": "completed",
        "provider_key_reference_release_status": "released",
        "task_content_included_in_facts": False,
        "provider_content_included_in_facts": False,
        "derived_closure_claim_included": False,
    }


def _inputs(fixture: SyntheticPreReceipt) -> tuple[PreReceiptDocumentBytes, bytes]:
    return fixture.inputs()


def _set_manifest_observation(
    fixture: SyntheticPreReceipt,
    *,
    state: str,
    stage: str,
    manifest_sha256: str | None,
    quarantine_reason: str | None = None,
) -> None:
    value = {
        "state": state,
        "manifest_sha256": manifest_sha256,
        "observed_at_utc": "2026-09-04T00:00:20Z",
        "observation_source_commitment_sha256": hashlib.sha256(
            b"observation-source"
        ).hexdigest(),
        "last_completed_artifact_write_stage": stage,
    }
    if quarantine_reason is not None:
        value["quarantine_reason"] = quarantine_reason
    fixture.observation["manifest_observation"] = value


def _anchor(entry: dict, observed_at: str) -> dict:
    return {
        "entry_sha256": entry["entry_sha256"],
        "ledger_id": entry["ledger_id"],
        "entry_type": entry["entry_type"],
        "sequence": entry["sequence"],
        "previous_head_sha256": entry["previous_head_sha256"],
        "resulting_head_sha256": entry["resulting_head_sha256"],
        "entry_occurred_at_utc": entry["occurred_at_utc"],
        "observed_at_utc": observed_at,
        "observation_source_commitment_sha256": hashlib.sha256(
            b"observation-source"
        ).hexdigest(),
    }


def _rebind_semantic_fixture(
    semantic: dict,
    *,
    envelope_id: str,
    campaign_id: str,
    runtime_plan: dict,
    authorization_grant_sha256: str,
    model_id: str,
) -> None:
    """Rebind the semantic fixture to the genuinely signed pre-receipt graph."""

    denominator = runtime_plan["denominator_plan"]
    index_entries = semantic["audit_index"]["runs"]
    old_run_ids = [entry["run_id"] for entry in index_entries]
    new_run_ids = [
        _semantic_helpers._run_id(envelope_id, case_id)
        for case_id in denominator["case_ids"]
    ]
    for ordinal, (entry, old_run_id, new_run_id, case_id) in enumerate(
        zip(index_entries, old_run_ids, new_run_ids, denominator["case_ids"])
    ):
        run = next(row for row in semantic["run_rows"] if row["run_id"] == old_run_id)
        events = [
            row for row in semantic["event_rows"] if row["run_id"] == old_run_id
        ]
        model_rows = [
            row
            for row in semantic["model_call_rows"]
            if row["run_id"] == old_run_id
        ]
        for row in model_rows:
            row["run_id"] = new_run_id
        for row in events:
            row["run_id"] = new_run_id

        decoded = {
            row["event_type"]: json.loads(row["safe_payload_json"])
            for row in events
        }
        send = decoded["provider_transport_request_sent"]
        send["external_plan_binding_sha256"] = runtime_plan[
            "external_plan_binding_sha256"
        ]
        send["authorization_grant_sha256"] = authorization_grant_sha256
        request_projection = {
            "campaign_id": campaign_id,
            "case_handle": case_id,
            "execution_input_commitment_sha256": send[
                "execution_input_commitment_sha256"
            ],
            "external_plan_binding_sha256": runtime_plan[
                "external_plan_binding_sha256"
            ],
            "authorization_grant_sha256": authorization_grant_sha256,
            "provider_id": denominator["provider_id"],
            "model_id": model_id,
            "api_surface": denominator["api_surface"],
            "transport_id": denominator["transport_id"],
        }
        request_sha256 = _semantic_helpers._canonical_hash(request_projection)
        decoded["run_started"]["request_sha256"] = request_sha256
        send["audit_request_sha256"] = request_sha256
        run["run_id"] = new_run_id
        run["request_sha256"] = request_sha256

        previous = "0" * 64
        for row in sorted(events, key=lambda candidate: candidate["sequence"]):
            row["safe_payload_json"] = canonical_json_bytes(
                decoded[row["event_type"]]
            ).decode("utf-8")
            row["prev_hash"] = previous
            row["event_hash"] = _semantic_helpers._event_hash(row)
            previous = row["event_hash"]

        entry["run_id"] = new_run_id
        entry["chain_verification"].update(
            run_id=new_run_id,
            event_count=len(events),
            chain_head=previous,
        )
        bridge = entry["completion_telemetry_event_commitment"]
        started = next(row for row in events if row["event_type"] == "model_request_started")
        terminal = next(
            row
            for row in events
            if row["event_type"] == "model_response_telemetry_recorded"
        )
        bridge["started"] = [
            {"attempt_index": ordinal, "event_hash": started["event_hash"]}
        ]
        bridge["terminals"] = [
            {"attempt_index": ordinal, "event_hash": terminal["event_hash"]}
        ]
        bridge_body = dict(bridge)
        bridge_body.pop("commitment_sha256")
        bridge["commitment_sha256"] = _semantic_helpers._canonical_hash(bridge_body)

    semantic["envelope_id"] = envelope_id
    semantic["campaign_id"] = campaign_id
    semantic["expected_runtime_plan"] = runtime_plan
    semantic["authorization_grant_sha256"] = authorization_grant_sha256


def _signed_complete_fixture() -> tuple[SyntheticPreReceipt, dict]:
    _semantic_helpers.ExternalClosureSemanticTests.setUpClass()
    semantic_case = _semantic_helpers.ExternalClosureSemanticTests()
    semantic = semantic_case._fixture()
    fixture = SyntheticPreReceipt()

    envelope = fixture.documents["preregistration_envelope"]
    envelope_id = envelope["envelope_id"]
    custody = envelope["task_custody"]
    campaign_id = envelope["runtime_plan"]["campaign_topology"]["campaign_id"]
    runtime_plan = copy.deepcopy(semantic["expected_runtime_plan"])
    denominator = runtime_plan["denominator_plan"]
    case_ids = denominator["case_ids"]
    case_set = _domain_hash(CASE_SET_DOMAIN, canonical_json_bytes(sorted(case_ids)))
    case_order = _domain_hash(CASE_ORDER_DOMAIN, canonical_json_bytes(case_ids))
    run_ids = [
        _semantic_helpers._run_id(envelope_id, case_id) for case_id in case_ids
    ]
    runtime_plan["campaign_topology"].update(
        campaign_id=campaign_id,
        case_handle_set_commitment_sha256=case_set,
        case_handle_order_commitment_sha256=case_order,
        audit_run_set_commitment_sha256=_domain_hash(
            AUDIT_SET_DOMAIN,
            b"planned",
            canonical_json_bytes(run_ids),
        ),
    )
    budget = runtime_plan["budget_policy"]
    budget["pricing_source_url"] = (
        "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
    )
    budget_body = dict(budget)
    budget_body.pop("budget_policy_commitment_sha256")
    budget["budget_policy_commitment_sha256"] = _domain_hash(
        RUNTIME_PLAN_DOMAIN,
        b"budget_policy",
        canonical_json_bytes(budget_body),
    )
    runtime_body = copy.deepcopy(runtime_plan)
    runtime_body.pop("external_plan_binding_sha256")
    runtime_plan["external_plan_binding_sha256"] = _domain_hash(
        RUNTIME_PLAN_DOMAIN, canonical_json_bytes(runtime_body)
    )

    execution = envelope["execution_binding"]
    execution.update(
        telemetry_schema_sha256=denominator["telemetry_schema_sha256"],
        mapping_sha256=denominator["mapping_sha256"],
        provider_id=denominator["provider_id"],
        api_surface=denominator["api_surface"],
        transport_id=denominator["transport_id"],
        adapter_version=denominator["adapter_version"],
    )
    custody.update(
        planned_case_count=len(case_ids),
        case_handle_set_commitment_sha256=case_set,
        case_order_commitment_sha256=case_order,
        execution_input_order_commitment_sha256=semantic[
            "execution_input_order_commitment_sha256"
        ],
    )
    envelope["runtime_plan"] = runtime_plan
    envelope["document_sha256"] = "0" * 64
    envelope["signatures"] = []
    fixture._finalize_document(envelope, ("freeze_authority", "task_custodian"))

    candidate = fixture.documents["candidate_freeze_receipt"]
    candidate_entry = fixture.documents["candidate_frozen_ledger_entry"]
    preregistration_entry = fixture._ledger_entry(
        entry_type="preregistration_frozen",
        sequence=8,
        previous_head=candidate_entry["resulting_head_sha256"],
        occurred_at="2026-09-04T00:00:09Z",
        candidate_hash=candidate["document_sha256"],
        envelope_hash=envelope["document_sha256"],
        grant_hash=None,
        consumption_hash=None,
    )
    fixture.documents["preregistration_frozen_ledger_entry"] = preregistration_entry

    grant = fixture.documents["authorization_grant"]
    grant.update(
        expires_at_utc="2026-09-04T05:00:00Z",
        preregistration_envelope_sha256=envelope["document_sha256"],
        preregistration_freeze_entry_sha256=preregistration_entry["entry_sha256"],
        ledger_sequence=preregistration_entry["sequence"],
        ledger_head_sha256=preregistration_entry["resulting_head_sha256"],
        denominator_plan_commitment_sha256=runtime_plan[
            "denominator_plan_commitment_sha256"
        ],
        external_plan_binding_sha256=runtime_plan["external_plan_binding_sha256"],
        budget_policy_commitment_sha256=budget[
            "budget_policy_commitment_sha256"
        ],
        pricing_snapshot_date=budget["pricing_snapshot_date"],
        pricing_source_snapshot_sha256=budget[
            "pricing_source_snapshot_sha256"
        ],
        pricing_retrieved_at_utc=budget["pricing_retrieved_at_utc"],
        official_pricing_attestation_current=budget[
            "official_pricing_attestation_current"
        ],
        provider_id=execution["provider_id"],
        model_id=execution["model_id"],
        api_origin=execution["api_origin"],
        api_surface=execution["api_surface"],
        transport_id=execution["transport_id"],
        agents_sdk_retries=denominator["agents_sdk_retries"],
        http_client_retries=denominator["http_client_retries"],
        resume=runtime_plan["transport_limits"]["resume"],
        fallback=runtime_plan["transport_limits"]["fallback"],
        explicit_user_authorization_binding_sha256="0" * 64,
        document_sha256="0" * 64,
        signatures=[],
    )
    grant_body = dict(grant)
    for omitted in (
        "explicit_user_authorization_binding_sha256",
        "document_sha256",
        "signatures",
    ):
        grant_body.pop(omitted)
    grant["explicit_user_authorization_binding_sha256"] = _domain_hash(
        USER_BINDING_DOMAIN, canonical_json_bytes(grant_body)
    )
    fixture._finalize_document(grant, ("freeze_authority", "task_custodian"))

    consumption = fixture.documents["consumption_receipt"]
    consumption.update(
        preregistration_envelope_sha256=envelope["document_sha256"],
        preregistration_freeze_entry_sha256=preregistration_entry["entry_sha256"],
        authorization_grant_sha256=grant["document_sha256"],
        denominator_plan_commitment_sha256=runtime_plan[
            "denominator_plan_commitment_sha256"
        ],
        external_plan_binding_sha256=runtime_plan["external_plan_binding_sha256"],
        document_sha256="0" * 64,
    )
    fixture._finalize_document(consumption)
    consumed_entry = fixture._ledger_entry(
        entry_type="authorization_consumed",
        sequence=9,
        previous_head=preregistration_entry["resulting_head_sha256"],
        occurred_at="2026-09-04T00:00:15Z",
        candidate_hash=candidate["document_sha256"],
        envelope_hash=envelope["document_sha256"],
        grant_hash=grant["document_sha256"],
        consumption_hash=consumption["document_sha256"],
    )
    fixture.documents["authorization_consumed_ledger_entry"] = consumed_entry

    fixture.observation["preregistration_envelope_observation"][
        "document_sha256"
    ] = envelope["document_sha256"]
    fixture.observation["preregistration_freeze_anchor"] = _anchor(
        preregistration_entry, "2026-09-04T00:00:10Z"
    )
    fixture.observation["user_authorization_observation"].update(
        authorization_id_sha256=grant["explicit_user_authorization_id_sha256"],
        authorization_binding_sha256=grant[
            "explicit_user_authorization_binding_sha256"
        ],
    )
    fixture.observation["authorization_consumed_anchor"] = _anchor(
        consumed_entry, "2026-09-04T00:00:16Z"
    )
    fixture.observation["manifest_observation"][
        "observed_at_utc"
    ] = "2026-09-04T03:30:30Z"

    _rebind_semantic_fixture(
        semantic,
        envelope_id=envelope_id,
        campaign_id=campaign_id,
        runtime_plan=runtime_plan,
        authorization_grant_sha256=grant["document_sha256"],
        model_id=execution["model_id"],
    )
    return fixture, semantic


def _write_complete_bundle(
    directory: Path, fixture: SyntheticPreReceipt, semantic: dict
) -> bytes:
    database = directory / "phase6_audit.sqlite3"
    AuditLedger(database)
    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            "INSERT INTO runs(run_id,mode,status,request_sha256,dataset_sha256,"
            "created_at_utc,updated_at_utc,terminal_error_code) "
            "VALUES(:run_id,:mode,:status,:request_sha256,:dataset_sha256,"
            ":created_at_utc,:updated_at_utc,:terminal_error_code)",
            semantic["run_rows"],
        )
        connection.executemany(
            "INSERT INTO audit_events(event_id,run_id,sequence,event_type,"
            "occurred_at_utc,actor_kind,safe_payload_json,prev_hash,event_hash) "
            "VALUES(:event_id,:run_id,:sequence,:event_type,:occurred_at_utc,"
            ":actor_kind,:safe_payload_json,:prev_hash,:event_hash)",
            semantic["event_rows"],
        )
        connection.executemany(
            "INSERT INTO model_calls(model_call_id,run_id,provider,model,"
            "started_at_utc,latency_ms,input_tokens,output_tokens,cached_tokens,"
            "cost_usd,outcome,error_code) VALUES(:model_call_id,:run_id,:provider,"
            ":model,:started_at_utc,:latency_ms,:input_tokens,:output_tokens,"
            ":cached_tokens,:cost_usd,:outcome,:error_code)",
            semantic["model_call_rows"],
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.commit()
    finally:
        connection.close()

    (directory / "phase6_audit_index.json").write_bytes(
        canonical_json_bytes(semantic["audit_index"]) + b"\n"
    )
    (directory / "phase6_completion_telemetry.json").write_bytes(
        canonical_json_bytes(semantic["telemetry"]) + b"\n"
    )
    envelope = fixture.documents["preregistration_envelope"]
    runtime = envelope["runtime_plan"]
    files = {}
    for name in _WRITER_ORDER_FOR_TEST[:3]:
        payload = (directory / name).read_bytes()
        files[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {
        "schema_version": "provider-completion-closure-bundle-manifest/1.0",
        "status": "complete",
        "campaign_id": runtime["campaign_topology"]["campaign_id"],
        "preregistration_envelope_sha256": envelope["document_sha256"],
        "preregistration_freeze_entry_sha256": fixture.documents[
            "preregistration_frozen_ledger_entry"
        ]["entry_sha256"],
        "authorization_grant_sha256": fixture.documents["authorization_grant"][
            "document_sha256"
        ],
        "consumption_receipt_sha256": fixture.documents["consumption_receipt"][
            "document_sha256"
        ],
        "closure_evidence_contract_commitment_sha256": (
            "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0"
        ),
        "denominator_plan_commitment_sha256": runtime[
            "denominator_plan_commitment_sha256"
        ],
        "external_plan_binding_sha256": runtime["external_plan_binding_sha256"],
        "audit_database_schema_commitment_sha256": (
            "c0df1f54a96fe3c4f5ee196e654203a62e5e775a3e21fd5f47ace9e349169385"
        ),
        "files": files,
        "manifest_self_hash_included": False,
        "raw_provider_content_persisted": False,
        "task_content_persisted": False,
        "api_key_persisted": False,
    }
    payload = canonical_json_bytes(manifest) + b"\n"
    (directory / "completion_closure_manifest.json").write_bytes(payload)
    return payload


_WRITER_ORDER_FOR_TEST = (
    "phase6_audit.sqlite3",
    "phase6_audit_index.json",
    "phase6_completion_telemetry.json",
    "completion_closure_manifest.json",
)


def _complete_postrun(fixture: SyntheticPreReceipt) -> dict:
    value = _postrun(fixture)
    value.update(
        completed_at_utc="2026-09-04T03:30:00Z",
        receipt_issued_at_utc="2026-09-04T03:31:00Z",
        task_released_at_utc="2026-09-04T02:55:00Z",
        provider_key_loaded_at_utc="2026-09-04T02:56:00Z",
    )
    return value


def _signed_receipt_bytes(
    fixture: SyntheticPreReceipt, projected: ReceiptProjectionReady
) -> bytes:
    receipt = json.loads(projected.unsigned_receipt_canonical_json)
    receipt["document_sha256"] = projected.receipt_document_sha256
    receipt["signatures"] = fixture._signatures(receipt, ("task_custodian",))
    return canonical_json_bytes(receipt)


def _final_entry(
    fixture: SyntheticPreReceipt,
    *,
    receipt_sha256: str,
    occurred_at_utc: str,
) -> dict:
    consumed = fixture.documents["authorization_consumed_ledger_entry"]
    entry = {
        "schema_version": "provider-completion-external-ledger-entry/1.0",
        "document_type": "completion_telemetry_external_ledger_entry",
        "entry_type": "closure_evidence_anchored",
        "ledger_id": consumed["ledger_id"],
        "sequence": consumed["sequence"] + 1,
        "previous_head_sha256": consumed["resulting_head_sha256"],
        "occurred_at_utc": occurred_at_utc,
        "candidate_freeze_receipt_sha256": fixture.documents[
            "candidate_freeze_receipt"
        ]["document_sha256"],
        "preregistration_envelope_sha256": fixture.documents[
            "preregistration_envelope"
        ]["document_sha256"],
        "authorization_grant_sha256": fixture.documents["authorization_grant"][
            "document_sha256"
        ],
        "consumption_receipt_sha256": fixture.documents["consumption_receipt"][
            "document_sha256"
        ],
        "closure_receipt_sha256": receipt_sha256,
        "entry_sha256": "0" * 64,
        "resulting_head_sha256": "0" * 64,
        "signatures": [],
    }
    entry["entry_sha256"] = compute_ledger_entry_sha256(entry)
    entry["resulting_head_sha256"] = compute_ledger_resulting_head(
        entry["previous_head_sha256"], entry["entry_sha256"]
    )
    entry["signatures"] = fixture._signatures(entry, ("ledger_witness",))
    return entry


def _final_observation_bytes(
    pre_observation: bytes,
    *,
    receipt_sha256: str,
    receipt_observed_at_utc: str,
    entry: dict,
    entry_observed_at_utc: str,
) -> bytes:
    observation = json.loads(pre_observation)
    observation["mode"] = "final"
    observation["closure_receipt_observation"] = {
        "document_sha256": receipt_sha256,
        "observed_at_utc": receipt_observed_at_utc,
        "observation_source_commitment_sha256": hashlib.sha256(
            b"observation-source"
        ).hexdigest(),
    }
    observation["closure_evidence_anchor"] = _anchor(
        entry, entry_observed_at_utc
    )
    return canonical_json_bytes(observation)


def _decode_projection(result: ReceiptProjectionReady) -> dict:
    value = json.loads(result.unsigned_receipt_canonical_json)
    assert type(value) is dict
    return value


def _assert_projection_schema(test: unittest.TestCase, result: ReceiptProjectionReady) -> dict:
    body = _decode_projection(result)
    test.assertEqual(len(body), 58)
    test.assertNotIn("document_sha256", body)
    test.assertNotIn("signatures", body)
    test.assertNotIn("case_ids", body)
    test.assertNotIn("records", body)
    test.assertEqual(compute_document_sha256(body), result.receipt_document_sha256)
    signed = {
        **body,
        "document_sha256": result.receipt_document_sha256,
        "signatures": [
            {
                "role": "task_custodian",
                "key_id": "PCEKEY-00000000000000000000000000000000",
                "algorithm": "ed25519",
                "signed_sha256": result.receipt_document_sha256,
                "signature_b64": "A" * 86 + "==",
            }
        ],
    }
    schema = json.loads(
        (SCHEMA_ROOT / "closure_receipt_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(signed)
    return body


class _PoisonArtifactDirectory:
    def __getattribute__(self, name: str):
        raise AssertionError(f"quarantine_artifact_directory_was_touched:{name}")


class ExternalClosureEvaluatorTests(unittest.TestCase):
    def test_manifest_recovery_rechecks_raw_file_commitments_and_error_priority(self) -> None:
        fixture, semantic = _signed_complete_fixture()
        schemas = evaluator_module._load_schemas(ROOT)
        facts = validate_postrun_attested_facts(
            canonical_json_bytes(_complete_postrun(fixture)), schemas=schemas
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = json.loads(_write_complete_bundle(directory, fixture, semantic))
            manifest["files"]["phase6_audit_index.json"]["sha256"] = "f" * 64
            payload = canonical_json_bytes(manifest) + b"\n"
            (directory / "completion_closure_manifest.json").write_bytes(payload)
            projection, recovered, errors = evaluator_module._recover_artifact_projection(
                directory,
                facts=facts,
                schemas=schemas,
                original_error="external_closure_audit_schema_invalid",
            )
        self.assertEqual(projection.bundle_status, "complete")
        self.assertEqual(projection.manifest_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(recovered, manifest)
        self.assertIn("closure_bundle_hash_mismatch", errors)
        self.assertEqual(
            evaluator_module._first_evidence_error(errors), "closure_bundle_hash_mismatch"
        )

    def test_schema_validation_forbids_remote_retrieval_before_validator_call(self) -> None:
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            with self.subTest(keyword=keyword), mock.patch.object(
                evaluator_module.jsonschema, "Draft202012Validator"
            ) as validator:
                self.assertFalse(
                    evaluator_module._schema_validate(
                        {},
                        schemas={"hostile": {keyword: "https://invalid.example/schema"}},
                        schema_name="hostile",
                    )
                )
                validator.assert_not_called()

    def test_attested_quarantine_recovery_never_inspects_poison_path(self) -> None:
        fixture = SyntheticPreReceipt()
        _set_manifest_observation(
            fixture,
            state="withheld_quarantined",
            stage="manifest_written",
            manifest_sha256=None,
            quarantine_reason="sensitive_detected",
        )
        documents, observation = _inputs(fixture)
        with mock.patch.object(
            evaluator_module,
            "extract_artifact_evaluation_inputs",
            side_effect=ValueError("bounded extractor failure"),
        ), mock.patch.object(
            evaluator_module, "read_exact_artifact_directory"
        ) as reader:
            result = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=_PoisonArtifactDirectory(),  # type: ignore[arg-type]
                postrun_attested_facts=canonical_json_bytes(
                    _postrun(fixture, canary_scan="leak_detected")
                ),
            )
        self.assertIsInstance(result, ReceiptProjectionReady)
        assert isinstance(result, ReceiptProjectionReady)
        self.assertEqual(result.evidence_error_code, "closure_sensitive_content_detected")
        reader.assert_not_called()

    def test_complete_manifest_binding_mismatch_is_not_a_schema_failure(self) -> None:
        for field, replacement in (
            ("campaign_id", "PCECAMP-" + "F" * 32),
            ("closure_evidence_contract_commitment_sha256", "f" * 64),
            ("audit_database_schema_commitment_sha256", "f" * 64),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                fixture, semantic = _signed_complete_fixture()
                directory = Path(temporary)
                manifest = json.loads(_write_complete_bundle(directory, fixture, semantic))
                manifest[field] = replacement
                payload = canonical_json_bytes(manifest) + b"\n"
                (directory / "completion_closure_manifest.json").write_bytes(payload)
                fixture.observation["manifest_observation"]["manifest_sha256"] = (
                    hashlib.sha256(payload).hexdigest()
                )
                documents, observation = _inputs(fixture)
                result = evaluate_closure_bundle(
                    ROOT,
                    documents,
                    external_observation_bundle=observation,
                    artifact_directory=directory,
                    postrun_attested_facts=canonical_json_bytes(_complete_postrun(fixture)),
                )
                self.assertIsInstance(result, ReceiptProjectionReady)
                assert isinstance(result, ReceiptProjectionReady)
                self.assertEqual(result.evidence_error_code, "closure_bundle_hash_mismatch")
                body = _assert_projection_schema(self, result)
                self.assertEqual(body["bundle_status"], "complete")
                self.assertEqual(body["closure_bundle_manifest_sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(body["partial_artifacts"], {})

    def test_real_chain_preserves_chain_error_priority_over_unadmitted_registry(self) -> None:
        for variant in ("valid_noneligible", "invalid_chain"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                fixture, semantic = _signed_complete_fixture()
                facts_value = _complete_postrun(fixture)
                if variant == "valid_noneligible":
                    facts_value["canary_injection_verified_for_every_send"] = False
                else:
                    semantic["event_rows"][0]["event_hash"] = "f" * 64
                directory = Path(temporary)
                manifest_payload = _write_complete_bundle(directory, fixture, semantic)
                fixture.observation["manifest_observation"]["manifest_sha256"] = (
                    hashlib.sha256(manifest_payload).hexdigest()
                )
                documents, observation = _inputs(fixture)
                facts = canonical_json_bytes(facts_value)
                projected = evaluate_closure_bundle(
                    ROOT,
                    documents,
                    external_observation_bundle=observation,
                    artifact_directory=directory,
                    postrun_attested_facts=facts,
                )
                self.assertIsInstance(projected, ReceiptProjectionReady)
                assert isinstance(projected, ReceiptProjectionReady)
                self.assertFalse(projected.pre_anchor_closure_eligible)
                if variant == "valid_noneligible":
                    # Standalone semantic tests cover valid/noneligible traces.
                    # This end-to-end fixture has no admitted execution proof:
                    # the real registry remains v2/offline-only.
                    self.assertFalse(projected.evidence_valid)
                    self.assertEqual(projected.evidence_error_code, "closure_denominator_invalid")
                    self.assertEqual(projected.closure_reasons, ())
                else:
                    self.assertFalse(projected.evidence_valid)
                    self.assertEqual(projected.evidence_error_code, "closure_audit_chain_invalid")
                entry = _final_entry(
                    fixture,
                    receipt_sha256=projected.receipt_document_sha256,
                    occurred_at_utc="2026-09-04T03:32:00Z",
                )
                closed = verify_closed_campaign(
                    ROOT,
                    FinalDocumentBytes(
                        documents,
                        _signed_receipt_bytes(fixture, projected),
                        canonical_json_bytes(entry),
                    ),
                    external_observation_bundle=_final_observation_bytes(
                        observation,
                        receipt_sha256=projected.receipt_document_sha256,
                        receipt_observed_at_utc="2026-09-04T03:31:30Z",
                        entry=entry,
                        entry_observed_at_utc="2026-09-04T03:33:00Z",
                    ),
                    artifact_directory=directory,
                    postrun_attested_facts=facts,
                )
                self.assertFalse(closed.closure_claim_allowed)
                self.assertFalse(closed.evidence_valid)
                self.assertEqual(
                    closed.status,
                    "closed_failure_attested",
                )

    def test_postrun_send_count_mismatch_precedes_registry_admission_failure(self) -> None:
        fixture, semantic = _signed_complete_fixture()
        facts = _complete_postrun(fixture)
        facts["network_attempt_count_observed"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest_payload = _write_complete_bundle(directory, fixture, semantic)
            _set_manifest_observation(
                fixture,
                state="present",
                stage="manifest_written",
                manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            )
            fixture.observation["manifest_observation"]["observed_at_utc"] = (
                "2026-09-04T03:30:30Z"
            )
            documents, observation = _inputs(fixture)
            result = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=directory,
                postrun_attested_facts=canonical_json_bytes(facts),
            )
        self.assertIsInstance(result, ReceiptProjectionReady)
        self.assertFalse(result.pre_anchor_closure_eligible)
        self.assertFalse(result.evidence_valid)
        self.assertEqual(result.evidence_error_code, "closure_event_projection_invalid")

    def test_signed_self_report_cannot_promote_the_real_offline_registry(self) -> None:
        fixture, semantic = _signed_complete_fixture()
        registry = json.loads(
            (ROOT / "evals/provider_completion_telemetry_v2/provider_completion_surface_registry_v2.json").read_text(encoding="utf-8")
        )
        self.assertTrue(all(entry["runtime_binding_allowed"] is False for entry in registry["entries"]))
        execution = fixture.documents["preregistration_envelope"]["execution_binding"]
        self.assertTrue(execution["runtime_registry_binding_allowed"])
        self.assertEqual(execution["first_live_validation_status"], "reviewed_success")
        self.assertEqual(execution["execution_commit"], "1" * 40)
        self.assertEqual(execution["execution_tree"], "2" * 40)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest_payload = _write_complete_bundle(directory, fixture, semantic)
            _set_manifest_observation(
                fixture,
                state="present",
                stage="manifest_written",
                manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            )
            fixture.observation["manifest_observation"][
                "observed_at_utc"
            ] = "2026-09-04T03:30:30Z"
            documents, observation = _inputs(fixture)
            result = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=directory,
                postrun_attested_facts=canonical_json_bytes(
                    _complete_postrun(fixture)
                ),
            )
        self.assertIsInstance(result, ReceiptProjectionReady)
        assert isinstance(result, ReceiptProjectionReady)
        body = _assert_projection_schema(self, result)
        self.assertFalse(result.evidence_valid)
        self.assertFalse(result.pre_anchor_closure_eligible)
        self.assertEqual(result.evidence_error_code, "closure_denominator_invalid")
        self.assertEqual(result.closure_reasons, ())
        self.assertIsNone(body["attempt_count"])
        self.assertIsNone(body["accepted_response_count"])
        self.assertIsNone(body["observed_input_tokens"])
        self.assertIsNone(body["observed_output_tokens"])
        self.assertIsNone(body["observed_cost_cny"])

    def test_real_signature_and_witness_attest_failure_without_execution_admission(self) -> None:
        fixture, semantic = _signed_complete_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest_payload = _write_complete_bundle(directory, fixture, semantic)
            _set_manifest_observation(
                fixture,
                state="present",
                stage="manifest_written",
                manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            )
            fixture.observation["manifest_observation"][
                "observed_at_utc"
            ] = "2026-09-04T03:30:30Z"
            documents, pre_observation = _inputs(fixture)
            facts = canonical_json_bytes(_complete_postrun(fixture))
            projected = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=pre_observation,
                artifact_directory=directory,
                postrun_attested_facts=facts,
            )
            self.assertIsInstance(projected, ReceiptProjectionReady)
            assert isinstance(projected, ReceiptProjectionReady)
            self.assertFalse(projected.pre_anchor_closure_eligible)
            self.assertEqual(projected.evidence_error_code, "closure_denominator_invalid")
            entry = _final_entry(
                fixture,
                receipt_sha256=projected.receipt_document_sha256,
                occurred_at_utc="2026-09-04T03:32:00Z",
            )
            closed = verify_closed_campaign(
                ROOT,
                FinalDocumentBytes(
                    pre_receipt=documents,
                    closure_receipt=_signed_receipt_bytes(fixture, projected),
                    closure_evidence_anchored_ledger_entry=canonical_json_bytes(
                        entry
                    ),
                ),
                external_observation_bundle=_final_observation_bytes(
                    pre_observation,
                    receipt_sha256=projected.receipt_document_sha256,
                    receipt_observed_at_utc="2026-09-04T03:31:30Z",
                    entry=entry,
                    entry_observed_at_utc="2026-09-04T03:33:00Z",
                ),
                artifact_directory=directory,
                postrun_attested_facts=facts,
            )
        self.assertEqual(closed.status, "closed_failure_attested")
        self.assertFalse(closed.evidence_valid)
        self.assertFalse(closed.closure_claim_allowed)

    def test_real_signatures_manifestless_prefix_returns_unsigned_failure_receipt(self) -> None:
        fixture = SyntheticPreReceipt()
        _set_manifest_observation(
            fixture,
            state="absent",
            stage="not_started",
            manifest_sha256=None,
        )
        documents, observation = _inputs(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=Path(temporary),
                postrun_attested_facts=canonical_json_bytes(
                    _postrun(fixture, stage="not_started")
                ),
            )
        self.assertIsInstance(result, ReceiptProjectionReady)
        assert isinstance(result, ReceiptProjectionReady)
        self.assertFalse(result.evidence_valid)
        self.assertEqual(result.evidence_error_code, "closure_bundle_manifest_invalid")
        body = _assert_projection_schema(self, result)
        self.assertEqual(body["bundle_status"], "manifestless_failure")
        self.assertEqual(body["partial_artifacts"], {})
        self.assertEqual(body["closure_reasons"], [])

    def test_real_layer_a_signature_and_layer_b_anchor_chain_closes_failure(self) -> None:
        fixture = SyntheticPreReceipt()
        _set_manifest_observation(
            fixture,
            state="absent",
            stage="not_started",
            manifest_sha256=None,
        )
        documents, observation = _inputs(fixture)
        facts = canonical_json_bytes(_postrun(fixture, stage="not_started"))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            projected = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=directory,
                postrun_attested_facts=facts,
            )
            self.assertIsInstance(projected, ReceiptProjectionReady)
            assert isinstance(projected, ReceiptProjectionReady)
            receipt = _decode_projection(projected)
            receipt["document_sha256"] = projected.receipt_document_sha256
            receipt["signatures"] = fixture._signatures(
                receipt, ("task_custodian",)
            )

            consumed = fixture.documents["authorization_consumed_ledger_entry"]
            final_entry = {
                "schema_version": "provider-completion-external-ledger-entry/1.0",
                "document_type": "completion_telemetry_external_ledger_entry",
                "entry_type": "closure_evidence_anchored",
                "ledger_id": consumed["ledger_id"],
                "sequence": consumed["sequence"] + 1,
                "previous_head_sha256": consumed["resulting_head_sha256"],
                "occurred_at_utc": "2026-09-04T00:00:22Z",
                "candidate_freeze_receipt_sha256": fixture.documents[
                    "candidate_freeze_receipt"
                ]["document_sha256"],
                "preregistration_envelope_sha256": fixture.documents[
                    "preregistration_envelope"
                ]["document_sha256"],
                "authorization_grant_sha256": fixture.documents[
                    "authorization_grant"
                ]["document_sha256"],
                "consumption_receipt_sha256": fixture.documents[
                    "consumption_receipt"
                ]["document_sha256"],
                "closure_receipt_sha256": projected.receipt_document_sha256,
                "entry_sha256": "0" * 64,
                "resulting_head_sha256": "0" * 64,
                "signatures": [],
            }
            final_entry["entry_sha256"] = compute_ledger_entry_sha256(final_entry)
            final_entry["resulting_head_sha256"] = compute_ledger_resulting_head(
                final_entry["previous_head_sha256"], final_entry["entry_sha256"]
            )
            final_entry["signatures"] = fixture._signatures(
                final_entry, ("ledger_witness",)
            )
            final_observation = json.loads(observation)
            final_observation["mode"] = "final"
            final_observation["closure_receipt_observation"] = {
                "document_sha256": projected.receipt_document_sha256,
                "observed_at_utc": "2026-09-04T00:00:21.5Z",
                "observation_source_commitment_sha256": hashlib.sha256(
                    b"observation-source"
                ).hexdigest(),
            }
            final_observation["closure_evidence_anchor"] = _anchor(
                final_entry, "2026-09-04T00:00:23Z"
            )
            closed = verify_closed_campaign(
                ROOT,
                FinalDocumentBytes(
                    pre_receipt=documents,
                    closure_receipt=canonical_json_bytes(receipt),
                    closure_evidence_anchored_ledger_entry=canonical_json_bytes(
                        final_entry
                    ),
                ),
                external_observation_bundle=canonical_json_bytes(final_observation),
                artifact_directory=directory,
                postrun_attested_facts=facts,
            )
        self.assertEqual(closed.status, "closed_failure_attested")
        self.assertFalse(closed.evidence_valid)
        self.assertFalse(closed.closure_claim_allowed)

    def test_manifest_present_invalid_hashes_exact_prefix_and_binds_observation(self) -> None:
        fixture = SyntheticPreReceipt()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            (directory / "completion_closure_manifest.json").write_bytes(
                b'{"status":"malformed"}\n'
            )
            manifest_payload = (directory / "completion_closure_manifest.json").read_bytes()
            manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
            _set_manifest_observation(
                fixture,
                state="present",
                stage="manifest_written",
                manifest_sha256=manifest_hash,
            )
            documents, observation = _inputs(fixture)
            result = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=directory,
                postrun_attested_facts=canonical_json_bytes(_postrun(fixture)),
            )
        self.assertIsInstance(result, ReceiptProjectionReady)
        assert isinstance(result, ReceiptProjectionReady)
        body = _assert_projection_schema(self, result)
        self.assertEqual(body["bundle_status"], "manifest_present_invalid")
        self.assertEqual(result.evidence_error_code, "closure_bundle_manifest_invalid")
        self.assertEqual(
            body["partial_artifacts"]["completion_closure_manifest.json"]["sha256"],
            manifest_hash,
        )

        mismatch = SyntheticPreReceipt()
        _set_manifest_observation(
            mismatch,
            state="present",
            stage="manifest_written",
            manifest_sha256="f" * 64,
        )
        documents, observation = _inputs(mismatch)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            (directory / "completion_closure_manifest.json").write_bytes(
                b'{"status":"malformed"}\n'
            )
            rejected = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=directory,
                postrun_attested_facts=canonical_json_bytes(_postrun(mismatch)),
            )
        self.assertEqual(
            rejected,
            PreReceiptRejected("external_closure_manifest_observation_mismatch"),
        )

    def test_attested_quarantine_has_strict_privacy_priority_and_zero_path_reads(self) -> None:
        fixture = SyntheticPreReceipt()
        _set_manifest_observation(
            fixture,
            state="withheld_quarantined",
            stage="manifest_written",
            manifest_sha256=None,
            quarantine_reason="sensitive_detected",
        )
        documents, observation = _inputs(fixture)
        result = evaluate_closure_bundle(
            ROOT,
            documents,
            external_observation_bundle=observation,
            artifact_directory=_PoisonArtifactDirectory(),  # type: ignore[arg-type]
            admission_evidence=_PoisonArtifactDirectory(),
            postrun_attested_facts=canonical_json_bytes(
                _postrun(
                    fixture,
                    canary_scan="leak_detected",
                    public_scan="unavailable",
                    database_origin="path_preexisted",
                )
            ),
        )
        self.assertIsInstance(result, ReceiptProjectionReady)
        assert isinstance(result, ReceiptProjectionReady)
        body = _assert_projection_schema(self, result)
        self.assertEqual(body["artifact_publication_disposition"], "sensitive_quarantined")
        self.assertEqual(result.evidence_error_code, "closure_sensitive_content_detected")
        self.assertEqual(body["partial_artifacts"], {})
        self.assertIsNone(body["closure_bundle_manifest_sha256"])

        entry = _final_entry(
            fixture,
            receipt_sha256=result.receipt_document_sha256,
            occurred_at_utc="2026-09-04T00:00:22Z",
        )
        closed = verify_closed_campaign(
            ROOT,
            FinalDocumentBytes(
                documents,
                _signed_receipt_bytes(fixture, result),
                canonical_json_bytes(entry),
            ),
            external_observation_bundle=_final_observation_bytes(
                observation,
                receipt_sha256=result.receipt_document_sha256,
                receipt_observed_at_utc="2026-09-04T00:00:21.5Z",
                entry=entry,
                entry_observed_at_utc="2026-09-04T00:00:23Z",
            ),
            artifact_directory=_PoisonArtifactDirectory(),  # type: ignore[arg-type]
            postrun_attested_facts=_PoisonArtifactDirectory(),  # type: ignore[arg-type]
            admission_evidence=_PoisonArtifactDirectory(),
        )
        self.assertEqual(closed.status, "closed_failure_attested")
        self.assertFalse(closed.evidence_valid)
        self.assertFalse(closed.closure_claim_allowed)

    def test_invalid_pre_timeline_rejects_before_artifact_access(self) -> None:
        for field, value in (
            ("completed_at_utc", "2026-09-04T00:00:20.00000000000000001Z"),
            ("task_released_at_utc", "2026-09-04T00:00:16Z"),
            ("provider_key_loaded_at_utc", "2026-09-04T00:00:16Z"),
        ):
            with self.subTest(field=field):
                fixture = SyntheticPreReceipt()
                documents, observation = _inputs(fixture)
                facts = _postrun(fixture)
                facts[field] = value
                result = evaluate_closure_bundle(
                    ROOT,
                    documents,
                    external_observation_bundle=observation,
                    artifact_directory=_PoisonArtifactDirectory(),  # type: ignore[arg-type]
                    postrun_attested_facts=canonical_json_bytes(facts),
                )
                self.assertEqual(
                    result, PreReceiptRejected("external_closure_postrun_facts_timeline_invalid")
                )

    def test_actual_generic_scan_overrides_attested_passed_status(self) -> None:
        fixture = SyntheticPreReceipt()
        _set_manifest_observation(
            fixture,
            state="withheld_quarantined",
            stage="completion_telemetry_written",
            manifest_sha256=None,
            quarantine_reason="sensitive_detected",
        )
        documents, observation = _inputs(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "completion_telemetry_written")
            telemetry_path = directory / "phase6_completion_telemetry.json"
            telemetry_path.write_bytes(telemetry_path.read_bytes() + b"sk-ABCDEFGH")
            result = evaluate_closure_bundle(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=directory,
                postrun_attested_facts=canonical_json_bytes(
                    _postrun(fixture, stage="completion_telemetry_written")
                ),
            )
        self.assertIsInstance(result, ReceiptProjectionReady)
        assert isinstance(result, ReceiptProjectionReady)
        body = _assert_projection_schema(self, result)
        self.assertEqual(body["public_generic_privacy_scan_status"], "sensitive_detected")
        self.assertEqual(result.evidence_error_code, "closure_sensitive_content_detected")

    def test_tampered_pre_receipt_and_invalid_postrun_are_rejected(self) -> None:
        fixture = SyntheticPreReceipt()
        documents, observation = _inputs(fixture)
        candidate = json.loads(documents.candidate_freeze_receipt)
        candidate["repository"] = "tampered/repository"
        tampered = PreReceiptDocumentBytes(
            **{
                name: (
                    canonical_json_bytes(candidate)
                    if name == "candidate_freeze_receipt"
                    else getattr(documents, name)
                )
                for name in documents.__dataclass_fields__
            }
        )
        result = evaluate_closure_bundle(
            ROOT,
            tampered,
            external_observation_bundle=observation,
            artifact_directory=None,
            postrun_attested_facts=canonical_json_bytes(_postrun(fixture)),
        )
        self.assertIsInstance(result, PreReceiptRejected)

        invalid_facts = _postrun(fixture)
        invalid_facts["unexpected"] = True
        rejected = evaluate_closure_bundle(
            ROOT,
            documents,
            external_observation_bundle=observation,
            artifact_directory=None,
            postrun_attested_facts=canonical_json_bytes(invalid_facts),
        )
        self.assertEqual(
            rejected,
            PreReceiptRejected("external_closure_postrun_facts_schema_invalid"),
        )

    def test_public_api_has_no_authority_or_evaluator_injection_surface(self) -> None:
        signature = inspect.signature(evaluate_closure_bundle)
        self.assertEqual(
            tuple(signature.parameters),
            (
                "project_root",
                "documents",
                "external_observation_bundle",
                "artifact_directory",
                "postrun_attested_facts",
                "admission_evidence",
            ),
        )
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(
            {
                "socket",
                "urlopen",
                "create_runtime_completion_binding",
                "RuntimeDenominatorTracker",
                "datetime",
            }.isdisjoint(calls)
        )
        self.assertNotIn("sensitive_canaries", signature.parameters)
        self.assertNotIn("evaluator", signature.parameters)
        self.assertIsNone(signature.parameters["admission_evidence"].default)


if __name__ == "__main__":
    unittest.main()
