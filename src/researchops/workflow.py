from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .analysis_tools import (
    TOOL_VERSION,
    AnalysisExecutionError,
    run_ancova,
    run_welch_t_test,
    runtime_versions,
)
from .artifact_security import ArtifactPermissionError, enable_parent_acl_inheritance
from .contracts import AnalysisBundle, ResearchDesign
from .data_quality import profile_csv
from .method_selection import recommend_method
from .visualization import CHART_VERSION, create_effect_estimate_chart


def run_phase3_analysis(
    csv_path: str | Path,
    design: ResearchDesign,
    output_directory: str | Path,
) -> AnalysisBundle:
    source_path = Path(csv_path).resolve()
    output_path = Path(output_directory).resolve()
    if output_path.exists():
        raise AnalysisExecutionError(
            "artifact_directory_exists",
            "输出目录已存在；第三阶段不会在未审批时覆盖现有产物。",
            {"path": str(output_path)},
        )

    profile = profile_csv(source_path)
    recommendation = recommend_method(profile, design)
    supported = {
        recommendation.primary_method.code,
        *(method.code for method in recommendation.sensitivity_methods),
    }
    required = {"ancova_linear_model", "welch_t_test"}
    if not required.issubset(supported):
        raise AnalysisExecutionError(
            "unsupported_execution_plan",
            "第三阶段只执行 ANCOVA 主分析和 Welch t 敏感性分析的受控计划。",
            {"selected_methods": sorted(supported)},
        )

    frame = pd.read_csv(source_path, encoding="utf-8-sig", low_memory=False)
    current_digest = _sha256(source_path)
    if current_digest != profile.sha256 or recommendation.dataset_sha256 != profile.sha256:
        raise AnalysisExecutionError(
            "dataset_changed_after_planning",
            "数据集在概要、方法选择与执行之间发生了变化。",
        )

    ancova = run_ancova(frame, profile, design, role="primary")
    welch = run_welch_t_test(frame, profile, design, role="sensitivity")
    run_id = _run_id(profile.sha256, design)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(
        tempfile.mkdtemp(prefix=f".researchops-{run_id}-", dir=output_path.parent)
    ).resolve()
    try:
        chart = create_effect_estimate_chart(
            design,
            ancova,
            welch,
            temporary_path / "effect_estimates.png",
        )
        warnings = list(
            dict.fromkeys(
                [*recommendation.warnings, *ancova.warnings, *welch.warnings]
            )
        )
        bundle = AnalysisBundle(
            schema_version="1.0",
            run_id=run_id,
            status="completed",
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            question=design.question,
            dataset={
                "source_name": profile.source_name,
                "sha256": profile.sha256,
                "source_rows": profile.row_count,
                "source_columns": profile.column_count,
                "raw_data_embedded": False,
            },
            design=asdict(design),
            recommendation=recommendation.to_dict(),
            evidence=[ancova, welch],
            artifacts=[chart],
            warnings=warnings,
            runtime={
                "python": platform.python_version(),
                "platform": platform.platform(),
                "analysis_tool": TOOL_VERSION,
                "chart_tool": CHART_VERSION,
                **runtime_versions(),
            },
        )
        payload = json.dumps(
            bundle.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        (temporary_path / "analysis_bundle.json").write_text(payload + "\n", encoding="utf-8")
        try:
            enable_parent_acl_inheritance(temporary_path)
        except ArtifactPermissionError as exc:
            raise AnalysisExecutionError(
                "artifact_acl_inheritance_failed",
                "无法让分析产物继承 artifacts 目录权限，已停止发布。",
            ) from exc
        if output_path.exists():
            raise AnalysisExecutionError(
                "artifact_directory_exists",
                "产物目录在执行期间被创建，已停止以避免覆盖。",
                {"path": str(output_path)},
            )
        os.replace(temporary_path, output_path)
        return bundle
    except Exception:
        if temporary_path.exists() and temporary_path.is_relative_to(output_path.parent):
            shutil.rmtree(temporary_path)
        raise


def validate_cli_output_directory(output_directory: Path) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = output_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise AnalysisExecutionError(
            "output_path_not_allowed",
            "CLI 分析产物必须写入项目 artifacts 目录下的独立子目录。",
            {"allowed_root": str(artifacts_root), "requested": str(resolved)},
        )
    return resolved


def _run_id(dataset_sha256: str, design: ResearchDesign) -> str:
    payload = {
        "dataset_sha256": dataset_sha256,
        "design": asdict(design),
        "analysis_tool_version": TOOL_VERSION,
        "chart_tool_version": CHART_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=list).encode("utf-8")
    ).hexdigest()
    return "RUN-" + digest[:12].upper()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
