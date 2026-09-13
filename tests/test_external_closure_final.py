from __future__ import annotations

import ast
import base64
import copy
import inspect
import json
import unittest
from pathlib import Path
from unittest import mock

import researchops_external_closure.final as final_module
from researchops_external_closure.final import verify_closed_campaign
from researchops_external_closure.primitives import (
    canonical_json_bytes,
    compute_ledger_entry_sha256,
    compute_ledger_resulting_head,
)
from researchops_external_closure.types import (
    FinalDocumentBytes,
    ReceiptProjectionReady,
)
from tests.test_external_closure_documents import SyntheticPreReceipt, _h
from tests.test_provider_completion_external_preregistration_contract import (
    _representative_documents,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_external_closure" / "final.py"


class SyntheticFinalClosure:
    """A fully linked final chain with genuine Ed25519 signatures."""

    def __init__(self) -> None:
        self.pre = SyntheticPreReceipt()
        envelope = self.pre.documents["preregistration_envelope"]
        preregistration_entry = self.pre.documents[
            "preregistration_frozen_ledger_entry"
        ]
        grant = self.pre.documents["authorization_grant"]
        consumption = self.pre.documents["consumption_receipt"]
        consumed_entry = self.pre.documents[
            "authorization_consumed_ledger_entry"
        ]
        custody = envelope["task_custody"]

        self.receipt = copy.deepcopy(
            _representative_documents()["closure_receipt_v1.schema.json"]
        )
        self.receipt.update(
            {
                "receipt_id": "PCECLOSE-0123456789ABCDEF0123456789ABCDEF",
                "campaign_id": envelope["runtime_plan"]["campaign_topology"][
                    "campaign_id"
                ],
                "completed_at_utc": "2026-09-04T00:00:30Z",
                "receipt_issued_at_utc": "2026-09-04T00:00:31Z",
                "preregistration_envelope_sha256": envelope["document_sha256"],
                "preregistration_freeze_entry_sha256": preregistration_entry[
                    "entry_sha256"
                ],
                "authorization_grant_sha256": grant["document_sha256"],
                "consumption_receipt_sha256": consumption["document_sha256"],
                "authorization_consumption_entry_sha256": consumed_entry[
                    "entry_sha256"
                ],
                "task_released_at_utc": "2026-09-04T00:00:17Z",
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
                "closure_bundle_manifest_sha256": self.pre.observation[
                    "manifest_observation"
                ]["manifest_sha256"],
                "planned_case_count": custody["planned_case_count"],
            }
        )
        self._seal_receipt()

        self.final_entry = self.pre._ledger_entry(
            entry_type="closure_evidence_anchored",
            sequence=consumed_entry["sequence"] + 1,
            previous_head=consumed_entry["resulting_head_sha256"],
            occurred_at="2026-09-04T00:00:32Z",
            candidate_hash=self.pre.documents["candidate_freeze_receipt"][
                "document_sha256"
            ],
            envelope_hash=envelope["document_sha256"],
            grant_hash=grant["document_sha256"],
            consumption_hash=consumption["document_sha256"],
        )
        self._seal_final_entry()

        self.observation = copy.deepcopy(self.pre.observation)
        self.observation["mode"] = "final"
        self.observation["manifest_observation"]["observed_at_utc"] = (
            "2026-09-04T00:00:30.5Z"
        )
        self._refresh_final_observation()

    def _seal_receipt(self) -> None:
        self.receipt["document_sha256"] = "0" * 64
        self.receipt["signatures"] = []
        self.pre._finalize_document(self.receipt, ("task_custodian",))

    def _seal_final_entry(self) -> None:
        self.final_entry["closure_receipt_sha256"] = self.receipt[
            "document_sha256"
        ]
        self.final_entry["entry_sha256"] = compute_ledger_entry_sha256(
            self.final_entry
        )
        self.final_entry["resulting_head_sha256"] = (
            compute_ledger_resulting_head(
                self.final_entry["previous_head_sha256"],
                self.final_entry["entry_sha256"],
            )
        )
        self.final_entry["signatures"] = self.pre._signatures(
            self.final_entry, ("ledger_witness",)
        )

    def _refresh_final_observation(self) -> None:
        self.observation["closure_receipt_observation"] = {
            "document_sha256": self.receipt["document_sha256"],
            "observed_at_utc": "2026-09-04T00:00:31.5Z",
            "observation_source_commitment_sha256": _h("receipt-observer"),
        }
        self.observation["closure_evidence_anchor"] = {
            "entry_sha256": self.final_entry["entry_sha256"],
            "ledger_id": self.final_entry["ledger_id"],
            "entry_type": self.final_entry["entry_type"],
            "sequence": self.final_entry["sequence"],
            "previous_head_sha256": self.final_entry[
                "previous_head_sha256"
            ],
            "resulting_head_sha256": self.final_entry[
                "resulting_head_sha256"
            ],
            "entry_occurred_at_utc": self.final_entry["occurred_at_utc"],
            "observed_at_utc": "2026-09-04T00:00:33Z",
            "observation_source_commitment_sha256": _h("final-observer"),
        }

    def reseal_chain(self) -> None:
        self._seal_receipt()
        self._seal_final_entry()
        self._refresh_final_observation()

    def inputs(self) -> tuple[FinalDocumentBytes, bytes]:
        pre_documents, _unused_pre_observation = self.pre.inputs()
        return (
            FinalDocumentBytes(
                pre_documents,
                canonical_json_bytes(self.receipt),
                canonical_json_bytes(self.final_entry),
            ),
            canonical_json_bytes(self.observation),
        )

    def ready(self) -> ReceiptProjectionReady:
        body = dict(self.receipt)
        body.pop("document_sha256")
        body.pop("signatures")
        return ReceiptProjectionReady(
            canonical_json_bytes(body),
            self.receipt["document_sha256"],
            self.receipt["evidence_valid"],
            self.receipt["pre_anchor_closure_eligible"],
            self.receipt["evidence_error_code"],
            tuple(self.receipt["closure_reasons"]),
        )


class RecordingEvaluator:
    """Layer-B unit-test stub, not a verified execution proof or live fixture."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self.result


class PoisonArtifactPath:
    """Any attempt to treat this object as a path is a test failure."""

    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError("quarantine_artifact_path_was_touched")

    def __fspath__(self) -> str:
        raise AssertionError("quarantine_artifact_path_was_read")


class PoisonPostrunFacts:
    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError("quarantine_postrun_facts_were_touched")

    def __bytes__(self) -> bytes:
        raise AssertionError("quarantine_postrun_facts_were_read")


class ExternalClosureFinalVerifierTests(unittest.TestCase):
    def verify(
        self,
        fixture: SyntheticFinalClosure,
        evaluator: object,
        *,
        artifact_directory: object = None,
        postrun_attested_facts: object = b"postrun-facts-forwarded-verbatim",
        admission_evidence: object = None,
    ):
        documents, observation = fixture.inputs()
        with mock.patch.object(
            final_module, "_default_rerun_evaluator", evaluator
        ):
            return verify_closed_campaign(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=artifact_directory,  # type: ignore[arg-type]
                postrun_attested_facts=postrun_attested_facts,  # type: ignore[arg-type]
                admission_evidence=admission_evidence,
            )

    def test_isolated_layer_b_forwards_the_same_admission_input_only_for_normal_publication(self) -> None:
        fixture = SyntheticFinalClosure()
        evaluator = RecordingEvaluator(fixture.ready())
        admission = object()
        result = self.verify(fixture, evaluator, admission_evidence=admission)
        self.assertEqual(result.status, "closed_evidence_verified")
        self.assertIs(evaluator.calls[0][1]["admission_evidence"], admission)

    def test_layer_b_terminal_truth_table_with_isolated_layer_a_stub(self) -> None:
        fixture = SyntheticFinalClosure()
        evaluator = RecordingEvaluator(fixture.ready())
        result = self.verify(fixture, evaluator, artifact_directory=ROOT / "artifacts")

        self.assertEqual(result.status, "closed_evidence_verified")
        self.assertTrue(result.evidence_valid)
        self.assertTrue(result.closure_claim_allowed)
        self.assertEqual(
            result.receipt_document_sha256,
            fixture.receipt["document_sha256"],
        )
        self.assertEqual(
            result.final_ledger_entry_sha256, fixture.final_entry["entry_sha256"]
        )
        self.assertEqual(len(evaluator.calls), 1)
        args, kwargs = evaluator.calls[0]
        self.assertEqual(args[:2], (ROOT, fixture.inputs()[0].pre_receipt))
        projected_observation = json.loads(kwargs["external_observation_bundle"])
        self.assertEqual(projected_observation["mode"], "pre_receipt")
        self.assertIsNone(projected_observation["closure_receipt_observation"])
        self.assertIsNone(projected_observation["closure_evidence_anchor"])
        self.assertEqual(kwargs["artifact_directory"], ROOT / "artifacts")
        self.assertEqual(
            kwargs["postrun_attested_facts"],
            b"postrun-facts-forwarded-verbatim",
        )
        self.assertNotIn("sensitive_canaries", kwargs)

    def test_receipt_strict_schema_hash_and_signature_fail_closed(self) -> None:
        schema_fixture = SyntheticFinalClosure()
        schema_fixture.receipt["unexpected"] = False
        schema_fixture.reseal_chain()
        result = self.verify(
            schema_fixture, RecordingEvaluator(schema_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_receipt_schema_invalid")

        tamper_fixture = SyntheticFinalClosure()
        tamper_fixture.receipt["campaign_id"] = (
            "PCECAMP-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        )
        result = self.verify(
            tamper_fixture, RecordingEvaluator(tamper_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_receipt_signature_invalid")

        signature_fixture = SyntheticFinalClosure()
        signature = signature_fixture.receipt["signatures"][0]
        raw = bytearray(base64.b64decode(signature["signature_b64"]))
        raw[-1] ^= 1
        signature["signature_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        result = self.verify(
            signature_fixture, RecordingEvaluator(signature_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_receipt_signature_invalid")

        expired_fixture = SyntheticFinalClosure()
        expired_fixture.receipt["receipt_issued_at_utc"] = "2026-09-06T00:00:00Z"
        expired_fixture.reseal_chain()
        result = self.verify(
            expired_fixture, RecordingEvaluator(expired_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_receipt_signature_invalid")

    def test_coordinated_receipt_and_layer_a_projection_tamper_are_rejected(
        self,
    ) -> None:
        cross_fixture = SyntheticFinalClosure()
        cross_fixture.receipt["campaign_id"] = (
            "PCECAMP-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        )
        cross_fixture.reseal_chain()
        evaluator = RecordingEvaluator(cross_fixture.ready())
        result = self.verify(cross_fixture, evaluator)
        self.assertEqual(result.error_code, "closure_receipt_projection_mismatch")
        self.assertEqual(len(evaluator.calls), 0)

        rerun_fixture = SyntheticFinalClosure()
        wrong = rerun_fixture.ready()
        wrong_body = bytearray(wrong.unsigned_receipt_canonical_json)
        wrong_body[-1] ^= 1
        mismatched = ReceiptProjectionReady(
            bytes(wrong_body),
            wrong.receipt_document_sha256,
            wrong.evidence_valid,
            wrong.pre_anchor_closure_eligible,
            wrong.evidence_error_code,
            wrong.closure_reasons,
        )
        evaluator = RecordingEvaluator(mismatched)
        result = self.verify(rerun_fixture, evaluator)
        self.assertEqual(result.error_code, "closure_receipt_projection_mismatch")
        self.assertEqual(len(evaluator.calls), 1)
        self.assertFalse(result.closure_claim_allowed)

    def test_normal_rerun_invalid_evidence_is_closed_failure_attested(self) -> None:
        fixture = SyntheticFinalClosure()
        fixture.receipt.update(
            {
                "bundle_status": "manifest_present_invalid",
                "closure_bundle_manifest_sha256": None,
                "partial_artifacts": {
                    name: {"bytes": index + 1, "sha256": _h(name)}
                    for index, name in enumerate(
                        (
                            "phase6_audit.sqlite3",
                            "phase6_audit_index.json",
                            "phase6_completion_telemetry.json",
                            "completion_closure_manifest.json",
                        )
                    )
                },
                "evidence_valid": False,
                "evidence_error_code": "closure_bundle_manifest_invalid",
                "pre_anchor_closure_eligible": False,
                "closure_reasons": [],
                "observed_audit_run_set_commitment_sha256": None,
                "ordered_final_chain_heads_commitment_sha256": None,
                "attempt_count": None,
                "accepted_response_count": None,
                "usage_complete": None,
                "observed_input_tokens": None,
                "observed_output_tokens": None,
                "observed_cost_cny": None,
            }
        )
        fixture.reseal_chain()
        evaluator = RecordingEvaluator(fixture.ready())
        result = self.verify(fixture, evaluator)
        self.assertEqual(result.error_code, "closure_receipt_projection_mismatch")
        self.assertEqual(evaluator.calls, [])
        fixture.observation["manifest_observation"]["manifest_sha256"] = _h(
            "completion_closure_manifest.json"
        )
        result = self.verify(fixture, evaluator)
        self.assertEqual(result.status, "closed_failure_attested")
        self.assertFalse(result.evidence_valid)
        self.assertFalse(result.closure_claim_allowed)
        self.assertEqual(len(evaluator.calls), 1)

    def test_signed_receipt_requires_exactly_all_sixty_frozen_fields(self) -> None:
        fixture = SyntheticFinalClosure()
        schemas = final_module._load_schemas(ROOT)
        self.assertEqual(len(fixture.receipt), 60)
        for field in fixture.receipt:
            with self.subTest(field=field):
                missing = dict(fixture.receipt)
                del missing[field]
                with self.assertRaises(final_module._FinalVerificationFailure) as caught:
                    final_module._decode_schema_document(
                        canonical_json_bytes(missing),
                        schemas=schemas,
                        schema_name="closure_receipt_v1.schema.json",
                        error_code="closure_receipt_schema_invalid",
                    )
                self.assertEqual(caught.exception.code, "closure_receipt_schema_invalid")

    def test_final_ledger_signature_history_and_external_anchor_are_exact(self) -> None:
        signature_fixture = SyntheticFinalClosure()
        signature = signature_fixture.final_entry["signatures"][0]
        raw = bytearray(base64.b64decode(signature["signature_b64"]))
        raw[0] ^= 1
        signature["signature_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        result = self.verify(
            signature_fixture, RecordingEvaluator(signature_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_external_anchor_mismatch")

        history_fixture = SyntheticFinalClosure()
        history_fixture.final_entry["authorization_grant_sha256"] = _h(
            "coordinated-wrong-grant"
        )
        history_fixture._seal_final_entry()
        history_fixture._refresh_final_observation()
        result = self.verify(
            history_fixture, RecordingEvaluator(history_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_external_anchor_mismatch")

        anchor_fixture = SyntheticFinalClosure()
        anchor_fixture.observation["closure_evidence_anchor"]["entry_sha256"] = _h(
            "different-final-entry"
        )
        result = self.verify(
            anchor_fixture, RecordingEvaluator(anchor_fixture.ready())
        )
        self.assertEqual(result.error_code, "closure_external_anchor_mismatch")

    def test_quarantine_is_closed_failure_and_never_touches_artifact_path(self) -> None:
        fixture = SyntheticFinalClosure()
        fixture.receipt.update(
            {
                "artifact_publication_disposition": "sensitive_quarantined",
                "closure_bundle_manifest_sha256": None,
                "evidence_valid": False,
                "evidence_error_code": "closure_sensitive_content_detected",
                "pre_anchor_closure_eligible": False,
                "closure_reasons": [],
                "observed_audit_run_set_commitment_sha256": None,
                "ordered_final_chain_heads_commitment_sha256": None,
                "attempt_count": None,
                "accepted_response_count": None,
                "usage_complete": None,
                "observed_input_tokens": None,
                "observed_output_tokens": None,
                "observed_cost_cny": None,
                "custodian_private_leak_canary_scan_status": "leak_detected",
            }
        )
        fixture.observation["manifest_observation"] = {
            "state": "withheld_quarantined",
            "manifest_sha256": None,
            "observed_at_utc": "2026-09-04T00:00:30.5Z",
            "observation_source_commitment_sha256": _h("quarantine-observer"),
            "last_completed_artifact_write_stage": "manifest_written",
            "quarantine_reason": "sensitive_detected",
        }
        fixture.reseal_chain()
        evaluator = RecordingEvaluator(AssertionError("must not be returned"))
        result = self.verify(
            fixture,
            evaluator,
            artifact_directory=PoisonArtifactPath(),
            postrun_attested_facts=PoisonPostrunFacts(),
            admission_evidence=PoisonPostrunFacts(),
        )
        self.assertEqual(result.status, "closed_failure_attested")
        self.assertFalse(result.evidence_valid)
        self.assertFalse(result.closure_claim_allowed)
        self.assertIsNone(result.error_code)
        self.assertEqual(evaluator.calls, [])

        # Quarantine validates the same public post-run timeline as layer A,
        # even though it must not inspect the unavailable artifact/facts inputs.
        fixture.observation["manifest_observation"]["observed_at_utc"] = (
            "2026-09-04T00:00:29.999999999999999999Z"
        )
        result = self.verify(
            fixture,
            evaluator,
            artifact_directory=PoisonArtifactPath(),
            postrun_attested_facts=PoisonPostrunFacts(),
        )
        self.assertEqual(result.status, "unclosed_invalid")
        self.assertEqual(result.error_code, "closure_receipt_projection_mismatch")
        self.assertEqual(evaluator.calls, [])

    def test_final_mode_and_timeline_are_required_before_rerun(self) -> None:
        fixture = SyntheticFinalClosure()
        fixture.observation["closure_receipt_observation"]["observed_at_utc"] = (
            "2026-09-04T00:00:32.1Z"
        )
        evaluator = RecordingEvaluator(fixture.ready())
        result = self.verify(fixture, evaluator)
        self.assertEqual(result.error_code, "closure_external_anchor_mismatch")
        self.assertEqual(evaluator.calls, [])

    def test_api_and_module_have_no_runtime_provider_network_signer_or_clock(
        self,
    ) -> None:
        self.assertEqual(
            tuple(inspect.signature(verify_closed_campaign).parameters),
            (
                "project_root",
                "documents",
                "external_observation_bundle",
                "artifact_directory",
                "postrun_attested_facts",
                "admission_evidence",
            ),
        )
        fixture = SyntheticFinalClosure()
        documents, observation = fixture.inputs()
        with self.assertRaisesRegex(TypeError, "rerun_evaluator"):
            verify_closed_campaign(
                ROOT,
                documents,
                external_observation_bundle=observation,
                artifact_directory=None,
                postrun_attested_facts=b"{}",
                rerun_evaluator=RecordingEvaluator(fixture.ready()),  # type: ignore[call-arg]
            )
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            imports.intersection(
                {
                    "socket",
                    "subprocess",
                    "urllib.request",
                    "httpx",
                    "openai",
                    "agents",
                    "researchops.model_providers",
                    "researchops.audit",
                }
            )
        )
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertFalse(
            attributes.intersection(
                {
                    "write_text",
                    "write_bytes",
                    "mkdir",
                    "unlink",
                    "environ",
                    "now",
                    "utcnow",
                    "sign",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
