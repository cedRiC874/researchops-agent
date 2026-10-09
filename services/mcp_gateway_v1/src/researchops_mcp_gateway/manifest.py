"""由本地操作者固定的上游工具定义清单。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from researchops.audit import sha256_json
from researchops.tool_runtime import RiskLevel, ToolRuntimeError


MANIFEST_SCHEMA_VERSION = "1.0"
SERVER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ENTRY_FIELDS = {"server_id", "name", "definition_sha256", "risk"}


def definition_sha256(definition: Mapping[str, Any]) -> str:
    """只对规范要求的四个字段求哈希；缺失与空对象保持区别。"""
    if not isinstance(definition, Mapping):
        raise ToolRuntimeError("gateway_manifest_invalid", "工具定义必须是对象。")
    projected = {key: definition.get(key) for key in ("name", "description", "inputSchema", "annotations")}
    try:
        return sha256_json(projected)
    except Exception:
        raise ToolRuntimeError("gateway_manifest_invalid", "工具定义不是可固定的 JSON。") from None


def prefixed_name(server_id: str, tool_name: str) -> str:
    return f"{server_id}__{tool_name}"


class ManifestStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    @staticmethod
    def _check_identity(server_id: Any, tool_name: Any) -> None:
        if not isinstance(server_id, str) or SERVER_ID.fullmatch(server_id) is None:
            raise ToolRuntimeError("gateway_manifest_invalid", "服务器标识格式无效。")
        if not isinstance(tool_name, str) or TOOL_NAME.fullmatch(tool_name) is None:
            raise ToolRuntimeError("gateway_manifest_invalid", "工具标识格式无效。")

    @staticmethod
    def _risk_value(risk: RiskLevel | str | None) -> str | None:
        if risk is None:
            return None
        try:
            return RiskLevel(risk).value
        except (TypeError, ValueError):
            raise ToolRuntimeError("gateway_manifest_invalid", "风险类别无效。") from None

    def show(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schema_version": MANIFEST_SCHEMA_VERSION, "tools": []}
        except OSError:
            raise ToolRuntimeError("gateway_manifest_unavailable", "固定清单无法读取。") from None
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            raise ToolRuntimeError("gateway_manifest_invalid", "固定清单不是有效 JSON。") from None
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "tools"} or payload["schema_version"] != MANIFEST_SCHEMA_VERSION or not isinstance(payload["tools"], list):
            raise ToolRuntimeError("gateway_manifest_invalid", "固定清单版本或结构不受支持。")
        seen = set()
        for entry in payload["tools"]:
            if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
                raise ToolRuntimeError("gateway_manifest_invalid", "固定清单条目结构无效。")
            self._check_identity(entry["server_id"], entry["name"])
            identity = (entry["server_id"], entry["name"])
            if identity in seen:
                raise ToolRuntimeError("gateway_manifest_invalid", "固定清单包含重复工具。")
            seen.add(identity)
            fingerprint = entry["definition_sha256"]
            if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
                raise ToolRuntimeError("gateway_manifest_invalid", "工具定义哈希无效。")
            if self._risk_value(entry["risk"]) != entry["risk"]:
                raise ToolRuntimeError("gateway_manifest_invalid", "风险类别无效。")
        return payload

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock.sqlite3")
        connection = sqlite3.connect(lock_path, timeout=10.0)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS manifest_lock (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.commit()
        finally:
            connection.close()

    def _write(self, payload: Mapping[str, Any]) -> None:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".manifest-", suffix=".tmp", delete=False) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError:
            raise ToolRuntimeError("gateway_manifest_unavailable", "固定清单无法原子保存。") from None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def pin(self, server_id: str, definition: Mapping[str, Any], risk: RiskLevel | str | None = None) -> dict[str, Any]:
        if not isinstance(definition, Mapping):
            raise ToolRuntimeError("gateway_manifest_invalid", "工具定义必须是对象。")
        self._check_identity(server_id, definition.get("name"))
        if not isinstance(definition.get("inputSchema"), Mapping):
            raise ToolRuntimeError("gateway_manifest_invalid", "工具定义缺少对象参数 schema。")
        for field in ("description", "annotations"):
            value = definition.get(field)
            expected = str if field == "description" else Mapping
            if value is not None and not isinstance(value, expected):
                raise ToolRuntimeError("gateway_manifest_invalid", "工具定义字段类型无效。")
        entry = {
            "server_id": server_id,
            "name": definition["name"],
            "definition_sha256": definition_sha256(definition),
            "risk": self._risk_value(risk),
        }
        with self._locked():
            payload = self.show()
            entries = [item for item in payload["tools"] if (item["server_id"], item["name"]) != (server_id, entry["name"])]
            entries.append(entry)
            payload["tools"] = sorted(entries, key=lambda item: (item["server_id"], item["name"]))
            self._write(payload)
        return dict(entry)

    def unpin(self, server_id: str, tool_name: str) -> dict[str, Any]:
        self._check_identity(server_id, tool_name)
        with self._locked():
            payload = self.show()
            before = len(payload["tools"])
            payload["tools"] = [item for item in payload["tools"] if (item["server_id"], item["name"]) != (server_id, tool_name)]
            self._write(payload)
        return {"server_id": server_id, "name": tool_name, "removed": len(payload["tools"]) < before}
