from __future__ import annotations

from pathlib import Path
from typing import Any

from .data_quality import profile_csv
from .eval_v2_contracts import EvalV2ContractError
from .eval_v2_dataset_prep import EvalV2LogicalDatasetRegistry


INSPECT_BACKEND_VERSION = "1.0"
_FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "csv",
        "file_name",
        "file_path",
        "path",
        "prepared_sha256",
        "raw_data",
        "raw_rows",
        "records",
        "sample_values",
        "sha256",
        "source_name",
        "subject_key",
    }
)


class EvalV2InspectDatasetBackend:
    """Aggregate-only inspection backend for prepared Eval v2 datasets."""

    def __init__(self, registry: EvalV2LogicalDatasetRegistry) -> None:
        self._registry = registry

    @classmethod
    def from_registry_path(
        cls, registry_path: str | Path
    ) -> "EvalV2InspectDatasetBackend":
        return cls(EvalV2LogicalDatasetRegistry.load(registry_path))

    def inspect_dataset(self, dataset_id: str) -> dict[str, Any]:
        handle = self._registry.resolve(dataset_id)
        profile = profile_csv(handle.path)
        if (
            profile.sha256 != handle.prepared_sha256
            or profile.row_count != handle.row_count
            or profile.column_count != handle.column_count
        ):
            raise EvalV2ContractError(
                "eval_v2_profile_scope_mismatch",
                "聚合 profile 与 registry 绑定的准备产物不一致。",
            )

        identifier_columns = {
            warning.column
            for warning in profile.warnings
            if warning.code == "possible_identifier" and warning.column is not None
        }
        result = {
            "schema_version": "2.0",
            "tool_name": "inspect_dataset",
            "tool_version": INSPECT_BACKEND_VERSION,
            "dataset": handle.public_metadata(),
            "profile": {
                "row_count": profile.row_count,
                "column_count": profile.column_count,
                "duplicate_row_count": profile.duplicate_row_count,
                "rows_with_missing": profile.rows_with_missing,
                "complete_row_count": profile.complete_row_count,
                "columns": [
                    {
                        "name": column.name,
                        "semantic_type": column.semantic_type,
                        "non_null_count": column.non_null_count,
                        "null_count": column.null_count,
                        "missing_rate": column.missing_rate,
                        "unique_count": column.unique_count,
                        "unique_rate": column.unique_rate,
                        "possible_identifier": column.name in identifier_columns,
                    }
                    for column in profile.columns
                ],
                "missing_patterns": [
                    {
                        "missing_columns": list(pattern.missing_columns),
                        "row_count": pattern.row_count,
                        "row_rate": pattern.row_rate,
                    }
                    for pattern in profile.missing_patterns
                ],
                "warnings": [
                    {
                        "code": warning.code,
                        "severity": warning.severity,
                        "column": warning.column,
                    }
                    for warning in profile.warnings
                ],
            },
            "privacy": {
                "row_level_values_exposed": False,
                "sample_values_exposed": False,
                "filesystem_path_exposed": False,
                "model_access": "aggregate_tools_only",
            },
        }
        _assert_aggregate_only(result)
        return result

    def public_catalog(self) -> list[dict[str, Any]]:
        catalog = self._registry.public_catalog()
        _assert_aggregate_only(catalog)
        return catalog


def _assert_aggregate_only(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = sorted(set(value).intersection(_FORBIDDEN_OUTPUT_KEYS))
        if forbidden:
            raise EvalV2ContractError(
                "eval_v2_inspect_projection_unsafe",
                "inspect projection 包含禁止字段。",
            )
        for item in value.values():
            _assert_aggregate_only(item)
    elif isinstance(value, list):
        for item in value:
            _assert_aggregate_only(item)
