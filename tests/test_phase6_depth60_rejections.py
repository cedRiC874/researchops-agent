from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyze_phase6_depth60_rejections.py"
SPEC = importlib.util.spec_from_file_location(
    "analyze_phase6_depth60_rejections", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)

LOCAL_RESULTS_CANDIDATE = (
    ROOT.parent
    / "researchops-agent-final-main"
    / "artifacts"
    / "deepseek_depth60_run_20260901_01"
    / "phase6_results.jsonl"
)
LOCAL_AUDIT_DB_CANDIDATE = LOCAL_RESULTS_CANDIDATE.with_name(
    "phase6_audit.sqlite3"
)
PUBLISHED_ARTIFACT = ROOT / analysis.ARTIFACT_RELATIVE_PATH
EXPECTED_ARTIFACT_SHA256 = (
    "7b7083856987c2f08385124f9683807296dbed218a2921833537000ad5337a1c"
)
PUBLISHED_SVG = PUBLISHED_ARTIFACT.with_suffix(".svg")
EXPECTED_SVG_SHA256 = (
    "62f9b50814d00e5ebdee65b290a595ac994c7ed4b899ba3e2d327a9c3e8313b8"
)
EMPTY_STRING_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


class Phase6Depth60RejectionAnalysisTests(unittest.TestCase):
    def _diagnose(
        self,
        text: str,
        required: tuple[str, ...] = ("mode=ok",),
        forbidden: tuple[str, ...] = (),
    ) -> dict[str, object]:
        return analysis.diagnose_required_phrase_rejection(
            "P6-DEV-017", text, required, forbidden
        )

    def test_imports_the_actual_locked_private_matcher_and_regexes(self) -> None:
        self.assertIs(analysis._contains_phrase, analysis.phase6_eval_module._contains_phrase)
        self.assertIs(
            analysis._structured_assertions_match,
            analysis.phase6_eval_module._structured_assertions_match,
        )
        self.assertIs(analysis._NUMBER_IN_TEXT, analysis.phase6_eval_module._NUMBER_IN_TEXT)
        self.assertIs(
            analysis._STRUCTURED_ASSERTION_LINE,
            analysis.phase6_eval_module._STRUCTURED_ASSERTION_LINE,
        )

    def test_synthetic_cases_cover_every_exact_rejection_branch(self) -> None:
        cases = {
            analysis.PLAIN_REQUIRED_PHRASE_MISSING: self._diagnose(
                "[ASSERT mode=ok]", ("plain literal", "mode=ok")
            ),
            analysis.MALFORMED_ASSERT_LINE: self._diagnose("[ASSERT mode=ok] trailing"),
            analysis.MISSING_EXPECTED_ASSERTION: self._diagnose("mode=ok"),
            analysis.UNEXPECTED_ASSERTION: self._diagnose("[ASSERT mode=bad]"),
            analysis.DUPLICATE_ASSERTION: self._diagnose(
                "[ASSERT mode=ok]\n[ASSERT mode=ok]"
            ),
            analysis.ASSIGNMENT_LABEL_REPEATED_IN_PROSE: self._diagnose(
                "[ASSERT mode=ok]\nmode=other"
            ),
            analysis.NUMERIC_LITERAL_IN_NON_CLAIM_PROSE: self._diagnose(
                "[ASSERT mode=ok]\nThere are 2 groups."
            ),
            analysis.ENUM_VALUE_REPEATED_IN_PROSE: self._diagnose(
                "[ASSERT mode=ok]\nStatus OK."
            ),
        }
        self.assertEqual(set(cases), set(analysis.REJECTION_REASON_ORDER))
        for target_reason, diagnostic in cases.items():
            with self.subTest(reason=target_reason):
                self.assertIn(
                    target_reason, diagnostic["structured_rejection_reasons"]
                )
                self.assertFalse(diagnostic["scorer_required_phrase_check"])
                if target_reason == analysis.PLAIN_REQUIRED_PHRASE_MISSING:
                    self.assertTrue(diagnostic["structured_assertions_match"])
                else:
                    self.assertFalse(diagnostic["structured_assertions_match"])

    def test_diagnostic_accumulates_overlapping_scorer_triggers(self) -> None:
        diagnostic = self._diagnose("mode=ok\n2 OK")
        self.assertEqual(
            diagnostic["structured_rejection_reasons"],
            [
                analysis.MISSING_EXPECTED_ASSERTION,
                analysis.ASSIGNMENT_LABEL_REPEATED_IN_PROSE,
                analysis.NUMERIC_LITERAL_IN_NON_CLAIM_PROSE,
                analysis.ENUM_VALUE_REPEATED_IN_PROSE,
            ],
        )
        self.assertFalse(diagnostic["structured_assertions_match"])

    def test_primary_buckets_are_the_objective_two_by_two(self) -> None:
        def row(
            *,
            required_only: bool,
            all_present: bool,
            bucket: str,
        ) -> dict[str, object]:
            return {
                "original_failure_reasons": (
                    ["required_phrases"]
                    if required_only
                    else ["required_phrases", "outcome"]
                ),
                "all_required_literals_present": all_present,
                "primary_bucket": bucket,
                "structured_rejection_reasons": [
                    analysis.NUMERIC_LITERAL_IN_NON_CLAIM_PROSE
                ],
                "first_failed_gate": analysis.FIRST_GATE_NUMERIC,
            }

        rows = [
            row(
                required_only=True,
                all_present=True,
                bucket=analysis.PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED,
            ),
            row(
                required_only=True,
                all_present=False,
                bucket=analysis.PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING,
            ),
            row(
                required_only=False,
                all_present=True,
                bucket=analysis.MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED,
            ),
            row(
                required_only=False,
                all_present=False,
                bucket=analysis.MIXED_ONE_OR_MORE_LITERALS_MISSING,
            ),
        ]
        histogram = analysis._cohort_histogram(rows)
        self.assertEqual(
            histogram["failure_scope_by_literal_presence_2x2"],
            {
                "required_phrases_only": {
                    "all_required_literals_present": 1,
                    "one_or_more_required_literals_missing": 1,
                },
                "mixed_failure_reasons": {
                    "all_required_literals_present": 1,
                    "one_or_more_required_literals_missing": 1,
                },
            },
        )
        self.assertIn(
            "not a semantic-correctness judgment",
            histogram["primary_bucket_interpretation"][
                analysis.PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED
            ],
        )
        self.assertEqual(
            histogram["primary_bucket_counts"],
            {bucket: 1 for bucket in analysis.OBJECTIVE_BUCKET_ORDER},
        )

    def test_emit_requires_an_explicit_results_path(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(analysis.main(["--emit"]), 1)
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["status"], "invalid")
        self.assertIn("explicit --results", error["detail"])

    def test_publication_default_path_hash_and_verification_are_required(self) -> None:
        self.assertEqual(
            analysis.ARTIFACT_RELATIVE_PATH.as_posix(),
            "docs/evidence/phase6-deepseek-depth60-interpretation-audit-v1/"
            "rejection_reason_histogram.json",
        )
        self.assertTrue(PUBLISHED_ARTIFACT.is_file())
        self.assertEqual(
            hashlib.sha256(PUBLISHED_ARTIFACT.read_bytes()).hexdigest(),
            EXPECTED_ARTIFACT_SHA256,
        )
        summary = analysis.verify_phase6_depth60_rejection_histogram(ROOT)
        self.assertEqual(summary["status"], "valid")
        self.assertTrue(summary["published_commitment_verified"])
        self.assertFalse(summary["out_of_band_expected_sha256_verified"])
        self.assertFalse(summary["omitted_results_artifact_loaded"])
        self.assertFalse(summary["audit_database_loaded"])
        self.assertFalse(summary["actual_scorer_input_bytes_loaded"])
        self.assertEqual(summary["sanitized_row_value_check_count"], 920)
        self.assertGreater(summary["derived_histogram_value_check_count"], 0)
        self.assertEqual(
            summary["default_verification_compared_value_count"],
            summary["sanitized_row_value_check_count"]
            + summary["derived_histogram_value_check_count"],
        )
        self.assertEqual(
            summary["verification_mode"],
            "committed_sanitized_projection_without_omitted_artifacts",
        )
        published = json.loads(PUBLISHED_ARTIFACT.read_text(encoding="utf-8"))
        self.assertEqual(
            published["schema_version"],
            "phase6-depth60-rejection-reason-histogram/1.1",
        )
        rows = {row["task_id"]: row for row in published["rows"]}
        for task_id in ("P6-DEV-043", "P6-DEV-060"):
            with self.subTest(task_id=task_id):
                row = rows[task_id]
                self.assertTrue(row["output_blank"])
                self.assertFalse(row["completion_integrity"])
                self.assertEqual(row["projection_output_sha256"], EMPTY_STRING_SHA256)
                self.assertEqual(row["scored_output_sha256"], EMPTY_STRING_SHA256)
                self.assertTrue(row["projection_matches_scored_output"])
                self.assertEqual(row["diagnostic_basis"], "scored_output_byte_exact")
        delivery = published["histograms"]["delivery_aware_interpretation"]
        self.assertFalse(delivery["first_failed_gate_is_causal_attribution"])
        self.assertFalse(
            delivery[
                "reported_findings_are_exhaustive_or_mutually_exclusive_causal_partition"
            ]
        )
        self.assertEqual(delivery["mechanical_required_phrases_failure_count"], 40)
        self.assertEqual(delivery["byte_empty_delivery_failure_count"], 2)
        self.assertEqual(
            delivery["byte_empty_delivery_failure_task_ids"],
            ["P6-DEV-043", "P6-DEV-060"],
        )
        self.assertEqual(delivery["content_bearing_failed_task_count"], 38)
        self.assertEqual(
            delivery["content_bearing_required_phrases_failure_count"], 38
        )
        self.assertEqual(
            delivery["content_bearing_scored_output_byte_exact_count"], 37
        )
        self.assertEqual(
            delivery["content_bearing_sanitized_projection_only_task_ids"],
            ["P6-DEV-057"],
        )
        self.assertFalse(
            delivery[
                "byte_empty_required_phrases_failure_is_textually_discriminating"
            ]
        )
        self.assertEqual(
            delivery["content_bearing_first_failed_gate_counts"],
            {
                analysis.FIRST_GATE_PLAIN: 1,
                analysis.FIRST_GATE_MALFORMED: 1,
                analysis.FIRST_GATE_INVENTORY: 20,
                analysis.FIRST_GATE_ASSIGNMENT: 2,
                analysis.FIRST_GATE_NUMERIC: 12,
                analysis.FIRST_GATE_ENUM: 2,
            },
        )
        self.assertEqual(
            delivery["content_bearing_failure_scope_by_literal_presence_2x2"],
            {
                "required_phrases_only": {
                    "all_required_literals_present": 30,
                    "one_or_more_required_literals_missing": 4,
                },
                "mixed_failure_reasons": {
                    "all_required_literals_present": 3,
                    "one_or_more_required_literals_missing": 1,
                },
            },
        )
        self.assertEqual(
            delivery[
                "content_bearing_non_short_circuit_diagnostic_trigger_counts"
            ],
            {
                analysis.PLAIN_REQUIRED_PHRASE_MISSING: 1,
                analysis.MALFORMED_ASSERT_LINE: 1,
                analysis.MISSING_EXPECTED_ASSERTION: 20,
                analysis.UNEXPECTED_ASSERTION: 7,
                analysis.ASSIGNMENT_LABEL_REPEATED_IN_PROSE: 15,
                analysis.NUMERIC_LITERAL_IN_NON_CLAIM_PROSE: 24,
                analysis.ENUM_VALUE_REPEATED_IN_PROSE: 25,
            },
        )
        self.assertEqual(
            delivery["plain_required_phrase_first_gate_decomposition"],
            {
                "mechanical_count": 3,
                "content_bearing_literal_omission_count": 1,
                "content_bearing_task_ids": ["P6-DEV-024"],
                "byte_empty_delivery_failure_count": 2,
                "byte_empty_task_ids": ["P6-DEV-043", "P6-DEV-060"],
            },
        )
        self.assertEqual(
            delivery["mixed_missing_literal_cell_decomposition"],
            {
                "mechanical_count": 3,
                "content_bearing_mixed_failure_count": 1,
                "content_bearing_task_ids": ["P6-DEV-023"],
                "byte_empty_delivery_failure_count": 2,
                "byte_empty_task_ids": ["P6-DEV-043", "P6-DEV-060"],
            },
        )
        self.assertEqual(
            delivery["missing_expected_assertion_trigger_decomposition"],
            {
                "mechanical_count": 22,
                "content_bearing_count": 20,
                "byte_empty_delivery_failure_count": 2,
                "byte_empty_task_ids": ["P6-DEV-043", "P6-DEV-060"],
                "byte_exact_content_bearing_count": 19,
                "sanitized_only_content_bearing_task_ids": ["P6-DEV-057"],
            },
        )
        out_of_band = analysis.verify_phase6_depth60_rejection_histogram(
            ROOT, expected_artifact_sha256=EXPECTED_ARTIFACT_SHA256
        )
        self.assertTrue(out_of_band["out_of_band_expected_sha256_verified"])

    def test_default_verifier_rejects_tampering_and_sensitive_fields(self) -> None:
        published = json.loads(PUBLISHED_ARTIFACT.read_text(encoding="utf-8"))
        tampered = json.loads(json.dumps(published))
        tampered["rows"][0]["result_row_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejection_reason_histogram.json"
            path.write_bytes(analysis._canonical_json_bytes(tampered))
            with self.assertRaisesRegex(
                analysis.RejectionAnalysisError, "published commitment"
            ):
                analysis.verify_phase6_depth60_rejection_histogram(ROOT, path)

        with self.assertRaisesRegex(
            analysis.RejectionAnalysisError, "out-of-band expected SHA-256"
        ):
            analysis.verify_phase6_depth60_rejection_histogram(
                ROOT, expected_artifact_sha256="f" * 64
            )

        hidden_output = json.loads(json.dumps(published))
        hidden_output["rows"][0]["final_output"] = "must not be published"
        with self.assertRaisesRegex(
            analysis.RejectionAnalysisError, "forbidden publication field"
        ):
            analysis._validate_sanitized_artifact(hidden_output, ROOT)

        matcher_drift = json.loads(json.dumps(published))
        matcher_drift["matcher_binding"]["structured_matcher"] = "other"
        with self.assertRaisesRegex(
            analysis.RejectionAnalysisError, "matcher binding"
        ):
            analysis._validate_sanitized_artifact(matcher_drift, ROOT)

        delivery_drift = json.loads(json.dumps(published))
        delivery_drift["histograms"]["delivery_aware_interpretation"][
            "content_bearing_failed_task_count"
        ] = 39
        with self.assertRaisesRegex(
            analysis.RejectionAnalysisError, "histogram does not match"
        ):
            analysis._validate_sanitized_artifact(delivery_drift, ROOT)

    def test_svg_histogram_is_bound_to_the_published_counts(self) -> None:
        self.assertTrue(PUBLISHED_SVG.is_file())
        payload = PUBLISHED_SVG.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), EXPECTED_SVG_SHA256)
        document = ET.fromstring(payload)
        elements = list(document.iter())
        gates = {
            element.attrib["data-gate"]: int(element.attrib["data-count"])
            for element in elements
            if "data-gate" in element.attrib
        }
        buckets = {
            element.attrib["data-bucket"]: int(element.attrib["data-count"])
            for element in elements
            if "data-bucket" in element.attrib
        }
        gate_elements = {
            element.attrib["data-gate"]: element
            for element in elements
            if "data-gate" in element.attrib
        }
        bucket_elements = {
            element.attrib["data-bucket"]: element
            for element in elements
            if "data-bucket" in element.attrib
        }
        self.assertEqual(
            gates,
            {
                analysis.FIRST_GATE_INVENTORY: 20,
                analysis.FIRST_GATE_NUMERIC: 12,
                analysis.FIRST_GATE_PLAIN: 3,
                analysis.FIRST_GATE_ASSIGNMENT: 2,
                analysis.FIRST_GATE_ENUM: 2,
                analysis.FIRST_GATE_MALFORMED: 1,
            },
        )
        self.assertEqual(
            buckets,
            {
                analysis.PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED: 30,
                analysis.PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING: 4,
                analysis.MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED: 3,
                analysis.MIXED_ONE_OR_MORE_LITERALS_MISSING: 3,
            },
        )
        plain_gate = gate_elements[analysis.FIRST_GATE_PLAIN]
        self.assertEqual(plain_gate.attrib["data-content-bearing-count"], "1")
        self.assertEqual(plain_gate.attrib["data-byte-empty-count"], "2")
        mixed_missing = bucket_elements[
            analysis.MIXED_ONE_OR_MORE_LITERALS_MISSING
        ]
        self.assertEqual(mixed_missing.attrib["data-content-bearing-count"], "1")
        self.assertEqual(mixed_missing.attrib["data-byte-empty-count"], "2")

    def test_optional_locked_projection_recomputation_is_complete_and_canonical(self) -> None:
        if not LOCAL_RESULTS_CANDIDATE.exists() or not LOCAL_AUDIT_DB_CANDIDATE.exists():
            self.skipTest("omitted SHA-matching raw artifact is not present in CI")
        artifact = analysis.build_phase6_depth60_rejection_histogram(
            LOCAL_RESULTS_CANDIDATE, LOCAL_AUDIT_DB_CANDIDATE, ROOT
        )
        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        self.assertNotIn("final_output", set(keys(artifact)))
        self.assertEqual(len(artifact["rows"]), 40)
        histograms = artifact["histograms"]["sanitized_projection"]
        all40 = histograms["all_failed_40"]
        sole34 = histograms["required_phrases_only_34"]
        self.assertEqual(all40["denominator"], 40)
        self.assertEqual(sole34["denominator"], 34)
        self.assertEqual(
            all40["all_literals_present_structured_rejected_vs_other"],
            {
                "all_required_literals_present_but_structured_rejected": 33,
                "other": 7,
            },
        )
        self.assertEqual(
            sole34["all_literals_present_structured_rejected_vs_other"],
            {
                "all_required_literals_present_but_structured_rejected": 30,
                "other": 4,
            },
        )
        self.assertEqual(
            all40["failure_scope_by_literal_presence_2x2"],
            {
                "required_phrases_only": {
                    "all_required_literals_present": 30,
                    "one_or_more_required_literals_missing": 4,
                },
                "mixed_failure_reasons": {
                    "all_required_literals_present": 3,
                    "one_or_more_required_literals_missing": 3,
                },
            },
        )
        self.assertEqual(
            all40["primary_bucket_counts"],
            {
                analysis.PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED: 30,
                analysis.PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING: 4,
                analysis.MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED: 3,
                analysis.MIXED_ONE_OR_MORE_LITERALS_MISSING: 3,
            },
        )
        self.assertEqual(
            sole34["primary_bucket_counts"],
            {
                analysis.PHRASE_ONLY_ALL_LITERALS_PRESENT_FORMAT_REJECTED: 30,
                analysis.PHRASE_ONLY_ONE_OR_MORE_LITERALS_MISSING: 4,
                analysis.MIXED_ALL_LITERALS_PRESENT_FORMAT_REJECTED: 0,
                analysis.MIXED_ONE_OR_MORE_LITERALS_MISSING: 0,
            },
        )
        self.assertTrue(all40["non_short_circuit_diagnostic_trigger_counts"])
        self.assertTrue(
            all40["exact_non_short_circuit_diagnostic_trigger_set_counts"]
        )
        self.assertEqual(
            all40["first_failed_gate_counts"],
            {
                analysis.FIRST_GATE_PLAIN: 3,
                analysis.FIRST_GATE_MALFORMED: 1,
                analysis.FIRST_GATE_INVENTORY: 20,
                analysis.FIRST_GATE_ASSIGNMENT: 2,
                analysis.FIRST_GATE_NUMERIC: 12,
                analysis.FIRST_GATE_ENUM: 2,
            },
        )
        exact39 = artifact["histograms"]["scored_output_byte_exact_projection"]
        self.assertEqual(exact39["indeterminate_task_ids"], ["P6-DEV-057"])
        self.assertEqual(
            exact39["all_failed_39"]["first_failed_gate_counts"],
            {
                analysis.FIRST_GATE_PLAIN: 3,
                analysis.FIRST_GATE_MALFORMED: 1,
                analysis.FIRST_GATE_INVENTORY: 19,
                analysis.FIRST_GATE_ASSIGNMENT: 2,
                analysis.FIRST_GATE_NUMERIC: 12,
                analysis.FIRST_GATE_ENUM: 2,
            },
        )
        self.assertEqual(
            artifact["publication"][
                "derivation_fully_recomputable_only_with_committed_sha256_matching_omitted_artifact"
            ],
            True,
        )
        emitted = analysis._canonical_json_bytes(artifact)
        self.assertEqual(json.loads(emitted), artifact)
        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "rejection_reason_histogram.json"
            artifact_path.write_bytes(emitted)
            summary = analysis.verify_phase6_depth60_rejection_histogram(
                ROOT,
                artifact_path,
                LOCAL_RESULTS_CANDIDATE,
                LOCAL_AUDIT_DB_CANDIDATE,
                EXPECTED_ARTIFACT_SHA256,
            )
        self.assertEqual(summary["status"], "valid")
        self.assertTrue(summary["published_commitment_verified"])
        self.assertTrue(summary["out_of_band_expected_sha256_verified"])
        self.assertTrue(summary["omitted_results_artifact_loaded"])
        self.assertTrue(summary["audit_database_loaded"])
        self.assertFalse(summary["actual_scorer_input_bytes_loaded"])
        self.assertGreater(summary["full_recomputation_compared_value_count"], 500)
        self.assertEqual(summary["mismatch_count"], 0)
        self.assertEqual(
            artifact["source_bindings"]["omitted_results_artifact"]["sha256"],
            hashlib.sha256(LOCAL_RESULTS_CANDIDATE.read_bytes()).hexdigest(),
        )
        self.assertIn(
            analysis.RUNNER_RELATIVE_PATH.as_posix(),
            artifact["source_bindings"]["commitments"],
        )
        self.assertEqual(
            artifact["source_bindings"]["locked_source_bundle_sha256"],
            "914acbe89f4d99240aa653ecfe07fc0a2c129d08aa6abee9eb401e5f9d7a8d84",
        )

    def test_diagnostics_do_not_use_network_provider_or_secret_environment(self) -> None:
        class ForbiddenEnvironment(dict[str, str]):
            def __getitem__(self, key: str) -> str:
                raise AssertionError(f"environment read is forbidden: {key}")

            def get(self, key: str, default=None):
                del default
                raise AssertionError(f"environment read is forbidden: {key}")

        with patch.object(
            socket, "socket", side_effect=AssertionError("network is forbidden")
        ), patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network is forbidden"),
        ), patch.object(
            os, "getenv", side_effect=AssertionError("environment is forbidden")
        ), patch.object(os, "environ", ForbiddenEnvironment()):
            diagnostic = self._diagnose("[ASSERT mode=ok]\nThere are 2 groups.")
            summary = analysis.verify_phase6_depth60_rejection_histogram(ROOT)
        self.assertIn(
            analysis.NUMERIC_LITERAL_IN_NON_CLAIM_PROSE,
            diagnostic["structured_rejection_reasons"],
        )
        self.assertEqual(summary["network_calls"], 0)
        self.assertEqual(summary["api_key_reads"], 0)


if __name__ == "__main__":
    unittest.main()
