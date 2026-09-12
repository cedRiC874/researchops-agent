from __future__ import annotations

import ast
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_external_closure.artifact_inputs import (
    ArtifactEvaluationInputs,
    _read_database_rows,
    _row_dicts,
    extract_artifact_evaluation_inputs,
)
from researchops_external_closure.artifacts import verify_artifact_bundle
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests.test_external_closure_artifacts import (
    _facts,
    _postrun,
    _schemas,
    _write_prefix,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "researchops_external_closure" / "artifact_inputs.py"


class ExternalClosureArtifactInputTests(unittest.TestCase):
    def test_database_row_materialization_is_bounded_before_semantic_validation(self) -> None:
        import sqlite3

        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE sample(value INTEGER)")
            connection.executemany(
                "INSERT INTO sample VALUES (?)", [(index,) for index in range(101)]
            )
            with self.assertRaisesRegex(
                ExternalClosurePrimitiveError, "^external_closure_audit_rows_invalid$"
            ):
                _row_dicts(connection, "SELECT value FROM sample", max_rows=100)
            bounded = _row_dicts(
                connection, "SELECT value FROM sample WHERE value < 100", max_rows=100
            )
            self.assertEqual(len(bounded), 100)
        finally:
            connection.close()

    def test_complete_bundle_yields_defensive_private_semantic_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")
            value = extract_artifact_evaluation_inputs(
                directory,
                postrun_facts=_facts(),
                schemas=_schemas(),
            )
            telemetry, index, runs, events, model_calls = value._semantic_inputs()
            self.assertEqual(value.summary.bundle_status, "complete")
            self.assertEqual(index, {"schema_version": "1.1", "runs": []})
            self.assertEqual(telemetry["status"], "recorded")
            self.assertEqual((runs, events, model_calls), ((), (), ()))
            self.assertEqual(
                (
                    value.tool_call_count,
                    value.tool_attempt_count,
                    value.approval_decision_count,
                ),
                (0, 0, 0),
            )

            assert telemetry is not None
            telemetry["status"] = "forged"
            fresh, *_rest = value._semantic_inputs()
            self.assertEqual(fresh["status"], "recorded")
            with self.assertRaises(FrozenInstanceError):
                value.tool_call_count = 1  # type: ignore[misc]
            with self.assertRaises(TypeError):
                ArtifactEvaluationInputs()

    def test_database_rows_are_read_without_mutating_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "phase6_audit.sqlite3"
            ledger = AuditLedger(database)
            run_id = ledger.start_run(
                run_id="PCERUN-0123456789ABCDEF0123456789ABCDEF",
                mode="external_closure_synthetic",
                request_summary={"safe": True},
            )
            before = database.read_bytes()
            value = extract_artifact_evaluation_inputs(
                directory,
                postrun_facts=_facts(_postrun(stage="audit_database_written")),
                schemas=_schemas(),
            )
            _telemetry, _index, runs, events, model_calls = value._semantic_inputs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_id"], run_id)
            self.assertEqual(len(events), 1)
            self.assertEqual(model_calls, ())
            self.assertEqual(database.read_bytes(), before)

    def test_quarantine_does_not_touch_poison_artifact_argument(self) -> None:
        facts = _postrun()
        facts["custodian_private_leak_canary_scan_status"] = "leak_detected"
        poison = Path("this-path-must-not-be-opened")
        with patch.object(Path, "lstat", side_effect=AssertionError("path touched")):
            value = extract_artifact_evaluation_inputs(
                poison,
                postrun_facts=_facts(facts),
                schemas=_schemas(),
            )
        self.assertEqual(
            value.summary.artifact_publication_disposition,
            "sensitive_quarantined",
        )
        self.assertEqual(value._semantic_inputs(), (None, None, (), (), ()))

    def test_normal_not_started_prefix_has_an_empty_verified_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = extract_artifact_evaluation_inputs(
                Path(temporary),
                postrun_facts=_facts(_postrun(stage="not_started")),
                schemas=_schemas(),
            )
        self.assertEqual(value.summary.bundle_status, "manifestless_failure")
        self.assertEqual(value.summary.artifact_publication_disposition, "normal")
        self.assertEqual(value._semantic_inputs(), (None, None, (), (), ()))

    def test_change_after_container_verification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_prefix(directory, "manifest_written")

            def verify_then_change(*args, **kwargs):
                summary = verify_artifact_bundle(*args, **kwargs)
                target = directory / "phase6_audit_index.json"
                target.write_bytes(target.read_bytes() + b" ")
                return summary

            with patch(
                "researchops_external_closure.artifact_inputs.verify_artifact_bundle",
                side_effect=verify_then_change,
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_artifact_bundle_identity_changed",
                ):
                    extract_artifact_evaluation_inputs(
                        directory,
                        postrun_facts=_facts(),
                        schemas=_schemas(),
                    )

    def test_extracted_rows_are_bound_to_committed_sqlite_bytes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as committed_temporary,
            tempfile.TemporaryDirectory() as alternate_temporary,
        ):
            committed = Path(committed_temporary) / "audit.sqlite3"
            alternate = Path(alternate_temporary) / "audit.sqlite3"
            AuditLedger(committed)
            import sqlite3

            connection = sqlite3.connect(alternate)
            try:
                connection.execute("VACUUM")
            finally:
                connection.close()
            expected_payload = committed.read_bytes()
            real_connect = sqlite3.connect

            def connect_alternate(*_args, **kwargs):
                return real_connect(
                    alternate.absolute().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                    timeout=kwargs.get("timeout", 1.0),
                )

            with patch(
                "researchops_external_closure.artifact_inputs.sqlite3.connect",
                side_effect=connect_alternate,
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_bundle_hash_mismatch",
                ):
                    _read_database_rows(
                        committed,
                        expected_payload=expected_payload,
                    )

    def test_unserializable_swapped_database_is_a_hash_mismatch(self) -> None:
        with (
            tempfile.TemporaryDirectory() as committed_temporary,
            tempfile.TemporaryDirectory() as alternate_temporary,
        ):
            committed = Path(committed_temporary) / "audit.sqlite3"
            alternate = Path(alternate_temporary) / "empty.sqlite3"
            AuditLedger(committed)
            alternate.write_bytes(b"")
            expected_payload = committed.read_bytes()
            import sqlite3

            real_connect = sqlite3.connect

            def connect_alternate(*_args, **kwargs):
                return real_connect(
                    alternate.absolute().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                    timeout=kwargs.get("timeout", 1.0),
                )

            with patch(
                "researchops_external_closure.artifact_inputs.sqlite3.connect",
                side_effect=connect_alternate,
            ):
                with self.assertRaisesRegex(
                    ExternalClosurePrimitiveError,
                    "external_closure_bundle_hash_mismatch",
                ):
                    _read_database_rows(
                        committed,
                        expected_payload=expected_payload,
                    )

    def test_module_has_no_write_network_environment_or_clock_capability(self) -> None:
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
                {"socket", "subprocess", "httpx", "openai", "agents", "requests"}
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
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
