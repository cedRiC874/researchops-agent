from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/inspect_t6c_admission_boundaries.py"
spec = importlib.util.spec_from_file_location("admission_boundary_diagnostic", SCRIPT)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class AdmissionBoundaryDiagnosticTests(unittest.TestCase):
    def test_original_diagnostic_replays_its_bound_historical_first_live_bytes(self):
        report = diagnostic.inspect_boundaries(historical_first_live=True)
        self.assertEqual(diagnostic.RECEIPT.read_bytes(), diagnostic.canonical(report) + b"\n")

    def test_only_the_counterfactual_runtime_flag_changes_the_full_mapping_hash(self):
        report = diagnostic.inspect_boundaries()
        result = report["mapping_counterfactual"]
        self.assertEqual(result["changed_projection_paths"],
                         ["surface_selection.runtime_binding_allowed"])
        self.assertTrue(result["provider_mapping_document_unchanged"])
        self.assertNotEqual(result["actual_selected_mapping_sha256"],
                            result["counterfactual_selected_mapping_sha256"])
        self.assertFalse(result["counterfactual_is_valid_registry"])
        self.assertEqual(result["counterfactual_rejection_code"],
                         "surface_registry_v2_runtime_promotion_forbidden")

    def test_static_evidence_is_explicitly_not_a_live_execution(self):
        report = diagnostic.inspect_boundaries()
        graph = report["first_live_static_call_graph"]
        self.assertEqual(len(graph["edges"]), 4)
        self.assertFalse(graph["call_graph_is_dynamic_execution_evidence"])
        self.assertEqual(graph["mutable_ancestry_ref"], "refs/remotes/origin/main")
        self.assertTrue(graph["mutable_ancestry_ref_lines"])
        for name in ("registry_files_modified", "first_live_verifier_executed",
                     "runtime_authority_constructed", "registry_promotion_performed",
                     "closure_claim_allowed"):
            self.assertIs(report[name], False)
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(report["key_loads"], 0)

    def test_recomputation_is_read_only_and_never_constructs_runtime_authority(self):
        from researchops_completion_telemetry import surface_mapping
        paths = (diagnostic.FIRST_LIVE, diagnostic.SURFACE,
                 diagnostic.REGISTRY, diagnostic.PREDECESSOR)
        before = {name: (ROOT / name).read_bytes() for name in paths}
        with patch.object(surface_mapping.VerifiedRuntimeCompletionBinding, "_create",
                          side_effect=AssertionError("runtime construction forbidden")):
            self.assertEqual(diagnostic.inspect_boundaries(), diagnostic.inspect_boundaries())
        self.assertEqual(before, {name: (ROOT / name).read_bytes() for name in paths})
        syntax = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(syntax)
                    if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("researchops.deepseek_completion_first_live_validation", imported)

    def test_receipt_write_is_exclusive_and_verification_detects_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"

            def run(*arguments):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = diagnostic.main(["--receipt", str(receipt), *arguments])
                return code, json.loads(output.getvalue())

            self.assertEqual(run()[0], 0)
            self.assertFalse(receipt.exists())
            self.assertEqual(run("--write")[0], 0)
            original = receipt.read_bytes()
            self.assertEqual(run("--verify")[0], 0)
            self.assertEqual(run("--write")[0], 1)
            self.assertEqual(receipt.read_bytes(), original)
            receipt.write_bytes(original + b"\n")
            self.assertEqual(run("--verify")[0], 1)
            self.assertEqual(receipt.read_bytes(), original + b"\n")


if __name__ == "__main__":
    unittest.main()
