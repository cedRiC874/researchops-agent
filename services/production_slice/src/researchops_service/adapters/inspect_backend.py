from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


class CoreAggregateInspector:
    """Adapter to the existing aggregate-only logical-registry backend."""

    def __init__(self, registry_path: str | Path) -> None:
        from researchops.eval_v2_inspect_backend import EvalV2InspectDatasetBackend

        self._backend = EvalV2InspectDatasetBackend.from_registry_path(registry_path)

    def inspect_dataset(self, dataset_id: str) -> Mapping[str, Any]:
        return self._backend.inspect_dataset(dataset_id)
