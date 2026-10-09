"""仅由本地运维配置启动的官方 SDK stdio 上游桥接。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
import mcp.types as types
from mcp.shared.exceptions import MCPError

from researchops.tool_runtime import ToolRuntimeError
from .manifest import definition_sha256

CONFIG_SCHEMA_VERSION = "mcp-upstreams/1.0"
MAX_TOOLS = 1000
MAX_PAGES = 100
_SERVER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def _error(code: str) -> ToolRuntimeError:
    return ToolRuntimeError(code, "上游工具不可用，请由本地操作者复核。")


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            yield from _leaves(child)
    else:
        yield exc


@dataclass(frozen=True)
class StdioUpstream:
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    timeout_seconds: float = 30.0

    async def _list_tools(self, client: Client) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        cursors: set[str] = set()
        names: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
            page = await client.session.list_tools(params=params)
            for tool in page.tools:
                if tool.name in names or len(definitions) >= MAX_TOOLS:
                    raise _error("gateway_upstream_error")
                names.add(tool.name)
                definitions.append(tool.model_dump(by_alias=True, mode="json", exclude_none=True))
            cursor = page.next_cursor
            if cursor is None:
                return definitions
            if cursor in cursors:
                raise _error("gateway_upstream_error")
            cursors.add(cursor)
        raise _error("gateway_upstream_error")

    async def _request(self, name: str | None, arguments: dict[str, Any] | None,
                       expected_definition_hash: str | None):
        with anyio.fail_after(self.timeout_seconds):
            parameters = StdioServerParameters(command=self.command, args=list(self.args), cwd=self.cwd)
            # 官方 auto 模式处理现代发现和旧版初始化；不提供采样、审批或凭据回调。
            async with Client(parameters, mode="auto", read_timeout_seconds=self.timeout_seconds) as client:
                definitions = await self._list_tools(client)
                if name is None:
                    return definitions
                selected = next((tool for tool in definitions if tool["name"] == name), None)
                if selected is None or (expected_definition_hash is not None
                        and definition_sha256(selected) != expected_definition_hash):
                    raise _error("gateway_tool_quarantined")
                # 校验与调用在同一连接内进行；定义哈希不能证明上游执行代码诚实。
                result = await client.session.call_tool(name, arguments or {})
                if result.is_error:
                    raise _error("gateway_upstream_error")
                return result.model_dump(by_alias=True, mode="json", exclude_none=True)

    def _run(self, name: str | None, arguments: dict[str, Any] | None,
             expected_definition_hash: str | None):
        try:
            return anyio.run(self._request, name, arguments, expected_definition_hash)
        except Exception as exc:
            leaves = list(_leaves(exc))
            allowed = {"gateway_tool_quarantined", "gateway_upstream_error",
                       "gateway_upstream_unavailable", "gateway_upstream_timeout"}
            for error in leaves:
                if isinstance(error, ToolRuntimeError) and error.code in allowed:
                    raise _error(error.code) from None
            if any(isinstance(error, TimeoutError) or
                   isinstance(error, MCPError) and error.code == types.REQUEST_TIMEOUT for error in leaves):
                raise _error("gateway_upstream_timeout") from None
            if any(isinstance(error, OSError) for error in leaves):
                raise _error("gateway_upstream_unavailable") from None
            raise _error("gateway_upstream_error") from None

    def list_tools(self) -> list[dict[str, Any]]:
        return self._run(None, None, None)

    def call_tool(self, name: str, arguments: dict[str, Any], *,
                  expected_definition_hash: str | None = None) -> dict[str, Any]:
        return self._run(name, arguments, expected_definition_hash)


def load_upstreams(config_path: str | Path) -> dict[str, StdioUpstream]:
    """严格读取版本化本地配置；禁止 env、HTTP 和未知字段。"""
    try:
        path = Path(config_path).resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "servers"}
                or payload["schema_version"] != CONFIG_SCHEMA_VERSION
                or not isinstance(payload["servers"], list) or len(payload["servers"]) > 32):
            raise ValueError
        clients: dict[str, StdioUpstream] = {}
        for server in payload["servers"]:
            required = {"server_id", "transport", "command", "args"}
            if not isinstance(server, dict) or not required.issubset(server):
                raise ValueError
            if set(server) - required - {"cwd", "timeout_seconds"}:
                raise ValueError
            server_id = server["server_id"]
            if not isinstance(server_id, str) or not _SERVER_ID.fullmatch(server_id) or server_id in clients:
                raise ValueError
            command, args = server["command"], server["args"]
            if (server["transport"] != "stdio" or not isinstance(command, str) or not command.strip()
                    or "\x00" in command or not isinstance(args, list) or len(args) > 1000
                    or any(not isinstance(value, str) or "\x00" in value for value in args)):
                raise ValueError
            timeout = server.get("timeout_seconds", 30.0)
            if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300:
                raise ValueError
            cwd = server.get("cwd")
            if cwd is not None:
                if not isinstance(cwd, str) or not cwd.strip():
                    raise ValueError
                directory = (path.parent / cwd).resolve()
                if not directory.is_dir():
                    raise ValueError
                cwd = str(directory)
            clients[server_id] = StdioUpstream(command, tuple(args), cwd, float(timeout))
        return clients
    except (OSError, ValueError, TypeError, KeyError):
        raise ToolRuntimeError("gateway_upstream_config_invalid", "上游配置无效。") from None
