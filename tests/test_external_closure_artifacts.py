from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_external_closure.artifacts import (
    ArtifactBundleSummary,
    ArtifactFileCommitment,
    VerifiedPostrunFacts,
    validate_postrun_attested_facts,
    verify_artifact_bundle,
)
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from researchops_external_closure.primitives import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = (
    ROOT / "evals" / "provider_completion_external_preregistration_v1" / "schemas"
)
MODULE = ROOT / "src" / "researchops_external_closure" / "artifacts.py"
SCHEMA_COMMITMENT = (
    "c0df1f54a96fe3c4f5ee196e654203a62e5e775a3e21fd5f47ace9e349169385"
)
CLOSURE_EVIDENCE_COMMITMENT = (
    "a96b39a1f4bafa3bcf512fd88a5e7b0ca3987ca2bc9804642a1d1259cbf8cad0"
)


def _h(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _schemas() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_ROOT.glob("*.schema.json")
    }


def _postrun(*, stage: str = "manifest_written") -> dict:
    artifact_error = {
        "not_started": "pce_artifact_not_started_failed",
        "audit_database_written": "pce_artifact_audit_index_failed",
        "audit_index_written": "pce_artifact_completion_telemetry_failed",
        "completion_telemetry_written": "pce_artifact_manifest_failed",
        "manifest_written": None,
    }[stage]
    return {
        "schema_version": "provider-completion-postrun-attested-facts/1.0",
        "campaign_id": "PCECAMP-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "receipt_id": "PCECLOSE-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "completed_at_utc": "2026-09-04T03:30:00Z",
        "receipt_issued_at_utc": "2026-09-04T03:31:00Z",
        "terminal_stage": "none",
        "local_terminal_outcome": "succeeded",
        "terminal_provider_outcome_state": "response_observed",
        "terminal_error_code": None,
        "last_completed_artifact_write_stage": stage,
        "artifact_error_code": artifact_error,
        "task_released": True,
        "task_released_at_utc": "2026-09-04T03:00:00Z",
        "provider_key_loaded": True,
        "provider_key_loaded_at_utc": "2026-09-04T03:00:01Z",
        "released_task_bundle_commitment_sha256": _h("task-bundle"),
        "released_case_handle_order_commitment_sha256": _h("case-order"),
        "released_execution_input_order_commitment_sha256": _h(
            "execution-order"
        ),
        "task_bundle_opening_verified_in_memory": True,
        "task_bundle_opening_persisted": False,
        "custodian_private_leak_canary_scan_status": "passed",
        "public_generic_privacy_scan_status": "passed",
        "canary_injection_verified_for_every_send": True,
        "database_origin_status": "fresh_path_confirmed",
        "database_mutation_status": "normative_only",
        "network_attempt_count_observed": 2,
        "model_request_count_observed": 2,
        "raw_response_cleanup_status": "completed",
        "post_request_cleanup_status": "completed",
        "provider_key_reference_release_status": "released",
        "task_content_included_in_facts": False,
        "provider_content_included_in_facts": False,
        "derived_closure_claim_included": False,
    }


def _facts(document: dict | None = None) -> VerifiedPostrunFacts:
    value = _postrun() if document is None else document
    return validate_postrun_attested_facts(
        canonical_json_bytes(value),
        schemas=_schemas(),
    )


def _canonical_file(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _audit_index() -> dict:
    return {"schema_version": "1.1", "runs": []}


def _telemetry() -> dict:
    return {
        "schema_version": "phase6-completion-telemetry/1.0",
        "status": "recorded",
        "runtime_plan": {},
        "runtime_denominator": {},
        "ledger_reconciliation": {},
        "closure": {},
        "historical_backfill_performed": False,
    }


def _write_prefix(directory: Path, stage: str) -> None:
    stages = {
        "not_started": 0,
        "audit_database_written": 1,
        "audit_index_written": 2,
        "completion_telemetry_written": 3,
        "manifest_written": 4,
    }
    count = stages[stage]
    if count >= 1:
        AuditLedger(directory / "phase6_audit.sqlite3")
    if count >= 2:
        (directory / "phase6_audit_index.json").write_bytes(
            _canonical_file(_audit_index())
        )
    if count >= 3:
        (directory / "phase6_completion_telemetry.json").write_bytes(
            _canonical_file(_telemetry())
        )
    if count >= 4:
        _write_manifest(directory)


def _manifest(directory: Path) -> dict:
    names = (
        "phase6_audit.sqlite3",
        "phase6_audit_index.json",
        "phase6_completion_telemetry.json",
    )
    return {
        "schema_version": "provider-completion-closure-bundle-manifest/1.0",
        "status": "complete",
        "campaign_id": "PCECAMP-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "preregistration_envelope_sha256": _h("envelope"),
        "preregistration_freeze_entry_sha256": _h("prereg-entry"),
        "authorization_grant_sha256": _h("grant"),
        "consumption_receipt_sha256": _h("consumption"),
        "closure_evidence_contract_commitment_sha256": (
            CLOSURE_EVIDENCE_COMMITMENT
        ),
        "denominator_plan_commitment_sha256": _h("denominator"),
        "external_plan_binding_sha256": _h("plan"),
        "audit_database_schema_commitment_sha256": SCHEMA_COMMITMENT,
        "files": {
            name: {
                "bytes": len(payload := (directory / name).read_bytes()),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name in names
        },
        "manifest_self_hash_included": False,
        "raw_provider_content_persisted": False,
        "task_content_persisted": False,
        "api_key_persisted": False,
    }


def _write_manifest(directory: Path) -> dict:
    value = _manifest(directory)
    (directory / "completion_closure_manifest.json").write_bytes(
        _canonical_file(value)
    )
    return value


class ExternalClosurePostrunFactsTests(unittest.TestCase):
    def test_authoritative_frozen_schema_map_is_accepted_without_mutation(self) -> None:
        from researchops.provider_completion_external_contract import (
            load_frozen_external_preregistration_contract,
        )

        contract = load_frozen_external_preregistration_contract(ROOT)
        schemas = contract["schemas"]
        required = schemas["postrun_attested_facts_v1.schema.json"]["required"]
        self.assertIsInstance(required, tuple)
        facts = validate_postrun_attested_facts(
            canonical_json_bytes(_postrun()), schemas=schemas
        )
        self.assertEqual(facts.campaign_id, _postrun()["campaign_id"])
        self.assertIs(
            schemas["postrun_attested_facts_v1.schema.json"]["required"], required
        )
        with self.assertRaises(TypeError):
            schemas["postrun_attested_facts_v1.schema.json"]["required"] = []

    def test_strict_schema_timeline_and_frozen_verified_value(self) -> None:
        payload = canonical_json_bytes(_postrun())
        result = validate_postrun_attested_facts(payload, schemas=_schemas())
        self.assertEqual(result.document_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.last_completed_artifact_write_stage, "manifest_written")
        with self.assertRaises(FrozenInstanceError):
            result.campaign_id = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            VerifiedPostrunFacts()

        extra = _postrun()
        extra["derived_result"] = True
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError,
            "external_closure_postrun_facts_schema_invalid",
        ):
            _facts(extra)

        for completed, issued in (
            ("2026-09-04T03:31:00Z", "2026-09-04T03:31:00Z"),
            ("2026-09-04T03:31:01Z", "2026-09-04T03:31:00Z"),
        ):
            value = _postrun()
            value["completed_at_utc"] = completed
            value["receipt_issued_at_utc"] = issued
            with self.subTest(completed=completed):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_postrun_facts_timeline_invalid",
                ):
                    _facts(value)

    def test_duplicate_keys_invalid_utf8_and_remote_schema_refs_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError, "external_closure_json_duplicate_key"
        ):
            validate_postrun_attested_facts(
                b'{"schema_version":"x","schema_version":"y"}',
                schemas=_schemas(),
            )
        with self.assertRaisesRegex(
            ExternalClosurePrimitiveError, "external_closure_json_utf8_invalid"
        ):
            validate_postrun_attested_facts(b"\xff", schemas=_schemas())
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            hostile = _schemas()
            hostile["postrun_attested_facts_v1.schema.json"] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                keyword: "https://attacker.invalid/schema.json",
            }
            with self.subTest(keyword=keyword), patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("schema retrieval attempted"),
            ) as retrieve:
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_schema_mapping_invalid",
                ):
                    validate_postrun_attested_facts(
                        canonical_json_bytes(_postrun()), schemas=hostile
                    )
                retrieve.assert_not_called()


class ExternalClosureArtifactBundleTests(unittest.TestCase):
    def test_all_exact_writer_prefixes_are_derived_from_attested_stage(self) -> None:
        stages = (
            ("not_started", 0),
            ("audit_database_written", 1),
            ("audit_index_written", 2),
            ("completion_telemetry_written", 3),
        )
        for stage, count in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                _write_prefix(directory, stage)
                result = verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(_postrun(stage=stage)),
                    schemas=_schemas(),
                )
                self.assertEqual(result.bundle_status, "manifestless_failure")
                self.assertEqual(result.artifact_count, count)
                self.assertEqual(len(result.file_commitments), count)
                self.assertEqual(
                    [item.name for item in result.file_commitments],
                    [
                        "phase6_audit.sqlite3",
                        "phase6_audit_index.json",
                        "phase6_completion_telemetry.json",
                    ][:count],
                )
                self.assertIsNone(result.manifest_sha256)
                self.assertEqual(
                    result.audit_database_schema_verified,
                    count >= 1,
                )

    def test_complete_manifest_hashes_exact_bytes_and_database_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            before = (directory / "phase6_audit.sqlite3").read_bytes()
            with patch(
                "researchops_external_closure.artifacts.sqlite3.connect",
                wraps=sqlite3.connect,
            ) as connect:
                result = verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(),
                    schemas=_schemas(),
                )
            self.assertEqual(result.bundle_status, "complete")
            self.assertEqual(result.artifact_publication_disposition, "normal")
            self.assertEqual(result.artifact_count, 4)
            self.assertEqual(len(result.file_commitments), 4)
            self.assertEqual(result.public_generic_privacy_scan_status, "passed")
            self.assertEqual(
                result.manifest_sha256,
                hashlib.sha256(
                    (directory / "completion_closure_manifest.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                result.audit_database_schema_commitment_sha256,
                SCHEMA_COMMITMENT,
            )
            self.assertEqual(result.audit_database_schema_object_count, 11)
            self.assertTrue(result.audit_database_schema_verified)
            self.assertTrue(result.audit_database_encoding_utf8)
            self.assertTrue(result.audit_database_freelist_empty)
            self.assertEqual(
                before, (directory / "phase6_audit.sqlite3").read_bytes()
            )
            uri = connect.call_args.args[0]
            self.assertIn("mode=ro", uri)
            self.assertTrue(connect.call_args.kwargs["uri"])
            self.assertFalse(
                any(path.name.endswith(("-wal", "-shm")) for path in directory.iterdir())
            )
            with self.assertRaises(FrozenInstanceError):
                result.bundle_status = "changed"  # type: ignore[misc]
            with self.assertRaises(TypeError):
                ArtifactBundleSummary()
            with self.assertRaises(TypeError):
                ArtifactFileCommitment()

    def test_manifest_present_invalid_is_safe_raw_commitment_representation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            manifest_path = directory / "completion_closure_manifest.json"
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_path.write_text(
                json.dumps(value, indent=2), encoding="utf-8", newline="\n"
            )
            result = verify_artifact_bundle(
                directory,
                postrun_facts=_facts(),
                schemas=_schemas(),
            )
            self.assertEqual(result.bundle_status, "manifest_present_invalid")
            self.assertEqual(result.artifact_count, 4)
            self.assertEqual(len(result.file_commitments), 4)
            self.assertIsNone(result.manifest_sha256)
            self.assertEqual(
                result.file_commitments[-1].sha256,
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            )

    def test_valid_manifest_with_wrong_file_commitment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            value = _manifest(directory)
            value["files"]["phase6_audit_index.json"]["sha256"] = _h("wrong")
            (directory / "completion_closure_manifest.json").write_bytes(
                _canonical_file(value)
            )
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_bundle_hash_mismatch",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(),
                    schemas=_schemas(),
                )

    def test_schema_valid_manifest_binding_drift_is_not_schema_invalid(self) -> None:
        replacements = {
            "campaign_id": "PCECAMP-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            "closure_evidence_contract_commitment_sha256": _h("other-contract"),
            "audit_database_schema_commitment_sha256": _h("other-schema"),
        }
        for field, replacement in replacements.items():
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                _write_prefix(directory, "manifest_written")
                manifest = _manifest(directory)
                manifest[field] = replacement
                (directory / "completion_closure_manifest.json").write_bytes(
                    _canonical_file(manifest)
                )
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "^external_closure_bundle_hash_mismatch$",
                ):
                    verify_artifact_bundle(
                        directory,
                        postrun_facts=_facts(),
                        schemas=_schemas(),
                    )

    def test_manifest_and_hash_priority_precede_audit_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            database = directory / "phase6_audit.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE unexpected(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            manifest_path = directory / "completion_closure_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_path.write_text(
                json.dumps(manifest, indent=2), encoding="utf-8", newline="\n"
            )
            result = verify_artifact_bundle(
                directory,
                postrun_facts=_facts(),
                schemas=_schemas(),
            )
            self.assertEqual(result.bundle_status, "manifest_present_invalid")
            self.assertFalse(result.audit_database_schema_verified)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            database = directory / "phase6_audit.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE unexpected(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            # Keep the manifest canonical but make one recorded commitment
            # independently wrong; this must win over the DB schema defect.
            manifest = _manifest(directory)
            manifest["files"]["phase6_audit_index.json"]["sha256"] = _h(
                "wrong-index"
            )
            (directory / "completion_closure_manifest.json").write_bytes(
                _canonical_file(manifest)
            )
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_bundle_hash_mismatch",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(),
                    schemas=_schemas(),
                )

    def test_sqlite_queries_are_bound_to_the_committed_database_bytes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as alternate_temporary,
        ):
            directory = Path(temporary)
            _write_prefix(directory, "audit_database_written")
            committed = directory / "phase6_audit.sqlite3"
            connection = sqlite3.connect(committed)
            try:
                connection.execute("CREATE TABLE unexpected(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            alternate = Path(alternate_temporary) / "alternate.sqlite3"
            AuditLedger(alternate)
            real_connect = sqlite3.connect

            def connect_alternate(*_args, **kwargs):
                return real_connect(
                    alternate.absolute().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                    timeout=kwargs.get("timeout", 1.0),
                )

            with patch(
                "researchops_external_closure.artifacts.sqlite3.connect",
                side_effect=connect_alternate,
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_bundle_hash_mismatch",
                ):
                    verify_artifact_bundle(
                        directory,
                        postrun_facts=_facts(
                            _postrun(stage="audit_database_written")
                        ),
                        schemas=_schemas(),
                    )

    def test_unserializable_swapped_database_is_a_bundle_hash_mismatch(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as alternate_temporary,
        ):
            directory = Path(temporary)
            _write_prefix(directory, "audit_database_written")
            alternate = Path(alternate_temporary) / "empty.sqlite3"
            alternate.write_bytes(b"")
            real_connect = sqlite3.connect

            def connect_alternate(*_args, **kwargs):
                return real_connect(
                    alternate.absolute().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                    timeout=kwargs.get("timeout", 1.0),
                )

            with patch(
                "researchops_external_closure.artifacts.sqlite3.connect",
                side_effect=connect_alternate,
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_bundle_hash_mismatch",
                ):
                    verify_artifact_bundle(
                        directory,
                        postrun_facts=_facts(
                            _postrun(stage="audit_database_written")
                        ),
                        schemas=_schemas(),
                    )

    def test_audit_schema_priority_precedes_index_and_outer_telemetry(self) -> None:
        for target in (
            "phase6_audit_index.json",
            "phase6_completion_telemetry.json",
        ):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                _write_prefix(directory, "manifest_written")
                database = directory / "phase6_audit.sqlite3"
                connection = sqlite3.connect(database)
                try:
                    connection.execute("CREATE TABLE unexpected(value TEXT)")
                    connection.commit()
                finally:
                    connection.close()
                if target == "phase6_audit_index.json":
                    (directory / target).write_bytes(b'{ "runs": [] }\n')
                else:
                    telemetry = _telemetry()
                    telemetry["unexpected"] = True
                    (directory / target).write_bytes(_canonical_file(telemetry))
                # Make the manifest canonical and exactly bind both defects;
                # the next gate must therefore be audit schema.
                _write_manifest(directory)
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_audit_schema_invalid",
                ):
                    verify_artifact_bundle(
                        directory,
                        postrun_facts=_facts(),
                        schemas=_schemas(),
                    )

    def test_index_telemetry_and_directory_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "audit_index_written")
            (directory / "phase6_audit_index.json").write_bytes(b'{ "runs": [] }\n')
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_audit_index_invalid",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(_postrun(stage="audit_index_written")),
                    schemas=_schemas(),
                )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "completion_telemetry_written")
            telemetry = _telemetry()
            telemetry["extra"] = False
            (directory / "phase6_completion_telemetry.json").write_bytes(
                _canonical_file(telemetry)
            )
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_completion_telemetry_invalid",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(
                        _postrun(stage="completion_telemetry_written")
                    ),
                    schemas=_schemas(),
                )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "audit_database_written")
            (directory / "phase6_audit.sqlite3-wal").write_bytes(b"sidecar")
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_directory_file_set_invalid",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(
                        _postrun(stage="audit_database_written")
                    ),
                    schemas=_schemas(),
                )

    def test_database_schema_freelist_and_hardlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "audit_database_written")
            database = directory / "phase6_audit.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE unexpected(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_audit_schema_invalid",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(
                        _postrun(stage="audit_database_written")
                    ),
                    schemas=_schemas(),
                )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "audit_database_written")
            database = directory / "phase6_audit.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE transient(value BLOB)")
                connection.executemany(
                    "INSERT INTO transient(value) VALUES (?)",
                    [(b"x" * 4096,)] * 100,
                )
                connection.execute("DROP TABLE transient")
                connection.commit()
            finally:
                connection.close()
            self.assertGreater(
                sqlite3.connect(database).execute("PRAGMA freelist_count").fetchone()[0],
                0,
            )
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_audit_schema_invalid",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(
                        _postrun(stage="audit_database_written")
                    ),
                    schemas=_schemas(),
                )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.sqlite3"
            AuditLedger(source)
            linked = directory / "phase6_audit.sqlite3"
            try:
                os.link(source, linked)
            except OSError:
                self.skipTest("hardlink creation unavailable")
            source.unlink()
            # The remaining link has count one after unlink, so keep both links
            # when exercising the artifact reader.
            os.link(linked, source)
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError,
                "external_closure_(?:directory_file_set|file_identity)_invalid",
            ):
                verify_artifact_bundle(
                    directory,
                    postrun_facts=_facts(
                        _postrun(stage="audit_database_written")
                    ),
                    schemas=_schemas(),
                )

    def test_attested_quarantine_never_touches_poison_artifact_argument(self) -> None:
        class PoisonArtifactPath:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(f"artifact path was touched: {name}")

        cases = (
            (
                "custodian_private_leak_canary_scan_status",
                "leak_detected",
                "sensitive_quarantined",
            ),
            (
                "public_generic_privacy_scan_status",
                "unavailable",
                "privacy_unverified_quarantined",
            ),
            (
                "database_origin_status",
                "path_preexisted",
                "privacy_unverified_quarantined",
            ),
            (
                "database_mutation_status",
                "reuse_or_delete_detected",
                "privacy_unverified_quarantined",
            ),
        )
        for field, value, expected in cases:
            document = _postrun()
            document[field] = value
            with self.subTest(field=field):
                result = verify_artifact_bundle(
                    PoisonArtifactPath(),  # type: ignore[arg-type]
                    postrun_facts=_facts(document),
                    schemas=_schemas(),
                )
                self.assertEqual(result.artifact_publication_disposition, expected)
                self.assertEqual(result.artifact_count, 4)
                self.assertEqual(result.file_commitments, ())
                self.assertEqual(result.total_byte_count, 0)
                self.assertIsNone(result.manifest_sha256)
                self.assertFalse(result.audit_database_schema_verified)

    def test_actual_generic_scan_has_priority_over_invalid_json_and_hides_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "audit_index_written")
            (directory / "phase6_audit_index.json").write_bytes(
                b'{"leaked":"sk-abcdefghijk"}\n'
            )
            result = verify_artifact_bundle(
                directory,
                postrun_facts=_facts(_postrun(stage="audit_index_written")),
                schemas=_schemas(),
            )
            self.assertEqual(
                result.artifact_publication_disposition,
                "sensitive_quarantined",
            )
            self.assertEqual(result.public_generic_privacy_scan_status, "sensitive_detected")
            self.assertEqual(result.file_commitments, ())
            self.assertEqual(result.total_byte_count, 0)

    def test_summary_contains_no_paths_payloads_or_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            result = verify_artifact_bundle(
                directory,
                postrun_facts=_facts(),
                schemas=_schemas(),
            )
            rendered = repr(asdict(result))
            self.assertNotIn(str(directory), rendered)
            self.assertNotIn("PCECASE-", rendered)
            self.assertNotIn("payload", rendered.casefold())
            self.assertNotIn("content", rendered.casefold())

    def test_production_module_has_no_write_network_environment_or_auditledger(self) -> None:
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
                    "researchops.audit",
                }
            )
        )
        self.assertNotIn("AuditLedger", MODULE.read_text(encoding="utf-8"))
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertFalse(
            attributes.intersection(
                {
                    "write",
                    "write_bytes",
                    "write_text",
                    "mkdir",
                    "unlink",
                    "remove",
                    "rename",
                    "environ",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
