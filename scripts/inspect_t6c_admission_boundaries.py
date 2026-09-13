"""Read-only mapping/AST diagnostic; not a verifier or admission authority.

The counterfactual exists only in memory and is rejected by the v2 loader.
No first-live code is executed, no registry is written, and no evidence of a
Provider response is produced. --write creates only an exclusive diagnostic.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from researchops_completion_telemetry.surface_mapping import (
    SurfaceMappingError,
    _offline_mapping_projection_from_documents,
    _selection_mapping_from_documents,
)


RECEIPT = ROOT / "docs/diagnostics/t6c-admission-boundaries-v1.json"
FIRST_LIVE = "src/researchops/deepseek_completion_first_live_validation.py"
SURFACE = "src/researchops_completion_telemetry/surface_mapping.py"
REGISTRY = "evals/provider_completion_telemetry_v2/provider_completion_surface_registry_v2.json"
PREDECESSOR = "evals/provider_completion_telemetry_v1/provider_completion_mapping_v1.json"
KEY = ("deepseek", "responses", "openai_compatible_responses")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def changed_paths(left: object, right: object, prefix: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in left or key not in right:
                result.append(path)
            else:
                result.extend(changed_paths(left[key], right[key], path))
        return result
    return [] if type(left) is type(right) and left == right else [prefix]


def inspect_boundaries(*, historical_first_live: bool = False) -> dict:
    # Only fixed, public source/contract inputs are read. In particular, never
    # import the first-live module or call its artifact verifier/factory.
    paths = (FIRST_LIVE, SURFACE, REGISTRY, PREDECESSOR)
    raw = {path: (ROOT / path).read_bytes() for path in paths if not historical_first_live or path != FIRST_LIVE}
    if historical_first_live:
        from researchops_external_closure.git_objects import read_git_object_snapshot
        snapshot = read_git_object_snapshot(ROOT, "5f6f9cde2f5e7092ddfbd20bed63c3baad0ea1ab", (FIRST_LIVE,),
            expected_tree_oid="30ecfd86ac00aecd8b67305a7c6ed2af88eeee10")
        raw[FIRST_LIVE] = snapshot.blobs[0].payload
    registry = json.loads(raw[REGISTRY])
    predecessor = json.loads(raw[PREDECESSOR])
    base = _offline_mapping_projection_from_documents(registry, predecessor, key=KEY)
    counterfactual = copy.deepcopy(registry)
    entry = next(item for item in counterfactual["entries"] if tuple(
        item[name] for name in ("provider_id", "api_surface", "transport_id")
    ) == KEY)
    if entry["runtime_binding_allowed"] is not False:
        raise ValueError("v2_baseline_not_offline")
    entry["runtime_binding_allowed"] = True
    # This private pure projector returns a dictionary, not a verified registry
    # or capability. The separately invoked validator MUST reject that input.
    projected = _selection_mapping_from_documents(
        predecessor, registry["predecessor_mapping"], key=KEY,
        entry=entry, provider_mapping=entry["provider_mapping"],
    )
    try:
        _offline_mapping_projection_from_documents(counterfactual, predecessor, key=KEY)
    except SurfaceMappingError as error:
        counterfactual_error = error.code
    else:
        raise ValueError("v2_counterfactual_was_not_rejected")

    syntax = ast.parse(raw[FIRST_LIVE].decode("utf-8"))
    functions = {node.name: node for node in syntax.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    edges = []
    for caller, callee in (
        ("verify_deepseek_first_live_artifacts", "_verify_deepseek_first_live_artifacts_impl"),
        ("_verify_deepseek_first_live_artifacts_impl", "_validate_persisted_execution_identity"),
        ("_validate_persisted_execution_identity", "_validation_runtime_binding"),
        ("_validation_runtime_binding", "_create_first_live_validation_binding"),
    ):
        lines = sorted(node.lineno for node in ast.walk(functions[caller])
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id == callee)
        if not lines:
            raise ValueError("expected_static_call_edge_missing")
        edges.append({"caller": caller, "callee": callee, "call_lines": lines})
    moving_ref_lines = sorted(node.lineno for node in ast.walk(
        functions["_validate_persisted_execution_identity"]
    ) if isinstance(node, ast.Constant) and node.value == "refs/remotes/origin/main")
    if not moving_ref_lines:
        raise ValueError("expected_moving_ref_missing")

    differences = changed_paths(base, projected)
    if differences != ["surface_selection.runtime_binding_allowed"]:
        raise ValueError("counterfactual_has_extra_changes")
    return {
        "schema_version": "t6c-admission-boundary-diagnostic/1.0",
        "scope": "offline_mapping_hash_and_static_call_graph_only",
        "inputs": [{"path": path, "bytes": len(raw[path]), "sha256": digest(raw[path])}
                   for path in paths],
        "mapping_counterfactual": {
            "actual_selected_mapping_sha256": digest(canonical(base)),
            "counterfactual_selected_mapping_sha256": digest(canonical(projected)),
            "changed_projection_paths": differences,
            "provider_mapping_document_unchanged": base["providers"] == projected["providers"],
            "full_selected_mapping_hash_changed": canonical(base) != canonical(projected),
            "counterfactual_is_valid_registry": False,
            "counterfactual_rejection_code": counterfactual_error,
        },
        "first_live_static_call_graph": {
            "file": FIRST_LIVE,
            "edges": edges,
            "mutable_ancestry_ref": "refs/remotes/origin/main",
            "mutable_ancestry_ref_lines": moving_ref_lines,
            "call_graph_is_dynamic_execution_evidence": False,
        },
        "registry_files_modified": False,
        "first_live_verifier_executed": False,
        "runtime_authority_constructed": False,
        "registry_promotion_performed": False,
        "provider_calls": 0,
        "key_loads": 0,
        "closure_claim_allowed": False,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--receipt", type=Path, default=RECEIPT)
    parser.add_argument("--historical-first-live", action="store_true",
                        help="Replay the original first-live source from fixed Git; other inputs must still match their recorded bytes.")
    options = parser.parse_args(arguments)
    try:
        result = inspect_boundaries(historical_first_live=options.historical_first_live)
        raw = canonical(result) + b"\n"
        if options.write:
            options.receipt.parent.mkdir(parents=True, exist_ok=True)
            with options.receipt.open("xb") as stream:
                stream.write(raw)
        elif options.verify:
            if options.receipt.read_bytes() != raw:
                raise ValueError("diagnostic_receipt_drift")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception:
        print('{"status":"failed","error_code":"admission_boundary_diagnostic_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
