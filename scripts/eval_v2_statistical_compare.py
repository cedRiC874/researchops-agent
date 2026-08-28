from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scripts.eval_v2_reference_projection import build_reference_projection


MAX_INPUT_BYTES = 2_000_000
PACKAGE_ROOT = Path("evals/v2/external_review_pre_results_v2")
STAT_ROOT = PACKAGE_ROOT / "statistical_crosscheck"


class ComparisonError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject_constant(value: str) -> Any:
    raise ComparisonError("comparison_json_non_finite")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ComparisonError("comparison_json_duplicate_key")
        result[key] = value
    return result


def _load_json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size > MAX_INPUT_BYTES:
            raise ComparisonError(code)
        value = json.loads(
            resolved.read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except ComparisonError:
        raise
    except Exception as exc:
        raise ComparisonError(code) from exc
    if not isinstance(value, dict):
        raise ComparisonError(code)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_schema(document: dict[str, Any], schema: dict[str, Any], *, code: str) -> None:
    if any(Draft202012Validator(schema).iter_errors(document)):
        raise ComparisonError(code)


def _canonical_exact(value: Any) -> str:
    if isinstance(value, float):
        raise ComparisonError("comparison_exact_value_invalid")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ComparisonError("comparison_exact_value_invalid") from exc


def _canonical_decimal(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparisonError("comparison_numeric_value_invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ComparisonError("comparison_numeric_value_invalid")
    return format(number, ".16e")


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ComparisonError("comparison_reference_value_invalid") from exc
    if not parsed.is_finite():
        raise ComparisonError("comparison_reference_value_invalid")
    return parsed


def _value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _discrepancy_id(field_path: str, external: str, reference: str) -> str:
    digest = hashlib.sha256(
        ("researchops-stat-xcheck-discrepancy-v1\n" + field_path + "\n" + external + "\n" + reference).encode(
            "utf-8"
        )
    ).hexdigest()
    return "RXC-DISC-" + digest[:16].upper()


def _field_rows(universe: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for anchor in universe.get("anchors", []):
        anchor_id = anchor.get("anchor_id")
        fields = anchor.get("fields")
        if not isinstance(anchor_id, str):
            raise ComparisonError("comparison_field_universe_invalid")
        if isinstance(fields, list):
            comparison_mode = anchor.get("comparison_mode")
            comparison_class = anchor.get("comparison_class")
            if not isinstance(comparison_mode, str) or not isinstance(comparison_class, str):
                raise ComparisonError("comparison_field_universe_invalid")
            rows.extend((f"{anchor_id}.{field}", comparison_mode, comparison_class) for field in fields)
        elif isinstance(fields, dict):
            for field, comparison_class in fields.items():
                if comparison_class == "exact_numeric":
                    comparison_mode = "exact"
                else:
                    comparison_mode = "numeric"
                rows.append((f"{anchor_id}.{field}", comparison_mode, comparison_class))
        else:
            raise ComparisonError("comparison_field_universe_invalid")
    if len(rows) != 75 or len({row[0] for row in rows}) != 75:
        raise ComparisonError("comparison_field_universe_invalid")
    return rows


def _flatten_external(result: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    anchors = result.get("anchor_results")
    if not isinstance(anchors, list):
        raise ComparisonError("comparison_external_result_invalid")
    for anchor in anchors:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("anchor_id"), str):
            raise ComparisonError("comparison_external_result_invalid")
        values = anchor.get("values")
        if not isinstance(values, dict):
            raise ComparisonError("comparison_external_result_invalid")
        for field, value in values.items():
            key = f'{anchor["anchor_id"]}.{field}'
            if key in flattened:
                raise ComparisonError("comparison_external_result_invalid")
            flattened[key] = value
    return flattened


def _reference_values(reference: dict[str, Any]) -> dict[str, str]:
    values = reference.get("values")
    if not isinstance(values, list):
        raise ComparisonError("comparison_reference_projection_invalid")
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ComparisonError("comparison_reference_projection_invalid")
        field_path = item.get("field_path")
        canonical = item.get("canonical_value")
        if not isinstance(field_path, str) or not isinstance(canonical, str) or field_path in result:
            raise ComparisonError("comparison_reference_projection_invalid")
        if item.get("value_sha256") != _value_sha256(canonical):
            raise ComparisonError("comparison_reference_projection_invalid")
        result[field_path] = canonical
    return result


def _safe_output_path(project_root: Path, output: Path) -> Path:
    root = project_root.resolve()
    target = output.resolve(strict=False)
    if target.is_relative_to(root) or target.exists() or target.parent.is_symlink() or not target.parent.is_dir():
        raise ComparisonError("comparison_output_path_invalid")
    return target


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    handle: int | None = None
    temp_name: str | None = None
    try:
        handle, temp_name = tempfile.mkstemp(prefix=".stat-xcheck-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(handle, "wb") as stream:
            handle = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if handle is not None:
            os.close(handle)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def compare(
    *,
    project_root: Path,
    execution_lock_path: Path,
    external_path: Path,
    reference_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    root = project_root.resolve(strict=True)
    package = _load_json(root / PACKAGE_ROOT / "package_commitments.json", code="comparison_package_invalid")
    delivery_path = root / STAT_ROOT / "detached_delivery_manifest.json"
    delivery = _load_json(delivery_path, code="comparison_delivery_invalid")
    universe_path = root / STAT_ROOT / "comparison_field_universe.json"
    universe = _load_json(universe_path, code="comparison_field_universe_invalid")
    tolerance_path = root / STAT_ROOT / "tolerance_policy.json"
    tolerance = _load_json(tolerance_path, code="comparison_tolerance_invalid")
    result_schema = _load_json(root / STAT_ROOT / "result_contract.json", code="comparison_result_schema_invalid")
    execution_lock_schema = _load_json(
        root / STAT_ROOT / "statistical_execution_lock.schema.json",
        code="comparison_execution_lock_schema_invalid",
    )
    reference_schema = _load_json(
        root / STAT_ROOT / "reference_projection.schema.json",
        code="comparison_reference_schema_invalid",
    )
    matrix_schema = _load_json(root / STAT_ROOT / "comparison_matrix.schema.json", code="comparison_matrix_schema_invalid")

    execution_lock = _load_json(execution_lock_path, code="comparison_execution_lock_invalid")
    external = _load_json(external_path, code="comparison_external_result_invalid")
    reference = _load_json(reference_path, code="comparison_reference_projection_invalid")
    _validate_schema(execution_lock, execution_lock_schema, code="comparison_execution_lock_invalid")
    _validate_schema(external, result_schema, code="comparison_external_result_invalid")
    _validate_schema(reference, reference_schema, code="comparison_reference_projection_invalid")

    package_commitment = package["package_commitment_sha256"]
    delivery_commitment = delivery["role_delivery_commitment_sha256"]
    delivery_sha = _sha256(delivery_path)
    universe_sha = _sha256(universe_path)
    tolerance_sha = _sha256(tolerance_path)

    for document, code in (
        (external, "comparison_external_binding_mismatch"),
        (reference, "comparison_reference_binding_mismatch"),
    ):
        if document.get("public_package_commitment_sha256") != package_commitment:
            raise ComparisonError(code)
        if document.get("statistical_delivery_commitment_sha256") != delivery_commitment:
            raise ComparisonError(code)
        if document.get("field_universe_sha256") != universe_sha:
            raise ComparisonError(code)
    if external.get("detached_delivery_manifest_sha256") != delivery_sha:
        raise ComparisonError("comparison_external_binding_mismatch")
    if external.get("tolerance_policy_sha256") != tolerance_sha:
        raise ComparisonError("comparison_external_binding_mismatch")
    if execution_lock.get("status") != "completed":
        raise ComparisonError("comparison_execution_lock_not_completed")
    if execution_lock.get("public_package_commitment_sha256") != package_commitment:
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if execution_lock.get("statistical_delivery_commitment_sha256") != delivery_commitment:
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if execution_lock.get("detached_delivery_manifest_sha256") != delivery_sha:
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if execution_lock.get("anchor_spec_sha256") != _sha256(root / STAT_ROOT / "anchor_spec.json"):
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if execution_lock.get("tolerance_policy_sha256") != tolerance_sha:
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if execution_lock.get("result_contract_sha256") != _sha256(root / STAT_ROOT / "result_contract.json"):
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if execution_lock.get("field_universe_sha256") != universe_sha:
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    external_resolved = external_path.resolve(strict=True)
    locked_result = execution_lock.get("external_result")
    if not isinstance(locked_result, dict):
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    if locked_result.get("sha256") != _sha256(external_resolved) or locked_result.get("bytes") != external_resolved.stat().st_size:
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    governance_commitment = execution_lock.get("pre_governance_commitment_sha256")
    if external.get("pre_governance_commitment_sha256") != governance_commitment or reference.get(
        "pre_governance_commitment_sha256"
    ) != governance_commitment:
        raise ComparisonError("comparison_governance_binding_mismatch")

    expected_reference = build_reference_projection(
        project_root=root,
        package_commitment=package_commitment,
        delivery_commitment=delivery_commitment,
        pre_governance_commitment=governance_commitment,
    )
    if reference != expected_reference:
        raise ComparisonError("comparison_reference_projection_mismatch")

    field_rows = _field_rows(universe)
    external_values = _flatten_external(external)
    reference_values = _reference_values(reference)
    runtime_target = execution_lock.get("runtime_target")
    if not isinstance(runtime_target, dict):
        raise ComparisonError("comparison_execution_lock_binding_mismatch")
    external_runtime = next(
        (
            entry.get("values")
            for entry in external.get("anchor_results", [])
            if isinstance(entry, dict) and entry.get("anchor_id") == "RXC-RUNTIME-001"
        ),
        None,
    )
    if not isinstance(external_runtime, dict) or external_runtime != runtime_target:
        raise ComparisonError("comparison_runtime_binding_mismatch")
    if not (
        execution_lock.get("engine")
        == external.get("engine")
        == runtime_target.get("engine")
        and execution_lock.get("engine_version")
        == external.get("engine_version")
        == runtime_target.get("engine_version")
        and execution_lock.get("implementation_source_sha256")
        == external.get("implementation_source_sha256")
        == runtime_target.get("implementation_source_sha256")
        and execution_lock.get("runtime_manifest_sha256")
        == external.get("runtime_manifest_sha256")
        == runtime_target.get("runtime_lock_sha256")
        and execution_lock.get("dependency_lock_sha256") == external.get("dependency_lock_sha256")
        and execution_lock.get("stdout_log_sha256") == runtime_target.get("stdout_log_sha256")
    ):
        raise ComparisonError("comparison_runtime_binding_mismatch")
    for field, value in runtime_target.items():
        reference_values[f"RXC-RUNTIME-001.{field}"] = _canonical_exact(value)
    expected_paths = [row[0] for row in field_rows]
    if set(external_values) != set(expected_paths) or set(reference_values) != set(expected_paths):
        raise ComparisonError("comparison_field_coverage_invalid")

    numeric_classes = tolerance["numeric_classes"]
    matrix_rows: list[dict[str, Any]] = []
    for field_path, mode, comparison_class in field_rows:
        if mode == "exact":
            external_canonical = _canonical_exact(external_values[field_path])
            reference_canonical = reference_values[field_path]
            status = "matched" if external_canonical == reference_canonical else "binding_failed"
            atol = rtol = absolute_error = allowed_error = None
        else:
            external_canonical = _canonical_decimal(external_values[field_path])
            reference_canonical = reference_values[field_path]
            external_decimal = _parse_decimal(external_canonical)
            reference_decimal = _parse_decimal(reference_canonical)
            policy = numeric_classes.get(comparison_class)
            if not isinstance(policy, dict):
                raise ComparisonError("comparison_tolerance_invalid")
            atol_decimal = Decimal(str(policy["atol"]))
            rtol_decimal = Decimal(str(policy["rtol"]))
            error = abs(external_decimal - reference_decimal)
            allowed = atol_decimal + rtol_decimal * max(abs(external_decimal), abs(reference_decimal))
            status = "matched" if error <= allowed else "outside_tolerance"
            atol = format(float(atol_decimal), ".16e")
            rtol = format(float(rtol_decimal), ".16e")
            absolute_error = format(float(error), ".16e")
            allowed_error = format(float(allowed), ".16e")
        discrepancy = (
            None
            if status == "matched"
            else _discrepancy_id(field_path, external_canonical, reference_canonical)
        )
        matrix_rows.append(
            {
                "field_path": field_path,
                "comparison_mode": mode,
                "comparison_class": comparison_class,
                "external_canonical_value": external_canonical,
                "reference_canonical_value": reference_canonical,
                "external_value_sha256": _value_sha256(external_canonical),
                "reference_value_sha256": _value_sha256(reference_canonical),
                "atol": atol,
                "rtol": rtol,
                "absolute_error": absolute_error,
                "allowed_error": allowed_error,
                "status": status,
                "discrepancy_id": discrepancy,
            }
        )

    counts = Counter(row["status"] for row in matrix_rows)
    discrepancy_count = len([row for row in matrix_rows if row["discrepancy_id"] is not None])
    matrix = {
        "schema_version": "1.0",
        "document_type": "statistical_crosscheck_comparison_matrix",
        "public_package_commitment_sha256": package_commitment,
        "statistical_delivery_commitment_sha256": delivery_commitment,
        "execution_lock_commitment_sha256": execution_lock["execution_lock_commitment_sha256"],
        "external_result_sha256": _sha256(external_resolved),
        "external_result_bytes": external_resolved.stat().st_size,
        "reference_projection_sha256": _sha256(reference_path.resolve(strict=True)),
        "reference_projection_commitment_sha256": reference[
            "reference_projection_commitment_sha256"
        ],
        "field_universe_sha256": universe_sha,
        "tolerance_policy_sha256": tolerance_sha,
        "rows": matrix_rows,
        "summary": {
            "field_count": 75,
            "unique_field_path_count": 75,
            "matched_count": counts["matched"],
            "outside_tolerance_count": counts["outside_tolerance"],
            "not_comparable_count": counts["not_comparable"],
            "binding_failed_count": counts["binding_failed"],
            "omitted_count": 0,
            "duplicate_count": 0,
            "unknown_count": 0,
            "discrepancy_count": discrepancy_count,
            "unresolved_count": discrepancy_count,
        },
    }
    _validate_schema(matrix, matrix_schema, code="comparison_matrix_invalid")
    target = _safe_output_path(root, output_path)
    _write_atomic(target, matrix)
    return {
        "schema_version": "eval-v2-statistical-compare/1.0",
        "status": "completed",
        "field_count": 75,
        "matched_count": counts["matched"],
        "discrepancy_count": discrepancy_count,
        "output_written": True,
        "network_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--execution-lock", required=True)
    parser.add_argument("--external-result", required=True)
    parser.add_argument("--reference-projection", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-compare", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_compare:
        print(
            json.dumps(
                {
                    "schema_version": "eval-v2-statistical-compare/1.0",
                    "status": "not_run",
                    "error_code": "comparison_confirmation_required",
                    "output_written": False,
                    "network_calls": 0,
                },
                sort_keys=True,
            )
        )
        return 4
    try:
        result = compare(
            project_root=Path(args.project_root),
            execution_lock_path=Path(args.execution_lock),
            external_path=Path(args.external_result),
            reference_path=Path(args.reference_projection),
            output_path=Path(args.output),
        )
    except ComparisonError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "eval-v2-statistical-compare/1.0",
                    "status": "failed",
                    "error_code": exc.code,
                    "output_written": False,
                    "network_calls": 0,
                },
                sort_keys=True,
            )
        )
        return 4
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
