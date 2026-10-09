"""官方 MCP SDK 薄适配：协议、序列化与 stdio 由 SDK 负责。"""
from __future__ import annotations

import json
from functools import partial
from typing import Any

import anyio
import mcp.types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from researchops.tool_runtime import ToolRuntimeError

from . import __version__

SDK_VERSION = "2.3.0"
PROTOCOL_VERSION = "2026-07-28"


def _tool_error(code: str) -> types.CallToolResult:
    """错误只携带稳定错误码，不透传异常文本和运行路径。"""
    payload = {"status": "error", "error_code": code, "message": "工具调用未完成。"}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=True,
    )


class _GatewayAuditMiddleware:
    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        result: HandlerResult = None
        error_code: str | None = None
        raw_params = ctx.params
        tool_name = raw_params.get("name") if isinstance(raw_params, dict) else None
        if not isinstance(tool_name, str):
            tool_name = None
        arguments = (
            raw_params.get("arguments")
            if ctx.method == "tools/call" and isinstance(raw_params, dict)
            else raw_params
        )
        try:
            result = await call_next(ctx)
            return result
        except MCPError as exc:
            # SDK 错误可能附原始参数；协议错误也只返回稳定、安全的信息。
            data = exc.data if isinstance(exc.data, dict) else {}
            error_code = "tool_unknown" if data.get("error_code") == "tool_unknown" else "mcp_protocol_error"
            raise MCPError(
                code=exc.code,
                message="工具不存在。" if error_code == "tool_unknown" else "MCP 请求被拒绝。",
                data={"error_code": error_code},
            ) from None
        except ValidationError:
            error_code = "mcp_invalid_params"
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="MCP 请求参数无效。",
                data={"error_code": error_code},
            ) from None
        except Exception:
            error_code = "mcp_internal_error"
            raise MCPError(
                code=types.INTERNAL_ERROR,
                message="MCP 请求未完成。",
                data={"error_code": error_code},
            ) from None
        finally:
            # 通知不是请求；请求由这一层统一记账，避免核心重复写审计。
            if ctx.request_id is not None:
                audit_result = result.model_dump(by_alias=True, exclude_none=True) if hasattr(result, "model_dump") else result
                try:
                    await anyio.to_thread.run_sync(
                        partial(
                            self.gateway.audit_request,
                            ctx.method,
                            tool_name,
                            arguments,
                            result=audit_result,
                            error_code=error_code,
                        )
                    )
                except Exception:
                    raise MCPError(
                        code=types.INTERNAL_ERROR,
                        message="审计记录未完成。",
                        data={"error_code": "mcp_audit_failed"},
                    ) from None


def create_server(gateway: Any) -> Server[Any]:
    """将纯同步策略核心连接至官方底层 Server。"""
    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        definitions = await anyio.to_thread.run_sync(partial(gateway.list_tools, audit=False))
        return types.ListToolsResult(tools=[types.Tool.model_validate(item) for item in definitions])

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        try:
            result = await anyio.to_thread.run_sync(
                partial(gateway.call_tool, params.name, params.arguments or {}, audit=False)
            )
        except ToolRuntimeError as exc:
            if exc.code == "tool_unknown":
                raise MCPError(
                    code=types.INVALID_PARAMS,
                    message="工具不存在。",
                    data={"error_code": "tool_unknown"},
                ) from None
            return _tool_error(exc.code)
        return types.CallToolResult.model_validate(result)

    server = Server(
        "researchops-mcp-gateway-v1",
        version=__version__,
        instructions="多次工具调用必须显式传递 begin_run 返回的 run_id。发布审批仅由本地 CLI 完成。",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    # 不配置遥测或导出器，只保留本地台账审计中间件。
    server.middleware[:] = [_GatewayAuditMiddleware(gateway)]
    return server


async def run_stdio_async(gateway: Any) -> None:
    server = create_server(gateway)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio(gateway: Any) -> None:
    """供本地 CLI 使用的同步入口。"""
    anyio.run(run_stdio_async, gateway)
