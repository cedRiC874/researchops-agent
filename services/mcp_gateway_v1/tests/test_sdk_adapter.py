"""真实策略网关的现代协议、stdio 与逐请求审计回归。"""
from __future__ import annotations

import os
import socket
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from researchops_mcp_gateway.cli import main
from researchops_mcp_gateway.sdk_adapter import create_server
from services.mcp_gateway_v1.tests.offline import prohibit_network
from services.mcp_gateway_v1.tests.support import GatewayFixture, PROJECT_ROOT


class SDKAdapterTests(GatewayFixture):
    def all_gateway_events(self):
        run_ids = [self.run_id, self.gateway.store.management_run_id]
        return [event for run_id in run_ids
                for event in self.gateway.ledger.export_run(run_id)["events"]
                if event["actor_kind"] == "mcp_gateway"]

    def test_modern_memory_protocol_error_mapping_and_request_audit(self):
        async def scenario():
            before = {event["event_id"] for event in self.all_gateway_events()}
            server = create_server(self.gateway)
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
                    async with ClientSession(*client_streams) as client:
                        await client.discover()
                        self.assertEqual(client.protocol_version, "2026-07-28")
                        listing = await client.list_tools()
                        self.assertNotIn("approvals_approve", {tool.name for tool in listing.tools})
                        result = await client.call_tool("inspect_dataset", {
                            "run_id": self.run_id, "dataset_id": "synthetic_trial"})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content["row_count"], 240)
                        bad = await client.call_tool("inspect_dataset", {
                            "run_id": self.run_id, "dataset_id": "synthetic_trial", "extra": "wrong"})
                        self.assertTrue(bad.is_error)
                        self.assertEqual(bad.structured_content["error_code"], "tool_arguments_invalid")
                        with self.assertRaises(MCPError) as unknown:
                            await client.call_tool("approvals_approve", {"run_id": self.run_id})
                        self.assertEqual(unknown.exception.code, types.INVALID_PARAMS)
                        self.assertEqual(unknown.exception.data, {"error_code": "tool_unknown"})
                        with self.assertRaises(MCPError) as invalid:
                            await client.send_request(types.Request(method="tools/call", params={
                                "name": "inspect_dataset", "arguments": "private/path/marker"}), types.Result)
                        self.assertEqual(invalid.exception.code, types.INVALID_PARAMS)
                        self.assertNotIn("private/path/marker", str(invalid.exception.error.model_dump()))
                    tasks.cancel_scope.cancel()
            events = [event for event in self.all_gateway_events() if event["event_id"] not in before]
            self.assertEqual(len(events), 6)
            methods = [event["safe_payload"]["method"] for event in events]
            self.assertEqual(methods.count("server/discover"), 1)
            self.assertEqual(methods.count("tools/list"), 1)
            self.assertEqual(methods.count("tools/call"), 4)
            for event in events:
                self.assertEqual(len(event["safe_payload"]["arguments_hash"]), 64)
                self.assertIn("decision", event["safe_payload"])
                self.assertIn("call_id", event["safe_payload"])
                self.assertIn("result_status", event["safe_payload"])
            self.assertEqual(self.executions, [])
        anyio.run(scenario)

    def test_stdio_subprocess_read_propose_cli_approve_execute(self):
        async def scenario():
            source_path = PROJECT_ROOT / "services/mcp_gateway_v1/src"
            child_code = (
                "import sys; "
                f"sys.path[:0] = [{str(PROJECT_ROOT)!r}, {str(source_path)!r}]; "
                "from services.mcp_gateway_v1.tests.offline import prohibit_network; "
                "sys.addaudithook(prohibit_network); "
                "from researchops_mcp_gateway.bootstrap import main; "
                "raise SystemExit(main())"
            )
            child_env = {"MPLCONFIGDIR": str(self.root / "mpl-cache")}
            if sys.platform == "win32":
                child_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", child_code, "--project-root", str(self.root),
                      "--state-dir", str(self.root / "state"), "serve"],
                env=child_env, cwd=self.root,
            )
            # 子进程仅继承明确列出的非敏感环境项；SDK 仍负责所有传输。
            with patch("mcp.client.stdio.get_default_environment", return_value={}):
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=20) as client:
                        await client.discover()
                        self.assertEqual(client.protocol_version, "2026-07-28")
                        await client.list_tools()
                        read = await client.call_tool("inspect_dataset", {
                            "run_id": self.run_id, "dataset_id": "synthetic_trial"})
                        self.assertFalse(read.is_error)
                        pending = await client.call_tool("publish_aggregate_results", {
                            "run_id": self.run_id, "bundle_id": "phase3", "release_name": "stdio-reviewed"})
                        self.assertFalse(pending.is_error)
                        self.assertEqual(pending.structured_content["status"], "awaiting_approval")
                        call_id = pending.structured_content["call_id"]
                        self.assertFalse((self.root / "artifacts/phase4/releases/stdio-reviewed").exists())
                        denied = await client.call_tool("execute_approved", {"run_id": self.run_id, "call_id": call_id})
                        self.assertTrue(denied.is_error)
                        self.assertEqual(denied.structured_content["error_code"], "tool_approval_required")
                        with redirect_stdout(StringIO()):
                            exit_code = main(["--project-root", str(self.root), "--state-dir", str(self.root / "state"),
                                              "approvals", "approve", call_id, "--approver", "stdio本地复核员"])
                        self.assertEqual(exit_code, 0)
                        result = await client.call_tool("execute_approved", {"run_id": self.run_id, "call_id": call_id})
                        self.assertFalse(result.is_error)
                        self.assertTrue((self.root / "artifacts/phase4/releases/stdio-reviewed/release_manifest.json").is_file())
                        self.assertEqual(len(self.gateway.ledger.list_attempts(call_id)), 1)
                        replay = await client.call_tool("execute_approved", {"run_id": self.run_id, "call_id": call_id})
                        self.assertEqual(replay.structured_content, result.structured_content)
                        self.assertEqual(len(self.gateway.ledger.list_attempts(call_id)), 1)
        anyio.run(scenario)

    def test_offline_guard_denies_network_and_ordinary_loopback(self):
        # 直接调用同一钩子，不发起真实连接或 DNS 请求。
        for address in [("203.0.113.1", 443), ("127.0.0.1", 12345), ("::1", 12345)]:
            with socket.socket() as handle:
                with self.assertRaisesRegex(RuntimeError, "offline_test_network_forbidden"):
                    prohibit_network("socket.connect", (handle, address))
        for event in ["socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"]:
            with self.assertRaisesRegex(RuntimeError, "offline_test_network_forbidden"):
                prohibit_network(event, ())
