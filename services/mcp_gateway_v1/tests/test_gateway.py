"""运行句柄、CLI 审批和调用恢复的离线回归。"""

from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
from uuid import UUID

from researchops.tool_runtime import ToolRuntimeError
from researchops_mcp_gateway.cli import main
from researchops_mcp_gateway.gateway import Gateway
from researchops_mcp_gateway.safety import sanitize_result

from services.mcp_gateway_v1.tests.support import GatewayFixture


class GatewayTests(GatewayFixture):
    def test_short_column_alias_does_not_rewrite_metadata(self):
        timestamp = "2026-10-09T12:34:56.123456+00:00"
        value = sanitize_result({"columns": [{"name": "."}, {"name": "col_1"}],
                                 "expires_at_utc": timestamp, "version": "1.0.0"})
        self.assertEqual(value["columns"][0]["name"], "col_2")
        self.assertEqual(value["columns"][1]["name"], "col_1")
        self.assertEqual(value["expires_at_utc"], timestamp)
        self.assertEqual(value["version"], "1.0.0")

    def test_catalog_and_aggregate_read(self):
        self.assertEqual(UUID(self.run_id).version, 4)
        catalog = {tool["name"]: tool for tool in self.gateway.list_tools()}
        self.assertEqual(set(catalog), {"begin_run", "inspect_dataset", "recommend_statistical_method",
                                      "read_aggregate_evidence", "publish_aggregate_results",
                                      "execute_approved", "get_call_status"})
        self.assertTrue(catalog["inspect_dataset"]["annotations"]["readOnlyHint"])
        self.assertFalse(catalog["publish_aggregate_results"]["annotations"]["readOnlyHint"])
        self.assertFalse(catalog["publish_aggregate_results"]["annotations"]["destructiveHint"])
        outcome = self.call("inspect_dataset", dataset_id="synthetic_trial")
        self.assertFalse(outcome["isError"])
        self.assertEqual(outcome["structuredContent"]["row_count"], 240)
        self.assertFalse(outcome["structuredContent"]["sample_values_embedded"])

    def test_cli_approval_is_persisted_and_restart_executes_same_call(self):
        call_id = self.propose_publish()
        self.assert_error(self.call("execute_approved", call_id=call_id), "tool_approval_required")
        self.approved_arguments[call_id] = dict(self.gateway.ledger.get_tool_call(call_id)["safe_args"])
        with redirect_stdout(StringIO()) as output:
            exit_code = main(["--project-root", str(self.root), "--state-dir", str(self.root / "state"),
                              "approvals", "approve", call_id, "--approver", "本地测试人员"])
        self.assertEqual(exit_code, 0, output.getvalue())
        restarted = Gateway(self.root, self.root / "state", registry=self.gateway.registry)
        result = restarted.call_tool("execute_approved", {"run_id": self.run_id, "call_id": call_id})
        self.assertFalse(result["isError"], result)
        self.assertEqual(len(self.executions), 1)
        self.assertTrue((self.root / "artifacts/phase4/releases/reviewed-release/release_manifest.json").exists())

    def test_run_ownership_and_expiration(self):
        call_id = self.propose_publish()
        another_run = self.gateway.call_tool("begin_run", {})["structuredContent"]["run_id"]
        result = self.gateway.call_tool("get_call_status", {"run_id": another_run, "call_id": call_id})
        self.assert_error(result, "gateway_call_run_mismatch")
        self.now[0] += timedelta(seconds=3601)
        self.assert_error(self.call("inspect_dataset", dataset_id="synthetic_trial"), "gateway_run_expired")

    def test_quota_survives_restart(self):
        limited = Gateway(self.root, self.root / "limited", max_calls_per_run=2)
        run_id = limited.call_tool("begin_run", {})["structuredContent"]["run_id"]
        self.assertFalse(limited.call_tool("inspect_dataset", {"run_id": run_id, "dataset_id": "synthetic_trial"})["isError"])
        restarted = Gateway(self.root, self.root / "limited", max_calls_per_run=2)
        self.assert_error(restarted.call_tool("inspect_dataset", {"run_id": run_id, "dataset_id": "synthetic_trial"}), "gateway_rate_limited")

    def test_unknown_tool_and_extra_arguments(self):
        with self.assertRaises(ToolRuntimeError) as error:
            self.call("approvals_approve", call_id="invented")
        self.assertEqual(error.exception.code, "tool_unknown")
        self.assert_error(self.call("inspect_dataset", dataset_id="synthetic_trial", extra="unexpected"), "tool_arguments_invalid")
