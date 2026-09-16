from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from . import bootstrap, identity
from .bridge import make_plan, invoke
from .run import collect_and_score
from ..aggregate_read_v1.core import load_json, fixed_run, coverage, not_executed, digest
from ..aggregate_read_v1.paths import agent_run

ROOT = identity.ROOT


class PreintegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = load_json(ROOT / "expectations.json")
        cls.result = collect_and_score()
        cls.contracts = load_json(ROOT / "contracts.json")["contracts"]

    def vector(self, report):
        reverse = {v: k for k, v in self.expected["legend"].items()}
        return "".join(reverse[report["dimensions"][d]["status"]] for d in self.expected["dimension_order"])

    def codes(self, report, dimension):
        return {c["code"] for c in report["dimensions"][dimension]["checks"]}

    def test_fixed_identity_and_import_sources(self):
        receipt = self.result["identity"]
        self.assertEqual(receipt["scorer_commit"], "fcc2026c60943de6016495ad244291689a9d491d")
        self.assertEqual(receipt["measurement_revision"], "internal-behavior-eval-v1.1")
        self.assertEqual(Path(receipt["core_import_file"]), identity.REPO / "src/researchops_behavior_eval_v1/core.py")
        identity.check_files(receipt)
        identity.check_module_locations()
        self.assertTrue(self.result["environment"]["used_dependencies_match_fixed_lock"])
        self.assertEqual(bootstrap.PACKAGE.parse_answer(None)["text_state"], "unobserved")

    def test_foreign_import_and_changed_scorer_rejected(self):
        fake = types.ModuleType("researchops_behavior_eval_v1.foreign")
        fake.__file__ = str(identity.REPO.parent / "wrong-tree/core.py")
        with patch.dict(sys.modules, {fake.__name__: fake}):
            with self.assertRaises(identity.IdentityError):
                identity.check_module_locations()
        with patch.object(identity, "sha", return_value="changed"):
            with self.assertRaises(identity.IdentityError):
                identity.check_files(bootstrap.IDENTITY)

    def test_both_full_plans_through_real_scorer(self):
        for path, item in self.result["business"].items():
            with self.subTest(path=path):
                call = item["scorer_call"]
                self.assertTrue(call["actual_call_attempted"])
                self.assertTrue(call["actual_scorer_connected"])
                self.assertFalse(call["formal_integration_accepted"])
                self.assertEqual(call["observation_scope"], "offline_preintegration")
                self.assertEqual(call["raw_report"]["counts"], self.expected["business"][path]["counts"])
                self.assertEqual(len(item["input_plan"]["tasks"]), 8)
                for i, report in enumerate(call["raw_report"]["tasks"]):
                    self.assertEqual(self.vector(report), self.expected["business"][path]["vectors"][i])
                    self.assertEqual(report["task_verdict"], self.expected["business"][path]["verdicts"][i])

    def test_original_scorer_reports_unmodified(self):
        for item in self.result["business"].values():
            plan = deepcopy(item["input_plan"]); before = deepcopy(plan)
            raw = bootstrap.PACKAGE.score_plan(plan)
            self.assertEqual(raw, item["scorer_call"]["raw_report"])
            self.assertEqual(plan, before)
            self.assertNotIn("actual_scorer_connected", raw)
            self.assertNotIn("scorer_commit", raw)

    def test_projection_does_not_rewrite_text_or_evidence(self):
        for path, item in self.result["business"].items():
            rows = self.result["observations"]["paths"][path]["rows"]
            for row, scored in zip(rows, item["input_plan"]["tasks"]):
                raw = row["observation"]
                self.assertEqual(scored["observation"]["final_output"], raw["final_output"])
                for event, source in zip(scored["observation"]["events"], raw["events"]):
                    self.assertEqual(event, {k: source[k] for k in event})
                for evidence, source in zip(scored["allowed_evidence"], raw["allowed_evidence"]):
                    self.assertEqual(evidence, {k: source[k] for k in evidence})

    def test_all_predeclared_fault_vectors_codes_and_rejections(self):
        actual = {x["id"]: x for x in self.result["fault_validation"]}
        self.assertEqual(set(actual), {x["id"] for x in self.expected["faults"]})
        for expected in self.expected["faults"]:
            with self.subTest(case=expected["id"]):
                call = actual[expected["id"]]["scorer_call"]
                self.assertTrue(call["actual_call_attempted"])
                self.assertTrue(call["input_unchanged"])
                if "error_path" in expected:
                    self.assertFalse(call["actual_scorer_connected"])
                    self.assertIsNone(call["raw_report"])
                    self.assertEqual(call["error"]["type"], "ContractError")
                    self.assertEqual(call["error"]["code"], expected["error_code"])
                    self.assertEqual(call["error"]["path"], expected["error_path"])
                else:
                    self.assertTrue(call["actual_scorer_connected"])
                    report = call["raw_report"]
                    self.assertEqual(self.vector(report), expected["vector"])
                    self.assertEqual(report["task_verdict"], expected["verdict"])
                    for dimension, codes in expected["codes"].items():
                        self.assertTrue(set(codes) <= self.codes(report, dimension))

    def test_free_expression_unknown_disclosed_not_rewritten(self):
        item = self.result["business"]["agent"]
        report = item["scorer_call"]["raw_report"]["tasks"][4]
        self.assertEqual(report["task_verdict"], "unknown")
        self.assertIn("unparsed_content", self.codes(report, "facts"))
        summary = self.result["unknown_summary"]["agent"]
        self.assertEqual(summary["task_unknown_fraction"], {"numerator": 1, "denominator": 8})
        self.assertIn("AGR-05", summary["unparsed_content_task_ids"])
        self.assertEqual(self.result["unknown_summary"]["fixed_workflow"]["task_unknown_fraction"], {"numerator": 0, "denominator": 8})

    def test_damaged_source_fails_task_without_claiming_model_defect(self):
        for path, item in self.result["business"].items():
            report = item["scorer_call"]["raw_report"]["tasks"][7]
            self.assertEqual(report["task_verdict"], "fail")
            self.assertIn("tool_status_mismatch", self.codes(report, "behavior"))
            self.assertIn("unparsed_content", self.codes(report, "behavior"))
            raw = self.result["observations"]["paths"][path]["rows"][7]["observation"]
            self.assertEqual(raw["status"], "failed")
            self.assertEqual(raw["known_failures"][0]["attribution"], "not_a_model_capability_verdict")

    def test_missing_and_unexecuted_keep_eight_denominator(self):
        diagnostic = self.result["observations"]["coverage_diagnostic"]
        plan = make_plan(self.contracts, diagnostic)
        call = invoke(bootstrap.PACKAGE, bootstrap.IDENTITY, "score_plan", plan)
        self.assertEqual(call["raw_report"]["counts"], {
            "planned": 8, "pass": 4, "fail": 2, "unknown": 2,
            "observed": 6, "not_executed": 1, "observation_missing": 1})
        self.assertEqual(call["raw_report"]["dimension_counts"]["facts"]["denominator"], 5)

    def test_denominator_mismatch_is_not_silently_repaired(self):
        rows = deepcopy(self.result["observations"]["paths"]["agent"])
        rows["rows"].pop()
        with self.assertRaises(ValueError):
            make_plan(self.contracts, rows)

    def test_rejected_input_never_falls_back_to_stub(self):
        case = deepcopy(self.result["business"]["agent"]["input_plan"]["tasks"][0])
        del case["observation"]["final_output"]
        with patch("services.agent_workflow_comparison_v1.adapter.transfer_stub", side_effect=AssertionError("no fallback")):
            call = invoke(bootstrap.PACKAGE, bootstrap.IDENTITY, "score_case", case)
        self.assertEqual(call["error"]["path"], "/observation")
        self.assertIsNone(call["raw_report"])
        self.assertTrue(call["actual_call_attempted"])

    def test_contracts_cannot_enter_either_decision_path(self):
        tasks = load_json(ROOT.parent / "aggregate_read_v1/fixtures/tasks.json")["tasks"]
        scripts = load_json(ROOT.parent / "aggregate_read_v1/fixtures/scripts.json")
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path.name in {"contracts.json", "expectations.json"} or "researchops_behavior_eval_v1" in str(path):
                raise AssertionError("gold/scorer read during decision")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            fixed_run(tasks[0], "gold-free-fixed")
            r = bootstrap.aggregate_run.LOOP.run_until_complete(agent_run(tasks[0], scripts["AGR-01"], "gold-free-agent"))
        self.assertEqual(r["status"], "completed")
        for path in self.result["observations"]["paths"].values():
            for row in path["rows"]:
                self.assertNotIn("contract", row["observation"])

    def test_actual_cli_reports_and_keeps_input(self):
        from researchops_behavior_eval_v1.__main__ import main as cli
        plan = self.result["business"]["agent"]["input_plan"]
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="cli-test-") as directory:
            input_path, output_path = Path(directory) / "plan.json", Path(directory) / "report.json"
            input_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            before = input_path.read_bytes()
            self.assertEqual(cli(["--input", str(input_path), "--output", str(output_path)]), 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report, self.result["business"]["agent"]["scorer_call"]["raw_report"])
            self.assertEqual(input_path.read_bytes(), before)
            with patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(cli(["--input", str(input_path), "--output", str(output_path)]), 2)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), report)

    def test_real_cli_error_has_location(self):
        from researchops_behavior_eval_v1.__main__ import main as cli
        plan = deepcopy(self.result["business"]["agent"]["input_plan"])
        del plan["tasks"][0]["observation"]["final_output"]
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="cli-error-") as directory:
            source, target = Path(directory) / "invalid.json", Path(directory) / "report.json"
            source.write_text(json.dumps(plan), encoding="utf-8")
            captured = io.StringIO()
            with patch("sys.stderr", captured):
                self.assertEqual(cli(["--input", str(source), "--output", str(target)]), 2)
            error = json.loads(captured.getvalue())
            self.assertEqual(error["error"], "invalid_input")
            self.assertEqual(error["location"], "/observation")
            self.assertFalse(target.exists())

    def test_offline_fence_in_scoring_process(self):
        with socket.socket() as sock:
            with self.assertRaises(PermissionError):
                sock.connect(("192.0.2.1", 443))
        with self.assertRaises(PermissionError):
            socket.getaddrinfo("example.invalid", 443)
        with socket.socket(type=socket.SOCK_DGRAM) as sock:
            with self.assertRaises(PermissionError):
                sock.sendto(b"denied", ("192.0.2.1", 9))
        real = type(os.environ).__getitem__
        def no_keys(env, key):
            if "KEY" in key.upper() or "SECRET" in key.upper():
                raise AssertionError("secret read")
            return real(env, key)
        with patch.object(type(os.environ), "__getitem__", no_keys):
            report = bootstrap.PACKAGE.score_plan(self.result["business"]["agent"]["input_plan"])
        self.assertEqual(report["counts"]["planned"], 8)
