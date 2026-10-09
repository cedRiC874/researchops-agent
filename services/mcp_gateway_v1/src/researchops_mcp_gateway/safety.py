"""出站结果过滤与发布参数模式检查；不把描述扫描当作权限防线。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from researchops.audit import safe_audit_value
from researchops.tool_runtime import ToolRuntimeError


COLUMN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
DEFAULT_PUBLISH_PATTERNS = (
    r"(?i)(?:^|[-_])p\d{3,}(?:$|[-_])",
    r"(?i)(?:subject|participant|patient|受试者)[-_]?[a-z]*\d+",
)
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\r\n\t\"']+")
_POSIX_PATH = re.compile(r"(?<![\w:/])/(?!/)[^\s\"'<>]+")
_SECRET_VALUE = re.compile(r"(?i)\b(?:sk|api-key|token|secret)[-_][A-Za-z0-9_-]{8,}\b")
_SECRET_FIELD = re.compile(r"(?i)(?:api[_-]?key|secret|password|authorization|cookie|credential|access[_-]?token)")
_MACHINE_FIELDS = frozenset({
    "run_id", "analysis_run_id", "call_id", "tool_name", "status", "error_code", "code",
    "schema_version", "version", "rule_version", "tool_version", "policy_version",
    "release_name", "release_id", "bundle_id", "dataset_id", "design_id", "method_code",
})


def safe_text(value: str) -> str:
    value = _SECRET_VALUE.sub("[SECRET_REDACTED]", value)
    value = _WINDOWS_PATH.sub("[PATH_REDACTED]", value)
    return _POSIX_PATH.sub("[PATH_REDACTED]", value)


def reject_sensitive_publish(arguments: Mapping[str, Any], patterns: Sequence[str] | None = None) -> None:
    release_name = arguments.get("release_name")
    if not isinstance(release_name, str):
        return
    for pattern in DEFAULT_PUBLISH_PATTERNS if patterns is None else patterns:
        if re.search(pattern, release_name):
            raise ToolRuntimeError("gateway_sensitive_arguments", "发布名称命中受限标识模式，请使用不含受试者标识的名称。")


def contains_path(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_WINDOWS_PATH.search(value) or value.startswith(("/", "~/", "~\\")) or re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", value))
    if isinstance(value, Mapping):
        return any(contains_path(key) or contains_path(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_path(item) for item in value)
    return False


def sanitize_result(value: Mapping[str, Any], *, column_names: Sequence[str] = ()) -> dict[str, Any]:
    names: list[str] = list(column_names)

    def collect(item: Any) -> None:
        if isinstance(item, Mapping):
            columns = item.get("columns")
            if isinstance(columns, list):
                for column in columns:
                    if isinstance(column, Mapping) and isinstance(column.get("name"), str):
                        names.append(column["name"])
            for key, child in item.items():
                if key in {"column", "column_name"} and isinstance(child, str):
                    names.append(child)
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)

    collect(value)
    occupied = {name for name in names if COLUMN_NAME.fullmatch(name)}
    aliases: dict[str, str] = {}
    for name in names:
        if COLUMN_NAME.fullmatch(name) or name in aliases:
            continue
        index = len(aliases) + 1
        while f"col_{index}" in occupied:
            index += 1
        alias = f"col_{index}"
        aliases[name] = alias
        occupied.add(alias)

    def clean_string(item: str, *, preserve_machine_value: bool = False) -> str:
        if preserve_machine_value:
            return safe_text(item)
        if item in aliases:
            return aliases[item]
        for original in sorted(aliases, key=len, reverse=True):
            if len(original) >= 8 and not original.isspace():
                item = item.replace(original, aliases[original])
            elif original:
                # 短列名仅替换明确引用，不能改写日期小数点、版本号或普通标点。
                for quote in ("'", '"', "`", "“", "‘"):
                    closing = {"“": "”", "‘": "’"}.get(quote, quote)
                    item = item.replace(f"{quote}{original}{closing}", f"{quote}{aliases[original]}{closing}")
        return safe_text(item)

    def clean(item: Any, *, field: str = "") -> Any:
        if isinstance(item, str):
            machine_field = field in _MACHINE_FIELDS or field.endswith(("_at_utc", "_sha256", "_hash"))
            return clean_string(item, preserve_machine_value=machine_field)
        if isinstance(item, Mapping):
            return {
                clean_string(str(key)): "[SECRET_REDACTED]" if _SECRET_FIELD.search(str(key)) else clean(child, field=str(key))
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [clean(child, field=field) for child in item]
        return item

    cleaned = clean(value)
    if aliases:
        warnings = cleaned.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
            cleaned["warnings"] = warnings
        warnings.append({"code": "gateway_column_alias_applied", "message": "不安全列名已替换为安全别名，原列名不会返回。", "alias_count": len(aliases)})
    # 台账的已有脱敏规则也用于首次响应，保证重放与首次返回边界一致。
    result = safe_audit_value(cleaned)
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


def error_payload(code: str) -> dict[str, Any]:
    messages = {
        "tool_arguments_invalid": "工具参数无效，请按工具 schema 提供已登记的逻辑资源和参数。",
        "tool_approval_required": "需要有人在本地 CLI 批准。",
        "tool_approval_missing": "缺少有效的本地 CLI 人工审批记录。",
        "tool_approval_rejected": "人工已拒绝该调用。",
        "tool_approval_expired": "人工审批已过期，需要重新提议并审批。",
        "tool_approval_mismatch": "参数或审批范围与原提议不一致，执行已拒绝。",
        "tool_precondition_changed": "源资源、工具或策略条件已发生变化，执行已拒绝。",
        "tool_policy_denied": "当前工具风险策略拒绝此操作。",
        "tool_call_not_found": "找不到该工具调用。",
        "tool_call_state_conflict": "当前工具调用状态不允许此操作。",
        "tool_source_not_found": "已登记的输入资源尚未准备完成。",
        "gateway_run_invalid": "运行句柄无效。",
        "gateway_run_not_found": "运行句柄不存在。",
        "gateway_run_expired": "运行已过期，请创建新运行并重新提议。",
        "gateway_rate_limited": "该运行的工具调用次数已达到上限。",
        "gateway_call_run_mismatch": "调用句柄不属于该运行。",
        "gateway_sensitive_arguments": "发布参数命中受限标识模式。",
    }
    safe_code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) else "gateway_internal_error"
    return {"status": "error", "error_code": safe_code, "message": messages.get(safe_code, "请求已拒绝或未能完成；请检查本地审计记录。")}
