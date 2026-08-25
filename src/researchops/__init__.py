"""ResearchOps Agent package."""

from .contracts import ResearchDesign
from .analysis_tools import run_ancova, run_welch_t_test
from .audit import AuditLedger
from .data_quality import CsvProfiler, CsvSafetyConfig, profile_csv
from .method_selection import StatisticalMethodSelector, recommend_method
from .model_providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OpenAIProvider,
    ProviderConfigurationError,
    SUPPORTED_PROVIDER_IDS,
    get_provider,
    provider_transport_status,
)
from .tool_runtime import ControlledToolExecutor, ToolRegistry
from .workflow import run_phase3_analysis

__all__ = [
    "AuditLedger",
    "AnthropicProvider",
    "ControlledToolExecutor",
    "CsvProfiler",
    "CsvSafetyConfig",
    "DeepSeekProvider",
    "OpenAIProvider",
    "ProviderConfigurationError",
    "ResearchDesign",
    "StatisticalMethodSelector",
    "SUPPORTED_PROVIDER_IDS",
    "ToolRegistry",
    "profile_csv",
    "get_provider",
    "provider_transport_status",
    "recommend_method",
    "run_ancova",
    "run_phase3_analysis",
    "run_welch_t_test",
]
