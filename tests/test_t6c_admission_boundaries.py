from __future__ import annotations

import ast
import contextlib
import hashlib
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
    def test_original_diagnostic_replays_all_bound_historical_inputs(self):
        frozen = diagnostic.RECEIPT.read_bytes()
        self.assertEqual(hashlib.sha256(frozen).hexdigest(),
                         "cf6ffacf80a13dfe95dd8767172125933b51728a24191f0dc69924419a573976")
        original_read = Path.read_bytes
        current_inputs = {ROOT / path for path in
                          (diagnostic.FIRST_LIVE, diagnostic.SURFACE, diagnostic.REGISTRY, diagnostic.PREDECESSOR)}

        def refuse_current_inputs(path):
            if path in current_inputs:
                raise AssertionError("historical replay must not read current input bytes")
            return original_read(path)

        with patch.object(Path, "read_bytes", new=refuse_current_inputs):
            report = diagnostic.inspect_boundaries(historical_inputs=True)
        self.assertEqual(frozen, diagnostic.canonical(report) + b"\n")
        self.assertEqual(diagnostic.RECEIPT.read_bytes(), frozen)

    def test_legacy_first_live_only_mode_still_observes_current_other_inputs(self):
        original_read = Path.read_bytes
        surface_path = ROOT / diagnostic.SURFACE
        changed_surface = original_read(surface_path) + b"\n# synthetic current input mutation\n"
        observed = []

        def changed_current_surface(path):
            if path == surface_path:
                observed.append(path)
                return changed_surface
            return original_read(path)

        with patch.object(Path, "read_bytes", new=changed_current_surface):
            report = diagnostic.inspect_boundaries(historical_first_live=True)
            current = diagnostic.inspect_boundaries()
        self.assertEqual(len(observed), 2)
        expected_surface = {"path": diagnostic.SURFACE, "bytes": len(changed_surface),
                            "sha256": hashlib.sha256(changed_surface).hexdigest()}
        for value in (report, current):
            self.assertEqual(next(item for item in value["inputs"] if item["path"] == diagnostic.SURFACE),
                             expected_surface)
        recorded = json.loads(diagnostic.RECEIPT.read_bytes())["inputs"]
        self.assertEqual(report["inputs"][0], recorded[0])
        self.assertEqual(current["inputs"][0]["sha256"],
                         hashlib.sha256(original_read(ROOT / diagnostic.FIRST_LIVE)).hexdigest())

    def test_missing_historical_objects_never_fall_back_to_current_inputs(self):
        from researchops_external_closure import git_objects
        with patch.object(git_objects, "read_git_object_snapshot",
                          side_effect=RuntimeError("synthetic missing historical object")) as reader, \
             patch.object(Path, "read_bytes", side_effect=AssertionError("current input fallback forbidden")) as local:
            with self.assertRaisesRegex(RuntimeError, "synthetic missing historical object"):
                diagnostic.inspect_boundaries(historical_inputs=True)
        self.assertEqual(local.call_count, 0)
        reader.assert_called_once_with(ROOT, diagnostic.HISTORICAL_COMMIT,
            (diagnostic.FIRST_LIVE, diagnostic.REGISTRY, diagnostic.PREDECESSOR),
            expected_tree_oid=diagnostic.HISTORICAL_TREE)

    def test_history_modes_are_mutually_exclusive_before_io(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("no IO")) as reader:
            with self.assertRaisesRegex(ValueError, "historical_modes_conflict"):
                diagnostic.inspect_boundaries(historical_first_live=True, historical_inputs=True)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                diagnostic.main(["--historical-first-live", "--historical-inputs"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(reader.call_count, 0)

    def test_cli_verifies_the_unchanged_original_receipt_with_all_historical_inputs(self):
        before = diagnostic.RECEIPT.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = diagnostic.main(["--historical-inputs", "--verify"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue()), json.loads(before))
        self.assertEqual(diagnostic.RECEIPT.read_bytes(), before)

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
