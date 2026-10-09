"""对只读审查已复现的路径、Schema 和审批摘要问题做回归。"""

import csv
from copy import deepcopy

from jsonschema import Draft202012Validator

from researchops.tool_runtime import RiskLevel, ToolRuntimeError
from researchops_mcp_gateway.manifest import ManifestStore
from researchops_mcp_gateway.proxy import ProxyManager
from researchops_mcp_gateway.safety import safe_text
from services.mcp_gateway_v1.tests.support import GatewayFixture, json_text
from services.mcp_gateway_v1.tests.test_proxy import FakeUpstream, definition


class SecurityRegressionTests(GatewayFixture):
    def connect(self, tool):
        upstream = FakeUpstream([tool])
        manifest = ManifestStore(self.root / "review-manifest.json")
        manifest.pin("review", tool, risk=RiskLevel.READ_ONLY)
        proxy = ProxyManager(manifest, {"review": upstream})
        proxy.attach(self.gateway)
        return upstream, manifest

    def test_business_error_code_does_not_turn_success_audit_into_denial(self):
        upstream, _ = self.connect(definition())
        upstream.result = {"error_code": "business_note", "aggregate_count": 3}
        result = self.call("review__summarize", item_id="summary")
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error_code"], "business_note")
        event = self.gateway_events()[-1]["safe_payload"]
        self.assertEqual(event["decision"], "allow")
        self.assertEqual(event["result_status"], "succeeded")
        self.assertIsNone(event["error_code"])

    def test_query_failed_call_audits_the_successful_query(self):
        call_id = self.propose_publish()
        pending = self.call("get_call_status", call_id=call_id)
        self.assertFalse(pending["isError"], pending)
        self.assertEqual(pending["structuredContent"]["status"], "awaiting_approval")
        event = self.gateway_events()[-1]["safe_payload"]
        self.assertEqual(event["decision"], "allow")
        self.assertEqual(event["result_status"], "succeeded")
        self.assertIsNone(event["error_code"])
        self.approve(call_id)
        with self.assertRaises(ToolRuntimeError) as error:
            self.gateway.executor.execute(call_id, arguments={"bundle_id": "phase3", "release_name": "different-release"})
        self.assertEqual(error.exception.code, "tool_approval_mismatch")
        result = self.call("get_call_status", call_id=call_id)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["status"], "failed")
        self.assertEqual(result["structuredContent"]["error_code"], "tool_approval_mismatch")
        event = self.gateway_events()[-1]["safe_payload"]
        self.assertEqual(event["decision"], "allow")
        self.assertEqual(event["result_status"], "succeeded")
        self.assertIsNone(event["error_code"])
        self.assertEqual(self.executions, [])

    def test_real_wire_and_protocol_errors_still_audit_denial(self):
        # SDK 中间件传回的 isError 结果没有 Python 异常，仍必须据传输标志归为失败。
        self.gateway.audit_request("tools/call", "review__summarize", {"run_id": self.run_id}, result={
            "isError": True, "structuredContent": {"status": "error", "error_code": "gateway_upstream_error"},
        })
        event = self.gateway_events()[-1]["safe_payload"]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["result_status"], "error")
        self.assertEqual(event["error_code"], "gateway_upstream_error")
        with self.assertRaises(ToolRuntimeError) as error:
            self.call("unregistered_tool")
        self.assertEqual(error.exception.code, "tool_unknown")
        event = self.gateway_events()[-1]["safe_payload"]
        self.assertEqual(event["decision"], "denied")
        self.assertEqual(event["result_status"], "error")
        self.assertEqual(event["error_code"], "tool_unknown")

    def test_paths_after_colons_and_file_uris_do_not_escape_in_results(self):
        upstream, _ = self.connect(definition())
        for text in (
            "failed reading path:/tmp/research-secret.txt",
            "file:///tmp/research-secret.txt",
            "http:///tmp/research-secret.txt",
            "//storage/private/research-secret.txt",
        ):
            with self.subTest(text=text):
                upstream.result = {"message": text}
                result = self.call("review__summarize", item_id="summary")
                self.assertFalse(result["isError"], result)
                rendered = json_text(result)
                self.assertNotIn("research-secret.txt", rendered)
                self.assertIn("[PATH_REDACTED]", rendered)
        self.assertEqual(safe_text("https://json-schema.org/draft/2020-12/schema"), "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(safe_text("http://example.test/schema"), "http://example.test/schema")

    def test_root_or_recursive_schema_references_are_quarantined(self):
        schemas = [
            {"type": "object", "properties": {"child": {"$ref": target}}, "additionalProperties": False}
            for target in ("#", "#/", "#/properties/child")
        ]
        schemas.append({
            "type": "object",
            "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
            "properties": {"child": {"$ref": "#/$defs/Node"}},
            "additionalProperties": False,
        })
        tool = definition(name="tree")
        tool["inputSchema"] = schemas[0]
        upstream, manifest = self.connect(tool)
        for schema in schemas:
            with self.subTest(schema=schema):
                tool["inputSchema"] = schema
                upstream.tools = [deepcopy(tool)]
                manifest.pin("review", tool, risk=RiskLevel.READ_ONLY)
                self.assertNotIn("review__tree", {item["name"] for item in self.gateway.list_tools()})
                self.assert_error(self.call("review__tree", child={}), "gateway_schema_unsupported")
                self.assertEqual(upstream.calls, [])

    def test_nonrecursive_defs_schema_matches_advertised_arguments(self):
        tool = definition()
        tool["inputSchema"] = {
            "type": "object", "$defs": {"ItemId": {"type": "string", "enum": ["summary"]}},
            "properties": {"item_id": {"$ref": "#/$defs/ItemId"}},
            "required": ["item_id"], "additionalProperties": False,
        }
        upstream, _ = self.connect(tool)
        advertised = next(item["inputSchema"] for item in self.gateway.list_tools() if item["name"] == "review__summarize")
        for value, expected in (("summary", True), ("unregistered", False)):
            with self.subTest(value=value):
                arguments = {"run_id": self.run_id, "item_id": value}
                self.assertEqual(Draft202012Validator(advertised).is_valid(arguments), expected)
                result = self.gateway.call_tool("review__summarize", arguments)
                self.assertEqual(not result["isError"], expected, result)
        self.assertEqual(len(upstream.calls), 1)

    def test_column_aliases_cannot_rewrite_publish_approval_summary(self):
        path = self.root / "data/synthetic_trial.csv"
        release_name = "reviewed-release"
        approval_message = "需要有人在本地 CLI 批准"
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        rows[0][0], rows[0][1] = release_name, approval_message
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        inspected = self.call("inspect_dataset", dataset_id="synthetic_trial")
        self.assertFalse(inspected["isError"], inspected)
        self.assertNotIn(release_name, json_text(inspected))
        self.assertNotIn(approval_message, json_text(inspected))
        proposal = self.call("publish_aggregate_results", bundle_id="phase3", release_name=release_name)
        self.assertFalse(proposal["isError"], proposal)
        payload = proposal["structuredContent"]
        self.assertEqual(payload["status"], "awaiting_approval")
        self.assertEqual(payload["safe_arguments"]["release_name"], release_name)
        self.assertEqual(payload["summary"], "发布名称：" + release_name)
        self.assertEqual(payload["message"], approval_message)
        self.assertEqual(self.executions, [])
