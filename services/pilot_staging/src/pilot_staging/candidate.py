from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from researchops.eval_v2_freeze import validate_public_regression_candidate
from researchops.eval_v2_inspect_backend import EvalV2InspectDatasetBackend
from researchops.eval_v2_provider_executor import EvalV2ProviderExecutor
from researchops.eval_v2_runner import EvalV2ToolGateway
from researchops.model_providers import get_provider

from .domain import (
    LOCKED_CANDIDATE_COMMITMENT,
    CandidateResult,
    CampaignDrift,
    InvalidRequest,
    PilotTask,
)


@dataclass(frozen=True, slots=True)
class _AuthorizedTask:
    context: Mapping[str, str]


class RegistryDatasetCatalog:
    def __init__(self, registry_path: str | Path) -> None:
        path = Path(registry_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidRequest("无法读取逻辑 dataset registry。") from exc
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise InvalidRequest("逻辑 dataset registry entries 无效。")
        identifiers: list[str] = []
        for entry in entries:
            dataset_id = entry.get("dataset_id") if isinstance(entry, dict) else None
            if not isinstance(dataset_id, str) or not dataset_id:
                raise InvalidRequest("逻辑 dataset registry dataset_id 无效。")
            identifiers.append(dataset_id)
        if len(identifiers) != len(set(identifiers)):
            raise InvalidRequest("逻辑 dataset registry 含重复 dataset_id。")
        self._ids = frozenset(identifiers)

    def dataset_ids(self) -> frozenset[str]:
        return self._ids


class LockedCandidateExecutor:
    """Run exactly the frozen DeepSeek candidate; no scorer or prompt is changed."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        registry_path: str | Path,
        api_key: str,
        provider_id: str = "deepseek",
        model_id: str = "deepseek-v4-flash",
        candidate_commitment_sha256: str = LOCKED_CANDIDATE_COMMITMENT,
        timeout_seconds: float = 120.0,
    ) -> None:
        if candidate_commitment_sha256 != LOCKED_CANDIDATE_COMMITMENT:
            raise CampaignDrift("Candidate commitment 不匹配。")
        validate_locked_candidate_files(project_root)
        if provider_id != "deepseek" or model_id != "deepseek-v4-flash":
            raise CampaignDrift("Provider/model 不属于已锁定 candidate。")
        if not isinstance(api_key, str) or not api_key.strip():
            raise InvalidRequest("服务器 Provider secret 未配置。")
        self._backend = EvalV2InspectDatasetBackend.from_registry_path(registry_path)
        provider = get_provider(provider_id)
        self._executor = EvalV2ProviderExecutor(
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            confirm_online=True,
            max_turns=8,
            run_timeout_seconds=timeout_seconds,
            tracing_disabled=True,
            bilingual_output=True,
            max_output_tokens=2000,
        )

    def execute(self, task: PilotTask) -> CandidateResult:
        gateway = EvalV2ToolGateway(_AuthorizedTask(task.context), self._backend)  # type: ignore[arg-type]
        started = time.monotonic()
        result = self._executor.execute(
            {
                "task_id": task.source_task_id,
                "prompt": task.prompt_en,
                "context": dict(task.context),
            },
            gateway,
        )
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        return CandidateResult(
            final_output=result.final_output,
            outcome=result.outcome,
            provider_latency_ms=latency_ms,
            model_call_count=result.model_call_count,
            model_requested_tool_call_count=result.model_requested_tool_call_count,
            backend_executed_tool_call_count=result.backend_executed_tool_call_count,
            error_code=result.error_code,
            completion_failure_source=result.completion_failure_source,
        )


def validate_locked_candidate_files(project_root: str | Path) -> Mapping[str, object]:
    resolved_root = Path(project_root).resolve()
    validation = validate_public_regression_candidate(
        project_root=resolved_root,
        candidate_path=resolved_root
        / "evals"
        / "v2"
        / "public_regression_candidate_v5.json",
        verify_environment=False,
    )
    if validation["candidate_commitment_sha256"] != LOCKED_CANDIDATE_COMMITMENT:
        raise CampaignDrift("部署中的 Eval v2 candidate 已漂移。")
    return validation
