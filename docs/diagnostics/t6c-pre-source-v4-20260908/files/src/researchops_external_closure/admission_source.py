"""Pure, complete-source byte equality; historical Git coverage is a separate wrapper."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .admission_contract import FrozenAdmissionLinkContract
from .errors import ExternalClosurePrimitiveError
from .execution_components import _path_inventory
from .primitives import canonical_json_bytes


def _fail(code: str):
    raise ExternalClosurePrimitiveError("admission_source_" + code) from None


def select_source_paths(contract: FrozenAdmissionLinkContract, complete_tree_paths: tuple[str, ...]) -> tuple[str, ...]:
    if type(contract) is not FrozenAdmissionLinkContract or type(complete_tree_paths) is not tuple:
        _fail("input_invalid")
    document = contract.document()
    paths = _path_inventory(complete_tree_paths)
    required = set(document["source_identity"]["mandatory_paths"])
    if not required.issubset(paths):
        _fail("required_path_missing")
    selected = tuple(sorted({path for path in paths if path.startswith("src/") and path.endswith(".py")} | required))
    if len(selected) > document["limits"]["source_selected_paths"]:
        _fail("inventory_limit")
    return selected


@dataclass(frozen=True, slots=True)
class SourceEqualityResult:
    source_byte_inventory_sha256: str
    dependency_lock_sha256: str
    pyproject_sha256: str
    source_file_count: int
    # The pure layer cannot attest who supplied the complete tree inventory.
    git_completeness_verified: bool = False
    installed_environment_verified: bool = False
    first_live_evidence_verified: bool = False
    runtime_authority_granted: bool = False


def compare_source_bytes(
    contract: FrozenAdmissionLinkContract, *, first_live_files: dict[str, bytes],
    first_live_tree_paths: tuple[str, ...], campaign_files: dict[str, bytes],
    campaign_tree_paths: tuple[str, ...],
) -> SourceEqualityResult:
    if type(first_live_files) is not dict or type(campaign_files) is not dict:
        _fail("input_invalid")
    left = select_source_paths(contract, first_live_tree_paths)
    right = select_source_paths(contract, campaign_tree_paths)
    if left != right or set(first_live_files) != set(left) or set(campaign_files) != set(right):
        _fail("inventory_mismatch")
    limits = contract.document()["limits"]
    total = 0
    for path in left:
        first = first_live_files[path]
        second = campaign_files[path]
        if type(first) is not bytes or type(second) is not bytes:
            _fail("bytes_required")
        if len(first) > limits["source_file_bytes"] or len(second) > limits["source_file_bytes"]:
            _fail("byte_limit")
        total += len(first)
        if total > limits["source_total_bytes"]:
            _fail("byte_limit")
        if first != second:
            _fail("byte_mismatch")
    source_paths = [path for path in left if path.startswith("src/") and path.endswith(".py")]
    inventory = [{"path": path, "bytes": len(first_live_files[path]),
                  "sha256": hashlib.sha256(first_live_files[path]).hexdigest()} for path in source_paths]
    domain = contract.document()["byte_protocol"]["domain_values"]["source_inventory"]
    return SourceEqualityResult(
        hashlib.sha256(domain.encode() + b"\0" + canonical_json_bytes(inventory)).hexdigest(),
        hashlib.sha256(first_live_files["requirements.lock"]).hexdigest(),
        hashlib.sha256(first_live_files["pyproject.toml"]).hexdigest(), len(source_paths),
    )


__all__ = ["SourceEqualityResult", "select_source_paths", "compare_source_bytes"]
