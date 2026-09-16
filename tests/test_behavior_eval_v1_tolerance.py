"""Authored cross-unit tolerance regressions through the actual score_case entry point."""

import unittest

from tests.test_behavior_eval_v1 import reason_codes, run_case, selected


# name, gold(value/unit), atol, rtol, answer(value/unit), tool(value/unit), F, E, rationale.
# Expected decisions are mathematical boundary choices, never generated from scorer output.
CASES = [
    ("reported_p1", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.02", "g"), "pass", "fail",
     "20mg differs from 12.40mg by 7.60mg, exceeding the CONTRACT's 0.01mg tolerance."),
    ("same_unit_exact", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("12.40", "mg"), "pass", "pass", "Equal values."),
    ("same_unit_boundary", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("12.41", "mg"), "pass", "pass", "Inclusive 0.01mg boundary."),
    ("same_unit_outside", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("12.411", "mg"), "pass", "fail", "Difference 0.011mg."),
    ("cross_unit_equivalent", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.0124", "g"), "pass", "pass", "Equivalent tool units."),
    ("cross_unit_upper_boundary", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.01241", "g"), "pass", "pass", "0.01241g is 12.41mg."),
    ("cross_unit_lower_boundary", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.01239", "g"), "pass", "pass", "Lower inclusive boundary."),
    ("cross_unit_upper_outside", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.012411", "g"), "pass", "fail", "Upper boundary exceeded."),
    ("cross_unit_lower_outside", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.012389", "g"), "pass", "fail", "Lower boundary exceeded."),
    ("reverse_equivalent", ("0.0124", "g"), "0.00001", "0", ("0.0124", "g"), ("12.4", "mg"), "pass", "pass", "Gold in g, tool in mg."),
    ("reverse_boundary", ("0.0124", "g"), "0.00001", "0", ("0.0124", "g"), ("12.41", "mg"), "pass", "pass", "0.00001g tolerance is 0.01mg."),
    ("reverse_outside", ("0.0124", "g"), "0.00001", "0", ("0.0124", "g"), ("12.411", "mg"), "pass", "fail", "Reverse conversion beyond boundary."),
    ("zero_equivalent", ("12.40", "mg"), "0", "0", ("12.40", "mg"), ("0.0124", "g"), "pass", "pass", "Zero tolerance still permits exact conversion."),
    ("zero_nonzero_difference", ("12.40", "mg"), "0", "0", ("12.40", "mg"), ("0.012400001", "g"), "pass", "fail", "Nonzero difference is rejected."),
    ("combined_absolute_boundary", ("12.40", "mg"), "0.01", "0.0001", ("12.40", "mg"), ("0.01241", "g"), "pass", "pass", "max(0.01mg,0.001241mg)=0.01mg."),
    ("combined_absolute_outside", ("12.40", "mg"), "0.01", "0.0001", ("12.40", "mg"), ("0.012411", "g"), "pass", "fail", "Outside the same combined tolerance."),
    ("relative_evidence_reference_boundary", ("12", "mg"), "0.001", "0.25", ("12", "mg"), ("0.016", "g"), "pass", "pass", "E's existing expected operand is tool evidence: 4mg = 0.25*16mg, not 0.25*12mg."),
    ("relative_evidence_reference_outside", ("12", "mg"), "0.001", "0.25", ("12", "mg"), ("0.016000001", "g"), "pass", "fail", "4.000001mg exceeds 0.25*16.000001mg."),
    ("relative_facts_reference_boundary", ("16", "mg"), "0.001", "0.25", ("0.012", "g"), ("12", "mg"), "pass", "pass", "F's expected operand remains gold: 4mg = 0.25*16mg; E has exact support."),
    ("incompatible_dimension", ("12.40", "mg"), "0.01", "0", ("12.40", "mg"), ("0.0124", "cm"), "pass", "fail", "Length cannot support mass."),
    ("answer_converted_exact", ("12.40", "mg"), "0.01", "0", ("0.0124", "g"), ("12.40", "mg"), "pass", "pass", "Existing answer conversion retained."),
    ("answer_converted_boundary", ("12.40", "mg"), "0.01", "0", ("0.01241", "g"), ("12.40", "mg"), "pass", "pass", "Answer at inclusive tolerance boundary."),
    ("answer_converted_outside", ("12.40", "mg"), "0.01", "0", ("0.012411", "g"), ("12.40", "mg"), "fail", "fail", "Both facts and evidence reject the answer beyond tolerance."),
]


def make_input(case):
    name, gold, atol, rtol, answer, tool, _, _, _ = case
    data = selected("g01_numeric_tolerance", "correct")
    data["contract"]["task_id"] = "tolerance-" + name
    data["observation"]["task_id"] = data["contract"]["task_id"]
    data["contract"]["facts"][0].update(value=gold[0], unit=gold[1], atol=atol, rtol=rtol)
    data["observation"]["final_output"] = f"The mass of A is {answer[0]} {answer[1]} [R1]."
    data["allowed_evidence"][0]["facts"][0].update(value=tool[0], unit=tool[1])
    return data


class ToleranceRegressionTests(unittest.TestCase):
    def test_uncontracted_extra_keeps_exact_comparison(self):
        for value, evidence_status, verdict in (("0.005", "pass", "unknown"), ("0.005001", "fail", "fail")):
            data = selected("g01_numeric_tolerance", "correct")
            data["observation"]["final_output"] += "\nThe mass of B is 5 mg [R1]."
            data["allowed_evidence"][0]["facts"].append({"subject": "B", "metric": "mass", "value": value, "unit": "g"})
            with self.subTest(tool_value=value):
                report = run_case(data)
                self.assertEqual(report["dimensions"]["facts"]["status"], "unknown")
                self.assertEqual(report["dimensions"]["evidence"]["status"], evidence_status)
                self.assertEqual(report["task_verdict"], verdict)


def regression_test(case):
    def test(self):
        report = run_case(make_input(case))
        expected_facts, expected_evidence = case[6:8]
        self.assertEqual({key: item["status"] for key, item in report["dimensions"].items()}, {
            "facts": expected_facts, "evidence": expected_evidence, "behavior": "pass",
            "expression": "not_applicable", "delivery": "pass"}, case[8])
        self.assertEqual(report["task_verdict"], "fail" if "fail" in (expected_facts, expected_evidence) else "pass")
        self.assertEqual(report["manual_review_required"], False)
        if expected_evidence == "fail":
            self.assertIn("evidence_content_mismatch", reason_codes(report))
        else:
            self.assertNotIn("evidence_content_mismatch", reason_codes(report))
    test.__doc__ = case[8]
    return test


for authored_case in CASES:
    setattr(ToleranceRegressionTests, "test_" + authored_case[0], regression_test(authored_case))
