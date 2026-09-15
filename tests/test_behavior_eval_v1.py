"""Only new evaluator tests; no existing runner, Provider, or legacy corpus import."""

import argparse
import ast
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from decimal import localcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from researchops_behavior_eval_v1 import ContractError, parse_answer, score_case, score_plan
from researchops_behavior_eval_v1.__main__ import main as cli_main
from researchops_behavior_eval_v1.core import DIMENSIONS, VERSION

FIXTURE = json.loads((ROOT / "evals/internal_behavior_eval_v1/scorer_cases.json").read_text(encoding="utf-8"))


def materialize(group, case):
    """Apply authored input changes only; never consult expected states or the scorer."""
    data = copy.deepcopy(FIXTURE["base"])
    for change in group["changes"] + case["changes"]:
        cursor = data
        path = change["path"]
        for component in path[:-1]:
            cursor = cursor[int(component)] if type(cursor) is list else cursor[component]
        key = int(path[-1]) if type(cursor) is list else path[-1]
        if change.get("delete"):
            del cursor[key]
        else:
            cursor[key] = copy.deepcopy(change["value"])
    task_id = group["name"] + "-" + case["name"]
    data["contract"]["task_id"] = task_id
    if data["observation"] is not None:
        data["observation"]["task_id"] = task_id
    return data


def run_case(data):
    return score_case(data["contract"], data["observation"], data["allowed_evidence"])


def reason_codes(report):
    return {c["code"] for d in report["dimensions"].values() for c in d["checks"]}


def selected(group_name, case_name):
    group = next(g for g in FIXTURE["groups"] if g["name"] == group_name)
    case = next(c for c in group["cases"] if c["name"] == case_name)
    return materialize(group, case)


def example_plan():
    names = [("g01_numeric_tolerance", "correct"), ("g06_citation", "nonexistent"),
             ("g05_lexical_escape", "escape"), ("g11_delivery", "not_executed"),
             ("g11_delivery", "observation_missing"), ("g08_stop_behavior", "clarify_zh")]
    return {"schema_version": VERSION, "source_kind": "synthetic_fixture",
            "tasks": [selected(*pair) for pair in names]}


class FixtureTests(unittest.TestCase):
    pass


def fixture_test(group, case):
    def test(self):
        data = materialize(group, case)
        if case["invalid_input"]:
            with self.assertRaises(ContractError) as caught:
                run_case(data)
            self.assertEqual(caught.exception.code, "invalid_input")
            self.assertTrue(caught.exception.path.startswith("/"))
            return
        report = run_case(data)
        states = [FIXTURE["legend"][letter] for letter in case["expected_states"]]
        self.assertEqual([report["dimensions"][d]["status"] for d in DIMENSIONS], states, case["why"])
        expected_verdict = "fail" if "fail" in states else "unknown" if "unknown" in states else "pass"
        self.assertEqual(report["task_verdict"], expected_verdict)
        self.assertTrue(set(case["expected_codes"]) <= reason_codes(report))
        self.assertEqual(report["manual_review_required"], any(
            check["status"] == "unknown" for d in report["dimensions"].values() for check in d["checks"]))
        for dimension in report["dimensions"].values():
            for check in dimension["checks"]:
                self.assertTrue(check["location"].startswith("/"))
    test.__doc__ = case["why"]
    return test


for fixture_group in FIXTURE["groups"]:
    for fixture_case in fixture_group["cases"]:
        setattr(FixtureTests, "test_" + fixture_group["name"] + "_" + fixture_case["name"],
                fixture_test(fixture_group, fixture_case))


class InvariantTests(unittest.TestCase):
    def test_fixture_provenance_and_group_count(self):
        self.assertEqual(FIXTURE["source_kind"], "synthetic_fixture")
        self.assertEqual(len(FIXTURE["groups"]), 12)
        self.assertTrue(all(g["why"] and all(c["why"] for c in g["cases"]) for g in FIXTURE["groups"]))

    def test_fixed_denominator(self):
        result = score_plan(example_plan())
        self.assertEqual(result["counts"], {"planned": 6, "observed": 4, "pass": 2, "fail": 2,
                                          "unknown": 2, "not_executed": 1, "observation_missing": 1})
        self.assertEqual(result["confirmed_pass_fraction"], {"numerator": 2, "denominator": 6})
        self.assertEqual(result["dimension_counts"]["facts"]["denominator"], 5)
        self.assertEqual(result["dimension_counts"]["expression"]["denominator"], 0)
        self.assertEqual(result["dimension_counts"]["behavior"]["denominator"], 6)
        self.assertEqual(result["claim_boundary"], "evaluator_test_examples_only")

    def test_deterministic_no_mutation_no_aliasing(self):
        data = selected("g01_numeric_tolerance", "boundary")
        before = copy.deepcopy(data)
        first, second = run_case(data), run_case(data)
        self.assertEqual(first, second)
        self.assertEqual(data, before)
        first["dimensions"]["facts"]["checks"].clear()
        self.assertEqual(run_case(data), second)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(run_case(data), second)

    def test_parser_does_not_read_gold(self):
        data = selected("g01_numeric_tolerance", "correct")
        extracted = parse_answer(data["observation"]["final_output"])
        data["contract"]["facts"][0]["value"] = "99"
        self.assertEqual(parse_answer(data["observation"]["final_output"]), extracted)
        result = run_case(data)
        self.assertEqual(result["dimensions"]["facts"]["status"], "fail")
        self.assertEqual(result["dimensions"]["evidence"]["status"], "pass")

    def test_core_no_io_or_runtime_dependency(self):
        source = (ROOT / "src/researchops_behavior_eval_v1/core.py").read_text(encoding="utf-8")
        imports = [n.module if isinstance(n, ast.ImportFrom) else a.name
                   for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.Import, ast.ImportFrom))
                   for a in (n.names if isinstance(n, ast.Import) else [None])]
        self.assertEqual(set(imports), {"json", "re", "decimal"})
        data = selected("g01_numeric_tolerance", "correct")
        with patch("builtins.open", side_effect=AssertionError("core file IO")), \
             patch("pathlib.Path.open", side_effect=AssertionError("core path IO")), \
             patch("socket.socket", side_effect=AssertionError("core network IO")), \
             patch("sqlite3.connect", side_effect=AssertionError("core database IO")):
            self.assertEqual(run_case(data)["task_verdict"], "pass")
            self.assertEqual(score_plan({"schema_version": VERSION, "source_kind": "synthetic_fixture", "tasks": [data]})["counts"]["pass"], 1)

    def test_tool_order_arguments_and_status(self):
        data = selected("g01_numeric_tolerance", "correct")
        data["observation"]["events"][0].update(tool="other", arguments={"sample": "B"}, status="failed")
        result = run_case(data)
        self.assertTrue({"tool_order_mismatch", "tool_arguments_mismatch", "tool_status_mismatch", "evidence_not_produced"} <= reason_codes(result))

    def test_optional_extra_is_checked(self):
        data = selected("g01_numeric_tolerance", "correct")
        data["contract"]["facts"].append({"subject": "B", "metric": "mass", "value": "5", "unit": "mg", "atol": "0", "rtol": "0", "required": False})
        self.assertEqual(run_case(data)["task_verdict"], "pass")
        data["observation"]["final_output"] += "\nThe mass of B is 7 mg [R1]."
        self.assertEqual(run_case(data)["dimensions"]["facts"]["status"], "fail")

    def test_json_empty_answer_is_delivery_failure(self):
        data = selected("g10_expression", "valid")
        data["observation"]["final_output"] = '{"answer":""}'
        result = run_case(data)
        self.assertEqual(result["dimensions"]["expression"]["status"], "pass")
        # Explicit v1.1 revision: empty JSON answer is not zero-byte final_output.
        self.assertIn("empty_answer", reason_codes(result))
        self.assertNotIn("empty_output", reason_codes(result))
        self.assertEqual(result["dimensions"]["delivery"]["status"], "fail")
        self.assertEqual(result["task_verdict"], "fail")

    def test_no_short_circuit_even_when_unknown_exists(self):
        data = selected("g12_contract_and_multiple", "multiple")
        data["observation"]["events_complete"] = False
        result = run_case(data)
        self.assertEqual(result["task_verdict"], "fail")
        self.assertTrue(result["manual_review_required"])
        self.assertTrue({"value_mismatch", "evidence_not_allowed", "prohibited_side_effect", "timeout", "event_coverage_incomplete"} <= reason_codes(result))

    def test_duplicate_or_mixed_plan_rejected(self):
        plan = example_plan()
        plan["tasks"].append(copy.deepcopy(plan["tasks"][0]))
        with self.assertRaises(ContractError):
            score_plan(plan)
        plan = example_plan()
        plan["source_kind"] = "internal/developer-known"
        with self.assertRaises(ContractError):
            score_plan(plan)

    def test_malformed_observations_and_contracts(self):
        for field, value in (("completion", None), ("events_complete", 1), ("final_output", 1), ("events", {})):
            data = selected("g01_numeric_tolerance", "correct")
            data["observation"][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                run_case(data)
        for value in ("NaN", "Infinity", "1e999", "１２.４", True):
            data = selected("g01_numeric_tolerance", "correct")
            data["contract"]["facts"][0]["value"] = value
            with self.subTest(value=value), self.assertRaises(ContractError):
                run_case(data)

    def test_reference_loss_is_unknown_not_na(self):
        data = selected("g01_numeric_tolerance", "correct")
        data["observation"] = None
        result = run_case(data)
        self.assertEqual(result["dimensions"]["evidence"]["status"], "unknown")

    def test_unavailable_evidence_content_is_not_empty_content(self):
        data = selected("g01_numeric_tolerance", "correct")
        data["allowed_evidence"][0]["facts"] = None
        result = run_case(data)
        self.assertEqual(result["dimensions"]["evidence"]["status"], "unknown")
        self.assertIn("evidence_content_unavailable", reason_codes(result))
        data["allowed_evidence"][0]["facts"] = []
        self.assertEqual(run_case(data)["dimensions"]["evidence"]["status"], "fail")

    def test_order_and_partial_event_alignment(self):
        data = selected("g01_numeric_tolerance", "correct")
        data["contract"]["behavior"]["calls"].insert(0, {"tool": "inspect", "arguments": {"sample": "A"}, "status": "succeeded"})
        earlier = {"call_id": "c0", "tool": "inspect", "arguments": {"sample": "A"}, "status": "succeeded", "produced_artifacts": []}
        data["observation"]["events_complete"] = False
        self.assertEqual(run_case(data)["dimensions"]["behavior"]["status"], "unknown")
        data["observation"]["events"].insert(0, earlier)
        data["observation"]["events_complete"] = True
        self.assertEqual(run_case(data)["task_verdict"], "pass")
        data["observation"]["events"].reverse()
        self.assertIn("tool_order_mismatch", reason_codes(run_case(data)))
        data["observation"]["events_complete"] = False
        self.assertIn("tool_order_mismatch", reason_codes(run_case(data)))

    def test_faulty_implementations_are_killed_by_authored_expectations(self):
        source = (ROOT / "src/researchops_behavior_eval_v1/core.py").read_text(encoding="utf-8")
        mutations = {
            "ignore_numeric_errors": ('return None if abs(left - right) <= limit else "value_mismatch"', 'return None'),
            "ignore_production": ('if not produced:', 'if False:'),
            "ignore_side_effects": ('if observation["side_effects"]:', 'if False:'),
            "ignore_citation_absence": ('add("evidence", "fail", "citation_missing", claim["location"])', 'pass'),
            "unknown_as_pass": ('else "unknown" if "unknown" in required_states else "pass"', 'else "pass"'),
        }
        for name, (old, new) in mutations.items():
            self.assertEqual(source.count(old), 1, name)
            namespace = {}
            exec(compile(source.replace(old, new), "<offline-evaluator-mutant>", "exec"), namespace)
            killed = False
            for group in FIXTURE["groups"]:
                for case in group["cases"]:
                    if case["invalid_input"]:
                        continue
                    data = materialize(group, case)
                    report = namespace["score_case"](data["contract"], data["observation"], data["allowed_evidence"])
                    states = [FIXTURE["legend"][letter] for letter in case["expected_states"]]
                    verdict = "fail" if "fail" in states else "unknown" if "unknown" in states else "pass"
                    if ([report["dimensions"][d]["status"] for d in DIMENSIONS] != states
                            or report["task_verdict"] != verdict
                            or not set(case["expected_codes"]) <= reason_codes(report)):
                        killed = True
                        break
                if killed:
                    break
            self.assertTrue(killed, "authored examples did not detect " + name)

    def test_all_declared_fields_reject_wrong_types(self):
        baseline = selected("g01_numeric_tolerance", "correct")
        # Exercise strict entry-point boundaries, not just the fixture happy paths.
        for name in ("contract", "observation", "allowed_evidence"):
            for wrong in (False, 0, "invalid", [] if name != "allowed_evidence" else {}):
                data = copy.deepcopy(baseline)
                data[name] = wrong
                with self.subTest(name=name, wrong=wrong), self.assertRaises(ContractError):
                    run_case(data)

    def test_cli_report_and_exclusive_output(self):
        with tempfile.TemporaryDirectory(prefix="behavior-v1-") as folder:
            input_path, output_path = Path(folder) / "input.json", Path(folder) / "report.json"
            input_path.write_text(json.dumps(example_plan()), encoding="utf-8")
            args = ["--input", str(input_path), "--output", str(output_path)]
            self.assertEqual(cli_main(args), 0)
            before = output_path.read_bytes()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli_main(args), 2)
            self.assertEqual(output_path.read_bytes(), before)
            result = json.loads(before)
            self.assertEqual(result["counts"]["planned"], 6)

    def test_cli_invalid_input_does_not_create_report(self):
        with tempfile.TemporaryDirectory(prefix="behavior-v1-") as folder:
            input_path, output_path = Path(folder) / "input.json", Path(folder) / "report.json"
            for payload in ('{"tasks":[],"tasks":[]}', '{"value":NaN}', '{}'):
                input_path.write_text(payload, encoding="utf-8")
                error = io.StringIO()
                with redirect_stderr(error):
                    self.assertEqual(cli_main(["--input", str(input_path), "--output", str(output_path)]), 2)
                self.assertFalse(output_path.exists())
                self.assertIn("error", json.loads(error.getvalue()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run only evaluator v1 offline tests")
    parser.add_argument("--report", type=Path, help="New machine-readable unittest report")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    exit_code = 0 if result.wasSuccessful() else 1
    report = {"scope": "internal_behavior_eval_v1_offline_tests_only", "source_kind": "synthetic_fixture",
              "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skips": len(result.skipped), "exit_code": exit_code,
              "failure_details": [{"test": str(test), "traceback": trace} for test, trace in result.failures],
              "error_details": [{"test": str(test), "traceback": trace} for test, trace in result.errors],
              "skip_details": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
              "python": sys.version}
    if args.report:
        with args.report.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    raise SystemExit(exit_code)
