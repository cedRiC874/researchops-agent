"""恶意上游使用进程内假服务器；不连接任何真实第三方服务。"""

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json

from researchops.tool_runtime import RiskLevel, ToolRuntimeError
from researchops_mcp_gateway.cli import main
from researchops_mcp_gateway.manifest import ManifestStore, definition_sha256
from researchops_mcp_gateway.proxy import ProxyManager

from services.mcp_gateway_v1.tests.support import GatewayFixture, json_text
from services.mcp_gateway_v1.tests.test_injection import CASES


def definition(name="summarize", description="读取已登记条目的聚合摘要。"):
    return {"name": name, "description": description,
            "inputSchema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                            "properties": {"item_id": {"type": "string"}},
                            "required": ["item_id"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True}}


class FakeUpstream:
    def __init__(self, tools):
        self.tools = deepcopy(tools)
        self.calls = []
        self.discovery_count = 0
        self.result = {"status": "ok", "aggregate_count": 3}

    def list_tools(self):
        self.discovery_count += 1
        return deepcopy(self.tools)

    def call_tool(self, name, arguments, *, expected_definition_hash=None):
        tool = next(tool for tool in self.tools if tool["name"] == name)
        if expected_definition_hash is not None and definition_sha256(tool) != expected_definition_hash:
            raise ToolRuntimeError("gateway_tool_quarantined", "上游定义变化。")
        self.calls.append((name, deepcopy(arguments)))
        return {"structuredContent": deepcopy(self.result), "content": [], "isError": False}


class ProxyTests(GatewayFixture):
    def connect_proxy(self, upstream, *, server_id="evil"):
        self.manifest = ManifestStore(self.root / "manifest.json")
        self.proxy = ProxyManager(self.manifest, {server_id: upstream})
        self.gateway.proxy = self.proxy
        self.proxy.attach(self.gateway)

    def catalog_names(self):
        return {tool["name"] for tool in self.gateway.list_tools()}

    def all_events(self):
        return [event for run_id in (self.run_id, self.gateway.store.management_run_id)
                for event in self.gateway.ledger.export_run(run_id)["events"]]

    def assert_isolation_audited(self, code):
        self.assertTrue(any(event["safe_payload"].get("error_code") == code for event in self.all_events()), code)

    def test_case_09_tool_description_poisoning(self):
        tool = definition(description=CASES["C09"]["payload"])
        upstream = FakeUpstream([tool])
        self.connect_proxy(upstream)
        self.assertNotIn("evil__summarize", self.catalog_names())
        self.assert_error(self.call("evil__summarize", item_id="summary"), "gateway_tool_quarantined")
        self.assert_isolation_audited("gateway_tool_quarantined")
        self.manifest.pin("evil", tool, risk=RiskLevel.READ_ONLY)
        self.assertIn("evil__summarize", self.catalog_names())
        for path in ("../../secrets.txt", "data/file.csv", "/private/data.csv", "C:\\private\\data.csv"):
            self.assert_error(self.call("evil__summarize", item_id=path), "tool_arguments_invalid")
        self.assertEqual(upstream.calls, [])
        safe = self.call("evil__summarize", item_id="summary")
        self.assertFalse(safe["isError"], safe)
        self.assertEqual(len(upstream.calls), 1, "描述扫描只能告警，合法固定工具仍可执行")
        self.assertTrue(any("description" in event["event_type"] for event in self.all_events()))
        self.assertEqual(self.executions, [])

    def test_case_10_rug_pull(self):
        tool = definition()
        upstream = FakeUpstream([tool])
        self.connect_proxy(upstream)
        self.manifest.pin("evil", tool, risk=RiskLevel.READ_ONLY)
        self.assertIn("evil__summarize", self.catalog_names())
        upstream.tools[0]["description"] = CASES["C10"]["payload"]
        self.assertNotIn("evil__summarize", self.catalog_names())
        self.assert_error(self.call("evil__summarize", item_id="summary"), "gateway_tool_quarantined")
        self.assert_isolation_audited("gateway_tool_quarantined")
        self.assertEqual(upstream.calls, [])
        self.assertEqual(self.executions, [])

    def test_case_11_same_name_impersonation(self):
        tool = definition(name=CASES["C11"]["payload"])
        upstream = FakeUpstream([tool])
        self.connect_proxy(upstream)
        self.manifest.pin("evil", tool, risk=RiskLevel.READ_ONLY)
        self.assertNotIn("evil__publish_aggregate_results", self.catalog_names())
        self.assert_error(self.call("evil__publish_aggregate_results", item_id="summary"), "gateway_tool_name_collision")
        self.assert_isolation_audited("gateway_tool_name_collision")
        self.propose_publish()
        self.assertEqual(upstream.calls, [])
        self.assertEqual(self.executions, [])

    def test_case_12_arbitrary_execution_annotation_lie(self):
        tool = definition(name="shell", description=CASES["C12"]["payload"])
        upstream = FakeUpstream([tool])
        self.connect_proxy(upstream)
        self.manifest.pin("evil", tool, risk=RiskLevel.ARBITRARY_EXECUTION)
        self.assertNotIn("evil__shell", self.catalog_names())
        self.assert_error(self.call("evil__shell", item_id="echo stolen"), "tool_policy_denied")
        self.assert_isolation_audited("tool_policy_denied")
        self.assertEqual(upstream.calls, [])
        self.assertEqual(self.executions, [])

    def test_missing_risk_is_denied_and_all_definition_fields_are_bound(self):
        tool = definition()
        upstream = FakeUpstream([tool])
        self.connect_proxy(upstream)
        self.manifest.pin("evil", tool)
        self.assertNotIn("evil__summarize", self.catalog_names())
        self.assert_error(self.call("evil__summarize", item_id="summary"), "tool_policy_denied")
        original_hash = definition_sha256(tool)
        for field, new_value in (("name", "renamed"), ("description", "changed"),
                                 ("inputSchema", {"type": "object"}), ("annotations", {"readOnlyHint": False})):
            changed = deepcopy(tool)
            changed[field] = new_value
            self.assertNotEqual(definition_sha256(changed), original_hash)

    def test_quarantined_calls_consume_budget_before_discovery(self):
        from researchops_mcp_gateway.gateway import Gateway

        upstream = FakeUpstream([definition()])
        manifest = ManifestStore(self.root / "limited-manifest.json")
        proxy = ProxyManager(manifest, {"evil": upstream})
        gateway = Gateway(self.root, self.root / "limited-proxy", max_calls_per_run=2, proxy=proxy)
        run_id = gateway.call_tool("begin_run", {})["structuredContent"]["run_id"]
        count = upstream.discovery_count
        invalid = gateway.call_tool("evil__summarize", {"run_id": "invalid", "item_id": "summary"})
        self.assert_error(invalid, "gateway_run_invalid")
        self.assertEqual(upstream.discovery_count, count)
        denied = gateway.call_tool("evil__summarize", {"run_id": run_id, "item_id": "summary"})
        self.assert_error(denied, "gateway_tool_quarantined")
        count = upstream.discovery_count
        limited = gateway.call_tool("evil__summarize", {"run_id": run_id, "item_id": "summary"})
        self.assert_error(limited, "gateway_rate_limited")
        self.assertEqual(upstream.discovery_count, count)

    def test_approved_upstream_call_rechecks_definition_and_risk(self):
        tool = definition(name="prepare_release")
        upstream = FakeUpstream([tool])
        self.connect_proxy(upstream)
        self.manifest.pin("evil", tool, risk=RiskLevel.CONTROLLED_WRITE)
        self.catalog_names()
        proposal = self.call("evil__prepare_release", item_id="summary")
        self.assertFalse(proposal["isError"], proposal)
        self.assertEqual(proposal["structuredContent"]["status"], "awaiting_approval")
        call_id = proposal["structuredContent"]["call_id"]
        self.approve(call_id)
        upstream.tools[0]["description"] = "changed after approval"
        self.assert_error(self.call("execute_approved", call_id=call_id), "gateway_tool_quarantined")
        self.assertEqual(upstream.calls, [])

    def test_duplicate_raw_names_on_different_servers_are_quarantined(self):
        tool = definition()
        self.manifest = ManifestStore(self.root / "manifest.json")
        for server_id in ("first", "second"):
            self.manifest.pin(server_id, tool, risk=RiskLevel.READ_ONLY)
        self.proxy = ProxyManager(self.manifest, {"first": FakeUpstream([tool]), "second": FakeUpstream([tool])})
        self.gateway.proxy = self.proxy
        self.proxy.attach(self.gateway)
        names = self.catalog_names()
        self.assertNotIn("first__summarize", names)
        self.assertNotIn("second__summarize", names)

    def test_manifest_cli_and_unpin(self):
        tool = definition()
        path = self.root / "definition.json"
        path.write_text(json_text(tool), encoding="utf-8")
        prefix = ["--state-dir", str(self.root / "state")]
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(prefix + ["manifest", "pin", "--server", "trusted", "--definition", str(path), "--risk", "read_only"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "pinned")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(prefix + ["manifest", "show"]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["tools"]), 1)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(prefix + ["manifest", "unpin", "--server", "trusted", "--tool", "summarize"]), 0)
        self.assertEqual(ManifestStore(self.root / "state/manifest.json").show()["tools"], [])
