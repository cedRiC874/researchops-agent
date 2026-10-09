"""与 MCP SDK 无关的策略网关；人工审批仅由本地 CLI 调用核心 API。"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from researchops.audit import AuditError, AuditLedger, sha256_json
from researchops.tool_runtime import (
    ControlledToolExecutor,
    IdempotencyMode,
    RiskLevel,
    ToolInvocationContext,
    ToolRegistry,
    ToolRuntimeError,
    ToolSpec,
    build_project_tool_registry,
)

from .safety import error_payload, reject_sensitive_publish, safe_text, sanitize_result
from .schemas import CALL_ID, RUN_ID, local_tool_definitions
from .state import StateStore


CONTROL_TOOLS = frozenset({"begin_run", "execute_approved", "get_call_status"})
LOCAL_TOOLS = frozenset({*CONTROL_TOOLS, "inspect_dataset", "recommend_statistical_method", "read_aggregate_evidence", "publish_aggregate_results"})


class Gateway:
    def __init__(
        self,
        project_root: str | Path,
        state_directory: str | Path,
        *,
        source_snapshot_root: str | Path | None = None,
        registry: ToolRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        run_ttl_seconds: int = 3600,
        max_calls_per_run: int = 100,
        publish_patterns: Sequence[str] | None = None,
        proxy: Any = None,
    ) -> None:
        if type(run_ttl_seconds) is not int or run_ttl_seconds <= 0:
            raise ValueError("运行有效期必须是正整数。")
        if type(max_calls_per_run) is not int or max_calls_per_run <= 0:
            raise ValueError("每运行调用次数上限必须是正整数。")
        self.project_root = Path(project_root).resolve()
        self.source_snapshot_root = Path(source_snapshot_root).resolve() if source_snapshot_root is not None else self.project_root
        self.state_directory = Path(state_directory).resolve()
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.run_ttl_seconds = run_ttl_seconds
        self.max_calls_per_run = max_calls_per_run
        self.publish_patterns = None if publish_patterns is None else tuple(publish_patterns)
        if self.publish_patterns is not None:
            for pattern in self.publish_patterns:
                re.compile(pattern)
        self._lock = threading.RLock()
        self.store = StateStore(self.state_directory / "gateway.sqlite3", clock=clock)
        self.ledger = AuditLedger(self.state_directory / "audit.sqlite3", clock=clock)
        self._management_run_id = self.store.management_run_id
        self._ensure_management_run()
        self.registry = ToolRegistry()
        definitions = {item["name"]: item for item in local_tool_definitions(self.run_ttl_seconds)}
        original = registry if registry is not None else build_project_tool_registry(
            self.project_root, source_snapshot_root=source_snapshot_root,
        )
        for spec in original.specs():
            if spec.name in CONTROL_TOOLS:
                # 重启时允许传入前一实例的注册表，但控制处理器必须重新绑定。
                continue
            if spec.name == "publish_aggregate_results":
                validator = spec.validate_arguments

                def validate_publish(arguments: Mapping[str, Any], validate: Any = validator) -> dict[str, Any]:
                    reject_sensitive_publish(arguments, self.publish_patterns)
                    return validate(arguments)

                spec = replace(spec, validate_arguments=validate_publish)
            if spec.name in definitions:
                spec = self._with_schema_validation(spec, definitions[spec.name]["inputSchema"], strip_run_id=True)
            self.registry.register(spec)
        self.executor = ControlledToolExecutor(self.ledger, self.registry, clock=clock)
        self._register_controls()
        available = {spec.name for spec in self.registry.specs()}
        self._definitions = {
            definition["name"]: definition
            for definition in local_tool_definitions(self.run_ttl_seconds)
            if definition["name"] in available
        }
        self.proxy = proxy
        if proxy is not None:
            proxy.attach(self)

    def _ensure_management_run(self) -> None:
        try:
            self.ledger.get_run(self._management_run_id)
        except AuditError:
            try:
                self.ledger.start_run(mode="mcp_gateway_management", request_summary={"kind": "gateway_management"}, run_id=self._management_run_id)
            except sqlite3.IntegrityError:
                # 并发启动时另一进程可能已经创建相同管理运行。
                self.ledger.get_run(self._management_run_id)

    @staticmethod
    def _validate_empty(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments):
            raise ToolRuntimeError("tool_arguments_invalid", "begin_run 不接受参数。")
        return {}

    @staticmethod
    def _with_schema_validation(spec: ToolSpec, schema: Mapping[str, Any], *, strip_run_id: bool = False) -> ToolSpec:
        business_schema = deepcopy(dict(schema))
        if strip_run_id:
            business_schema.get("properties", {}).pop("run_id", None)
            business_schema["required"] = [key for key in business_schema.get("required", []) if key != "run_id"]

        def check_refs(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                        raise ToolRuntimeError("gateway_schema_unsupported", "不允许需要外部资源的参数 schema。")
                    check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)

        check_refs(business_schema)
        try:
            Draft202012Validator.check_schema(business_schema)
        except Exception:
            raise ToolRuntimeError("gateway_schema_unsupported", "工具参数 schema 无效。") from None
        validator = Draft202012Validator(business_schema)
        original = spec.validate_arguments

        def validate(arguments: Mapping[str, Any]) -> dict[str, Any]:
            try:
                invalid = next(validator.iter_errors(arguments), None)
            except Exception:
                raise ToolRuntimeError("tool_arguments_invalid", "参数无法通过本地 schema 校验。") from None
            if invalid is not None:
                raise ToolRuntimeError("tool_arguments_invalid", "参数不符合工具 schema。")
            return original(arguments)

        return replace(spec, validate_arguments=validate)

    @staticmethod
    def _validate_handle(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"call_id"} or not isinstance(arguments.get("call_id"), str):
            raise ToolRuntimeError("tool_arguments_invalid", "只允许提供调用句柄。")
        if re.fullmatch(CALL_ID["pattern"], arguments["call_id"]) is None:
            raise ToolRuntimeError("tool_arguments_invalid", "调用句柄无效。")
        return {"call_id": arguments["call_id"]}

    def _register_controls(self) -> None:
        controls = (
            ("begin_run", self._validate_empty, self._begin_result),
            ("execute_approved", self._validate_handle, self._execute_handle),
            ("get_call_status", self._validate_handle, self._status_handle),
        )
        for name, validator, handler in controls:
            self.registry.register(ToolSpec(
                name=name,
                version="1.0.0",
                risk=RiskLevel.READ_ONLY,
                handler=handler,
                validate_arguments=validator,
                safe_arguments=lambda arguments: dict(arguments),
                safe_result=lambda result: dict(result),
                scope_resources=lambda arguments: {"gateway_control_version": "1.0.0", **dict(arguments)},
                idempotency=IdempotencyMode.IDEMPOTENT,
            ))

    def _begin_result(self, arguments: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        del arguments
        run = self.store.check_run(context.run_id)
        return {
            "status": "ready",
            "run_id": context.run_id,
            "expires_at_utc": run["expires_at_utc"],
            "ttl_seconds": run["ttl_seconds"],
            "max_calls_per_run": run["max_calls"],
            "message": "运行到期后所有调用均拒绝；需要创建新运行并重新提议。",
        }

    def _owned_call(self, run_id: str, call_id: str) -> dict[str, Any]:
        self.store.check_run(run_id)
        try:
            call = self.ledger.get_tool_call(call_id)
        except AuditError as exc:
            raise ToolRuntimeError(exc.code, "调用句柄无效。") from None
        if call["run_id"] != run_id:
            raise ToolRuntimeError("gateway_call_run_mismatch", "调用句柄不属于该运行。")
        return call

    def _execute_handle(self, arguments: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        call = self._owned_call(context.run_id, str(arguments["call_id"]))
        # 控制调用不能作为另一个执行控制调用的目标，避免递归或重放控制状态。
        if call["tool_name"] in CONTROL_TOOLS:
            raise ToolRuntimeError("tool_arguments_invalid", "执行句柄必须指向业务工具调用。")
        if self.proxy is not None:
            self.proxy.before_call(str(call["tool_name"]), call["safe_args"])
        return self.executor.execute(str(call["call_id"])).to_dict()

    def _status_handle(self, arguments: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        call = self._owned_call(context.run_id, str(arguments["call_id"]))
        return {
            "run_id": context.run_id,
            "call_id": call["call_id"],
            "tool_name": call["tool_name"],
            "status": call["status"],
            "attempt_count": call["attempt_count"],
            "error_code": call["terminal_error_code"],
            "result": call["safe_result"],
        }

    def register_tool(self, spec: ToolSpec, definition: Mapping[str, Any]) -> None:
        """仅供可信启动配置中的代理管理器注册已核验工具。"""
        with self._lock:
            if spec.name in LOCAL_TOOLS or spec.name in self._definitions or definition.get("name") != spec.name:
                raise ToolRuntimeError("gateway_tool_name_collision", "工具名称冲突。")
            wire = deepcopy(dict(definition))
            schema = wire.get("inputSchema")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ToolRuntimeError("tool_arguments_invalid", "上游工具必须声明对象参数 schema。")
            properties = schema.setdefault("properties", {})
            if "run_id" in properties:
                raise ToolRuntimeError("gateway_tool_name_collision", "上游参数不能占用运行句柄。")
            spec = self._with_schema_validation(spec, schema)
            properties["run_id"] = deepcopy(RUN_ID)
            schema.setdefault("required", []).append("run_id")
            wire["annotations"] = {
                "readOnlyHint": spec.risk is RiskLevel.READ_ONLY,
                "destructiveHint": spec.risk is RiskLevel.DESTRUCTIVE,
            }
            self.registry.register(spec)
            self._definitions[spec.name] = wire

    def unregister_tool(self, name: str) -> None:
        """清单不匹配立即下线；核心注册表没有删除接口，故重建其视图。"""
        with self._lock:
            if name in LOCAL_TOOLS:
                raise ToolRuntimeError("gateway_tool_name_collision", "不能下线本地工具。")
            replacement = ToolRegistry()
            for spec in self.registry.specs():
                if spec.name != name:
                    replacement.register(spec)
            self.registry = replacement
            self.executor.registry = replacement
            self._definitions.pop(name, None)

    def list_tools(self, *, audit: bool = True) -> list[dict[str, Any]]:
        error_code = None
        result = None
        try:
            if self.proxy is not None:
                self.proxy.refresh()
            with self._lock:
                result = [deepcopy(self._definitions[name]) for name in sorted(self._definitions)]
            return result
        except (AuditError, ToolRuntimeError) as exc:
            error_code = exc.code
            raise
        finally:
            if audit:
                self.audit_request("tools/list", None, {}, result={"status": "succeeded"} if result is not None else None, error_code=error_code)

    def _column_names(self) -> list[str]:
        # 只读取已登记 CSV 的表头，覆盖方法说明中的同源列名引用。
        path = self.source_snapshot_root / "data" / "synthetic_trial.csv"
        try:
            if not path.resolve().is_relative_to((self.source_snapshot_root / "data").resolve()):
                return []
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return next(csv.reader(handle), [])
        except (OSError, UnicodeError, csv.Error):
            return []

    def _wire(self, payload: Mapping[str, Any], *, is_error: bool = False, gateway_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        metadata = gateway_metadata or {}
        body = {key: value for key, value in payload.items() if key not in metadata}
        safe = sanitize_result(body, column_names=self._column_names())
        # 审批摘要来自已保存的安全参数，不能被不相关的数据列名改写；仍进行路径及秘密过滤。
        safe.update(sanitize_result(metadata))
        return {"content": [{"type": "text", "text": json.dumps(safe, ensure_ascii=False, sort_keys=True)}], "structuredContent": safe, "isError": is_error}

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        """供本地 CLI 读取；不注册为 MCP 工具。"""
        pending = []
        for run in self.store.list_runs():
            exported = self.ledger.export_run(run["run_id"])
            if exported["run"]["mode"] != "mcp_gateway":
                continue
            for call in exported["tool_calls"]:
                if call["status"] == "awaiting_approval" and call["tool_name"] not in CONTROL_TOOLS:
                    pending.append({
                        "run_id": run["run_id"], "call_id": call["call_id"], "tool_name": call["tool_name"],
                        "status": call["status"], "safe_arguments": call["safe_args"],
                        "run_expires_at_utc": run["expires_at_utc"],
                    })
        return [sanitize_result(item, column_names=self._column_names()) for item in pending]

    def decide(self, call_id: str, *, decision: str, approver: str, ttl: int = 900) -> dict[str, Any]:
        """本地 CLI 的审批入口；模型可调用列表始终没有该方法。"""
        try:
            call = self.ledger.get_tool_call(call_id)
            run = self.ledger.get_run(str(call["run_id"]))
        except AuditError as exc:
            raise ToolRuntimeError(exc.code, "找不到可审批调用。") from None
        if run["mode"] != "mcp_gateway" or call["tool_name"] in CONTROL_TOOLS:
            raise ToolRuntimeError("tool_policy_denied", "该调用不属于网关业务审批范围。")
        self.store.check_run(str(call["run_id"]))
        if self.proxy is not None:
            self.proxy.before_call(str(call["tool_name"]), call["safe_args"])
        return self.executor.decide(call_id, decision=decision, approver=approver, expires_in_seconds=ttl).to_dict()

    def call_tool(self, name: str, arguments: Any, *, audit: bool = True) -> dict[str, Any]:
        result = None
        error_code = None
        try:
            recognized = isinstance(name, str) and (name in self._definitions or (self.proxy is not None and self.proxy.recognizes(name)))
            if not recognized:
                # 有效运行上的未知工具尝试也消耗配额；未知名始终保留协议错误语义。
                if isinstance(arguments, Mapping) and "run_id" in arguments:
                    try:
                        self.store.consume_call(arguments["run_id"])
                    except ToolRuntimeError:
                        pass
                # 未知工具仍先进入核心策略路径，但不会产生业务执行。
                unknown_name = name if isinstance(name, str) else "invalid_tool_name"
                if unknown_name not in {spec.name for spec in self.registry.specs()}:
                    self.executor.propose(self._management_run_id, unknown_name, {})
                raise ToolRuntimeError("tool_unknown", "未知工具。")
            if not isinstance(arguments, Mapping):
                raise ToolRuntimeError("tool_arguments_invalid", "工具参数必须是 JSON 对象。")
            if name == "begin_run":
                self._validate_empty(arguments)
                run_id = str(uuid.uuid4())
                self.ledger.start_run(mode="mcp_gateway", request_summary={"kind": "mcp_run", "ttl_seconds": self.run_ttl_seconds, "max_calls": self.max_calls_per_run}, run_id=run_id)
                self.store.create_run(run_id, ttl_seconds=self.run_ttl_seconds, max_calls=self.max_calls_per_run)
                normalized = {}
            else:
                run_id = self.store.validate_run_id(arguments.get("run_id"))
                normalized = {key: value for key, value in arguments.items() if key != "run_id"}
            self.store.consume_call(run_id)
            if self.proxy is not None:
                self.proxy.before_call(name, normalized)
            outcome = self.executor.propose(run_id, name, normalized)
            gateway_metadata = None
            if outcome.requires_approval:
                call = self.ledger.get_tool_call(outcome.call_id)
                safe_arguments = call["safe_args"]
                release_name = safe_arguments.get("release_name")
                summary = f"发布名称：{release_name}" if release_name is not None else "受控工具提议等待本地人工审批。"
                payload = {
                    "status": "awaiting_approval", "run_id": run_id, "call_id": outcome.call_id,
                    "tool_name": name, "safe_arguments": safe_arguments,
                    "summary": summary, "message": "需要有人在本地 CLI 批准",
                }
                gateway_metadata = {"summary": payload["summary"], "message": payload["message"]}
            elif name in CONTROL_TOOLS:
                payload = dict(outcome.result or {})
            else:
                payload = {**dict(outcome.result or {}), "status": outcome.status, "run_id": run_id, "call_id": outcome.call_id, "tool_name": name}
            result = self._wire(payload, gateway_metadata=gateway_metadata)
            return result
        except (AuditError, ToolRuntimeError) as exc:
            error_code = exc.code
            if exc.code == "tool_unknown":
                raise ToolRuntimeError("tool_unknown", "未知工具。") from None
            result = self._wire(error_payload(exc.code), is_error=True)
            return result
        except Exception:
            error_code = "gateway_internal_error"
            result = self._wire(error_payload(error_code), is_error=True)
            return result
        finally:
            if audit:
                self.audit_request("tools/call", name, arguments, result=result, error_code=error_code)

    def audit_request(
        self,
        method: str,
        tool_name: str | None,
        arguments: Any,
        result: Any = None,
        error_code: str | None = None,
    ) -> None:
        """每个 MCP 请求一次；不持久化原始请求参数或不可信错误文本。"""
        structured = result.get("structuredContent", result) if isinstance(result, Mapping) else {}
        structured = structured if isinstance(structured, Mapping) else {}
        candidate_run = structured.get("run_id")
        if candidate_run is None and isinstance(arguments, Mapping):
            candidate_run = arguments.get("run_id")
        run_id = self._management_run_id
        if isinstance(candidate_run, str):
            try:
                run = self.ledger.get_run(candidate_run)
                if run["mode"] == "mcp_gateway":
                    run_id = candidate_run
            except AuditError:
                pass
        try:
            arguments_hash = sha256_json(arguments)
        except Exception:
            arguments_hash = sha256_json({"invalid_json_arguments": True})
        status = structured.get("status", "error" if error_code else "succeeded")
        effective_error = error_code or structured.get("error_code")
        if isinstance(result, Mapping) and result.get("isError"):
            status = "error"
        call_id = structured.get("call_id")
        if call_id is None and isinstance(arguments, Mapping):
            call_id = arguments.get("call_id")
        payload = {
            "method": safe_text(str(method)),
            "tool_name": safe_text(tool_name) if isinstance(tool_name, str) else None,
            "arguments_hash": arguments_hash,
            "decision": "denied" if effective_error else ("require_approval" if status == "awaiting_approval" else "allow"),
            "call_id": call_id if isinstance(call_id, str) and re.fullmatch(CALL_ID["pattern"], call_id) else None,
            "result_status": status if isinstance(status, str) else "unknown",
            "error_code": effective_error,
        }
        self.ledger.append_event(run_id, "mcp_request", payload, actor_kind="mcp_gateway")
