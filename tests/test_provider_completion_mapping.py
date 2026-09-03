from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_telemetry.mapping import (
    CompletionMappingError,
    map_completion,
    may_claim_truncation_excluded,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "evals" / "provider_completion_telemetry_v1"
MAPPING_PATH = PACKAGE_ROOT / "provider_completion_mapping_v1.json"
MANIFEST_PATH = PACKAGE_ROOT / "fixture_manifest.json"
COVERAGE_PATH = PACKAGE_ROOT / "rule_coverage_v1.json"
FIXTURE_ROOT = PACKAGE_ROOT / "fixtures"
MODULE_PATH = ROOT / "src" / "researchops_completion_telemetry" / "mapping.py"

TARGETED_INLINE_SELECTION_CASES = {
    "deepseek_missing_vs_null": (
        "deepseek",
        {"finish_reason": None},
        "deepseek-cc-absent-002",
    ),
    "openai_unknown_incomplete_reason": (
        "openai",
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "future_reason"},
        },
        "openai-resp-unknown-002",
    ),
}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = child
    return value


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def _collect_rules(mapping: dict[str, object]) -> dict[str, dict[str, object]]:
    providers = mapping["providers"]
    assert isinstance(providers, dict)
    rules: dict[str, dict[str, object]] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if {
                "rule_id",
                "precedence_stage",
                "condition",
                "normalized_completion_state",
                "truncation_signal_source",
            }.issubset(value):
                rule_id = value["rule_id"]
                assert isinstance(rule_id, str)
                if rule_id in rules:
                    raise AssertionError(f"duplicate rule_id: {rule_id}")
                rules[rule_id] = value
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(providers)
    return rules


def _reverse_rule_arrays(value: object) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _reverse_rule_arrays(child)
    elif isinstance(value, list):
        if value and all(isinstance(child, dict) and "rule_id" in child for child in value):
            value.reverse()
        for child in value:
            _reverse_rule_arrays(child)


class ProviderCompletionMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mapping = _load_json(MAPPING_PATH)
        cls.manifest = _load_json(MANIFEST_PATH)
        cls.coverage = _load_json(COVERAGE_PATH)
        cls.fixtures = {
            fixture["fixture_id"]: fixture
            for fixture in (
                _load_json(path) for path in sorted(FIXTURE_ROOT.glob("*.json"))
            )
        }

    def test_all_fixture_expectations_execute_as_exact_four_tuples(self) -> None:
        expectations = self.mapping["fixture_expectations"]
        self.assertIsInstance(expectations, dict)
        self.assertEqual(set(expectations), set(self.fixtures))
        self.assertEqual(len(self.fixtures), 29)

        mapping_before = copy.deepcopy(self.mapping)
        for fixture_id, fixture in self.fixtures.items():
            with self.subTest(fixture_id=fixture_id):
                projection = fixture["response_projection"]
                projection_before = copy.deepcopy(projection)
                expected = expectations[fixture_id]
                self.assertEqual(
                    map_completion(projection, fixture["provider_id"], self.mapping),
                    (
                        expected["normalized_completion_state"],
                        expected["truncation_signal_source"],
                        expected["preserved_native_value"],
                        expected["matched_rule_id"],
                    ),
                )
                self.assertEqual(projection, projection_before)
        self.assertEqual(self.mapping, mapping_before)

    def test_manifest_commits_every_fixture_mapping_and_coverage_file(self) -> None:
        entries = {entry["fixture_id"]: entry for entry in self.manifest["fixtures"]}
        self.assertEqual(set(entries), set(self.fixtures))
        for fixture_id, entry in entries.items():
            with self.subTest(fixture_id=fixture_id):
                raw = (FIXTURE_ROOT / entry["file"]).read_bytes()
                self.assertEqual(len(raw), entry["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
                fixture = self.fixtures[fixture_id]
                self.assertEqual(
                    fixture["provenance"]["tier"], entry["provenance_tier"]
                )
                self.assertEqual(
                    fixture["provenance"]["unverified_shape"],
                    entry["unverified_shape"],
                )

        for key, path in (
            ("mapping", MAPPING_PATH),
            ("rule_coverage", COVERAGE_PATH),
        ):
            raw = path.read_bytes()
            commitment = self.manifest[key]
            self.assertEqual(len(raw), commitment["bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), commitment["sha256"])

        self.assertEqual(self.manifest["summary"]["fixture_count"], 29)
        self.assertEqual(
            self.manifest["summary"]["provenance_counts"],
            {"doc_prose": 4, "live_capture": 4, "official_schema": 21},
        )
        privacy = self.manifest["privacy_review"]
        self.assertEqual(privacy["manual_review_scope"], "all_29_fixtures")
        self.assertTrue(privacy["manual_review_completed"])
        self.assertEqual(privacy["manual_review_finding_count"], 0)

    def test_missing_and_null_take_distinct_rules(self) -> None:
        missing = map_completion({}, "deepseek", self.mapping)
        explicit_null = map_completion({"finish_reason": None}, "deepseek", self.mapping)
        self.assertEqual(missing[:3], ("not_provided", "none", None))
        self.assertEqual(explicit_null[:3], ("not_provided", "none", None))
        self.assertEqual(missing[3], "deepseek-cc-absent-001")
        self.assertEqual(explicit_null[3], "deepseek-cc-absent-002")
        self.assertNotEqual(missing[3], explicit_null[3])

    def test_rule_array_order_does_not_change_any_fixture_result(self) -> None:
        reordered = copy.deepcopy(self.mapping)
        _reverse_rule_arrays(reordered["providers"])
        for fixture_id, fixture in self.fixtures.items():
            with self.subTest(fixture_id=fixture_id):
                self.assertEqual(
                    map_completion(
                        fixture["response_projection"],
                        fixture["provider_id"],
                        reordered,
                    ),
                    map_completion(
                        fixture["response_projection"],
                        fixture["provider_id"],
                        self.mapping,
                    ),
                )

    def test_mapping_precedence_not_container_order_selects_conflict(self) -> None:
        fixture = self.fixtures[
            "anthropic_conflict_end_turn_with_stop_sequence_20260902"
        ]
        normal = map_completion(
            fixture["response_projection"], fixture["provider_id"], self.mapping
        )
        self.assertEqual(normal[3], "anthropic-msg-conflict-001")

        changed = copy.deepcopy(self.mapping)
        precedence = changed["mapping_precedence"]
        precedence[0], precedence[1] = precedence[1], precedence[0]
        selected = map_completion(
            fixture["response_projection"], fixture["provider_id"], changed
        )
        self.assertEqual(selected[3], "anthropic-msg-recognized-001")

    def test_multiple_matches_in_one_stage_fail_closed(self) -> None:
        ambiguous = copy.deepcopy(self.mapping)
        provider = ambiguous["providers"]["deepseek"]
        duplicate = copy.deepcopy(provider["active_rules"][0])
        duplicate["rule_id"] = "deepseek-cc-recognized-999"
        provider["active_rules"].append(duplicate)
        ambiguous["precedence_stage_contract"][
            "recognized_non_null_native_value"
        ]["materialized_rule_count"] += 1
        ambiguous["materialized_provider_rule_count"] += 1
        with self.assertRaises(CompletionMappingError) as context:
            map_completion({"finish_reason": "stop"}, "deepseek", ambiguous)
        self.assertEqual(context.exception.code, "mapping_rule_ambiguous")

        lower_stage_ambiguous = copy.deepcopy(self.mapping)
        provider = lower_stage_ambiguous["providers"]["anthropic"]
        duplicate = copy.deepcopy(provider["active_rules"][0])
        duplicate["rule_id"] = "anthropic-msg-recognized-999"
        provider["active_rules"].append(duplicate)
        lower_stage_ambiguous["precedence_stage_contract"][
            "recognized_non_null_native_value"
        ]["materialized_rule_count"] += 1
        lower_stage_ambiguous["materialized_provider_rule_count"] += 1
        with self.assertRaises(CompletionMappingError) as lower:
            map_completion(
                {
                    "stop_reason": "end_turn",
                    "stop_sequence": "unexpected",
                },
                "anthropic",
                lower_stage_ambiguous,
            )
        self.assertEqual(lower.exception.code, "mapping_rule_ambiguous")

    def test_default_unknown_rules_are_explicit_and_unambiguous(self) -> None:
        status_unknown = map_completion(
            {"status": "future_status"}, "openai", self.mapping
        )
        reason_unknown = map_completion(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "future_reason"},
            },
            "openai",
            self.mapping,
        )
        self.assertEqual(status_unknown[3], "openai-resp-unknown-001")
        self.assertEqual(status_unknown[2], "future_status")
        self.assertEqual(reason_unknown[3], "openai-resp-unknown-002")
        self.assertEqual(reason_unknown[2], "future_reason")

    def test_non_object_incomplete_details_is_a_conflict_not_missing_reason(self) -> None:
        for value in ("unexpected-scalar", [], 42, True):
            with self.subTest(value=value):
                result = map_completion(
                    {"status": "incomplete", "incomplete_details": value},
                    "openai",
                    self.mapping,
                )
                self.assertEqual(
                    result,
                    (
                        "unmapped",
                        "native_status",
                        None,
                        "openai-resp-conflict-006",
                    ),
                )
                self.assertFalse(
                    may_claim_truncation_excluded(
                        [(result[0], result[1])], self.mapping
                    )
                )

    def test_non_string_anthropic_stop_sequence_is_a_conflict(self) -> None:
        for value in (7, [], {}, True):
            with self.subTest(value=value):
                result = map_completion(
                    {"stop_reason": "stop_sequence", "stop_sequence": value},
                    "anthropic",
                    self.mapping,
                )
                self.assertEqual(
                    result,
                    (
                        "unmapped",
                        "native_status",
                        None,
                        "anthropic-msg-conflict-011",
                    ),
                )
                self.assertFalse(
                    may_claim_truncation_excluded(
                        [(result[0], result[1])], self.mapping
                    )
                )

    def test_rule_ids_stages_and_materialized_counts_are_closed(self) -> None:
        rules = _collect_rules(self.mapping)
        precedence = self.mapping["mapping_precedence"]
        self.assertEqual(len(rules), 55)
        self.assertEqual(len(set(rules)), 55)
        rule_id_pattern = re.compile(
            r"^(deepseek-cc|openai-resp|anthropic-msg|kimi-cc)-"
            r"(conflict|recognized|unknown|absent)-\d{3}$"
        )
        self.assertTrue(all(rule_id_pattern.fullmatch(rule_id) for rule_id in rules))
        self.assertEqual(
            {rule["precedence_stage"] for rule in rules.values()}, set(precedence)
            - {"token_cap_fallback_when_native_metadata_absent"}
        )
        counts = {
            stage: sum(
                rule["precedence_stage"] == stage for rule in rules.values()
            )
            for stage in precedence
        }
        self.assertEqual(
            counts,
            {
                "contradictory_native_metadata": 17,
                "recognized_non_null_native_value": 23,
                "unknown_non_null_native_value": 5,
                "token_cap_fallback_when_native_metadata_absent": 0,
                "not_provided_when_native_metadata_absent_and_fallback_not_applicable": 10,
            },
        )
        stage_contract = self.mapping["precedence_stage_contract"]
        for stage, count in counts.items():
            self.assertEqual(stage_contract[stage]["materialized_rule_count"], count)
        fallback = stage_contract["token_cap_fallback_when_native_metadata_absent"]
        self.assertFalse(fallback["executable_in_v1"])
        self.assertIn("does not contain", fallback["deferred_reason"])

    def test_run_level_truncation_exclusion_regressions(self) -> None:
        self.assertFalse(
            may_claim_truncation_excluded(
                [("unmapped", "native_status"), ("unmapped", "native_status")],
                self.mapping,
            )
        )
        self.assertTrue(
            may_claim_truncation_excluded(
                [("completed", "native_status"), ("completed", "native_status")],
                self.mapping,
            )
        )
        self.assertFalse(
            may_claim_truncation_excluded(
                [
                    ("completed", "native_status"),
                    ("completed", "token_cap_fallback"),
                ],
                self.mapping,
            )
        )
        self.assertFalse(may_claim_truncation_excluded([], self.mapping))
        self.assertFalse(
            may_claim_truncation_excluded([([], "native_status")], self.mapping)
        )
        for invalid in (None, 0, True, "completed/native_status", {}):
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    may_claim_truncation_excluded(invalid, self.mapping)  # type: ignore[arg-type]
                )

    def test_run_level_policy_is_read_from_the_mapping(self) -> None:
        policy = self.mapping["global_rules"][
            "additional_truncation_exclusion_claim"
        ]
        self.assertEqual(
            set(policy["allowed_only_if_every_response_normalized_completion_state_in"]),
            {
                "completed",
                "incomplete_length",
                "incomplete_content_filter",
                "incomplete_other",
                "error",
            },
        )
        self.assertEqual(
            policy["allowed_only_if_every_response_truncation_signal_source_equals"],
            "native_status",
        )
        self.assertEqual(
            set(policy["ineligible_normalized_completion_states"]),
            {"unmapped", "not_provided", "not_persisted"},
        )
        self.assertEqual(
            self.mapping["native_completion_signal_source"], "native_status"
        )
        self.assertEqual(policy["minimum_response_count"], 1)
        self.assertTrue(policy["empty_response_set_forbids_claim"])
        self.assertTrue(policy["unmapped_forbids_claim"])
        self.assertEqual(
            policy["runtime_binding"],
            "researchops_completion_telemetry.mapping.may_claim_truncation_excluded",
        )

        changed = copy.deepcopy(self.mapping)
        changed_policy = changed["global_rules"][
            "additional_truncation_exclusion_claim"
        ]
        changed_policy[
            "allowed_only_if_every_response_normalized_completion_state_in"
        ].remove("completed")
        changed_policy["ineligible_normalized_completion_states"].append("completed")
        self.assertFalse(
            may_claim_truncation_excluded(
                [("completed", "native_status")], changed
            )
        )

    def test_run_level_self_contradictory_policies_fail_closed(self) -> None:
        mutations = []

        allows_unmapped = copy.deepcopy(self.mapping)
        allows_unmapped_policy = allows_unmapped["global_rules"][
            "additional_truncation_exclusion_claim"
        ]
        allows_unmapped_policy[
            "allowed_only_if_every_response_normalized_completion_state_in"
        ] = ["unmapped"]
        mutations.append(allows_unmapped)

        wrong_source = copy.deepcopy(self.mapping)
        wrong_source["global_rules"]["additional_truncation_exclusion_claim"][
            "allowed_only_if_every_response_truncation_signal_source_equals"
        ] = "none"
        mutations.append(wrong_source)

        empty_state = copy.deepcopy(self.mapping)
        empty_state["global_rules"]["additional_truncation_exclusion_claim"][
            "allowed_only_if_every_response_normalized_completion_state_in"
        ] = [""]
        mutations.append(empty_state)

        missing_empty_guard = copy.deepcopy(self.mapping)
        missing_empty_guard["global_rules"][
            "additional_truncation_exclusion_claim"
        ].pop("empty_response_set_forbids_claim")
        mutations.append(missing_empty_guard)

        invalid_rationale = copy.deepcopy(self.mapping)
        invalid_rationale["global_rules"][
            "additional_truncation_exclusion_claim"
        ]["unmapped_rationale"] = None
        mutations.append(invalid_rationale)

        for index, changed in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(CompletionMappingError) as context:
                    may_claim_truncation_excluded(
                        [("completed", "native_status")], changed
                    )
                self.assertEqual(context.exception.code, "mapping_contract_invalid")

        malformed_rule_universe = copy.deepcopy(self.mapping)
        malformed_rule_universe["providers"]["anthropic"]["conflict_rules"][
            0
        ].pop("condition")
        with self.assertRaises(CompletionMappingError) as malformed:
            may_claim_truncation_excluded(
                [("completed", "native_status")], malformed_rule_universe
            )
        self.assertEqual(malformed.exception.code, "mapping_rule_shape_invalid")

    def test_rule_coverage_report_is_exact_and_does_not_overclaim(self) -> None:
        rules = _collect_rules(self.mapping)
        fixture_to_rule = {
            fixture_id: map_completion(
                fixture["response_projection"], fixture["provider_id"], self.mapping
            )[3]
            for fixture_id, fixture in self.fixtures.items()
        }
        covered = set(fixture_to_rule.values())
        self.assertEqual(
            self.coverage["schema_version"],
            "provider-completion-rule-coverage/1.1",
        )
        self.assertEqual(len(covered), 29)
        self.assertEqual(len(rules), 55)
        self.assertEqual(self.coverage["materialized_rule_count"], 55)
        self.assertEqual(self.coverage["fixture_covered_rule_count"], 29)
        self.assertEqual(self.coverage["uncovered_rule_count"], 26)
        self.assertEqual(self.coverage["coverage_ratio"], "29/55")
        self.assertEqual(self.coverage["coverage_percent"], "52.73")
        self.assertEqual(self.coverage["fixture_to_matched_rule_id"], fixture_to_rule)
        self.assertEqual(set(self.coverage["covered_rule_ids"]), covered)
        self.assertEqual(set(self.coverage["uncovered_rule_ids"]), set(rules) - covered)
        self.assertEqual(
            self.coverage["mapping_sha256"],
            hashlib.sha256(MAPPING_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            self.coverage["attribution_boundary"],
            {
                "live_attribution_fixture_count": 2,
                "live_attribution_rule_count": 2,
                "fixture_branch_coverage_is_attribution_evidence": False,
            },
        )

    def test_targeted_inline_selection_and_never_selected_counts_are_exact(self) -> None:
        rules = _collect_rules(self.mapping)
        fixture_selected = set(self.coverage["covered_rule_ids"])
        inline_selected = {
            map_completion(projection, provider_id, self.mapping)[3]
            for provider_id, projection, _ in TARGETED_INLINE_SELECTION_CASES.values()
        }
        expected_inline = {
            expected_rule_id
            for _, _, expected_rule_id in TARGETED_INLINE_SELECTION_CASES.values()
        }
        self.assertEqual(
            inline_selected - fixture_selected,
            {
                "deepseek-cc-absent-002",
                "openai-resp-unknown-002",
            },
        )
        self.assertEqual(inline_selected, expected_inline)
        combined_selected = fixture_selected | inline_selected
        never_selected = set(rules) - combined_selected
        self.assertEqual(len(fixture_selected), 29)
        self.assertEqual(len(combined_selected), 31)
        self.assertEqual(len(never_selected), 24)
        selection = self.coverage["selection_coverage"]
        self.assertEqual(selection["selection_definition"], "final_matched_rule_id")
        self.assertEqual(selection["fixture_selected_rule_count"], 29)
        self.assertEqual(selection["targeted_inline_only_rule_count"], 2)
        self.assertEqual(
            set(selection["targeted_inline_only_rule_ids"]),
            inline_selected - fixture_selected,
        )
        self.assertEqual(selection["combined_selected_rule_count"], 31)
        self.assertEqual(selection["never_selected_rule_count"], 24)
        self.assertEqual(set(selection["never_selected_rule_ids"]), never_selected)
        self.assertFalse(selection["condition_evaluation_coverage_claimed"])

    def test_mapper_has_exact_projection_only_signature_and_no_adapter_import(self) -> None:
        self.assertEqual(
            list(inspect.signature(map_completion).parameters),
            ["response_projection", "provider_id", "mapping"],
        )
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        self.assertFalse(
            any(
                module == "researchops" or module.startswith("researchops.")
                for module in imported_modules
            )
        )
        self.assertFalse(any("adapter" in module for module in imported_modules))
        for provider_literal in ('"deepseek"', '"openai"', '"anthropic"', '"moonshot_kimi"'):
            self.assertNotIn(provider_literal, source)

    def test_importing_the_mapper_imports_no_researchops_module(self) -> None:
        probe = (
            "import sys\n"
            "import researchops_completion_telemetry.mapping as mapper\n"
            "assert callable(mapper.map_completion)\n"
            "leaked = sorted(\n"
            "    name\n"
            "    for name in sys.modules\n"
            "    if name == 'researchops' or name.startswith('researchops.')\n"
            ")\n"
            "print(repr(leaked))\n"
        )
        environment = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT / "src"),
        }
        for name in (
            "SystemRoot",
            "WINDIR",
            "PATH",
            "PATHEXT",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "TMPDIR",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        completed = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_mapper_performs_no_io(self) -> None:
        fixture = self.fixtures["deepseek_completed_20260902"]
        with patch("builtins.open", side_effect=AssertionError("I/O is forbidden")):
            result = map_completion(
                fixture["response_projection"], fixture["provider_id"], self.mapping
            )
        self.assertEqual(result[3], "deepseek-cc-recognized-001")

    def test_unknown_provider_and_no_match_fail_closed(self) -> None:
        with self.assertRaises(CompletionMappingError) as unknown:
            map_completion({}, "not-a-provider", self.mapping)
        self.assertEqual(unknown.exception.code, "mapping_provider_unknown")

        no_match = copy.deepcopy(self.mapping)
        no_match["providers"]["deepseek"]["active_rules"][0]["condition"][
            "value"
        ] = "different-recognized-value"
        with self.assertRaises(CompletionMappingError) as missing:
            map_completion({"finish_reason": "stop"}, "deepseek", no_match)
        self.assertEqual(missing.exception.code, "mapping_rule_no_match")

    def test_missing_or_extra_condition_fields_fail_closed(self) -> None:
        missing_value = copy.deepcopy(self.mapping)
        condition = missing_value["providers"]["deepseek"]["active_rules"][0][
            "condition"
        ]
        condition.pop("value")
        with self.assertRaises(CompletionMappingError) as missing:
            map_completion({"finish_reason": None}, "deepseek", missing_value)
        self.assertEqual(missing.exception.code, "mapping_condition_shape_invalid")

        extra_guard = copy.deepcopy(self.mapping)
        condition = extra_guard["providers"]["deepseek"]["active_rules"][0][
            "condition"
        ]
        condition["requires_other_metadata"] = True
        with self.assertRaises(CompletionMappingError) as extra:
            map_completion({"finish_reason": "stop"}, "deepseek", extra_guard)
        self.assertEqual(extra.exception.code, "mapping_condition_shape_invalid")

    def test_every_rule_ast_is_validated_before_any_rule_is_evaluated(self) -> None:
        malformed_lower_stage = copy.deepcopy(self.mapping)
        malformed_lower_stage["providers"]["deepseek"]["active_rules"][-1][
            "condition"
        ].pop("field")
        with self.assertRaises(CompletionMappingError) as lower:
            map_completion(
                {"finish_reason": "stop"}, "deepseek", malformed_lower_stage
            )
        self.assertEqual(lower.exception.code, "mapping_condition_shape_invalid")

        malformed_short_circuit_child = copy.deepcopy(self.mapping)
        malformed_short_circuit_child["providers"]["anthropic"]["conflict_rules"][
            0
        ]["condition"]["conditions"][1].pop("field")
        with self.assertRaises(CompletionMappingError) as child:
            map_completion(
                {"stop_reason": "max_tokens", "stop_sequence": None},
                "anthropic",
                malformed_short_circuit_child,
            )
        self.assertEqual(child.exception.code, "mapping_condition_shape_invalid")

        missing_entire_condition = copy.deepcopy(self.mapping)
        missing_entire_condition["providers"]["anthropic"]["conflict_rules"][
            0
        ].pop("condition")
        with self.assertRaises(CompletionMappingError) as missing_condition:
            map_completion(
                {
                    "stop_reason": "end_turn",
                    "stop_sequence": "unexpected",
                },
                "anthropic",
                missing_entire_condition,
            )
        self.assertEqual(
            missing_condition.exception.code,
            "mapping_rule_shape_invalid",
        )

        malformed_other_provider = copy.deepcopy(self.mapping)
        malformed_other_provider["providers"]["anthropic"]["conflict_rules"][
            0
        ].pop("condition")
        with self.assertRaises(CompletionMappingError) as cross_provider:
            map_completion(
                {"finish_reason": "stop"},
                "deepseek",
                malformed_other_provider,
            )
        self.assertEqual(
            cross_provider.exception.code,
            "mapping_rule_shape_invalid",
        )

    def test_unmatched_rule_metadata_and_invalid_json_type_fail_closed(self) -> None:
        malformed_metadata = copy.deepcopy(self.mapping)
        malformed_metadata["providers"]["deepseek"]["default_unknown_rule"][
            "preserved_native_value_utf8_bytes_max"
        ] = 0
        with self.assertRaises(CompletionMappingError) as metadata:
            map_completion({"finish_reason": "stop"}, "deepseek", malformed_metadata)
        self.assertEqual(metadata.exception.code, "mapping_rule_shape_invalid")

        malformed_json_type = copy.deepcopy(self.mapping)
        malformed_json_type["providers"]["openai"]["conflict_rules"][5][
            "condition"
        ]["conditions"][2]["condition"]["value"] = []
        with self.assertRaises(CompletionMappingError) as json_type:
            map_completion(
                {"status": "completed", "incomplete_details": None},
                "openai",
                malformed_json_type,
            )
        self.assertEqual(json_type.exception.code, "mapping_contract_invalid")

        malformed_path = copy.deepcopy(self.mapping)
        malformed_path["providers"]["deepseek"]["default_unknown_rule"][
            "preserved_native_value_field"
        ] = "finish..reason"
        with self.assertRaises(CompletionMappingError) as path:
            map_completion({"finish_reason": "stop"}, "deepseek", malformed_path)
        self.assertEqual(path.exception.code, "mapping_contract_invalid")

        undeclared_implementation = copy.deepcopy(self.mapping)
        undeclared_implementation["condition_operators"].append("future_operator")
        with self.assertRaises(CompletionMappingError) as operator:
            map_completion(
                {"finish_reason": "stop"},
                "deepseek",
                undeclared_implementation,
            )
        self.assertEqual(operator.exception.code, "mapping_contract_invalid")

    def test_duplicate_declarations_and_stage_selection_fail_closed(self) -> None:
        mutations = []
        for field in (
            "condition_operators",
            "normalized_completion_states",
            "truncation_signal_sources",
        ):
            changed = copy.deepcopy(self.mapping)
            changed[field].append(changed[field][0])
            mutations.append(changed)

        changed = copy.deepcopy(self.mapping)
        changed["precedence_stage_contract"]["contradictory_native_metadata"][
            "selection"
        ] = "array_order"
        mutations.append(changed)

        for index, changed in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(CompletionMappingError) as context:
                    map_completion(
                        {"finish_reason": "stop"},
                        "deepseek",
                        changed,
                    )
                self.assertEqual(context.exception.code, "mapping_contract_invalid")

    def test_preserved_unknown_native_value_is_bounded(self) -> None:
        boundary = "x" * 64
        self.assertEqual(
            map_completion({"finish_reason": boundary}, "deepseek", self.mapping)[2],
            boundary,
        )
        with self.assertRaises(CompletionMappingError) as over_limit:
            map_completion({"finish_reason": "x" * 65}, "deepseek", self.mapping)
        self.assertEqual(
            over_limit.exception.code,
            "mapping_preserved_native_value_over_limit",
        )
        with self.assertRaises(CompletionMappingError) as wrong_type:
            map_completion({"finish_reason": 7}, "deepseek", self.mapping)
        self.assertEqual(
            wrong_type.exception.code,
            "mapping_preserved_native_value_type_invalid",
        )
        with self.assertRaises(CompletionMappingError) as invalid_utf8:
            map_completion({"finish_reason": "\ud800"}, "deepseek", self.mapping)
        self.assertEqual(
            invalid_utf8.exception.code,
            "mapping_preserved_native_value_encoding_invalid",
        )


if __name__ == "__main__":
    unittest.main()
