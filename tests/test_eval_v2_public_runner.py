from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from researchops.cli import build_parser
from researchops.eval_v2_contracts import EvalV2ContractError
from researchops.eval_v2_public import (
    load_eval_v2_dataset_manifest,
    load_eval_v2_public_tasks,
)
from researchops.eval_v2_public_runner import (
    DeterministicFaultExecutor,
    _apply_provider_usage,
    _atomic_write_json,
    _bind_candidate_receipt,
    _build_report,
    _case_record_sha256,
    _execute_and_checkpoint_case,
    _initialize_or_resume_state,
    _provider_start_guard,
    _validate_existing_candidate_receipt,
    _verify_provider_model_access,
    conservative_cost_cny,
    run_public_regression_online,
)
from researchops.eval_v2_runner import (
    EvalV2Observation,
    EvalV2ToolGateway,
    score_eval_v2_observation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAULT_SCENARIOS = {
    "provider_timeout",
    "output_truncation",
    "side_effect_outcome_unknown",
}


class _Raises:
    def __init__(self, expected: type[BaseException]) -> None:
        self.expected = expected
        self.value: BaseException | None = None

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            raise AssertionError(f"{self.expected.__name__} was not raised")
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        return True


def test_public_preflight_rejects_anthropic_before_event_loop_or_client() -> None:
    provider = SimpleNamespace(
        provider_id="anthropic",
        base_url="https://api.anthropic.com",
    )
    with patch(
        "researchops.eval_v2_public_runner.asyncio.get_running_loop",
        side_effect=AssertionError("event loop must not be inspected"),
    ):
        with _Raises(EvalV2ContractError) as caught:
            _verify_provider_model_access(
                provider=provider,
                model_id="claude-sonnet-5",
                api_key="must-not-be-forwarded",
                timeout_seconds=5.0,
            )

    assert caught.value.code == "eval_v2_provider_preflight_provider_invalid"


def _raises(expected: type[BaseException]) -> _Raises:
    return _Raises(expected)


class _MonkeyPatch:
    _MISSING = object()

    def __init__(self) -> None:
        self._changes: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        previous = getattr(target, name, self._MISSING)
        self._changes.append((target, name, previous))
        setattr(target, name, value)

    def undo(self) -> None:
        for target, name, previous in reversed(self._changes):
            if previous is self._MISSING:
                delattr(target, name)
            else:
                setattr(target, name, previous)
        self._changes.clear()


class _AggregateOnlyBackend:
    def inspect_dataset(self, dataset_id: str) -> dict[str, object]:
        return {
            "dataset": {"dataset_id": dataset_id},
            "privacy": {"row_level_values_exposed": False},
        }


def test_conservative_cost_uses_all_cache_miss_pricing() -> None:
    assert conservative_cost_cny(144_429, 161_472) == Decimal("1.886535")
    assert conservative_cost_cny(2_000_000, 2_000_000) == Decimal("24.000000")


def test_conservative_cost_rejects_invalid_usage() -> None:
    for input_tokens, output_tokens in ((-1, 0), (0, -1), (True, 0)):
        with _raises(EvalV2ContractError) as captured:
            conservative_cost_cny(input_tokens, output_tokens)
        assert captured.value is not None
        assert captured.value.code == "eval_v2_budget_usage_invalid"


def test_all_nine_fault_tasks_pass_without_a_model_call() -> None:
    manifest = load_eval_v2_dataset_manifest(
        PROJECT_ROOT / "evals" / "v2" / "external_datasets.json"
    )
    tasks = load_eval_v2_public_tasks(
        PROJECT_ROOT / "evals" / "v2" / "public_tasks.jsonl", manifest
    )
    fault_tasks = [
        task
        for task in tasks
        if task.split == "public_regression" and task.scenario in FAULT_SCENARIOS
    ]
    assert len(fault_tasks) == 9

    for task in fault_tasks:
        gateway = EvalV2ToolGateway(task, _AggregateOnlyBackend())
        result = DeterministicFaultExecutor(task.scenario).execute(
            task.public_input(), gateway
        )
        observation = EvalV2Observation(
            task_id=task.task_id,
            outcome=result.outcome,
            final_output=result.final_output,
            tool_calls=gateway.tool_calls,
            approval_state=result.approval_state,
            safety_violation=result.safety_violation,
            side_effect_occurred=result.side_effect_occurred,
            error_code=result.error_code,
            completion_status=result.completion_status,
            latency_ms=0.0,
            model_call_count=result.model_call_count,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider_id=result.provider_id,
            model_id=result.model_id,
            transport_id=result.transport_id,
            gateway_dispatched_tool_call_count=len(gateway.tool_calls),
            backend_executed_tool_call_count=(
                gateway.backend_executed_tool_call_count
            ),
            completion_failure_source=result.completion_failure_source,
        )
        score = score_eval_v2_observation(task, observation)
        assert score.passed, (task.task_id, score.failures)
        assert result.model_call_count == 0
        assert result.side_effect_occurred is False
        if task.scenario == "output_truncation":
            assert result.completion_failure_source == "output_limit_suspected"
        else:
            assert result.completion_failure_source is None


def test_missing_provider_usage_stops_before_another_case() -> None:
    state = _budget_state()
    entry = {
        "diagnostics": {
            "usage": {
                "model_call_count": 1,
                "input_tokens": None,
                "output_tokens": None,
            }
        }
    }
    _apply_provider_usage(state, entry)
    assert state["budget"]["usage_complete"] is False
    assert _provider_start_guard(state) == "provider_usage_unavailable"


def test_next_case_cost_reserve_boundary_is_fail_closed() -> None:
    state = _budget_state()
    state["budget"]["limits"] = {
        "input_tokens": 10_000_000,
        "output_tokens": 10_000_000,
        "model_calls": 744,
    }
    state["budget"]["observed_usage"]["conservative_estimated_cost_cny"] = (
        "2.000000"
    )
    assert _provider_start_guard(state) is None
    state["budget"]["observed_usage"]["conservative_estimated_cost_cny"] = (
        "2.000001"
    )
    assert _provider_start_guard(state) == "budget_reserve_exhausted"


def test_atomic_writer_rejects_outputs_and_credentials(tmp_path: Path) -> None:
    target = tmp_path / "checkpoint.json"
    for payload in (
        {"final_output": "answer"},
        {"nested": {"value": "sk-this-is-a-test-secret-value"}},
        {"provider_response_body": "raw body"},
        {"provider_output_body": "raw output"},
        {"provider_status_raw_value": "future-provider-status"},
        {"incomplete_details": {"reason": "raw"}},
        {"credentials": "secret"},
        {"direct_identifiers": ["participant@example.invalid"]},
    ):
        with _raises(EvalV2ContractError) as captured:
            _atomic_write_json(target, payload)
        assert captured.value is not None
        assert captured.value.code == "eval_v2_public_artifact_unsafe"
    assert not target.exists()

    _atomic_write_json(target, {"status": "safe"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "safe"}
    assert not list(tmp_path.glob("*.tmp"))


def test_resume_refuses_ambiguous_inflight_case(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    state_path = output / "public_regression_state.json"
    spec = _minimal_state_spec()
    state = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=spec,
        resume=False,
        output_created=True,
    )
    state["in_progress_case"] = {
        "case_key": "provider_behavior:1:V2-PUB-001",
        "channel": "provider_behavior",
        "repetition_index": 1,
        "task_id": "V2-PUB-001",
        "marked_at_utc": "2026-08-21T00:00:00Z",
    }
    _atomic_write_json(state_path, state)

    with _raises(EvalV2ContractError) as captured:
        _initialize_or_resume_state(
            output=output,
            state_path=state_path,
            state_spec=spec,
            resume=True,
            output_created=False,
        )
    assert captured.value.code == "eval_v2_public_resume_ambiguous_inflight"


def test_stopped_checkpoint_can_resume_for_idempotent_finalize(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    state_path = output / "public_regression_state.json"
    spec = _minimal_state_spec()
    state = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=spec,
        resume=False,
        output_created=True,
    )
    state["status"] = "stopped"
    state["stop_reason"] = "provider_usage_unavailable"
    _atomic_write_json(state_path, state)

    resumed = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=spec,
        resume=True,
        output_created=False,
    )
    assert resumed["status"] == "stopped"
    assert resumed["stop_reason"] == "provider_usage_unavailable"


def test_candidate_receipt_blocks_a_second_output_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = root / "artifacts" / "run-01"
    output.mkdir(parents=True)
    receipt = root / "artifacts" / "receipts" / ("a" * 64 + ".receipt.json")
    state = {
        "candidate": {
            "candidate_id": "candidate",
            "candidate_commitment_sha256": "a" * 64,
        },
        "run_id": "PUBREG-TEST",
        "started_at_utc": "2026-08-21T00:00:00Z",
    }
    _bind_candidate_receipt(
        receipt_path=receipt,
        root=root,
        output=output,
        state=state,
    )
    freeze = {
        "candidate_id": "candidate",
        "candidate_commitment_sha256": "a" * 64,
    }
    _validate_existing_candidate_receipt(
        receipt_path=receipt,
        root=root,
        output=output,
        freeze=freeze,
        resume=True,
    )

    with _raises(EvalV2ContractError) as second_output:
        _validate_existing_candidate_receipt(
            receipt_path=receipt,
            root=root,
            output=root / "artifacts" / "run-02",
            freeze=freeze,
            resume=True,
        )
    assert second_output.value.code == "eval_v2_public_candidate_receipt_mismatch"

    with _raises(EvalV2ContractError) as duplicate_start:
        _validate_existing_candidate_receipt(
            receipt_path=receipt,
            root=root,
            output=output,
            freeze=freeze,
            resume=False,
        )
    assert duplicate_start.value.code == "eval_v2_public_candidate_already_started"


def test_resume_detects_score_tampering_with_case_hash_chain(tmp_path: Path) -> None:
    manifest = load_eval_v2_dataset_manifest(
        PROJECT_ROOT / "evals" / "v2" / "external_datasets.json"
    )
    task = next(
        item
        for item in load_eval_v2_public_tasks(
            PROJECT_ROOT / "evals" / "v2" / "public_tasks.jsonl", manifest
        )
        if item.task_id == "V2-PUB-008"
    )
    output = tmp_path / "run"
    output.mkdir()
    state_path = output / "public_regression_state.json"
    spec = _minimal_state_spec()
    spec["execution_plan"]["orders"]["deterministic_fault_injection"]["1"] = [
        task.task_id
    ]
    state = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=spec,
        resume=False,
        output_created=True,
    )
    _execute_and_checkpoint_case(
        state=state,
        state_path=state_path,
        channel="deterministic_fault_injection",
        repetition_index=1,
        task=task,
        executor=DeterministicFaultExecutor(task.scenario),
        inspect_backend=_AggregateOnlyBackend(),
    )
    tampered = json.loads(state_path.read_text(encoding="utf-8"))
    tampered["completed_cases"][0]["score"]["passed"] = False
    _atomic_write_json(state_path, tampered)

    with _raises(EvalV2ContractError) as captured:
        _initialize_or_resume_state(
            output=output,
            state_path=state_path,
            state_spec=spec,
            resume=True,
            output_created=False,
        )
    assert captured.value.code == "eval_v2_public_resume_case_chain_invalid"


def test_legacy_checkpoint_without_completion_source_is_unknown_not_complete(
    tmp_path: Path,
) -> None:
    manifest = load_eval_v2_dataset_manifest(
        PROJECT_ROOT / "evals" / "v2" / "external_datasets.json"
    )
    task = next(
        item
        for item in load_eval_v2_public_tasks(
            PROJECT_ROOT / "evals" / "v2" / "public_tasks.jsonl", manifest
        )
        if item.scenario == "output_truncation"
        and item.split == "public_regression"
    )
    output = tmp_path / "run"
    output.mkdir()
    state_path = output / "public_regression_state.json"
    spec = _minimal_state_spec()
    spec["execution_plan"]["orders"]["deterministic_fault_injection"]["1"] = [
        task.task_id
    ]
    state = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=spec,
        resume=False,
        output_created=True,
    )
    _execute_and_checkpoint_case(
        state=state,
        state_path=state_path,
        channel="deterministic_fault_injection",
        repetition_index=1,
        task=task,
        executor=DeterministicFaultExecutor(task.scenario),
        inspect_backend=_AggregateOnlyBackend(),
    )

    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    entry = legacy["completed_cases"][0]
    assert entry["diagnostics"].pop("completion_failure_source") == (
        "output_limit_suspected"
    )
    entry["case_record_sha256"] = _case_record_sha256(entry)
    legacy["case_chain_head_sha256"] = entry["case_record_sha256"]
    _atomic_write_json(state_path, legacy)

    resumed = _initialize_or_resume_state(
        output=output,
        state_path=state_path,
        state_spec=spec,
        resume=True,
        output_created=False,
    )
    telemetry = _build_report(resumed)["deterministic_fault_injection"][
        "completion_telemetry"
    ]

    assert telemetry["eligible_failure_count"] == 1
    assert telemetry["classified_failure_count"] == 0
    assert telemetry["legacy_unknown_count"] == 1
    assert telemetry["classified_failure_coverage"] == 0.0
    assert telemetry["coverage_status"] == "partial"
    assert telemetry["coverage_complete"] is False


def test_public_runner_cli_has_budget_but_no_api_key_argument() -> None:
    args = build_parser().parse_args(
        [
            "eval-v2-run-public-online",
            "--registry",
            "artifacts/data/logical_dataset_registry.json",
            "--output-dir",
            "artifacts/public/run-01",
            "--budget-cny",
            "6",
            "--confirm-online",
        ]
    )
    assert args.budget_cny == 6.0
    assert args.confirm_online is True
    assert not hasattr(args, "api_key")


def test_online_confirmation_and_key_fail_before_filesystem_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    output = root / "artifacts" / "run-01"
    common = {
        "project_root": root,
        "candidate_path": root / "missing-candidate.json",
        "registry_path": root / "missing-registry.json",
        "output_directory": output,
        "budget_cny": 6,
    }
    with _raises(EvalV2ContractError) as confirmation:
        run_public_regression_online(
            **common,
            api_key="unused-test-key",
            confirm_online=False,
        )
    assert confirmation.value.code == "eval_v2_online_confirmation_required"
    assert not output.exists()

    with _raises(EvalV2ContractError) as missing_key:
        run_public_regression_online(
            **common,
            api_key="",
            confirm_online=True,
        )
    assert missing_key.value.code == "eval_v2_provider_key_missing"
    assert not output.exists()


def test_provider_catalog_preflight_uses_no_model_token_call(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Models:
        async def list(self):
            return SimpleNamespace(
                data=[SimpleNamespace(id="deepseek-v4-flash")]
            )

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.models = _Models()

        async def close(self):
            captured["closed"] = True

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
    provider = SimpleNamespace(
        provider_id="deepseek", base_url="https://api.deepseek.com"
    )
    result = _verify_provider_model_access(
        provider=provider,
        model_id="deepseek-v4-flash",
        api_key="test-key-not-persisted",
        timeout_seconds=20.0,
    )

    assert result["status"] == "verified"
    assert result["network_calls"] == 1
    assert result["model_token_calls"] == 0
    assert captured["closed"] is True

def _budget_state() -> dict[str, object]:
    return {
        "provider": {"max_turns": 8},
        "provider_preflight": {
            "status": "verified",
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "verification_method": "provider_models_list",
            "network_calls": 1,
            "model_token_calls": 0,
        },
        "budget": {
            "authorized_budget_cny": "6.000000",
            "usage_complete": True,
            "limits": {
                "input_tokens": 1_000_000,
                "output_tokens": 333_333,
                "model_calls": 744,
            },
            "observed_usage": {
                "model_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "conservative_estimated_cost_cny": "0.000000",
            },
        },
    }


def _minimal_state_spec() -> dict[str, object]:
    orders = {
        "deterministic_fault_injection": {"1": [], "2": [], "3": []},
        "provider_behavior": {"1": [], "2": [], "3": []},
    }
    return {
        "schema_version": "1.0",
        "runner_version": "1.0",
        "candidate": {
            "candidate_id": "candidate",
            "candidate_commitment_sha256": "a" * 64,
        },
        "provider": {"max_turns": 8},
        "provider_preflight": {
            "status": "verified",
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "verification_method": "provider_models_list",
            "network_calls": 1,
            "model_token_calls": 0,
        },
        "registry_binding": {
            "dataset_manifest_sha256": "b" * 64,
            "registry_file_sha256": "c" * 64,
        },
        "execution_plan": {"orders": orders},
        "budget": {
            "currency": "CNY",
            "authorized_budget_cny": "6.000000",
            "estimate_method": "conservative_peak_all_input_cache_miss",
            "provider_billing_hard_cap": False,
            "pricing": {},
            "limits": {
                "input_tokens": 1_000_000,
                "output_tokens": 333_333,
                "model_calls": 744,
            },
            "per_case_start_reserve": {
                "input_tokens": 250_000,
                "output_tokens": 16_000,
                "cost_cny": "4.000000",
            },
        },
    }


def _run_function_test(function) -> None:
    parameters = inspect.signature(function).parameters
    unknown = set(parameters).difference({"tmp_path", "monkeypatch"})
    if unknown:
        raise AssertionError(f"unsupported unittest fixture parameters: {sorted(unknown)}")

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    monkeypatch: _MonkeyPatch | None = None
    arguments: dict[str, object] = {}
    try:
        if "tmp_path" in parameters:
            temporary_directory = tempfile.TemporaryDirectory()
            arguments["tmp_path"] = Path(temporary_directory.name)
        if "monkeypatch" in parameters:
            monkeypatch = _MonkeyPatch()
            arguments["monkeypatch"] = monkeypatch
        function(**arguments)
    finally:
        if monkeypatch is not None:
            monkeypatch.undo()
        if temporary_directory is not None:
            temporary_directory.cleanup()


def load_tests(loader, standard_tests, pattern):
    del loader, standard_tests, pattern
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            suite.addTest(
                unittest.FunctionTestCase(
                    lambda function=function: _run_function_test(function),
                    description=name,
                )
            )
    return suite
