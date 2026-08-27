from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops.kimi_synthetic_tools import (
    KimiSyntheticToolExecutor,
    KimiSyntheticToolError,
    MAX_ARGUMENT_BYTES,
    MAX_TOOL_EXECUTIONS,
    METRIC_ID,
    MISSING_DATASET_ID,
    SUCCESS_DATASET_ID,
    TOOL_NAME,
    execute_synthetic_tool_batch,
    synthetic_tool_schema,
)


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = (
    ROOT / "evals" / "v2" / "kimi_controlled_pilot_scenarios_v1.json"
)


def _arguments(dataset_id: str = SUCCESS_DATASET_ID) -> str:
    return json.dumps(
        {"dataset_id": dataset_id, "metric_id": METRIC_ID},
        sort_keys=True,
        separators=(",", ":"),
    )


def _call(
    call_id: str,
    *,
    dataset_id: str = SUCCESS_DATASET_ID,
    name: str = TOOL_NAME,
    arguments: str | None = None,
) -> dict[str, str]:
    return {
        "call_id": call_id,
        "name": name,
        "arguments": _arguments(dataset_id) if arguments is None else arguments,
    }


class KimiSyntheticToolTests(unittest.TestCase):
    def test_tool_schema_is_one_exact_strict_function(self) -> None:
        schema = synthetic_tool_schema()

        self.assertEqual(schema["type"], "function")
        function = schema["function"]
        self.assertEqual(function["name"], TOOL_NAME)
        self.assertIs(function["strict"], True)
        parameters = function["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertIs(parameters["additionalProperties"], False)
        self.assertEqual(
            parameters["required"], ["dataset_id", "metric_id"]
        )
        self.assertEqual(
            parameters["properties"]["dataset_id"]["enum"],
            [SUCCESS_DATASET_ID, MISSING_DATASET_ID],
        )
        self.assertEqual(
            parameters["properties"]["metric_id"]["const"], METRIC_ID
        )
        schema["function"]["name"] = "mutated"
        self.assertEqual(synthetic_tool_schema()["function"]["name"], TOOL_NAME)

    def test_success_fixture_returns_one_fixed_aggregate(self) -> None:
        result = execute_synthetic_tool_batch([_call("call_success_01")])

        self.assertEqual(result["requested_tool_call_count"], 1)
        self.assertEqual(result["deduplicated_tool_call_count"], 0)
        self.assertEqual(result["executed_tool_call_count"], 1)
        self.assertEqual(result["execution_limit"], MAX_TOOL_EXECUTIONS)
        self.assertEqual(
            result["results"],
            [
                {
                    "call_id": "call_success_01",
                    "name": TOOL_NAME,
                    "deduplicated": False,
                    "executed": True,
                    "result": {
                        "status": "ok",
                        "dataset_id": SUCCESS_DATASET_ID,
                        "metric_id": METRIC_ID,
                        "value": 0.375,
                        "unit": "synthetic_standardized_units",
                    },
                }
            ],
        )

    def test_missing_fixture_returns_stable_error_and_counts_execution(self) -> None:
        result = execute_synthetic_tool_batch(
            [_call("call_missing_01", dataset_id=MISSING_DATASET_ID)]
        )

        self.assertEqual(result["requested_tool_call_count"], 1)
        self.assertEqual(result["executed_tool_call_count"], 1)
        self.assertEqual(
            result["results"][0]["result"],
            {"status": "error", "error_code": "synthetic_metric_not_found"},
        )

    def test_idempotent_call_id_replay_is_deduplicated(self) -> None:
        first = _call("call_replay_01")
        replay_with_different_json_formatting = _call(
            "call_replay_01",
            arguments=(
                '{ "metric_id": "effect_size", '
                '"dataset_id": "kimi_synth_success_v1" }'
            ),
        )

        result = execute_synthetic_tool_batch(
            [first, replay_with_different_json_formatting]
        )

        self.assertEqual(result["requested_tool_call_count"], 2)
        self.assertEqual(result["deduplicated_tool_call_count"], 1)
        self.assertEqual(result["executed_tool_call_count"], 1)
        self.assertFalse(result["results"][0]["deduplicated"])
        self.assertTrue(result["results"][1]["deduplicated"])
        self.assertTrue(result["results"][0]["executed"])
        self.assertFalse(result["results"][1]["executed"])
        self.assertEqual(
            result["results"][0]["result"], result["results"][1]["result"]
        )

    def test_call_id_conflict_fails_before_any_execution(self) -> None:
        calls = [
            _call("call_conflict_01"),
            _call("call_conflict_01", dataset_id=MISSING_DATASET_ID),
        ]

        with (
            patch(
                "researchops.kimi_synthetic_tools._execute_validated_call"
            ) as execute,
            self.assertRaises(KimiSyntheticToolError) as caught,
        ):
            execute_synthetic_tool_batch(calls)

        self.assertEqual(caught.exception.code, "synthetic_tool_call_id_conflict")
        execute.assert_not_called()

    def test_whole_batch_is_validated_before_first_execution(self) -> None:
        calls = [
            _call("call_valid_01"),
            _call("call_invalid_02", name="not_allowlisted"),
        ]

        with (
            patch(
                "researchops.kimi_synthetic_tools._execute_validated_call"
            ) as execute,
            self.assertRaises(KimiSyntheticToolError) as caught,
        ):
            execute_synthetic_tool_batch(calls)

        self.assertEqual(caught.exception.code, "synthetic_tool_name_not_allowed")
        execute.assert_not_called()

    def test_unique_calls_execute_sequentially_in_first_seen_order(self) -> None:
        calls = [
            _call("call_order_01"),
            _call("call_order_02", dataset_id=MISSING_DATASET_ID),
            _call("call_order_01"),
        ]
        observed: list[str] = []

        def execute(call):
            observed.append(call.call_id)
            if call.arguments["dataset_id"] == SUCCESS_DATASET_ID:
                return {"status": "ok", "marker": "first"}
            return {"status": "error", "error_code": "second"}

        with patch(
            "researchops.kimi_synthetic_tools._execute_validated_call",
            side_effect=execute,
        ):
            result = execute_synthetic_tool_batch(calls)

        self.assertEqual(observed, ["call_order_01", "call_order_02"])
        self.assertEqual(result["requested_tool_call_count"], 3)
        self.assertEqual(result["deduplicated_tool_call_count"], 1)
        self.assertEqual(result["executed_tool_call_count"], 2)

    def test_execution_budget_is_checked_before_any_lookup(self) -> None:
        calls = [_call(f"call_budget_{index:02d}") for index in range(7)]

        with (
            patch(
                "researchops.kimi_synthetic_tools._execute_validated_call"
            ) as execute,
            self.assertRaises(KimiSyntheticToolError) as caught,
        ):
            execute_synthetic_tool_batch(calls)

        self.assertEqual(
            caught.exception.code, "synthetic_tool_execution_budget_exceeded"
        )
        execute.assert_not_called()

    def test_execution_budget_and_dedupe_persist_across_batches(self) -> None:
        executor = KimiSyntheticToolExecutor()
        first = executor.execute_batch(
            [_call(f"call_total_{index:02d}") for index in range(6)]
        )
        self.assertEqual(first["requested_tool_call_count_total"], 6)
        self.assertEqual(first["executed_tool_call_count_total"], 6)
        self.assertEqual(executor.requested_total, 6)
        self.assertEqual(executor.executed_total, 6)

        replay = executor.execute_batch([_call("call_total_00")])
        self.assertEqual(replay["requested_tool_call_count"], 1)
        self.assertEqual(replay["deduplicated_tool_call_count"], 1)
        self.assertEqual(replay["executed_tool_call_count"], 0)
        self.assertEqual(replay["requested_tool_call_count_total"], 7)
        self.assertEqual(replay["executed_tool_call_count_total"], 6)

        with self.assertRaises(KimiSyntheticToolError) as caught:
            executor.execute_batch([_call("call_total_06")])
        self.assertEqual(
            caught.exception.code, "synthetic_tool_execution_budget_exceeded"
        )
        self.assertEqual(executor.requested_total, 7)
        self.assertEqual(executor.executed_total, 6)

    def test_call_id_name_and_exact_fields_are_fail_closed(self) -> None:
        cases = (
            (
                {"call_id": "../unsafe", "name": TOOL_NAME, "arguments": _arguments()},
                "synthetic_tool_call_id_invalid",
            ),
            (
                _call("call_safe_01", name="lookup_synthetic_metric_extra"),
                "synthetic_tool_name_not_allowed",
            ),
            (
                {
                    **_call("call_safe_02"),
                    "extra": "not_allowed",
                },
                "synthetic_tool_call_invalid",
            ),
        )
        for raw_call, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(KimiSyntheticToolError) as caught:
                    execute_synthetic_tool_batch([raw_call])
                self.assertEqual(caught.exception.code, expected_code)

    def test_arguments_reject_duplicate_keys_nan_and_unknown_fields(self) -> None:
        invalid = (
            '{"dataset_id":"kimi_synth_success_v1",'
            '"dataset_id":"kimi_synth_missing_v1",'
            '"metric_id":"effect_size"}',
            '{"dataset_id":"kimi_synth_success_v1",'
            '"metric_id":"effect_size","value":NaN}',
            '{"dataset_id":"kimi_synth_success_v1",'
            '"metric_id":"effect_size","extra":true}',
        )
        for index, arguments in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaises(KimiSyntheticToolError) as caught:
                    execute_synthetic_tool_batch(
                        [_call(f"call_invalid_args_{index}", arguments=arguments)]
                    )
                self.assertEqual(
                    caught.exception.code, "synthetic_tool_arguments_invalid"
                )

    def test_argument_size_limit_is_utf8_bytes_and_allows_exact_boundary(self) -> None:
        compact = _arguments()
        exact = compact + (" " * (MAX_ARGUMENT_BYTES - len(compact.encode("utf-8"))))
        self.assertEqual(len(exact.encode("utf-8")), MAX_ARGUMENT_BYTES)
        result = execute_synthetic_tool_batch(
            [_call("call_exact_4k", arguments=exact)]
        )
        self.assertEqual(result["executed_tool_call_count"], 1)

        with self.assertRaises(KimiSyntheticToolError) as caught:
            execute_synthetic_tool_batch(
                [_call("call_over_4k", arguments=exact + " ")]
            )
        self.assertEqual(
            caught.exception.code, "synthetic_tool_arguments_too_large"
        )

    def test_scenarios_are_new_synthetic_and_match_tool_contract(self) -> None:
        payload = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(
            payload["scenario_set_id"],
            "kimi-controlled-pilot-synthetic-tools-v1",
        )
        self.assertIs(payload["synthetic_only"], True)
        self.assertIs(payload["prompt_tuning_allowed"], False)
        self.assertEqual(
            payload["source_isolation"],
            {"fresh_synthetic_cases": True, "external_case_ids": []},
        )
        self.assertEqual(payload["tool"]["name"], TOOL_NAME)
        self.assertEqual(payload["tool"]["max_argument_bytes"], MAX_ARGUMENT_BYTES)
        self.assertEqual(payload["tool"]["max_total_executions"], MAX_TOOL_EXECUTIONS)
        self.assertIs(payload["tool"]["additional_properties_allowed"], False)
        scenarios = payload["scenarios"]
        self.assertEqual(len(scenarios), 3)
        self.assertEqual(len({item["scenario_id"] for item in scenarios}), 3)

        success = execute_synthetic_tool_batch(scenarios[0]["tool_calls"])
        missing = execute_synthetic_tool_batch(scenarios[1]["tool_calls"])
        self.assertEqual(success["results"][0]["result"], scenarios[0]["expected"]["tool_result"])
        self.assertEqual(missing["results"][0]["result"], scenarios[1]["expected"]["tool_result"])
        for index in (0, 1):
            expected = scenarios[index]["expected"]
            observed = success if index == 0 else missing
            self.assertEqual(
                observed["requested_tool_call_count"],
                expected["requested_tool_call_count"],
            )
            self.assertEqual(
                observed["executed_tool_call_count"],
                expected["executed_tool_call_count"],
            )

        provider_error = scenarios[2]
        self.assertEqual(provider_error["scenario"], "provider_invalid_request")
        self.assertEqual(
            provider_error["provider_error"],
            {
                "http_status": 400,
                "error_type": "invalid_request_error",
                "stable_error_code": "kimi_chat_invalid_request",
            },
        )
        with patch(
            "researchops.kimi_synthetic_tools._execute_validated_call"
        ) as execute:
            no_tools = execute_synthetic_tool_batch(provider_error["tool_calls"])
        execute.assert_not_called()
        self.assertEqual(no_tools["requested_tool_call_count"], 0)
        self.assertEqual(no_tools["executed_tool_call_count"], 0)
        self.assertIs(provider_error["expected"]["tool_execution_allowed"], False)

        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "V2-PUB-",
            "PILOT-TASK-",
            "HOLD-",
            "public_regression",
            "holdout",
            "supervised",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(payload["evaluation_boundary"]["network_calls"], 0)
        self.assertEqual(payload["evaluation_boundary"]["provider_calls"], 0)
        self.assertIs(
            payload["evaluation_boundary"]["model_quality_claim_allowed"],
            False,
        )


if __name__ == "__main__":
    unittest.main()
