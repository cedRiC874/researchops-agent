from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from researchops.audit import AuditLedger
from researchops.openai_agent import (
    OpenAIAgentIntegrationError,
    build_openai_agent,
    resume_openai_agent,
    run_openai_agent,
    sdk_status,
)
from researchops.tool_runtime import ControlledToolExecutor, build_project_tool_registry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OpenAIAgentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.ledger = AuditLedger(Path(self.temporary_directory.name) / "audit.sqlite3")
        self.run_id = self.ledger.start_run(mode="sdk-test", request_summary={"kind": "smoke"})
        self.executor = ControlledToolExecutor(
            self.ledger, build_project_tool_registry(PROJECT_ROOT), sleeper=lambda _: None
        )

    def test_sdk_is_locked_and_agent_constructs_without_network(self) -> None:
        status = sdk_status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["version"], "0.21.0")
        agent = build_openai_agent(self.executor, self.run_id)
        self.assertEqual(
            [tool.name for tool in agent.tools],
            ["read_aggregate_evidence", "publish_aggregate_results"],
        )
        self.assertTrue(agent.tools[1].needs_approval)

    def test_missing_api_key_stops_before_runner_or_network(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OpenAIAgentIntegrationError) as context:
                asyncio.run(
                    run_openai_agent(
                        self.executor,
                        self.run_id,
                        "Summarize aggregate evidence.",
                    )
                )
        self.assertEqual(context.exception.code, "api_key_missing")

    def test_resume_awaits_state_deserialization_and_records_only_usage_delta(self) -> None:
        agent = build_openai_agent(self.executor, self.run_id)
        approval_callback = agent.tools[1].needs_approval
        self.assertTrue(callable(approval_callback))
        self.assertTrue(
            asyncio.run(
                approval_callback(
                    SimpleNamespace(),
                    {"release_name": "phase6-test"},
                    "sdk-call-1",
                )
            )
        )
        interruption = SimpleNamespace(
            call_id="sdk-call-1",
            name="publish_aggregate_results",
            arguments='{"release_name":"phase6-test"}',
        )
        state = SimpleNamespace(
            _model_responses=[object()],
            get_interruptions=Mock(return_value=[interruption]),
            approve=Mock(),
            reject=Mock(),
        )
        completed_state = SimpleNamespace(get_interruptions=Mock(return_value=[]))

        def response(input_tokens: int, output_tokens: int):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                )
            )

        result = SimpleNamespace(
            raw_responses=[response(100, 20), response(30, 7)],
            final_output="done",
            to_state=Mock(return_value=completed_state),
            context_wrapper=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=130,
                    output_tokens=27,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                )
            ),
        )
        with (
            patch("agents.RunState.from_string", new=AsyncMock(return_value=state)) as loader,
            patch("agents.Runner.run", new=AsyncMock(return_value=result)),
            patch("agents.set_default_openai_key"),
        ):
            outcome = asyncio.run(
                resume_openai_agent(
                    self.executor,
                    self.run_id,
                    "serialized-state",
                    sdk_call_id="sdk-call-1",
                    decision="approve",
                    approver="reviewer-1",
                    api_key="sk-test-only",
                )
            )

        self.assertEqual(outcome.status, "completed")
        loader.assert_awaited_once()
        state.approve.assert_called_once_with(interruption)
        model_calls = self.ledger.export_run(self.run_id)["model_calls"]
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0]["input_tokens"], 30)
        self.assertEqual(model_calls[0]["output_tokens"], 7)
        local_calls = [
            item
            for item in self.ledger.export_run(self.run_id)["tool_calls"]
            if item["tool_name"] == "publish_aggregate_results"
        ]
        self.assertEqual(len(local_calls), 1)
        self.assertEqual(local_calls[0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
