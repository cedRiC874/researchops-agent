"""固定定义、受信风险策略和受控执行器之间的上游代理。"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from researchops.audit import canonical_json, safe_audit_value, sha256_json
from researchops.tool_runtime import IdempotencyMode, PolicyDisposition, RiskLevel, ToolRuntimeError, ToolSpec

from .gateway import LOCAL_TOOLS
from .manifest import ManifestStore, SERVER_ID, TOOL_NAME, definition_sha256, prefixed_name
from .safety import contains_path, safe_text, sanitize_result


_TOP_LEVEL_SCHEMA_KEYS = frozenset({"$schema", "$defs", "$comment", "type", "title", "description", "properties", "required", "additionalProperties"})
_DESCRIPTION_PATTERNS = {
    "filesystem_instruction": re.compile(r"(?i)(?:read|open|读取|打开).{0,80}(?:file|path|文件|路径)"),
    "credential_instruction": re.compile(r"(?i)(?:api[_ -]?key|credential|secret|password|密钥|密码|环境变量)"),
    "tool_chaining_instruction": re.compile(r"(?i)(?:call|invoke|execute|调用|执行).{0,80}(?:tool|publish|shell|工具|发布)"),
}
_UPSTREAM_ERROR_CODES = frozenset({"gateway_tool_quarantined", "gateway_upstream_unavailable", "gateway_upstream_error", "gateway_upstream_timeout"})


class ProxyManager:
    def __init__(self, manifest: ManifestStore, upstreams: Mapping[str, Any]) -> None:
        self.manifest = manifest
        self.upstreams = dict(upstreams)
        for server_id in self.upstreams:
            if not isinstance(server_id, str) or SERVER_ID.fullmatch(server_id) is None:
                raise ToolRuntimeError("gateway_configuration_invalid", "上游服务器标识无效。")
        self.gateway = None
        self._lock = threading.RLock()
        self._registered: set[str] = set()
        self._blocked: dict[str, str] = {}
        self._bindings: dict[str, dict[str, Any]] = {}
        self._warning_fingerprints: set[tuple[str, str]] = set()
        self._last_quarantine: dict[str, str] = {}

    def attach(self, gateway: Any) -> None:
        with self._lock:
            if self.gateway is not None and self.gateway is not gateway:
                raise ToolRuntimeError("gateway_configuration_invalid", "代理实例已经绑定另一网关。")
            self.gateway = gateway
            gateway.proxy = self
            self.refresh()

    def _audit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.gateway.ledger.append_event(self.gateway.store.management_run_id, event_type, payload, actor_kind="policy")

    def _quarantine(self, alias: str, code: str, *, server_id: str | None = None, tool_name: str | None = None) -> None:
        self._blocked[alias] = code
        if self._last_quarantine.get(alias) != code:
            self._audit("upstream_tool_quarantined", {"tool_name": alias, "server_id": server_id, "upstream_tool_name": tool_name, "error_code": code})
        self._last_quarantine[alias] = code

    def _clear_registered(self) -> None:
        for alias in sorted(self._registered):
            self.gateway.unregister_tool(alias)
        self._registered.clear()
        self._bindings.clear()

    @staticmethod
    def _check_schema(schema: Any) -> None:
        if not isinstance(schema, Mapping) or schema.get("type") != "object" or set(schema) - _TOP_LEVEL_SCHEMA_KEYS:
            raise ToolRuntimeError("gateway_schema_unsupported", "代理仅支持可安全加入运行句柄的顶层对象 schema。")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list) or "run_id" in properties or "run_id" in required:
            raise ToolRuntimeError("gateway_schema_unsupported", "上游 schema 不能占用运行句柄。")
        ProxyManager._check_schema_references(schema)

        def display_projection(value: Any) -> Any:
            if isinstance(value, Mapping):
                # 已验证的本地 JSON 指针不是文件路径，检查其余可展示文本即可。
                return {key: "validated_local_schema_reference" if key == "$ref" else display_projection(item) for key, item in value.items()}
            if isinstance(value, list):
                return [display_projection(item) for item in value]
            return value

        projected = display_projection(schema)
        if canonical_json(sanitize_result(projected)) != canonical_json(projected):
            raise ToolRuntimeError("gateway_schema_unsupported", "上游 schema 包含不能安全展示的字段。")

    @staticmethod
    def _check_schema_references(schema: Mapping[str, Any]) -> None:
        # 加入 run_id 会改变指向根对象的引用语义；仅接受 $defs 中不递归的静态定义。
        active: set[int] = set()

        def visit(value: Any) -> None:
            if not isinstance(value, (Mapping, list)):
                return
            identity = id(value)
            if identity in active:
                raise ToolRuntimeError("gateway_schema_unsupported", "代理不支持递归参数 schema。")
            active.add(identity)
            try:
                if isinstance(value, Mapping):
                    for key, item in value.items():
                        if key in {"$id", "$anchor", "$dynamicAnchor", "$dynamicRef"}:
                            raise ToolRuntimeError("gateway_schema_unsupported", "代理仅支持不改变解析基址的静态定义引用。")
                        if key == "$ref":
                            if not isinstance(item, str) or re.fullmatch(r"#/\$defs/[^/~%]+", item) is None:
                                raise ToolRuntimeError("gateway_schema_unsupported", "代理仅允许指向 $defs 具名定义的引用。")
                            definitions = schema.get("$defs", {})
                            target = definitions.get(item[len("#/$defs/"):]) if isinstance(definitions, Mapping) else None
                            if not isinstance(target, (Mapping, bool)):
                                raise ToolRuntimeError("gateway_schema_unsupported", "引用的参数定义不存在。")
                            visit(target)
                        else:
                            visit(item)
                else:
                    for item in value:
                        visit(item)
            finally:
                active.remove(identity)

        visit(schema)

    @staticmethod
    def _validate_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if contains_path(arguments):
            raise ToolRuntimeError("tool_arguments_invalid", "上游工具参数不能包含文件路径。")
        try:
            normalized = dict(arguments)
            if canonical_json(safe_audit_value(normalized)) != canonical_json(normalized):
                raise ToolRuntimeError("tool_arguments_invalid", "上游参数不能包含需脱敏或截断的内容。")
            return normalized
        except ToolRuntimeError:
            raise
        except Exception:
            raise ToolRuntimeError("tool_arguments_invalid", "上游参数不是可安全保存的 JSON。") from None

    def _warn_description(self, alias: str, server_id: str, definition: Mapping[str, Any], fingerprint: str) -> None:
        description = definition.get("description")
        if not isinstance(description, str) or (alias, fingerprint) in self._warning_fingerprints:
            return
        categories = [name for name, pattern in _DESCRIPTION_PATTERNS.items() if pattern.search(description)]
        if categories:
            self._audit("upstream_description_warning", {"code": "gateway_suspicious_description", "server_id": server_id, "tool_name": alias, "definition_sha256": fingerprint, "warning_categories": categories})
            self._warning_fingerprints.add((alias, fingerprint))

    def refresh(self) -> None:
        if self.gateway is None:
            raise ToolRuntimeError("gateway_configuration_invalid", "代理尚未绑定网关。")
        with self._lock:
            previous_names = set(self._registered) | set(self._blocked)
            self._clear_registered()
            self._blocked = {}
            try:
                entries = self.manifest.show()["tools"]
            except ToolRuntimeError:
                for alias in previous_names:
                    self._quarantine(alias, "gateway_tool_quarantined")
                raise
            pinned = {(entry["server_id"], entry["name"]): entry for entry in entries}
            discovered: list[tuple[str, dict[str, Any]]] = []
            unavailable: dict[str, str] = {}
            for server_id, client in self.upstreams.items():
                try:
                    definitions = client.list_tools()
                    if not isinstance(definitions, list):
                        raise ToolRuntimeError("gateway_upstream_error", "上游工具列表无效。")
                    for definition in definitions:
                        if not isinstance(definition, Mapping) or not isinstance(definition.get("name"), str) or TOOL_NAME.fullmatch(definition["name"]) is None:
                            raise ToolRuntimeError("gateway_upstream_error", "上游工具标识无效。")
                        discovered.append((server_id, deepcopy(dict(definition))))
                except Exception as exc:
                    code = exc.code if isinstance(exc, ToolRuntimeError) and exc.code in _UPSTREAM_ERROR_CODES else "gateway_upstream_unavailable"
                    unavailable[server_id] = code
                    # 部分列表也不能继续暴露；发现失败按整台上游隔离。
                    discovered = [item for item in discovered if item[0] != server_id]
            raw_counts = Counter(definition["name"] for _, definition in discovered)
            alias_counts = Counter(prefixed_name(server_id, definition["name"]) for server_id, definition in discovered)
            seen_aliases: set[str] = set()
            for server_id, definition in discovered:
                name = definition["name"]
                alias = prefixed_name(server_id, name)
                seen_aliases.add(alias)
                if name in LOCAL_TOOLS or raw_counts[name] > 1 or alias_counts[alias] > 1:
                    self._quarantine(alias, "gateway_tool_name_collision", server_id=server_id, tool_name=name)
                    continue
                try:
                    fingerprint = definition_sha256(definition)
                    self._warn_description(alias, server_id, definition, fingerprint)
                    entry = pinned.get((server_id, name))
                    if entry is None or entry["definition_sha256"] != fingerprint:
                        self._quarantine(alias, "gateway_tool_quarantined", server_id=server_id, tool_name=name)
                        continue
                    risk = RiskLevel(entry["risk"]) if entry["risk"] is not None else None
                    try:
                        disposition = self.gateway.executor.policy.evaluate(risk)
                    except ToolRuntimeError:
                        disposition = PolicyDisposition.DENY
                    if disposition is PolicyDisposition.DENY:
                        self._quarantine(alias, "tool_policy_denied", server_id=server_id, tool_name=name)
                        continue
                    self._check_schema(definition.get("inputSchema"))
                    binding = {"server_id": server_id, "name": name, "definition_sha256": fingerprint, "risk": risk.value}
                    spec = self._make_spec(alias, binding)
                    description = definition.get("description")
                    if description is not None and not isinstance(description, str):
                        raise ToolRuntimeError("gateway_schema_unsupported", "上游描述字段无效。")
                    wire = {"name": alias, "description": safe_text(description or "上游受控工具。"), "inputSchema": deepcopy(definition["inputSchema"])}
                    self.gateway.register_tool(spec, wire)
                    self._registered.add(alias)
                    self._bindings[alias] = binding
                    self._last_quarantine.pop(alias, None)
                except ToolRuntimeError as exc:
                    self._quarantine(alias, exc.code, server_id=server_id, tool_name=name)
            for entry in entries:
                alias = prefixed_name(entry["server_id"], entry["name"])
                if alias not in seen_aliases:
                    code = unavailable.get(entry["server_id"], "gateway_tool_quarantined")
                    self._quarantine(alias, code, server_id=entry["server_id"], tool_name=entry["name"])
            for alias in previous_names - self._registered - set(self._blocked):
                self._quarantine(alias, "gateway_tool_quarantined")

    def recognizes(self, name: str) -> bool:
        """仅判断本地名称空间，不读取清单或访问任何上游。"""
        if not isinstance(name, str) or name in LOCAL_TOOLS:
            return False
        with self._lock:
            return name in self._registered or name in self._blocked or any(name.startswith(f"{server_id}__") for server_id in self.upstreams)

    def before_call(self, name: str, arguments: Any) -> None:
        if name in LOCAL_TOOLS:
            return
        with self._lock:
            if not self.recognizes(name):
                return
            self.refresh()
            if name in self._blocked:
                raise ToolRuntimeError(self._blocked[name], "上游工具当前不可调用。")
            if name not in self._registered:
                return
            if not isinstance(arguments, Mapping):
                raise ToolRuntimeError("tool_arguments_invalid", "工具参数必须是 JSON 对象。")
            # 运行句柄只属于网关，不能传给上游或作为上游业务参数保存。
            self._validate_arguments({key: value for key, value in arguments.items() if key != "run_id"})

    def _check_binding(self, alias: str, expected: Mapping[str, Any]) -> dict[str, Any]:
        entries = self.manifest.show()["tools"]
        current = next((item for item in entries if (item["server_id"], item["name"]) == (expected["server_id"], expected["name"])), None)
        if current != expected:
            raise ToolRuntimeError("tool_precondition_changed", "上游工具的固定定义或风险策略已经改变。")
        return dict(expected)

    def _make_spec(self, alias: str, binding: Mapping[str, Any]) -> ToolSpec:
        expected = dict(binding)

        def handler(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
            del context
            with self._lock:
                self.before_call(alias, arguments)
                self._check_binding(alias, expected)
                if self._bindings.get(alias) != expected:
                    raise ToolRuntimeError("tool_precondition_changed", "上游定义或风险策略在执行前发生变化。")
                client = self.upstreams[expected["server_id"]]
                try:
                    result = client.call_tool(expected["name"], dict(arguments), expected_definition_hash=expected["definition_sha256"])
                except Exception as exc:
                    code = exc.code if isinstance(exc, ToolRuntimeError) and exc.code in _UPSTREAM_ERROR_CODES else "gateway_upstream_unavailable"
                    self.gateway.unregister_tool(alias)
                    self._registered.discard(alias)
                    self._bindings.pop(alias, None)
                    self._quarantine(alias, code, server_id=expected["server_id"], tool_name=expected["name"])
                    raise ToolRuntimeError(code, "上游工具调用未能安全完成。") from None
                if not isinstance(result, Mapping) or result.get("isError"):
                    raise ToolRuntimeError("gateway_upstream_error", "上游工具返回业务错误。")
                structured = result.get("structuredContent")
                if structured is None:
                    content = result.get("content")
                    if not isinstance(content, list) or not content or any(not isinstance(item, Mapping) or item.get("type") != "text" or not isinstance(item.get("text"), str) for item in content):
                        raise ToolRuntimeError("gateway_upstream_error", "上游工具结果格式不受支持。")
                    text = "\n".join(item["text"] for item in content)
                    try:
                        structured = json.loads(text)
                    except (ValueError, TypeError):
                        structured = {"text": text}
                if not isinstance(structured, Mapping):
                    raise ToolRuntimeError("gateway_upstream_error", "上游工具结果必须是对象。")
                return sanitize_result(structured)

        return ToolSpec(
            name=alias,
            version="proxy-v1:" + sha256_json(expected),
            policy_version="proxy-policy-v1:" + expected["risk"],
            risk=RiskLevel(expected["risk"]),
            handler=handler,
            validate_arguments=self._validate_arguments,
            safe_arguments=lambda arguments: dict(arguments),
            safe_result=lambda result: dict(result),
            scope_resources=lambda arguments: self._check_binding(alias, expected),
            idempotency=IdempotencyMode.NONE,
        )
