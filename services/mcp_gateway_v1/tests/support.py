"""仅使用仓库合成数据的独立夹具。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from researchops.tool_runtime import ToolRegistry, build_project_tool_registry
from researchops_mcp_gateway.gateway import Gateway


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class GatewayFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ("data", "artifacts/phase3"):
            shutil.copytree(PROJECT_ROOT / relative, self.root / relative)
        self.now = [datetime.now(timezone.utc)]
        self.executions = []
        self.unapproved_executions = []
        self.approved_arguments = {}
        registry = ToolRegistry()
        for spec in build_project_tool_registry(self.root).specs():
            if spec.name == "publish_aggregate_results":
                original_handler = spec.handler

                def observed_handler(arguments, context, original=original_handler):
                    saved = self.approved_arguments.get(context.call_id)
                    approval = self.gateway.ledger.get_approval(context.call_id)
                    if not approval or approval["decision"] != "approve" or saved != arguments:
                        self.unapproved_executions.append(context.call_id)
                    self.executions.append((context.call_id, dict(arguments)))
                    return original(arguments, context)

                spec = replace(spec, handler=observed_handler)
            registry.register(spec)
        self.gateway = Gateway(self.root, self.root / "state", registry=registry, clock=lambda: self.now[0])
        self.run_id = self.gateway.call_tool("begin_run", {})["structuredContent"]["run_id"]

    def call(self, name, **arguments):
        return self.gateway.call_tool(name, {"run_id": self.run_id, **arguments})

    def propose_publish(self, release_name="reviewed-release"):
        result = self.call("publish_aggregate_results", bundle_id="phase3", release_name=release_name)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["status"], "awaiting_approval")
        return result["structuredContent"]["call_id"]

    def approve(self, call_id, ttl=900):
        self.approved_arguments[call_id] = dict(self.gateway.ledger.get_tool_call(call_id)["safe_args"])
        return self.gateway.decide(call_id, decision="approve", approver="离线测试审批人", ttl=ttl)

    def assert_error(self, result, code):
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error_code"], code)

    def gateway_events(self):
        return [event for event in self.gateway.ledger.export_run(self.run_id)["events"]
                if event["actor_kind"] == "mcp_gateway"]

    def tearDown(self):
        self.assertEqual(self.unapproved_executions, [], "出现未经匹配审批的发布")
        for call_id, arguments in self.executions:
            self.assertEqual(arguments, self.approved_arguments[call_id])
        self.assertTrue(self.gateway.ledger.verify_chain(self.run_id).valid)
        self.assertTrue(self.gateway_events(), "缺少 MCP 网关审计事件")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
