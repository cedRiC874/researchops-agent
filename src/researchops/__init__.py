"""ResearchOps Agent package."""

from .contracts import ResearchDesign
from .analysis_tools import run_ancova, run_welch_t_test
from .audit import AuditLedger
from .data_quality import CsvProfiler, CsvSafetyConfig, profile_csv
from .method_selection import StatisticalMethodSelector, recommend_method
from .tool_runtime import ControlledToolExecutor, ToolRegistry
from .workflow import run_phase3_analysis

__all__ = [
    "AuditLedger",
    "ControlledToolExecutor",
    "CsvProfiler",
    "CsvSafetyConfig",
    "ResearchDesign",
    "StatisticalMethodSelector",
    "ToolRegistry",
    "profile_csv",
    "recommend_method",
    "run_ancova",
    "run_phase3_analysis",
    "run_welch_t_test",
]
