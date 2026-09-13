"""No-retrieval guard for caller-supplied JSON Schemas.

`jsonschema` may retrieve a remote resource transitively for `$ref` or
`$dynamicRef` even when the verifier itself imports no networking package.
External-closure verification accepts only self-contained fragment references
and also installs a registry whose retrieval callback always fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
import math

from referencing import Registry

from .errors import ExternalClosurePrimitiveError


_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


def _deny_retrieval(_uri: str) -> object:
    raise ExternalClosurePrimitiveError(
        "external_closure_schema_reference_forbidden"
    )


LOCAL_ONLY_SCHEMA_REGISTRY: Registry = Registry(retrieve=_deny_retrieval)


def only_local_schema_references(value: object, *, depth: int = 0) -> bool:
    """Return true only for a bounded tree of same-document JSON pointers."""

    if depth > 128:
        return False
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _REFERENCE_KEYWORDS and (
                type(item) is not str or not item.startswith("#/")
            ):
                return False
            if not only_local_schema_references(item, depth=depth + 1):
                return False
        return True
    if isinstance(value, (list, tuple)):
        return all(
            only_local_schema_references(item, depth=depth + 1)
            for item in value
        )
    return True


def snapshot_local_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """Privately copy a frozen schema into JSON containers used by jsonschema.

    Frozen contract schemas contain tuples and mapping proxies. Meta-schema
    validation expects actual JSON arrays/objects; thawing only a private copy
    keeps the verified source immutable and makes the checker portable.
    """

    def clone(value: object, depth: int) -> object:
        if depth > 128:
            raise ExternalClosurePrimitiveError("external_closure_schema_mapping_invalid")
        if isinstance(value, Mapping):
            if any(type(key) is not str for key in value):
                raise ExternalClosurePrimitiveError("external_closure_schema_mapping_invalid")
            return {key: clone(item, depth + 1) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clone(item, depth + 1) for item in value]
        if value is None or type(value) in {str, bool, int}:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise ExternalClosurePrimitiveError("external_closure_schema_mapping_invalid")

    if not isinstance(schema, Mapping):
        raise ExternalClosurePrimitiveError("external_closure_schema_mapping_invalid")
    result = clone(schema, 0)
    if not isinstance(result, dict) or not only_local_schema_references(result):
        raise ExternalClosurePrimitiveError("external_closure_schema_mapping_invalid")
    return result


__all__ = [
    "LOCAL_ONLY_SCHEMA_REGISTRY",
    "only_local_schema_references",
    "snapshot_local_schema",
]
