"""Pinned admission-link contract loading; no runtime or Provider capability."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import jsonschema

from .errors import ExternalClosurePrimitiveError
from .io import read_regular_file_no_follow
from .primitives import canonical_json_bytes, decode_strict_json_object
from .schema_guard import LOCAL_ONLY_SCHEMA_REGISTRY, only_local_schema_references


CONTRACT_PATH = "evals/provider_completion_admission_link_v1/admission_link_contract_v1.json"
CONTRACT_SHA256 = "94c23d0f64593d2fb52adcffd2d0decf531f2e78907d0e0b4cd85573c2b60561"
CONTRACT_COMMITMENT = "fe99ad889afbc85b3ad172d107ba85663e78898045c25b05112c222cf4d82dac"
_TOKEN = object()


def _fail(code: str):
    raise ExternalClosurePrimitiveError("admission_" + code) from None


@dataclass(frozen=True, slots=True, init=False)
class FrozenAdmissionLinkContract:
    contract_bytes: bytes
    schema_bytes: Mapping[str, bytes]
    _token: object

    def __init__(self, *args, **kwargs):
        raise TypeError("use load_admission_link_contract")

    def document(self) -> dict:
        if self._token is not _TOKEN:
            _fail("contract_invalid")
        return decode_strict_json_object(self.contract_bytes, max_bytes=8192)

    def validate(self, name: str, payload: bytes, *, max_bytes: int) -> dict:
        if self._token is not _TOKEN or name not in self.schema_bytes:
            _fail("contract_invalid")
        value = decode_strict_json_object(payload, max_bytes=max_bytes)
        schema = decode_strict_json_object(self.schema_bytes[name], max_bytes=65536)
        try:
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.FormatChecker(),
                registry=LOCAL_ONLY_SCHEMA_REGISTRY,
            ).validate(value)
        except Exception:
            _fail("schema_invalid")
        return value


def load_admission_link_contract(root: Path) -> FrozenAdmissionLinkContract:
    if not isinstance(root, Path):
        _fail("contract_invalid")
    try:
        raw = read_regular_file_no_follow(root / CONTRACT_PATH, max_bytes=8192)
        if len(raw) != 6831 or hashlib.sha256(raw).hexdigest() != CONTRACT_SHA256:
            _fail("contract_invalid")
        document = decode_strict_json_object(raw, max_bytes=8192)
        domain = document["byte_protocol"]["domain_values"]["contract"]
        commitment = hashlib.sha256(domain.encode() + b"\0" + canonical_json_bytes(document)).hexdigest()
        if commitment != CONTRACT_COMMITMENT:
            _fail("contract_invalid")
        schemas = {}
        entries = [(item["name"], root / Path(CONTRACT_PATH).parent / "schemas" / item["name"], item)
                   for item in document["schema_bindings"]]
        trust = document["predecessors"]["trust_schema"]
        entries.append(("external_trust_manifest_v1.schema.json", root / trust["path"], trust))
        for name, path, binding in entries:
            payload = read_regular_file_no_follow(path, max_bytes=65536)
            if len(payload) != binding["bytes"] or hashlib.sha256(payload).hexdigest() != binding["sha256"]:
                _fail("contract_invalid")
            schema = decode_strict_json_object(payload, max_bytes=65536)
            if not only_local_schema_references(schema):
                _fail("contract_invalid")
            jsonschema.Draft202012Validator.check_schema(schema)
            schemas[name] = payload
    except ExternalClosurePrimitiveError:
        raise
    except Exception:
        _fail("contract_invalid")
    value = object.__new__(FrozenAdmissionLinkContract)
    object.__setattr__(value, "contract_bytes", raw)
    object.__setattr__(value, "schema_bytes", MappingProxyType(schemas))
    object.__setattr__(value, "_token", _TOKEN)
    return value


__all__ = ["CONTRACT_PATH", "CONTRACT_SHA256", "CONTRACT_COMMITMENT",
           "FrozenAdmissionLinkContract", "load_admission_link_contract"]
