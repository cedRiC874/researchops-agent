from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd
import scipy
from scipy import special, stats
import statsmodels
import statsmodels.api as sm
from statsmodels.stats.diagnostic import het_breuschpagan
import matplotlib

from .contracts import DatasetProfile, ResearchDesign, SampleFlow, StatisticalEvidence


TOOL_VERSION = "1.0.0"


class AnalysisExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


def run_welch_t_test(
    frame: pd.DataFrame,
    profile: DatasetProfile,
    design: ResearchDesign,
    *,
    role: str = "sensitivity",
) -> StatisticalEvidence:
    _require_two_group_continuous_design(design)
    group = design.predictor
    outcome = design.outcome
    reference = design.reference_level
    contrast = design.contrast_level
    assert reference is not None and contrast is not None

    prepared = _prepare_numeric_analysis_frame(
        frame,
        required_columns=[group, outcome],
        numeric_columns=[outcome],
        group_column=group,
        allowed_levels=[reference, contrast],
    )
    complete = prepared["complete"]
    sample_flow = _sample_flow(
        frame,
        complete,
        required_columns=[group, outcome],
        group_column=group,
        levels=[reference, contrast],
    )

    reference_values = complete.loc[complete[group] == reference, outcome].to_numpy(dtype=float)
    contrast_values = complete.loc[complete[group] == contrast, outcome].to_numpy(dtype=float)
    _validate_two_numeric_groups(reference_values, contrast_values, outcome)

    n_reference = len(reference_values)
    n_contrast = len(contrast_values)
    mean_reference = float(np.mean(reference_values))
    mean_contrast = float(np.mean(contrast_values))
    sd_reference = float(np.std(reference_values, ddof=1))
    sd_contrast = float(np.std(contrast_values, ddof=1))
    difference = mean_contrast - mean_reference
    variance_term_reference = sd_reference**2 / n_reference
    variance_term_contrast = sd_contrast**2 / n_contrast
    standard_error = math.sqrt(variance_term_reference + variance_term_contrast)
    if standard_error == 0:
        raise AnalysisExecutionError(
            "zero_standard_error",
            "两组结局均无变异，无法计算 Welch t 统计量。",
        )

    denominator = (
        variance_term_reference**2 / (n_reference - 1)
        + variance_term_contrast**2 / (n_contrast - 1)
    )
    degrees_of_freedom = (variance_term_reference + variance_term_contrast) ** 2 / denominator
    statistic = difference / standard_error
    p_value = float(2 * stats.t.sf(abs(statistic), degrees_of_freedom))
    alpha = 1 - float(design.confidence_level)
    critical_value = float(stats.t.ppf(1 - alpha / 2, degrees_of_freedom))
    ci_lower = difference - critical_value * standard_error
    ci_upper = difference + critical_value * standard_error

    reference_mean_ci = _mean_confidence_interval(
        mean_reference,
        sd_reference,
        n_reference,
        float(design.confidence_level),
    )
    contrast_mean_ci = _mean_confidence_interval(
        mean_contrast,
        sd_contrast,
        n_contrast,
        float(design.confidence_level),
    )

    pooled_df = n_reference + n_contrast - 2
    pooled_sd = math.sqrt(
        ((n_reference - 1) * sd_reference**2 + (n_contrast - 1) * sd_contrast**2)
        / pooled_df
    )
    cohen_d = difference / pooled_sd if pooled_sd else math.nan
    correction = math.exp(
        special.gammaln(pooled_df / 2)
        - 0.5 * math.log(pooled_df / 2)
        - special.gammaln((pooled_df - 1) / 2)
    )
    hedges_g = correction * cohen_d

    input_spec = {
        "outcome": outcome,
        "group": group,
        "reference_level": _json_scalar(reference),
        "contrast_level": _json_scalar(contrast),
        "contrast_direction": f"{contrast} - {reference}",
        "confidence_level": float(design.confidence_level),
        "alternative": "two-sided",
        "missing_data_policy": "available_case_for_required_columns",
        "outlier_policy": "no_automatic_removal",
    }
    estimates = {
        "reference_group": {
            "level": _json_scalar(reference),
            "n": n_reference,
            "mean": _finite(mean_reference),
            "standard_deviation": _finite(sd_reference),
            "mean_confidence_interval": reference_mean_ci,
        },
        "contrast_group": {
            "level": _json_scalar(contrast),
            "n": n_contrast,
            "mean": _finite(mean_contrast),
            "standard_deviation": _finite(sd_contrast),
            "mean_confidence_interval": contrast_mean_ci,
        },
        "contrast": {
            "direction": f"{contrast} - {reference}",
            "mean_difference": _finite(difference),
            "standard_error": _finite(standard_error),
            "confidence_level": float(design.confidence_level),
            "confidence_interval": {
                "lower": _finite(ci_lower),
                "upper": _finite(ci_upper),
            },
            "cohen_d_pooled_sd": _finite(cohen_d),
            "hedges_g_pooled_sd": _finite(hedges_g),
        },
    }
    test = {
        "statistic_name": "t",
        "statistic": _finite(statistic),
        "degrees_of_freedom": _finite(degrees_of_freedom),
        "p_value": _finite(p_value),
        "alternative": "two-sided",
        "equal_variance_assumed": False,
    }
    diagnostics = {
        "group_variance_ratio_max_over_min": _finite(
            max(sd_reference**2, sd_contrast**2) / min(sd_reference**2, sd_contrast**2)
            if min(sd_reference, sd_contrast) > 0
            else math.inf
        ),
        "automatic_normality_switch_used": False,
        "automatic_outlier_removal_used": False,
    }
    warnings = _analysis_warnings(sample_flow, design, reference, contrast)
    warnings.append(
        "Hedges g 使用合并样本标准差进行标准化，它不是 Welch 检验统计量。"
    )
    return _evidence(
        role=role,
        method_code="welch_t_test",
        tool_name="run_welch_t_test",
        dataset_sha256=profile.sha256,
        input_spec=input_spec,
        sample_flow=sample_flow,
        estimates=estimates,
        test=test,
        diagnostics=diagnostics,
        warnings=warnings,
    )


def run_ancova(
    frame: pd.DataFrame,
    profile: DatasetProfile,
    design: ResearchDesign,
    *,
    role: str = "primary",
) -> StatisticalEvidence:
    _require_two_group_continuous_design(design)
    if not design.covariates:
        raise AnalysisExecutionError(
            "covariate_required",
            "ANCOVA 至少需要一个预指定协变量。",
        )

    group = design.predictor
    outcome = design.outcome
    reference = design.reference_level
    contrast = design.contrast_level
    assert reference is not None and contrast is not None
    covariates = list(design.covariates)
    required_columns = [group, outcome, *covariates]

    prepared = _prepare_numeric_analysis_frame(
        frame,
        required_columns=required_columns,
        numeric_columns=[outcome, *covariates],
        group_column=group,
        allowed_levels=[reference, contrast],
    )
    complete = prepared["complete"]
    sample_flow = _sample_flow(
        frame,
        complete,
        required_columns=required_columns,
        group_column=group,
        levels=[reference, contrast],
    )

    group_counts = complete[group].value_counts()
    if any(int(group_counts.get(level, 0)) < 2 for level in [reference, contrast]):
        raise AnalysisExecutionError(
            "insufficient_complete_cases",
            "ANCOVA 的每个组别至少需要 2 个完整观测。",
            {str(level): int(group_counts.get(level, 0)) for level in [reference, contrast]},
        )

    y = complete[outcome].to_numpy(dtype=float)
    if not np.isfinite(y).all():
        raise AnalysisExecutionError("non_finite_values", f"结局变量 {outcome} 包含非有限值。")
    if float(np.var(y, ddof=1)) == 0:
        raise AnalysisExecutionError("constant_outcome", f"结局变量 {outcome} 没有变异。")

    treatment = (complete[group] == contrast).astype(float).to_numpy()
    centered_covariates: list[np.ndarray] = []
    center_values: dict[str, float] = {}
    for name in covariates:
        values = complete[name].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise AnalysisExecutionError("non_finite_values", f"协变量 {name} 包含非有限值。", {"column": name})
        if float(np.var(values, ddof=1)) == 0:
            raise AnalysisExecutionError("constant_covariate", f"协变量 {name} 没有变异。", {"column": name})
        center = float(np.mean(values))
        center_values[name] = center
        centered_covariates.append(values - center)

    design_matrix = np.column_stack(
        [np.ones(len(complete)), treatment, *centered_covariates]
    )
    column_names = ["intercept", f"{contrast}_vs_{reference}", *covariates]
    rank = int(np.linalg.matrix_rank(design_matrix))
    if rank < design_matrix.shape[1]:
        raise AnalysisExecutionError(
            "rank_deficient_design",
            "ANCOVA 设计矩阵不满秩，可能存在常量或完全共线协变量。",
            {"rank": rank, "columns": design_matrix.shape[1]},
        )
    if len(complete) <= rank:
        raise AnalysisExecutionError(
            "insufficient_residual_degrees_of_freedom",
            "ANCOVA 完整观测数不足以估计模型。",
            {"included_rows": len(complete), "rank": rank},
        )

    ordinary_fit = sm.OLS(y, design_matrix).fit()
    leverage = ordinary_fit.get_influence().hat_matrix_diag
    if not np.isfinite(leverage).all() or float(np.max(leverage)) >= 1 - 1e-10:
        raise AnalysisExecutionError(
            "hc3_leverage_failure",
            "存在接近 1 的极端杠杆值，HC3 协方差无法稳定估计。",
            {"max_leverage": float(np.max(leverage))},
        )
    fit = ordinary_fit.get_robustcov_results(cov_type="HC3", use_t=True)
    covariance = np.asarray(fit.cov_params(), dtype=float)
    alpha = 1 - float(design.confidence_level)
    degrees_of_freedom = float(fit.df_resid)
    critical_value = float(stats.t.ppf(1 - alpha / 2, degrees_of_freedom))

    effect = float(fit.params[1])
    effect_se = float(fit.bse[1])
    effect_t = float(fit.tvalues[1])
    effect_p = float(fit.pvalues[1])
    effect_ci = np.asarray(fit.conf_int(alpha=alpha), dtype=float)[1]

    reference_vector = np.array([1.0, 0.0, *([0.0] * len(covariates))])
    contrast_vector = np.array([1.0, 1.0, *([0.0] * len(covariates))])
    adjusted_reference = _linear_contrast(
        reference_vector,
        np.asarray(fit.params, dtype=float),
        covariance,
        critical_value,
    )
    adjusted_contrast = _linear_contrast(
        contrast_vector,
        np.asarray(fit.params, dtype=float),
        covariance,
        critical_value,
    )

    interaction_diagnostics: list[dict[str, Any]] = []
    for covariate_index, covariate in enumerate(covariates):
        interaction_column = treatment * centered_covariates[covariate_index]
        interaction_matrix = np.column_stack([design_matrix, interaction_column])
        interaction_rank = int(np.linalg.matrix_rank(interaction_matrix))
        if interaction_rank < interaction_matrix.shape[1]:
            interaction_diagnostics.append(
                {
                    "covariate": covariate,
                    "status": "not_estimable",
                    "reason": "rank_deficient_interaction_model",
                }
            )
            continue
        interaction_fit = sm.OLS(y, interaction_matrix).fit().get_robustcov_results(
            cov_type="HC3",
            use_t=True,
        )
        index = interaction_matrix.shape[1] - 1
        interaction_ci = np.asarray(interaction_fit.conf_int(alpha=alpha), dtype=float)[index]
        interaction_diagnostics.append(
            {
                "covariate": covariate,
                "status": "estimated",
                "slope_difference": _finite(interaction_fit.params[index]),
                "standard_error": _finite(interaction_fit.bse[index]),
                "degrees_of_freedom": _finite(interaction_fit.df_resid),
                "p_value": _finite(interaction_fit.pvalues[index]),
                "confidence_level": float(design.confidence_level),
                "confidence_interval": {
                    "lower": _finite(interaction_ci[0]),
                    "upper": _finite(interaction_ci[1]),
                },
                "interpretation_guardrail": "Do not treat a non-significant interaction as proof of equal slopes.",
            }
        )

    bp_lm, bp_lm_p, bp_f, bp_f_p = het_breuschpagan(ordinary_fit.resid, design_matrix)
    condition_number = float(np.linalg.cond(design_matrix))
    input_spec = {
        "outcome": outcome,
        "group": group,
        "reference_level": _json_scalar(reference),
        "contrast_level": _json_scalar(contrast),
        "contrast_direction": f"{contrast} - {reference}",
        "covariates": covariates,
        "covariate_timing": design.covariate_timing,
        "covariate_center_values": {name: _finite(value) for name, value in center_values.items()},
        "confidence_level": float(design.confidence_level),
        "covariance_estimator": "HC3",
        "use_t_distribution": True,
        "missing_data_policy": "available_case_for_required_columns",
        "outlier_policy": "no_automatic_removal",
    }
    estimates = {
        "reference_group": {
            "level": _json_scalar(reference),
            "adjusted_mean": adjusted_reference,
        },
        "contrast_group": {
            "level": _json_scalar(contrast),
            "adjusted_mean": adjusted_contrast,
        },
        "contrast": {
            "direction": f"{contrast} - {reference}",
            "adjusted_mean_difference": _finite(effect),
            "standard_error_hc3": _finite(effect_se),
            "confidence_level": float(design.confidence_level),
            "confidence_interval": {
                "lower": _finite(effect_ci[0]),
                "upper": _finite(effect_ci[1]),
            },
        },
        "coefficients": {
            name: _finite(value) for name, value in zip(column_names, fit.params)
        },
    }
    test = {
        "statistic_name": "t",
        "statistic": _finite(effect_t),
        "degrees_of_freedom": _finite(degrees_of_freedom),
        "p_value": _finite(effect_p),
        "alternative": "two-sided",
        "covariance_estimator": "HC3",
    }
    diagnostics = {
        "model_rank": rank,
        "parameter_count": int(design_matrix.shape[1]),
        "residual_degrees_of_freedom": _finite(degrees_of_freedom),
        "r_squared": _finite(ordinary_fit.rsquared),
        "adjusted_r_squared": _finite(ordinary_fit.rsquared_adj),
        "condition_number": _finite(condition_number),
        "max_leverage": _finite(np.max(leverage)),
        "residual_skewness": _finite(stats.skew(ordinary_fit.resid, bias=False)),
        "breusch_pagan": {
            "lm_statistic": _finite(bp_lm),
            "lm_p_value": _finite(bp_lm_p),
            "f_statistic": _finite(bp_f),
            "f_p_value": _finite(bp_f_p),
            "automatic_method_switch_used": False,
        },
        "slope_homogeneity": interaction_diagnostics,
        "automatic_normality_switch_used": False,
        "automatic_outlier_removal_used": False,
    }
    warnings = _analysis_warnings(sample_flow, design, reference, contrast)
    if condition_number > 1e8:
        warnings.append("设计矩阵条件数较高，系数可能对数值扰动敏感。")
    if any(
        item.get("status") == "estimated" and item.get("p_value", 1.0) < alpha
        for item in interaction_diagnostics
    ):
        warnings.append(
            "组别×协变量交互诊断提示斜率可能不同；在解释单一平均效应前需要人工审查。"
        )
    return _evidence(
        role=role,
        method_code="ancova_linear_model",
        tool_name="run_ancova",
        dataset_sha256=profile.sha256,
        input_spec=input_spec,
        sample_flow=sample_flow,
        estimates=estimates,
        test=test,
        diagnostics=diagnostics,
        warnings=warnings,
    )


def runtime_versions() -> dict[str, str]:
    return {
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scipy": scipy.__version__,
        "statsmodels": statsmodels.__version__,
        "matplotlib": matplotlib.__version__,
    }


def _require_two_group_continuous_design(design: ResearchDesign) -> None:
    if design.outcome_type != "continuous" or design.group_count != 2:
        raise AnalysisExecutionError(
            "unsupported_execution_design",
            "第三阶段执行器仅支持两组连续结局。",
        )
    if design.reference_level is None or design.contrast_level is None:
        raise AnalysisExecutionError(
            "contrast_levels_required",
            "执行前必须显式指定 reference_level 和 contrast_level。",
        )


def _prepare_numeric_analysis_frame(
    frame: pd.DataFrame,
    *,
    required_columns: list[str],
    numeric_columns: list[str],
    group_column: str,
    allowed_levels: list[Any],
) -> dict[str, pd.DataFrame]:
    missing_columns = [name for name in required_columns if name not in frame.columns]
    if missing_columns:
        raise AnalysisExecutionError(
            "column_not_found",
            "执行时数据缺少所需字段。",
            {"columns": missing_columns},
        )
    unexpected_levels = sorted(
        str(value)
        for value in frame[group_column].dropna().unique()
        if value not in allowed_levels
    )
    if unexpected_levels:
        raise AnalysisExecutionError(
            "unexpected_group_levels",
            "分组字段包含未在显式对比中声明的水平。",
            {"levels": unexpected_levels},
        )

    working = frame[required_columns].copy()
    for name in numeric_columns:
        original = working[name]
        converted = pd.to_numeric(original, errors="coerce")
        invalid_mask = original.notna() & converted.isna()
        if bool(invalid_mask.any()):
            raise AnalysisExecutionError(
                "incompatible_numeric_values",
                f"字段 {name} 包含无法转换为数值的非空值。",
                {"column": name, "invalid_count": int(invalid_mask.sum())},
            )
        finite_mask = converted.notna() & ~np.isfinite(converted.astype(float))
        if bool(finite_mask.any()):
            raise AnalysisExecutionError(
                "non_finite_values",
                f"字段 {name} 包含无穷大或非有限值。",
                {"column": name, "invalid_count": int(finite_mask.sum())},
            )
        working[name] = converted
    complete_mask = working[required_columns].notna().all(axis=1)
    complete = (
        working.loc[complete_mask]
        .sort_values(required_columns, kind="mergesort")
        .reset_index(drop=True)
    )
    return {"working": working, "complete": complete}


def _sample_flow(
    source: pd.DataFrame,
    complete: pd.DataFrame,
    *,
    required_columns: list[str],
    group_column: str,
    levels: list[Any],
) -> SampleFlow:
    by_group: dict[str, dict[str, int]] = {}
    for level in levels:
        source_mask = source[group_column] == level
        included_mask = complete[group_column] == level
        source_count = int(source_mask.sum())
        included_count = int(included_mask.sum())
        by_group[str(level)] = {
            "source_rows": source_count,
            "included_rows": included_count,
            "excluded_rows": source_count - included_count,
        }
    return SampleFlow(
        source_rows=int(len(source)),
        included_rows=int(len(complete)),
        excluded_rows=int(len(source) - len(complete)),
        required_columns=required_columns,
        missing_by_column={name: int(source[name].isna().sum()) for name in required_columns},
        by_group=by_group,
    )


def _validate_two_numeric_groups(
    reference_values: np.ndarray,
    contrast_values: np.ndarray,
    outcome: str,
) -> None:
    group_sizes = {"reference": len(reference_values), "contrast": len(contrast_values)}
    if min(group_sizes.values()) < 2:
        raise AnalysisExecutionError(
            "insufficient_complete_cases",
            "Welch t 检验每组至少需要 2 个有效观测。",
            group_sizes,
        )
    if not np.isfinite(reference_values).all() or not np.isfinite(contrast_values).all():
        raise AnalysisExecutionError("non_finite_values", f"结局变量 {outcome} 包含非有限值。")
    combined = np.concatenate([reference_values, contrast_values])
    if float(np.var(combined, ddof=1)) == 0:
        raise AnalysisExecutionError("constant_outcome", f"结局变量 {outcome} 没有变异。")


def _mean_confidence_interval(
    mean: float,
    standard_deviation: float,
    sample_size: int,
    confidence_level: float,
) -> dict[str, float]:
    critical = float(stats.t.ppf(1 - (1 - confidence_level) / 2, sample_size - 1))
    margin = critical * standard_deviation / math.sqrt(sample_size)
    return {
        "lower": _finite(mean - margin),
        "upper": _finite(mean + margin),
        "confidence_level": confidence_level,
    }


def _linear_contrast(
    vector: np.ndarray,
    coefficients: np.ndarray,
    covariance: np.ndarray,
    critical_value: float,
) -> dict[str, float]:
    estimate = float(vector @ coefficients)
    standard_error = math.sqrt(float(vector @ covariance @ vector))
    return {
        "estimate": _finite(estimate),
        "standard_error_hc3": _finite(standard_error),
        "confidence_interval": {
            "lower": _finite(estimate - critical_value * standard_error),
            "upper": _finite(estimate + critical_value * standard_error),
        },
    }


def _analysis_warnings(
    sample_flow: SampleFlow,
    design: ResearchDesign,
    reference: Any,
    contrast: Any,
) -> list[str]:
    warnings = ["未自动插补缺失值，也未自动删除异常值。"]
    if sample_flow.excluded_rows:
        warnings.append(
            f"该方法按所需字段执行可用病例分析："
            f"{sample_flow.source_rows} 行中纳入 {sample_flow.included_rows} 行，"
            f"排除 {sample_flow.excluded_rows} 行。"
        )
    if design.analysis_population == "intention_to_treat" and sample_flow.excluded_rows:
        warnings.append(
            "requested_population=intention_to_treat，但 realized_population=available_case；"
            "当前结果不得声称已完整实现 ITT。"
        )
    reference_flow = sample_flow.by_group[str(reference)]
    contrast_flow = sample_flow.by_group[str(contrast)]
    reference_rate = (
        reference_flow["excluded_rows"] / reference_flow["source_rows"]
        if reference_flow["source_rows"]
        else 0.0
    )
    contrast_rate = (
        contrast_flow["excluded_rows"] / contrast_flow["source_rows"]
        if contrast_flow["source_rows"]
        else 0.0
    )
    if abs(reference_rate - contrast_rate) >= 0.05:
        warnings.append(
            f"组间排除率相差 {abs(reference_rate - contrast_rate):.1%}"
            "，需评估差异性失访偏倚。"
        )
    return warnings


def _evidence(
    *,
    role: str,
    method_code: str,
    tool_name: str,
    dataset_sha256: str,
    input_spec: dict[str, Any],
    sample_flow: SampleFlow,
    estimates: dict[str, Any],
    test: dict[str, Any],
    diagnostics: dict[str, Any],
    warnings: list[str],
) -> StatisticalEvidence:
    evidence_payload = {
        "dataset_sha256": dataset_sha256,
        "method_code": method_code,
        "tool_name": tool_name,
        "tool_version": TOOL_VERSION,
        "input_spec": input_spec,
        "sample_flow": asdict(sample_flow),
        "estimates": estimates,
        "test": test,
        "diagnostics": diagnostics,
    }
    evidence_id = "E-" + hashlib.sha256(_canonical_json(evidence_payload)).hexdigest()[:12].upper()
    return StatisticalEvidence(
        schema_version="1.0",
        evidence_id=evidence_id,
        role=role,
        tool_name=tool_name,
        tool_version=TOOL_VERSION,
        method_code=method_code,
        dataset_sha256=dataset_sha256,
        input_spec=input_spec,
        sample_flow=sample_flow,
        estimates=estimates,
        test=test,
        diagnostics=diagnostics,
        warnings=list(dict.fromkeys(warnings)),
    )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _finite(value: Any) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise AnalysisExecutionError(
            "non_finite_result",
            "统计工具生成了非有限数值，结果已阻止。",
        )
    return converted


def _json_scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    return value
