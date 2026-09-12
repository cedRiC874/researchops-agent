from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class ReportGenerationError(ValueError):
    """Raised when aggregate evidence cannot support a structured report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StructuredEvidenceReport:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def build_structured_evidence_report(
    bundle: Mapping[str, Any],
) -> StructuredEvidenceReport:
    """Build a deterministic claim manifest and Markdown from aggregate evidence.

    The report never receives the source CSV. Every displayed statistical value is
    bound to an evidence ID and a metric path in the aggregate analysis bundle.
    """

    evidence_items = bundle.get("evidence")
    artifacts = bundle.get("artifacts")
    if not isinstance(evidence_items, list) or not isinstance(artifacts, list):
        raise ReportGenerationError(
            "report_bundle_invalid", "分析证据包缺少 evidence 或 artifacts 数组。"
        )
    by_method = {
        item.get("method_code"): item
        for item in evidence_items
        if isinstance(item, Mapping)
    }
    ancova = _require_evidence(by_method, "ancova_linear_model")
    welch = _require_evidence(by_method, "welch_t_test")
    ancova_id = _require_string(ancova, "evidence_id")
    welch_id = _require_string(welch, "evidence_id")

    ancova_contrast = _nested_mapping(ancova, "estimates", "contrast")
    welch_contrast = _nested_mapping(welch, "estimates", "contrast")
    ancova_test = _nested_mapping(ancova, "test")
    welch_test = _nested_mapping(welch, "test")
    sample_flow = _nested_mapping(ancova, "sample_flow")

    adjusted_effect = _require_number(ancova_contrast, "adjusted_mean_difference")
    adjusted_lower = _require_number(
        _nested_mapping(ancova_contrast, "confidence_interval"), "lower"
    )
    adjusted_upper = _require_number(
        _nested_mapping(ancova_contrast, "confidence_interval"), "upper"
    )
    adjusted_p = _require_number(ancova_test, "p_value")
    unadjusted_effect = _require_number(welch_contrast, "mean_difference")
    unadjusted_lower = _require_number(
        _nested_mapping(welch_contrast, "confidence_interval"), "lower"
    )
    unadjusted_upper = _require_number(
        _nested_mapping(welch_contrast, "confidence_interval"), "upper"
    )
    unadjusted_p = _require_number(welch_test, "p_value")

    source_rows = _require_int(sample_flow, "source_rows")
    included_rows = _require_int(sample_flow, "included_rows")
    excluded_rows = _require_int(sample_flow, "excluded_rows")
    direction = str(ancova_contrast.get("direction", ""))
    if direction != "treatment - control":
        raise ReportGenerationError(
            "report_contrast_direction_invalid",
            "报告仅接受显式的 treatment - control 对比方向。",
        )

    chart_payload: dict[str, Any]
    if artifacts:
        chart = artifacts[0]
        if not isinstance(chart, Mapping):
            raise ReportGenerationError("report_chart_invalid", "图表元数据必须是对象。")
        chart_evidence = chart.get("evidence_ids")
        if not isinstance(chart_evidence, list):
            raise ReportGenerationError(
                "report_chart_invalid", "图表缺少 evidence_ids。"
            )
        citations_complete = {ancova_id, welch_id}.issubset(set(chart_evidence))
        chart_payload = {
            "chart_id": chart.get("chart_id"),
            "row_level_points_present": False,
            "citations_complete": citations_complete,
        }
    else:
        chart_payload = {
            "chart_id": None,
            "row_level_points_present": False,
            "citations_complete": False,
        }

    claim_manifest = [
        {
            "claim_id": "primary_adjusted_effect",
            "evidence_id": ancova_id,
            "metric_path": "estimates.contrast.adjusted_mean_difference",
            "displayed_value": adjusted_effect,
            "direction": direction,
        },
        {
            "claim_id": "sensitivity_unadjusted_effect",
            "evidence_id": welch_id,
            "metric_path": "estimates.contrast.mean_difference",
            "displayed_value": unadjusted_effect,
            "direction": str(welch_contrast.get("direction", "")),
        },
    ]
    _validate_claim_manifest(claim_manifest, evidence_items)

    markdown = (
        "# 聚合分析报告\n\n"
        f"ANCOVA 校正后的 treatment - control 差为 {adjusted_effect:.2f} mmHg "
        f"（95% CI {adjusted_lower:.2f} 至 {adjusted_upper:.2f}，"
        f"p={adjusted_p:.4g}）[{ancova_id}]。\n\n"
        f"Welch 敏感性分析的未校正差为 {unadjusted_effect:.2f} mmHg "
        f"（95% CI {unadjusted_lower:.2f} 至 {unadjusted_upper:.2f}，"
        f"p={unadjusted_p:.4g}）[{welch_id}]。\n\n"
        f"局限性：{source_rows} 行中纳入 {included_rows} 行，排除 {excluded_rows} 行；"
        "当前实现是 available-case 分析，不得声称已完整实现 ITT。\n"
    )

    payload = {
        "schema_version": "1.0",
        "claims": {
            "primary": {
                "direction": "treatment_lower" if adjusted_effect < 0 else "treatment_higher",
                "statistically_significant_at_0_05": adjusted_p < 0.05,
                "citation_present": True,
                "estimate": adjusted_effect,
                "evidence_id": ancova_id,
            },
            "sensitivity": {
                "direction": "treatment_lower" if unadjusted_effect < 0 else "treatment_higher",
                "statistically_significant_at_0_05": unadjusted_p < 0.05,
                "citation_present": True,
                "estimate": unadjusted_effect,
                "evidence_id": welch_id,
            },
        },
        "limitations": {
            "outcome_missing": excluded_rows > 0,
            "realized_population": "available_case",
            "full_itt_claimed": False,
            "source_rows": source_rows,
            "included_rows": included_rows,
            "excluded_rows": excluded_rows,
        },
        "chart": chart_payload,
        "guardrails": {
            "causal_overclaim": False,
            "raw_identifier_exposed": False,
            "uncertainty_reported": True,
            "sensitivity_result_reported": True,
        },
        "claim_manifest": claim_manifest,
        "markdown": markdown,
    }
    return StructuredEvidenceReport(payload)


def _validate_claim_manifest(
    claims: list[dict[str, Any]], evidence_items: list[Any]
) -> None:
    by_id = {
        item.get("evidence_id"): item
        for item in evidence_items
        if isinstance(item, Mapping)
    }
    for claim in claims:
        evidence_id = claim["evidence_id"]
        evidence = by_id.get(evidence_id)
        if not isinstance(evidence, Mapping):
            raise ReportGenerationError(
                "report_evidence_reference_invalid",
                f"报告引用了未知证据 ID：{evidence_id}",
            )
        current: Any = evidence
        for segment in str(claim["metric_path"]).split("."):
            if not isinstance(current, Mapping) or segment not in current:
                raise ReportGenerationError(
                    "report_metric_path_invalid",
                    f"证据指标路径不存在：{claim['metric_path']}",
                )
            current = current[segment]
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            raise ReportGenerationError(
                "report_metric_value_invalid", "报告引用的指标不是数值。"
            )
        if float(current) != float(claim["displayed_value"]):
            raise ReportGenerationError(
                "report_metric_value_mismatch", "报告显示值与证据值不一致。"
            )


def _require_evidence(
    by_method: Mapping[str, Any], method_code: str
) -> Mapping[str, Any]:
    value = by_method.get(method_code)
    if not isinstance(value, Mapping):
        raise ReportGenerationError(
            "report_required_evidence_missing", f"缺少方法证据：{method_code}"
        )
    return value


def _nested_mapping(value: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = value
    for segment in path:
        if not isinstance(current, Mapping) or not isinstance(
            current.get(segment), Mapping
        ):
            raise ReportGenerationError(
                "report_bundle_invalid", "分析证据包缺少结构化统计字段。"
            )
        current = current[segment]
    return current


def _require_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ReportGenerationError("report_bundle_invalid", f"字段 {key} 必须是字符串。")
    return item


def _require_number(value: Mapping[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ReportGenerationError("report_bundle_invalid", f"字段 {key} 必须是数值。")
    return float(item)


def _require_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ReportGenerationError("report_bundle_invalid", f"字段 {key} 必须是整数。")
    return item
