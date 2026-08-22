from __future__ import annotations

from pathlib import Path
import re

import yaml


SERVICE_ROOT = Path(__file__).resolve().parents[1]
EXACT_REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+==[^\s]+$")


def test_compose_and_otel_configs_are_valid_and_pinned() -> None:
    compose = yaml.safe_load((SERVICE_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {
        "postgres",
        "migrate",
        "minio",
        "otel-collector",
        "api",
        "worker",
    }
    for name in ("postgres", "migrate", "minio", "otel-collector"):
        image = compose["services"][name]["image"]
        assert "@sha256:" in image
        assert ":latest" not in image
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:8080:8080"]
    assert compose["services"]["api"]["read_only"] is True
    assert compose["services"]["worker"]["read_only"] is True
    migrate_script = compose["services"]["migrate"]["command"][-1]
    assert "/migrations/*.sql" in migrate_script
    assert (SERVICE_ROOT / "migrations" / "0001_jobs.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "0002_publishing_reconcile.sql").is_file()

    dockerfile = (SERVICE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.splitlines()[0] == (
        "FROM mirror.gcr.io/library/python:3.12.13-slim-bookworm@"
        "sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
    )

    collector = yaml.safe_load(
        (SERVICE_ROOT / "config" / "otel-collector.yaml").read_text(encoding="utf-8")
    )
    assert collector["service"]["pipelines"]["traces"]["exporters"] == ["debug"]


def test_service_lock_files_are_exact_and_runtime_excludes_test_tools() -> None:
    complete = [
        line.strip()
        for line in (SERVICE_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    runtime = [
        line.strip()
        for line in (SERVICE_ROOT / "requirements.runtime.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert all(EXACT_REQUIREMENT.fullmatch(line) for line in complete + runtime)
    assert len(complete) == len(set(complete))
    assert len(runtime) == len(set(runtime))
    assert set(runtime).issubset(set(complete))
    runtime_names = {line.split("==", 1)[0].lower() for line in runtime}
    assert {"pytest", "pyyaml", "httpx2"}.isdisjoint(runtime_names)


def test_docker_context_is_deny_by_default() -> None:
    repo_root = SERVICE_ROOT.parents[1]
    rules = [
        line.strip()
        for line in (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rules[0] == "**"
    assert "!src/**" in rules
    assert "!services/production_slice/src/**" in rules
    assert not any("artifact" in rule.lower() for rule in rules)
    assert not any("secret" in rule.lower() for rule in rules)

    e2e_script = (SERVICE_ROOT / "scripts" / "run-e2e.ps1").read_text(
        encoding="utf-8"
    )
    assert "[switch]$SkipBuild" in e2e_script
    assert "[switch]$StopAfter" in e2e_script
    assert "down -v" not in e2e_script.lower()
    assert "secrets_persisted = $false" in e2e_script
    assert "Get-SanitizedDiagnosticLogs" in e2e_script
    assert "diagnostic_log_redaction_failed" in e2e_script
    assert "diagnostic_logs_sanitized" in e2e_script
    assert "[REDACTED_SECRET]" in e2e_script
    assert "response_body_persisted = $false" in e2e_script
    assert '"..\\.."' not in e2e_script
    assert '"production_slice\\e2e"' not in e2e_script

    bootstrap_ci = (SERVICE_ROOT / "scripts" / "bootstrap-ci.ps1").read_text(
        encoding="utf-8"
    )
    assert '$env:CI -ne "true"' in bootstrap_ci
    assert "secret_values_printed = $false" in bootstrap_ci
    assert "prepared_sha256" in bootstrap_ci

    workflow_path = repo_root / ".github" / "workflows" / "production-slice-e2e.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    job = workflow["jobs"]["compose-e2e"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert workflow["permissions"] == {"contents": "read"}
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", value) for value in uses)
    assert "${{ secrets." not in workflow_text
    assert "docker compose -f services/production_slice/compose.yaml down" in workflow_text
    assert "down -v" not in workflow_text.lower()
    assert "artifacts/production_slice/e2e/**" in workflow_text
