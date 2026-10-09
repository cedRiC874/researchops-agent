"""官方 SDK 假上游的离线桥接、分页、超时与配置验证。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops.tool_runtime import ToolRuntimeError
from researchops_mcp_gateway.manifest import definition_sha256
from researchops_mcp_gateway.upstream import StdioUpstream, load_upstreams
from services.mcp_gateway_v1.tests.support import PROJECT_ROOT


FAKE_SERVER = r'''
import sys
sys.path.insert(0, ROOT_PLACEHOLDER)
from services.mcp_gateway_v1.tests.offline import prohibit_network
sys.addaudithook(prohibit_network)
import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

mode = sys.argv[1]
async def list_tools(ctx, params):
    cursor = params.cursor if params else None
    names = ["echo"] if cursor is None else ["fail", "stall"]
    if mode == "loop":
        names = ["echo" if cursor is None else "again"]
    items = [types.Tool(name=name, description="离线假上游工具", inputSchema={
        "type": "object", "properties": {"value": {"type": "integer"}}, "additionalProperties": False
    }) for name in names]
    return types.ListToolsResult(tools=items, nextCursor="second" if cursor is None or mode == "loop" else None)
async def call_tool(ctx, params):
    if params.name == "fail":
        raise MCPError(code=types.INTERNAL_ERROR, message="不应透传的内部路径 C:/private/fixture")
    if params.name == "stall":
        await anyio.sleep(60)
    return types.CallToolResult(content=[types.TextContent(type="text", text="成功")],
                                structuredContent={"value": (params.arguments or {}).get("value")})
server = Server("offline-fixture", on_list_tools=list_tools, on_call_tool=call_tool)
async def main():
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())
anyio.run(main)
'''


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Windows runner 的 TEMP 可能使用 8.3 别名；夹具与 loader 都使用规范路径。
        self.root = Path(temporary.name).resolve(strict=True)
        environment = {"MPLCONFIGDIR": str(self.root / "mpl-cache")}
        if sys.platform == "win32":
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        safe_env = patch("mcp.client.stdio.get_default_environment", return_value=environment)
        safe_env.start()
        self.addCleanup(safe_env.stop)

    def upstream(self, mode="normal", timeout=15):
        script = FAKE_SERVER.replace("ROOT_PLACEHOLDER", repr(str(PROJECT_ROOT)))
        return StdioUpstream(sys.executable, ("-c", script, mode), str(self.root), timeout)

    def assert_code(self, code, callback):
        with self.assertRaises(ToolRuntimeError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        self.assertNotIn(str(self.root), str(captured.exception))
        self.assertNotIn("C:/private/fixture", str(captured.exception))

    def test_paginated_listing_and_pinned_same_connection_call(self):
        client = self.upstream()
        definitions = client.list_tools()
        self.assertEqual([tool["name"] for tool in definitions], ["echo", "fail", "stall"])
        digest = definition_sha256(definitions[0])
        result = client.call_tool("echo", {"value": 7}, expected_definition_hash=digest)
        self.assertEqual(result["structuredContent"], {"value": 7})
        self.assertFalse(result["isError"])
        self.assert_code("gateway_tool_quarantined", lambda: client.call_tool(
            "echo", {"value": 7}, expected_definition_hash="0" * 64))
        self.assert_code("gateway_tool_quarantined", lambda: client.call_tool("missing", {}))

    def test_repeated_pagination_and_total_tool_limit_are_rejected(self):
        self.assert_code("gateway_upstream_error", self.upstream("loop").list_tools)
        with patch("researchops_mcp_gateway.upstream.MAX_TOOLS", 1):
            self.assert_code("gateway_upstream_error", self.upstream().list_tools)

    def test_upstream_errors_timeout_and_missing_process_are_sanitized(self):
        self.assert_code("gateway_upstream_error", lambda: self.upstream().call_tool("fail", {}))
        self.assert_code("gateway_upstream_timeout", lambda: self.upstream(timeout=3).call_tool("stall", {}))
        self.assert_code("gateway_upstream_unavailable", StdioUpstream(
            str(self.root / "missing-executable"), (), str(self.root), 3).list_tools)

    def test_versioned_config_is_strict_and_resolves_local_cwd(self):
        entry = {"server_id": "fixture", "transport": "stdio", "command": sys.executable,
                 "args": ["-c", "pass"], "cwd": ".", "timeout_seconds": 2}
        payload = {"schema_version": "mcp-upstreams/1.0", "servers": [entry]}
        target = self.root / "upstreams.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_upstreams(target)
        self.assertEqual(loaded["fixture"].cwd, str(self.root))
        for extra in [{"env": {"DUMMY": "value"}}, {"transport": "http"},
                      {"timeout_seconds": 0}, {"timeout_seconds": True},
                      {"timeout_seconds": float("inf")}, {"server_id": "../escape"}]:
            with self.subTest(extra=extra):
                target.write_text(json.dumps({**payload, "servers": [{**entry, **extra}]}), encoding="utf-8")
                self.assert_code("gateway_upstream_config_invalid", lambda: load_upstreams(target))
        for malformed in [{**payload, "unknown": True}, {**payload, "servers": [entry, entry]},
                          {**payload, "schema_version": "unrecognized"}]:
            target.write_text(json.dumps(malformed), encoding="utf-8")
            self.assert_code("gateway_upstream_config_invalid", lambda: load_upstreams(target))
