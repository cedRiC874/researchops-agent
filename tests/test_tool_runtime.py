from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from researchops.audit import AuditLedger
from researchops.tool_runtime import (
    AmbiguousToolOutcome,
    ControlledToolExecutor,
    IdempotencyMode,
    PermanentToolError,
    RetryPolicy,
    RiskLevel,
    ToolRegistry,
    ToolRuntimeError,
    ToolSpec,
    TransientToolError,
    build_project_tool_registry,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def simple_spec(
    handler,
    *,
    name: str = "fixture_tool",
    risk: RiskLevel = RiskLevel.READ_ONLY,
    max_attempts: int = 1,
    scope_state: dict[str, str] | None = None,
) -> ToolSpec:
    state = scope_state or {"source": "source-v1"}

    def validate(arguments):
        if set(arguments) != {"value"} or not isinstance(arguments["value"], int):
            raise ToolRuntimeError("tool_arguments_invalid", "value must be an integer")
        return {"value": arguments["value"]}

    return ToolSpec(
        name=name,
        version="1.0.0",
        risk=risk,
        handler=handler,
        validate_arguments=validate,
        safe_arguments=lambda args: {"value": args["value"]},
        safe_result=lambda result: {"answer": result["answer"]},
        scope_resources=lambda _: {"source_sha256": state["source"]},
        retry_policy=RetryPolicy(max_attempts=max_attempts, initial_backoff_ms=10),
        idempotency=IdempotencyMode.IDEMPOTENT,
    )


class ControlledToolExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)
        self.now = [datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)]
        self.ledger = AuditLedger(self.temp_path / "audit.sqlite3", clock=lambda: self.now[0])
        self.run_id = self.ledger.start_run(mode="test", request_summary={"objective": "test"})

    def executor(self, spec: ToolSpec, *, sleeps: list[float] | None = None):
        registry = ToolRegistry()
        registry.register(spec)
        return ControlledToolExecutor(
            self.ledger,
            registry,
            sleeper=(sleeps.append if sleeps is not None else lambda _: None),
            clock=lambda: self.now[0],
        )

    def test_transient_errors_are_logged_and_retried_then_succeed(self) -> None:
        attempts = {"count": 0}
        sleeps: list[float] = []

        def handler(arguments, context):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise TransientToolError("temporary_lock", "temporarily locked")
            return {"answer": arguments["value"] * 2}

        executor = self.executor(simple_spec(handler, max_attempts=3), sleeps=sleeps)
        outcome = executor.propose(self.run_id, "fixture_tool", {"value": 4})

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.result, {"answer": 8})
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(sleeps, [0.01, 0.02])
        logged = self.ledger.list_attempts(outcome.call_id)
        self.assertEqual([item["outcome"] for item in logged], ["failed", "failed", "succeeded"])
        self.assertTrue(self.ledger.verify_chain(self.run_id).valid)

    def test_risky_tool_never_executes_before_approval_and_replay_is_idempotent(self) -> None:
        calls = {"count": 0}

        def handler(arguments, context):
            calls["count"] += 1
            return {"answer": arguments["value"]}

        executor = self.executor(simple_spec(handler, risk=RiskLevel.CONTROLLED_WRITE))
        pending = executor.propose(self.run_id, "fixture_tool", {"value": 7})
        self.assertEqual(pending.status, "awaiting_approval")
        with self.assertRaises(ToolRuntimeError) as context:
            executor.execute(pending.call_id)
        self.assertEqual(context.exception.code, "tool_approval_required")
        self.assertEqual(calls["count"], 0)

        executor.decide(pending.call_id, decision="approve", approver="reviewer-1")
        first = executor.execute(pending.call_id)
        second = executor.execute(pending.call_id)
        self.assertEqual(first.result, second.result)
        self.assertEqual(calls["count"], 1)

    def test_rejection_and_expiry_fail_closed(self) -> None:
        calls = {"count": 0}

        def handler(arguments, context):
            calls["count"] += 1
            return {"answer": 1}

        spec = simple_spec(handler, risk=RiskLevel.CONTROLLED_WRITE)
        executor = self.executor(spec)
        rejected = executor.propose(self.run_id, "fixture_tool", {"value": 1})
        executor.decide(rejected.call_id, decision="reject", approver="reviewer")
        with self.assertRaises(ToolRuntimeError) as context:
            executor.execute(rejected.call_id)
        self.assertEqual(context.exception.code, "tool_approval_rejected")

        second = executor.propose(self.run_id, "fixture_tool", {"value": 2})
        executor.decide(
            second.call_id,
            decision="approve",
            approver="reviewer",
            expires_in_seconds=1,
        )
        self.now[0] += timedelta(seconds=2)
        with self.assertRaises(ToolRuntimeError) as context:
            executor.execute(second.call_id)
        self.assertEqual(context.exception.code, "tool_approval_expired")
        self.assertEqual(calls["count"], 0)

    def test_argument_or_source_change_invalidates_approval(self) -> None:
        calls = {"count": 0}
        source = {"source": "source-v1"}

        def handler(arguments, context):
            calls["count"] += 1
            return {"answer": 1}

        executor = self.executor(
            simple_spec(handler, risk=RiskLevel.CONTROLLED_WRITE, scope_state=source)
        )
        pending = executor.propose(self.run_id, "fixture_tool", {"value": 1})
        executor.decide(pending.call_id, decision="approve", approver="reviewer")
        with self.assertRaises(ToolRuntimeError) as context:
            executor.execute(pending.call_id, arguments={"value": 2})
        self.assertEqual(context.exception.code, "tool_approval_mismatch")
        self.assertEqual(calls["count"], 0)

        another = executor.propose(self.run_id, "fixture_tool", {"value": 3})
        executor.decide(another.call_id, decision="approve", approver="reviewer")
        source["source"] = "source-v2"
        with self.assertRaises(ToolRuntimeError) as context:
            executor.execute(another.call_id)
        self.assertEqual(context.exception.code, "tool_precondition_changed")
        self.assertEqual(calls["count"], 0)

    def test_permanent_failure_is_not_retried(self) -> None:
        calls = {"count": 0}

        def handler(arguments, context):
            calls["count"] += 1
            raise PermanentToolError("invalid_domain", "invalid domain value")

        executor = self.executor(simple_spec(handler, max_attempts=3))
        with self.assertRaises(ToolRuntimeError) as context:
            executor.propose(self.run_id, "fixture_tool", {"value": 1})
        self.assertEqual(context.exception.code, "invalid_domain")
        self.assertEqual(calls["count"], 1)

    def test_denied_risk_never_reaches_handler(self) -> None:
        calls = {"count": 0}

        def handler(arguments, context):
            calls["count"] += 1
            return {"answer": 1}

        executor = self.executor(simple_spec(handler, risk=RiskLevel.SENSITIVE_EXPORT))
        with self.assertRaises(ToolRuntimeError) as context:
            executor.propose(self.run_id, "fixture_tool", {"value": 1})
        self.assertEqual(context.exception.code, "tool_policy_denied")
        self.assertEqual(calls["count"], 0)

    def test_ambiguous_side_effect_is_not_blindly_retried(self) -> None:
        calls = {"count": 0}

        def handler(arguments, context):
            calls["count"] += 1
            raise AmbiguousToolOutcome(
                "write_acknowledgement_lost", "The write may have committed."
            )

        executor = self.executor(
            simple_spec(handler, risk=RiskLevel.CONTROLLED_WRITE, max_attempts=3)
        )
        pending = executor.propose(self.run_id, "fixture_tool", {"value": 1})
        executor.decide(pending.call_id, decision="approve", approver="reviewer")
        with self.assertRaises(ToolRuntimeError) as context:
            executor.execute(pending.call_id)
        self.assertEqual(context.exception.code, "write_acknowledgement_lost")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(self.ledger.get_tool_call(pending.call_id)["status"], "outcome_unknown")


class ProjectToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        target = self.root / "artifacts" / "phase3"
        target.parent.mkdir(parents=True)
        shutil.copytree(PROJECT_ROOT / "artifacts" / "phase3", target)
        shutil.copytree(PROJECT_ROOT / "data", self.root / "data")
        self.ledger = AuditLedger(self.root / "audit.sqlite3")
        self.run_id = self.ledger.start_run(mode="project-test", request_summary={"kind": "publish"})
        self.executor = ControlledToolExecutor(
            self.ledger, build_project_tool_registry(self.root), sleeper=lambda _: None
        )

    def test_publish_is_aggregate_only_guarded_atomic_and_no_overwrite(self) -> None:
        pending = self.executor.propose(
            self.run_id,
            "publish_aggregate_results",
            {"bundle_id": "phase3", "release_name": "reviewed-release"},
        )
        target = self.root / "artifacts" / "phase4" / "releases" / "reviewed-release"
        self.assertFalse(target.exists())
        self.executor.decide(pending.call_id, decision="approve", approver="reviewer")
        restarted_executor = ControlledToolExecutor(
            AuditLedger(self.root / "audit.sqlite3"),
            build_project_tool_registry(self.root),
            sleeper=lambda _: None,
        )
        outcome = restarted_executor.execute(pending.call_id)
        self.assertTrue((target / "analysis_bundle.json").is_file())
        manifest = json.loads((target / "release_manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["raw_data_embedded"])
        self.assertEqual(outcome.result["release_id"], "phase4-release:reviewed-release")
        restarted_executor.execute(pending.call_id)
        self.assertEqual(len(list(target.glob("release_manifest.json"))), 1)

    def test_inspect_dataset_returns_aggregate_profile_without_samples(self) -> None:
        outcome = self.executor.propose(
            self.run_id,
            "inspect_dataset",
            {"dataset_id": "synthetic_trial"},
        )

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.result["row_count"], 240)
        self.assertEqual(outcome.result["column_count"], 10)
        self.assertFalse(outcome.result["sample_values_embedded"])
        self.assertTrue(
            all("sample_values" not in column for column in outcome.result["columns"])
        )

    def test_method_tool_uses_only_registered_design_ids(self) -> None:
        primary = self.executor.propose(
            self.run_id,
            "recommend_statistical_method",
            {"dataset_id": "synthetic_trial", "design_id": "trial_primary"},
        )
        unadjusted = self.executor.propose(
            self.run_id,
            "recommend_statistical_method",
            {"dataset_id": "synthetic_trial", "design_id": "trial_unadjusted"},
        )

        self.assertEqual(primary.result["primary_method"]["code"], "ancova_linear_model")
        self.assertEqual(unadjusted.result["primary_method"]["code"], "welch_t_test")
        with self.assertRaises(ToolRuntimeError) as context:
            self.executor.propose(
                self.run_id,
                "recommend_statistical_method",
                {"dataset_id": "synthetic_trial", "design_id": "../../unsafe"},
            )
        self.assertEqual(context.exception.code, "tool_arguments_invalid")


if __name__ == "__main__":
    unittest.main()
