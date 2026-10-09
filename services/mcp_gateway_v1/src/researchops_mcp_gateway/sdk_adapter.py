"""官方 MCP SDK 薄适配：协议、序列化与 stdio 由 SDK 负责。"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
import mcp.types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from researchops.tool_runtime import ToolRuntimeError

from . import __version__

SDK_VERSION = "2.3.0"
PROTOCOL_VERSION = "2026-07-28"


@dataclass
class _RequestObservation:
    request_id: str | int
    method: str
    tool_name: str | None
    arguments: Any
    audited: bool = False


_request_observation: ContextVar[_RequestObservation | None] = ContextVar("mcp_request_observation", default=None)


class _ObservedReceiveStream(ObjectReceiveStream[SessionMessage | Exception]):
    """SDK 为每条消息保留 sender context；观察对象不依赖请求 ID 唯一性。"""
    def __init__(self, stream: ObjectReceiveStream[SessionMessage | Exception]) -> None:
        self.stream = stream

    async def receive(self) -> SessionMessage | Exception:
        item = await self.stream.receive()
        observed = None
        if isinstance(item, SessionMessage) and isinstance(item.message, types.JSONRPCRequest):
            request = item.message
            params = request.params
            tool_name = params.get("name") if isinstance(params, dict) else None
            arguments = params.get("arguments") if request.method == "tools/call" and isinstance(params, dict) else params
            observed = _RequestObservation(request.id, request.method, tool_name if isinstance(tool_name, str) else None, arguments)
        _request_observation.set(observed)
        return item

    async def aclose(self) -> None:
        await self.stream.aclose()


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
                    observed = _request_observation.get()
                    if observed is not None and observed.request_id == ctx.request_id:
                        observed.audited = True
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


class _ProtocolSafeSendStream(ObjectSendStream[SessionMessage]):
    """清洗 SDK 前置版本错误的反射字段；消息解析与序列化仍由 SDK 负责。"""
    def __init__(self, stream: ObjectSendStream[SessionMessage], gateway: Any) -> None:
        self.stream = stream
        self.gateway = gateway

    async def send(self, item: SessionMessage) -> None:
        message = item.message
        if isinstance(message, types.JSONRPCError) and message.error.code == types.UNSUPPORTED_PROTOCOL_VERSION:
            data = message.error.data
            if isinstance(data, dict) and isinstance(data.get("requested"), str):
                requested = data["requested"]
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", requested) is None:
                    # 日期形版本可用于协商；任意其他输入不应通过错误响应返回。
                    error = message.error.model_copy(update={
                        "data": {**data, "requested": "<invalid-protocol-version>"}})
                    message = message.model_copy(update={"error": error})
                    item = replace(item, message=message)
        observed = _request_observation.get()
        if (isinstance(message, types.JSONRPCError) and observed is not None
                and observed.request_id == message.id and not observed.audited):
            try:
                # 前置拒绝没有进入 middleware；按原请求补记，不保存输入原文。
                await anyio.to_thread.run_sync(partial(
                    self.gateway.audit_request, observed.method, observed.tool_name, observed.arguments,
                    error_code="mcp_protocol_error"))
                observed.audited = True
            except Exception:
                # 仍由 SDK 发出原响应包络；审计失败必须返回安全错误。
                error = types.ErrorData(code=types.INTERNAL_ERROR, message="审计记录未完成。",
                                        data={"error_code": "mcp_audit_failed"})
                item = replace(item, message=message.model_copy(update={"error": error}))
        await self.stream.send(item)

    async def aclose(self) -> None:
        await self.stream.aclose()


async def run_stdio_async(gateway: Any) -> None:
    server = create_server(gateway)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(_ObservedReceiveStream(read_stream), _ProtocolSafeSendStream(write_stream, gateway),
                         server.create_initialization_options())


def run_stdio(gateway: Any) -> None:
    """供本地 CLI 使用的同步入口。"""
    anyio.run(run_stdio_async, gateway)
