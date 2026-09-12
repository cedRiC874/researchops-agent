from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parents[2] / ".matplotlib"),
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

from .contracts import ChartArtifact, ResearchDesign, StatisticalEvidence


CHART_VERSION = "1.0.0"


def create_effect_estimate_chart(
    design: ResearchDesign,
    ancova: StatisticalEvidence,
    welch: StatisticalEvidence,
    output_path: Path,
) -> ChartArtifact:
    reference = design.reference_level
    contrast = design.contrast_level
    assert reference is not None and contrast is not None
    direction = f"{contrast} - {reference}"
    ancova_contrast = ancova.estimates["contrast"]
    welch_contrast = welch.estimates["contrast"]
    rows = [
        {
            "label": "ANCOVA adjusted",
            "estimate": ancova_contrast["adjusted_mean_difference"],
            "interval": ancova_contrast["confidence_interval"],
            "color": "#B91C1C",
        },
        {
            "label": "Welch unadjusted",
            "estimate": welch_contrast["mean_difference"],
            "interval": welch_contrast["confidence_interval"],
            "color": "#2563EB",
        },
    ]
    plot_spec = {
        "chart_version": CHART_VERSION,
        "chart_type": "aggregate_effect_interval_plot",
        "outcome": design.outcome,
        "contrast_direction": direction,
        "confidence_level": float(design.confidence_level),
        "methods": [row["label"] for row in rows],
        "raw_individual_points": False,
        "seed": None,
    }
    plot_spec_sha256 = hashlib.sha256(
        json.dumps(plot_spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    figure, axis = plt.subplots(figsize=(8, 4.6), dpi=150)
    y_positions = [1, 0]
    for y_position, row in zip(y_positions, rows):
        estimate = row["estimate"]
        interval = row["interval"]
        axis.errorbar(
            estimate,
            y_position,
            xerr=[[estimate - interval["lower"]], [interval["upper"] - estimate]],
            fmt="o",
            color=row["color"],
            markersize=7,
            capsize=4,
            linewidth=2,
            zorder=3,
        )
        axis.text(
            interval["upper"],
            y_position + 0.12,
            f"{estimate:.2f} [{interval['lower']:.2f}, {interval['upper']:.2f}]",
            color=row["color"],
            fontsize=9,
            ha="right",
        )

    axis.axvline(0, color="#4B5563", linestyle="--", linewidth=1.2, zorder=1)
    axis.set_yticks(y_positions, [row["label"] for row in rows])
    axis.set_xlabel(f"Difference in {design.outcome} ({direction})")
    axis.set_title("Adjusted and unadjusted group contrasts (95% CI)")
    axis.grid(axis="x", color="#E5E7EB", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_visible(False)
    axis.tick_params(axis="y", length=0)
    analyzed_n = ancova.sample_flow.included_rows
    figure.text(
        0.01,
        0.01,
        f"Available-case analysis: n={analyzed_n}. Requested ITT population is not fully realized when outcomes are missing.",
        fontsize=7.5,
        color="#4B5563",
    )
    figure.tight_layout(rect=(0, 0.045, 1, 1))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        output_path,
        format="png",
        dpi=150,
        bbox_inches="tight",
        metadata={"Software": f"ResearchOps chart tool {CHART_VERSION}"},
    )
    plt.close(figure)

    with Image.open(output_path) as image:
        image.verify()
    with Image.open(output_path) as image:
        width_px, height_px = image.size
    digest = _sha256(output_path)
    chart_id = "CH-" + hashlib.sha256(
        (digest + ancova.evidence_id + welch.evidence_id).encode("utf-8")
    ).hexdigest()[:12].upper()
    return ChartArtifact(
        schema_version="1.0",
        chart_id=chart_id,
        file_name=output_path.name,
        mime_type="image/png",
        sha256=digest,
        width_px=width_px,
        height_px=height_px,
        byte_size=output_path.stat().st_size,
        plot_spec_sha256=plot_spec_sha256,
        evidence_ids=[ancova.evidence_id, welch.evidence_id],
        alt_text=(
            f"聚合效应图：{direction} 方向的 ANCOVA 校正差与 Welch 未校正差，"
            "均带 95% 置信区间和零效应参考线，不包含行级数据点。"
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
