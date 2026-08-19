from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    pandas_dtype: str
    semantic_type: str
    non_null_count: int
    null_count: int
    missing_rate: float
    unique_count: int
    unique_rate: float
    sample_values: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class QualityWarning:
    code: str
    severity: str
    message: str
    column: str | None = None


@dataclass(frozen=True)
class MissingPattern:
    missing_columns: list[str]
    row_count: int
    row_rate: float


@dataclass(frozen=True)
class DatasetProfile:
    source_name: str
    file_size_bytes: int
    sha256: str
    row_count: int
    column_count: int
    duplicate_row_count: int
    rows_with_missing: int
    complete_row_count: int
    columns: list[ColumnProfile]
    missing_patterns: list[MissingPattern]
    warnings: list[QualityWarning]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchDesign:
    """Explicit study-design facts that must not be guessed from a CSV."""

    question: str
    objective: str
    outcome: str
    outcome_type: str
    predictor: str
    predictor_type: str
    group_count: int | None = None
    paired: bool = False
    repeated_measures: bool = False
    covariates: tuple[str, ...] = ()
    normality: str = "unknown"
    expected_cell_count: str = "unknown"
    overdispersion: str = "unknown"
    subject_id: str | None = None
    time_variable: str | None = None
    analysis_population: str = "available_case"
    randomized: bool | None = None
    covariate_timing: dict[str, str] = field(default_factory=dict)
    reference_level: str | int | float | bool | None = None
    contrast_level: str | int | float | bool | None = None
    confidence_level: float = 0.95

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResearchDesign":
        allowed = {
            "question",
            "objective",
            "outcome",
            "outcome_type",
            "predictor",
            "predictor_type",
            "group_count",
            "paired",
            "repeated_measures",
            "covariates",
            "normality",
            "expected_cell_count",
            "overdispersion",
            "subject_id",
            "time_variable",
            "analysis_population",
            "randomized",
            "covariate_timing",
            "reference_level",
            "contrast_level",
            "confidence_level",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"研究设计包含未知字段：{', '.join(unknown)}")

        required = {
            "question",
            "objective",
            "outcome",
            "outcome_type",
            "predictor",
            "predictor_type",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"研究设计缺少字段：{', '.join(missing)}")

        normalized = dict(payload)
        covariates = normalized.get("covariates", [])
        if not isinstance(covariates, list) or not all(
            isinstance(item, str) for item in covariates
        ):
            raise ValueError("covariates 必须是字符串数组。")
        normalized["covariates"] = tuple(covariates)
        covariate_timing = normalized.get("covariate_timing", {})
        if not isinstance(covariate_timing, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in covariate_timing.items()
        ):
            raise ValueError("covariate_timing 必须是字符串到字符串的 JSON 对象。")
        normalized["covariate_timing"] = dict(covariate_timing)
        return cls(**normalized)


@dataclass(frozen=True)
class MethodChoice:
    code: str
    label: str
    purpose: str


@dataclass(frozen=True)
class MethodRecommendation:
    status: str
    rule_version: str
    dataset_sha256: str
    question: str
    primary_method: MethodChoice
    sensitivity_methods: list[MethodChoice]
    rationale: list[str]
    assumptions_to_check: list[str]
    required_diagnostics: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SampleFlow:
    source_rows: int
    included_rows: int
    excluded_rows: int
    required_columns: list[str]
    missing_by_column: dict[str, int]
    by_group: dict[str, dict[str, int]]


@dataclass(frozen=True)
class StatisticalEvidence:
    schema_version: str
    evidence_id: str
    role: str
    tool_name: str
    tool_version: str
    method_code: str
    dataset_sha256: str
    input_spec: dict[str, Any]
    sample_flow: SampleFlow
    estimates: dict[str, Any]
    test: dict[str, Any]
    diagnostics: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChartArtifact:
    schema_version: str
    chart_id: str
    file_name: str
    mime_type: str
    sha256: str
    width_px: int
    height_px: int
    byte_size: int
    plot_spec_sha256: str
    evidence_ids: list[str]
    alt_text: str


@dataclass(frozen=True)
class AnalysisBundle:
    schema_version: str
    run_id: str
    status: str
    created_at_utc: str
    question: str
    dataset: dict[str, Any]
    design: dict[str, Any]
    recommendation: dict[str, Any]
    evidence: list[StatisticalEvidence]
    artifacts: list[ChartArtifact]
    warnings: list[str]
    runtime: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
