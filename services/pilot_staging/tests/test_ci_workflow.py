from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pilot-staging-ci.yml"


def test_pilot_ci_is_pinned_offline_and_provider_key_free() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["offline-and-postgres"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "30"
    assert "@sha256:" in job["services"]["postgres"]["image"]

    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert uses
    assert all(
        re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", value) for value in uses
    )

    assert "${{ secrets." not in text
    assert "--profile online" not in text
    assert "researchops-pilot-worker" not in text
    assert "provider_api_key.txt" in text  # negative existence assertions only
    assert "CI must not create a Provider API key file" in text
    assert "test_postgres_integration.py" in text
    assert "bootstrap-ci.ps1" in text
    assert "Offline pilot Compose startup failed" in text
    assert "--no-color --tail 200 migrate api" in text
    assert "docker compose -f services/pilot_staging/compose.yaml down" in text
    assert "down -v" not in text.lower()
    assert "id: teardown" in text
    assert "steps.teardown.outcome" in text
    assert "ps -a -q" in text
    assert "resources remain after teardown" in text


def test_ci_bootstrap_never_creates_or_prints_provider_credentials() -> None:
    bootstrap = (
        ROOT / "services" / "pilot_staging" / "scripts" / "bootstrap-ci.ps1"
    ).read_text(encoding="utf-8")
    assert '$env:CI -ne "true"' in bootstrap
    assert "provider_api_key.txt" not in bootstrap
    assert "DEEPSEEK_API_KEY" not in bootstrap
    assert "OPENAI_API_KEY" not in bootstrap
    assert "secret_values_printed = $false" in bootstrap
    assert "CreateNew" in bootstrap
    assert "refusing overwrite" in bootstrap
    assert 'Join-Path $serviceRoot "..\\.."' not in bootstrap
    assert 'Join-Path $projectRoot "artifacts\\self_pilot_data\\run-01"' not in bootstrap
    assert 'Join-Path $projectRoot "evals\\v2\\external_datasets.json"' not in bootstrap
    assert "New-Item -ItemType Directory -Path $secretRoot -Force" in bootstrap
    assert "New-Item -ItemType Directory -Path $datasetRoot -Force" in bootstrap
