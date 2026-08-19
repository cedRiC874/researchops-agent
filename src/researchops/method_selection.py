from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    ColumnProfile,
    DatasetProfile,
    MethodChoice,
    MethodRecommendation,
    ResearchDesign,
)


RULE_VERSION = "2026-08-15.1"
VALID_OBJECTIVES = {"group_difference", "association", "prediction"}
VALID_OUTCOME_TYPES = {"continuous", "binary", "categorical", "count"}
VALID_PREDICTOR_TYPES = {"continuous", "binary", "categorical"}
VALID_NORMALITY = {"unknown", "reasonable", "violated"}
VALID_CELL_COUNTS = {"unknown", "adequate", "sparse"}
VALID_OVERDISPERSION = {"unknown", "absent", "present"}
VALID_POPULATIONS = {"available_case", "complete_case", "intention_to_treat", "per_protocol"}
VALID_COVARIATE_TIMING = {"pre_treatment", "post_treatment", "unknown"}
NUMERIC_SEMANTIC_TYPES = {"continuous_numeric", "discrete_numeric", "binary_numeric"}


@dataclass(frozen=True)
class SelectionIssue:
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.field is not None:
            payload["field"] = self.field
        return payload


class MethodSelectionError(ValueError):
    """A safe-stop error containing questions the caller must resolve."""

    def __init__(self, issues: list[SelectionIssue]) -> None:
        self.issues = issues
        super().__init__("；".join(issue.message for issue in issues))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "needs_input",
            "error": "research_design_incomplete_or_inconsistent",
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class _Selection:
    primary: MethodChoice
    sensitivity: list[MethodChoice]
    rationale: list[str]
    assumptions: list[str]
    diagnostics: list[str]
    warnings: list[str]


class StatisticalMethodSelector:
    """Deterministic, auditable rules for selecting a statistical method family."""

    def recommend(
        self,
        profile: DatasetProfile,
        design: ResearchDesign,
    ) -> MethodRecommendation:
        columns = {column.name: column for column in profile.columns}
        issues = self._validate(profile, design, columns)
        if issues:
            raise MethodSelectionError(issues)

        if design.objective == "group_difference":
            selection = self._group_difference(design)
        elif design.objective == "association":
            selection = self._association(design)
        else:
            selection = self._prediction(design)

        self._append_data_quality_guidance(selection, design, columns)
        return MethodRecommendation(
            status="ready",
            rule_version=RULE_VERSION,
            dataset_sha256=profile.sha256,
            question=design.question,
            primary_method=selection.primary,
            sensitivity_methods=selection.sensitivity,
            rationale=selection.rationale,
            assumptions_to_check=_deduplicate(selection.assumptions),
            required_diagnostics=_deduplicate(selection.diagnostics),
            warnings=_deduplicate(selection.warnings),
        )

    def _validate(
        self,
        profile: DatasetProfile,
        design: ResearchDesign,
        columns: dict[str, ColumnProfile],
    ) -> list[SelectionIssue]:
        issues: list[SelectionIssue] = []

        def add(code: str, message: str, field: str | None = None) -> None:
            issues.append(SelectionIssue(code=code, message=message, field=field))

        if not isinstance(design.question, str) or not design.question.strip():
            add("invalid_question", "请提供非空的研究问题。", "question")
        if design.objective not in VALID_OBJECTIVES:
            add("invalid_design_value", f"objective 必须是 {sorted(VALID_OBJECTIVES)} 之一。", "objective")
        if design.outcome_type not in VALID_OUTCOME_TYPES:
            add("invalid_design_value", f"outcome_type 必须是 {sorted(VALID_OUTCOME_TYPES)} 之一。", "outcome_type")
        if design.predictor_type not in VALID_PREDICTOR_TYPES:
            add("invalid_design_value", f"predictor_type 必须是 {sorted(VALID_PREDICTOR_TYPES)} 之一。", "predictor_type")
        if design.normality not in VALID_NORMALITY:
            add("invalid_design_value", f"normality 必须是 {sorted(VALID_NORMALITY)} 之一。", "normality")
        if design.expected_cell_count not in VALID_CELL_COUNTS:
            add("invalid_design_value", f"expected_cell_count 必须是 {sorted(VALID_CELL_COUNTS)} 之一。", "expected_cell_count")
        if design.overdispersion not in VALID_OVERDISPERSION:
            add("invalid_design_value", f"overdispersion 必须是 {sorted(VALID_OVERDISPERSION)} 之一。", "overdispersion")
        if design.analysis_population not in VALID_POPULATIONS:
            add("invalid_design_value", f"analysis_population 必须是 {sorted(VALID_POPULATIONS)} 之一。", "analysis_population")
        if design.randomized is not None and not isinstance(design.randomized, bool):
            add("invalid_design_value", "randomized 必须是 true、false 或 null。", "randomized")
        if isinstance(design.confidence_level, bool) or not isinstance(
            design.confidence_level, (int, float)
        ):
            add("invalid_confidence_level", "confidence_level 必须是数值。", "confidence_level")
        elif not 0.50 < float(design.confidence_level) < 1.0:
            add("invalid_confidence_level", "confidence_level 必须大于 0.50 且小于 1.0。", "confidence_level")
        if not isinstance(design.paired, bool) or not isinstance(design.repeated_measures, bool):
            add("invalid_design_value", "paired 和 repeated_measures 必须是布尔值。")
        if design.paired and design.repeated_measures:
            add("invalid_pair_structure",
                "paired 和 repeated_measures 不能同时为 true：本阶段用 paired 表示两次配对测量，"
                "repeated_measures 表示多于两次。"
            )

        requested_columns = [design.outcome, design.predictor, *design.covariates]
        if design.subject_id:
            requested_columns.append(design.subject_id)
        if design.time_variable:
            requested_columns.append(design.time_variable)
        for name in requested_columns:
            if name not in columns:
                add("column_not_found", f"数据集中不存在字段 {name}。", name)

        if len(set(design.covariates)) != len(design.covariates):
            add("duplicate_covariate", "covariates 不能包含重复字段。", "covariates")
        if design.outcome in design.covariates or design.predictor in design.covariates:
            add("role_conflict", "结局变量和主要预测变量不能同时列为协变量。")
        if design.outcome == design.predictor:
            add("role_conflict", "结局变量和主要预测变量不能是同一字段。")

        timing_keys = set(design.covariate_timing)
        covariate_keys = set(design.covariates)
        for name in sorted(covariate_keys - timing_keys):
            add("covariate_timing_missing", f"协变量 {name} 缺少干预时序声明。", name)
        for name in sorted(timing_keys - covariate_keys):
            add("covariate_timing_extra", f"covariate_timing 包含未列入 covariates 的字段 {name}。", name)
        for name, timing in design.covariate_timing.items():
            if timing not in VALID_COVARIATE_TIMING:
                add("invalid_covariate_timing", f"协变量 {name} 的时序必须是 {sorted(VALID_COVARIATE_TIMING)} 之一。", name)
            elif timing == "post_treatment":
                add("post_treatment_covariate", f"协变量 {name} 位于干预之后，受控选择器拒绝自动调整。", name)
            elif timing == "unknown":
                add("covariate_timing_unknown", f"协变量 {name} 的干预时序未知，需要人工确认。", name)

        identifier_columns = {
            warning.column
            for warning in profile.warnings
            if warning.code == "possible_identifier" and warning.column
        }
        analysis_columns = {design.outcome, design.predictor, *design.covariates}
        misused_identifiers = sorted(identifier_columns & analysis_columns)
        if misused_identifiers:
            add("identifier_as_analysis_variable",
                "疑似行级标识符不能作为分析变量：" + ", ".join(misused_identifiers)
            )

        if design.paired or design.repeated_measures:
            if not design.subject_id:
                add("pair_id_required", "配对或重复测量设计必须提供 subject_id。", "subject_id")
            if not design.time_variable:
                add("time_variable_required", "配对或重复测量设计必须提供 time_variable。", "time_variable")

        if design.objective == "group_difference":
            if design.predictor_type not in {"binary", "categorical"}:
                add("invalid_group_variable", "group_difference 需要二分或分类的分组变量。", "predictor_type")
            if isinstance(design.group_count, bool) or not isinstance(design.group_count, int):
                add("invalid_group_count", "group_difference 必须提供整数 group_count。", "group_count")
            elif design.group_count < 2:
                add("insufficient_group_levels", "group_count 必须至少为 2。", "group_count")
            elif design.predictor in columns:
                observed_groups = columns[design.predictor].unique_count
                if observed_groups != design.group_count:
                    add("group_count_mismatch",
                        f"声明的 group_count={design.group_count}，但数据中观察到 "
                        f"{observed_groups} 个非空组别。", "group_count"
                    )
            if design.group_count == 2:
                if design.reference_level is None:
                    add("reference_level_required", "两组比较必须显式指定 reference_level。", "reference_level")
                if design.contrast_level is None:
                    add("contrast_level_required", "两组比较必须显式指定 contrast_level。", "contrast_level")
                if (
                    design.reference_level is not None
                    and design.reference_level == design.contrast_level
                ):
                    add("invalid_contrast", "reference_level 和 contrast_level 不能相同。")
                if design.predictor in columns:
                    observed_levels = set(columns[design.predictor].sample_values)
                    for field_name, value in (
                        ("reference_level", design.reference_level),
                        ("contrast_level", design.contrast_level),
                    ):
                        if value is not None and value not in observed_levels:
                            add(
                                "group_level_not_found",
                                f"{field_name}={value!r} 不在分组字段 {design.predictor} 的观察水平中。",
                                field_name,
                            )

        if design.outcome in columns and not _compatible_outcome(
            columns[design.outcome], design.outcome_type
        ):
            add("incompatible_semantic_type",
                f"字段 {design.outcome} 的检测类型为 "
                f"{columns[design.outcome].semantic_type}，与声明的 "
                f"outcome_type={design.outcome_type} 不兼容。", design.outcome
            )
        if design.predictor in columns and not _compatible_predictor(
            columns[design.predictor], design.predictor_type
        ):
            add("incompatible_semantic_type",
                f"字段 {design.predictor} 的检测类型为 "
                f"{columns[design.predictor].semantic_type}，与声明的 "
                f"predictor_type={design.predictor_type} 不兼容。", design.predictor
            )
        return issues

    def _group_difference(self, design: ResearchDesign) -> _Selection:
        if design.outcome_type == "continuous":
            return self._continuous_group_difference(design)
        if design.outcome_type in {"binary", "categorical"}:
            return self._categorical_group_difference(design)
        return self._count_model(design, purpose="比较组间计数结局")

    def _continuous_group_difference(self, design: ResearchDesign) -> _Selection:
        assumptions = ["观测单位之间独立，或已在模型中正确表示相关结构。"]
        diagnostics = ["检查结局分布、异常值和各组有效样本量。"]
        warnings: list[str] = []

        if design.repeated_measures:
            return _Selection(
                primary=_method(
                    "linear_mixed_effects_model",
                    "线性混合效应模型",
                    "估计组别、时间及交互效应，并表示受试者内相关性。",
                ),
                sensitivity=[],
                rationale=["多时点重复测量不满足普通独立样本检验的独立性假设。"],
                assumptions=assumptions + ["随机效应结构和时间函数设定合理。"],
                diagnostics=diagnostics + ["比较候选协方差或随机效应结构。"],
                warnings=warnings,
            )

        if design.paired:
            if design.normality == "violated":
                primary = _method(
                    "wilcoxon_signed_rank",
                    "Wilcoxon 符号秩检验",
                    "比较配对差值的位置分布。",
                )
                sensitivity = [
                    _method(
                        "paired_permutation_test",
                        "配对置换检验",
                        "作为对配对差值强分布假设更少的敏感性分析。",
                    )
                ]
            else:
                primary = _method(
                    "paired_t_test",
                    "配对 t 检验",
                    "估计两次配对测量的平均差。",
                )
                sensitivity = [
                    _method(
                        "wilcoxon_signed_rank",
                        "Wilcoxon 符号秩检验",
                        "评估结论对差值分布假设的敏感性。",
                    )
                ]
            return _Selection(
                primary=primary,
                sensitivity=sensitivity,
                rationale=["两次测量来自同一受试者，需要对受试者内配对。"],
                assumptions=["各受试者的配对差值之间相互独立。"],
                diagnostics=["检查配对差值的分布、对称性和异常值。"],
                warnings=warnings,
            )

        if design.covariates:
            unadjusted = (
                _method(
                    "welch_t_test",
                    "Welch 独立样本 t 检验",
                    "提供两组未校正平均差。",
                )
                if design.group_count == 2
                else _method(
                    "welch_anova",
                    "Welch ANOVA",
                    "提供多组未校正总体差异检验。",
                )
            )
            return _Selection(
                primary=_method(
                    "ancova_linear_model",
                    "ANCOVA（协方差分析）",
                    "在校正预指定协变量后估计组间平均结局差异。",
                ),
                sensitivity=[unadjusted],
                rationale=[
                    f"研究设计预指定了 {len(design.covariates)} 个协变量："
                    + ", ".join(design.covariates)
                    + "。",
                    "线性模型可同时给出校正组间效应、置信区间和模型诊断。",
                ],
                assumptions=assumptions
                + [
                    "结局与连续协变量的关系在所设定的函数尺度上合理。",
                    "组别与基线协变量的斜率齐性假设成立，或已显式建模交互项。",
                    "模型残差均值、方差和分布假设足以支持所选推断。",
                ],
                diagnostics=diagnostics
                + [
                    "检查残差图、异方差、强影响点和非线性。",
                    "检查组别×基线协变量交互，并根据研究方案决定是否保留。",
                ],
                warnings=warnings,
            )

        if design.group_count == 2:
            if design.normality == "violated":
                primary = _method(
                    "permutation_mean_difference",
                    "平均差置换检验",
                    "在较少分布假设下检验两组平均差。",
                )
                sensitivity = [
                    _method(
                        "mann_whitney_u",
                        "Mann–Whitney U 检验",
                        "比较两组秩分布，但不自动等价于中位数检验。",
                    )
                ]
                warnings.append("Mann–Whitney U 检验的结论不应在无额外假设时表述为‘中位数差异’。")
            else:
                primary = _method(
                    "welch_t_test",
                    "Welch 独立样本 t 检验",
                    "估计两独立组的平均差，不强制等方差。",
                )
                sensitivity = []
            return _Selection(
                primary=primary,
                sensitivity=sensitivity,
                rationale=["连续结局、两个独立组别且未指定协变量。"],
                assumptions=assumptions,
                diagnostics=diagnostics,
                warnings=warnings,
            )

        return _Selection(
            primary=_method(
                "welch_anova",
                "Welch ANOVA",
                "检验三组或以上独立组别的总体平均差异。",
            ),
            sensitivity=[
                _method(
                    "kruskal_wallis",
                    "Kruskal–Wallis 检验",
                    "评估结论对分布假设的敏感性。",
                )
            ],
            rationale=[f"连续结局与 {design.group_count} 个独立组别。"],
            assumptions=assumptions,
            diagnostics=diagnostics + ["若总体检验显著，使用 Games–Howell 进行多重比较校正。"],
            warnings=warnings,
        )

    def _categorical_group_difference(self, design: ResearchDesign) -> _Selection:
        if design.paired:
            primary = _method(
                "mcnemar_test",
                "McNemar 检验",
                "比较两次配对二分结局的边际比例。",
            )
            if design.outcome_type != "binary":
                raise MethodSelectionError([SelectionIssue("unsupported_design", "本阶段的配对分类检验仅支持二分结局。")])
            return _Selection(
                primary=primary,
                sensitivity=[],
                rationale=["二分结局在同一受试者内配对。"],
                assumptions=["不同受试者之间相互独立。"],
                diagnostics=["报告不一致配对的两个方向计数。"],
                warnings=[],
            )
        if design.repeated_measures:
            return _Selection(
                primary=_method(
                    "gee_logistic",
                    "Logistic GEE",
                    "估计重复二分结局的边际组别和时间效应。",
                ),
                sensitivity=[],
                rationale=["重复分类结局存在受试者内相关性。"],
                assumptions=["工作相关结构设定合理，并使用稳健标准误。"],
                diagnostics=["报告聚类数、每个时点样本量和拟合收敛状态。"],
                warnings=[],
            )
        if design.covariates:
            return _Selection(
                primary=_method(
                    "logistic_regression" if design.outcome_type == "binary" else "multinomial_logistic_regression",
                    "Logistic 回归" if design.outcome_type == "binary" else "多项 Logistic 回归",
                    "估计校正协变量后的组别关联和置信区间。",
                ),
                sensitivity=[],
                rationale=["分类结局需要校正预指定协变量。"],
                assumptions=["模型形式、样本量和每个参数的事件数足够。"],
                diagnostics=["检查分离、共线性、拟合收敛和校准。"],
                warnings=[],
            )

        sparse_two_by_two = design.group_count == 2 and design.expected_cell_count == "sparse"
        primary = (
            _method(
                "fisher_exact_test",
                "Fisher 精确检验",
                "在 2×2 稀疏列联表中检验组别与结局的关联。",
            )
            if sparse_two_by_two
            else _method(
                "chi_square_independence",
                "Pearson 卡方独立性检验",
                "检验组别与分类结局是否独立。",
            )
        )
        diagnostics = ["生成列联表并检查每个单元格的期望计数。"]
        warnings: list[str] = []
        if design.expected_cell_count == "unknown":
            diagnostics.append("根据期望计数决定是否从卡方检验切换到精确或置换方法。")
            warnings.append("期望单元格计数未知，当前推荐必须在列联表生成后重新确认。")
        return _Selection(
            primary=primary,
            sensitivity=[
                _method(
                    "logistic_regression",
                    "Logistic 回归",
                    "对二分结局报告效应量和置信区间。",
                )
            ]
            if design.outcome_type == "binary"
            else [],
            rationale=["结局和分组变量均为分类变量。"],
            assumptions=["观测单位之间独立。"],
            diagnostics=diagnostics,
            warnings=warnings,
        )

    def _association(self, design: ResearchDesign) -> _Selection:
        if design.outcome_type == "continuous" and design.predictor_type == "continuous":
            if design.covariates:
                return _Selection(
                    primary=_method(
                        "multiple_linear_regression",
                        "多元线性回归",
                        "估计校正协变量后连续预测变量与结局的关联。",
                    ),
                    sensitivity=[],
                    rationale=["研究目标是连续变量的校正关联。"],
                    assumptions=["线性、独立性、方差结构和残差设定合理。"],
                    diagnostics=["检查散点图、残差、共线性和强影响点。"],
                    warnings=[],
                )
            if design.normality == "violated":
                primary = _method(
                    "spearman_correlation",
                    "Spearman 秩相关",
                    "估计两连续或有序变量的单调关联。",
                )
                sensitivity: list[MethodChoice] = []
            else:
                primary = _method(
                    "pearson_correlation",
                    "Pearson 相关",
                    "估计两连续变量的线性关联。",
                )
                sensitivity = [
                    _method(
                        "spearman_correlation",
                        "Spearman 秩相关",
                        "评估结论对异常值和分布假设的敏感性。",
                    )
                ]
            return _Selection(
                primary=primary,
                sensitivity=sensitivity,
                rationale=["研究目标是两个连续变量的未校正关联。"],
                assumptions=["不同观测单位之间独立。"],
                diagnostics=["检查散点图、关系形状、异常值和有效样本量。"],
                warnings=["相关不能单独支持因果结论。"],
            )
        if design.outcome_type == "binary":
            return _Selection(
                primary=_method(
                    "logistic_regression",
                    "Logistic 回归",
                    "估计预测变量与二分结局的关联。",
                ),
                sensitivity=[],
                rationale=["结局为二分变量。"],
                assumptions=["连续预测变量与对数优势的函数形式合理。"],
                diagnostics=["检查分离、收敛、线性设定和校准。"],
                warnings=["观察性关联不能单独支持因果结论。"],
            )
        if design.outcome_type == "count":
            return self._count_model(design, purpose="估计计数结局的关联")
        if design.predictor_type not in {"binary", "categorical"}:
            raise MethodSelectionError([SelectionIssue("unsupported_design", "分类结局的本阶段关联规则需要分类预测变量。")])
        return self._categorical_group_difference(design)

    def _prediction(self, design: ResearchDesign) -> _Selection:
        if design.paired or design.repeated_measures:
            raise MethodSelectionError([SelectionIssue("unsupported_design", "本阶段尚不支持配对或重复测量的预测模型选择。")])
        if design.outcome_type == "continuous":
            primary = _method(
                "linear_regression_prediction",
                "线性回归预测",
                "预测连续结局。",
            )
        elif design.outcome_type == "binary":
            primary = _method(
                "logistic_regression_prediction",
                "Logistic 回归预测",
                "预测二分结局概率。",
            )
        elif design.outcome_type == "categorical":
            primary = _method(
                "multinomial_logistic_prediction",
                "多项 Logistic 回归预测",
                "预测多分类结局概率。",
            )
        else:
            return self._count_model(design, purpose="预测计数结局")
        return _Selection(
            primary=primary,
            sensitivity=[],
            rationale=["研究目标是样本外预测，而不是单个系数的假设检验。"],
            assumptions=["训练样本与目标人群的数据生成机制足够接近。"],
            diagnostics=[
                "在建模前划分训练与测试集，或使用嵌套交叉验证。",
                "报告适合结局类型的区分度、校准和不确定性。",
            ],
            warnings=["不得在全数据上选特征后再随机切分，以避免数据泄漏。"],
        )

    def _count_model(self, design: ResearchDesign, purpose: str) -> _Selection:
        if design.overdispersion == "present":
            primary = _method(
                "negative_binomial_regression",
                "负二项回归",
                purpose + "，并允许方差大于均值。",
            )
            sensitivity: list[MethodChoice] = []
        else:
            primary = _method(
                "poisson_regression",
                "Poisson 回归",
                purpose + "。",
            )
            sensitivity = [
                _method(
                    "negative_binomial_regression",
                    "负二项回归",
                    "在存在过度离散时作为备选模型。",
                )
            ]
        warnings = []
        if design.overdispersion == "unknown":
            warnings.append("过度离散状态未知，必须在拟合 Poisson 模型后重新评估方法。")
        return _Selection(
            primary=primary,
            sensitivity=sensitivity,
            rationale=["结局是非负整数计数。"],
            assumptions=["计数的暴露时间一致，或已通过 offset 纳入模型。"],
            diagnostics=["检查过度离散、零值比例、残差和拟合收敛。"],
            warnings=warnings,
        )

    @staticmethod
    def _append_data_quality_guidance(
        selection: _Selection,
        design: ResearchDesign,
        columns: dict[str, ColumnProfile],
    ) -> None:
        if design.objective == "group_difference":
            if design.randomized is True:
                selection.rationale.append(
                    "研究设计已显式声明为随机分组；选择器不会仅根据列名推断这一事实。"
                )
            elif design.randomized is False:
                selection.warnings.append(
                    "研究未随机分组；校正组间差异应解释为关联，不能单独支持因果结论。"
                )
            else:
                selection.warnings.append(
                    "是否随机分组未知；选择器不会根据 group 列名猜测，且当前结果不应使用因果措辞。"
                )

        analyzed = [design.outcome, design.predictor, *design.covariates]
        missing = [
            f"{name}={columns[name].missing_rate:.1%}"
            for name in analyzed
            if columns[name].missing_rate > 0
        ]
        if missing:
            selection.warnings.append("分析变量存在缺失：" + ", ".join(missing) + "。")
            selection.diagnostics.append(
                "报告各组缺失比例，比较完整与缺失个体的基线特征，并预指定缺失数据策略。"
            )
            if design.analysis_population == "intention_to_treat":
                selection.warnings.append(
                    "声明了意向性治疗分析，但结局存在缺失；仅删除缺失行不足以自动实现 ITT。"
                )
        if design.normality == "unknown" and design.outcome_type == "continuous":
            selection.diagnostics.append(
                "normality=unknown：使用图形和模型残差诊断，不要仅根据正态性检验 p 值自动切换方法。"
            )


def recommend_method(
    profile: DatasetProfile,
    design: ResearchDesign,
) -> MethodRecommendation:
    return StatisticalMethodSelector().recommend(profile, design)


def _compatible_outcome(column: ColumnProfile, declared_type: str) -> bool:
    if declared_type == "continuous":
        return column.semantic_type in NUMERIC_SEMANTIC_TYPES and column.unique_count > 2
    if declared_type == "binary":
        return column.unique_count == 2
    if declared_type == "count":
        return column.semantic_type in NUMERIC_SEMANTIC_TYPES
    return column.semantic_type in {"categorical", "text", "discrete_numeric", "binary_numeric"}


def _compatible_predictor(column: ColumnProfile, declared_type: str) -> bool:
    if declared_type == "continuous":
        return column.semantic_type in NUMERIC_SEMANTIC_TYPES and column.unique_count > 2
    if declared_type == "binary":
        return column.unique_count == 2
    return column.semantic_type in {"categorical", "text", "discrete_numeric", "binary_numeric"}


def _method(code: str, label: str, purpose: str) -> MethodChoice:
    return MethodChoice(code=code, label=label, purpose=purpose)


def _deduplicate(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
