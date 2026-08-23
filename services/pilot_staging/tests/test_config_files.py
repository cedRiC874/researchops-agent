from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from pilot_staging.config import Settings
from pilot_staging.domain import LOCKED_CANDIDATE_COMMITMENT


SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
EXACT = re.compile(r"^[A-Za-z0-9_.-]+==[^\s]+$")


def base_settings(tmp_path: Path) -> dict:
    password = tmp_path / "db.txt"
    admin = tmp_path / "admin.txt"
    pepper = tmp_path / "pepper.txt"
    password.write_text("database-password", encoding="utf-8")
    admin.write_text("a" * 24, encoding="utf-8")
    pepper.write_text("p" * 32, encoding="utf-8")
    return {
        "database_password_file": password,
        "admin_token_file": admin,
        "token_pepper_file": pepper,
        "registry_path": ROOT / "artifacts/self_pilot_data/run-01/logical_dataset_registry.json",
        "project_root": ROOT,
    }


def test_staging_configuration_fails_closed(tmp_path: Path) -> None:
    values = base_settings(tmp_path)
    with pytest.raises(ValueError):
        Settings(**values, environment="stagin")
    with pytest.raises(ValueError):
        Settings(**values, environment="staging")
    with pytest.raises(ValueError):
        Settings(
            **values,
            environment="staging",
            public_base_url="https://pilot.example.org",
            secure_cookies=True,
            database_sslmode="disable",
        )
    valid = Settings(
        **values,
        environment="staging",
        public_base_url="https://pilot.example.org",
        allowed_hosts="pilot.example.org",
        secure_cookies=True,
        database_sslmode="verify-full",
        retention_schedule_confirmed=True,
        deployment_git_sha="a" * 40,
        deployment_image_digest="sha256:" + "b" * 64,
    )
    assert valid.candidate_commitment_sha256 == LOCKED_CANDIDATE_COMMITMENT
    assert valid.allowed_host_values() == ("pilot.example.org",)


def test_supervised_configuration_is_strict_but_allows_local_postgres(
    tmp_path: Path,
) -> None:
    values = base_settings(tmp_path)
    provider_key = tmp_path / "provider.txt"
    provider_key.write_text("offline-configuration-placeholder", encoding="utf-8")
    configured = {
        **values,
        "environment": "supervised",
        "public_base_url": "https://pilot.example.org",
        "allowed_hosts": "pilot.example.org",
        "secure_cookies": True,
        "database_sslmode": "disable",
        "provider_execution_enabled": True,
        "provider_api_key_file": provider_key,
        "retention_schedule_confirmed": True,
        "deployment_git_sha": "a" * 40,
        "deployment_image_digest": "sha256:" + "b" * 64,
    }
    valid = Settings(**configured)
    assert valid.environment == "supervised"
    assert valid.database_sslmode == "disable"

    invalid_overrides = (
        {"public_base_url": "http://pilot.example.org"},
        {"secure_cookies": False},
        {"allowed_hosts": "*.example.org"},
        {"provider_execution_enabled": False},
        {"retention_schedule_confirmed": False},
        {"deployment_git_sha": None},
        {"deployment_image_digest": None},
    )
    for override in invalid_overrides:
        with pytest.raises(ValueError):
            Settings(**{**configured, **override})


def test_compose_keeps_provider_secret_out_of_api_and_is_local_only() -> None:
    compose = yaml.safe_load((SERVICE / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {
        "postgres",
        "migrate",
        "api",
        "worker",
        "retention",
    }
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:8090:8090"]
    assert "provider_api_key" not in compose["services"]["api"]["secrets"]
    assert "provider_api_key" in compose["services"]["worker"]["secrets"]
    assert compose["services"]["worker"]["profiles"] == ["online"]
    assert compose["services"]["retention"]["profiles"] == ["maintenance"]
    assert "@sha256:" in compose["services"]["postgres"]["image"]
    assert compose["services"]["api"]["read_only"] is True


def test_provider_lock_is_exact_and_docker_context_is_scoped() -> None:
    lines = [
        line.strip()
        for line in (SERVICE / "requirements.provider.lock").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(EXACT.fullmatch(line) for line in lines)
    assert len(lines) == len(set(lines))
    assert "openai-agents==0.21.0" in lines
    dockerfile = (SERVICE / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-deps /app/core" in dockerfile
    assert "USER 10002:10002" in dockerfile
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert rules[0] == "**"
    assert "!services/pilot_staging/src/**" in rules
    assert not any(line == "!artifacts/**" for line in rules)


def test_contract_json_and_pack_are_parseable() -> None:
    contract_paths = sorted((SERVICE / "contracts").glob("*.schema.json"))
    assert len(contract_paths) == 4
    for path in contract_paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert value["additionalProperties"] is False
    pack = json.loads(
        (SERVICE / "content" / "pilot_pack.public_v1.json").read_text(encoding="utf-8")
    )
    assert len(pack["tasks"]) == 6
    assert len({item["dataset_id"] for item in pack["tasks"]}) == 3
    assert len({item["scenario"] for item in pack["tasks"]}) >= 5
    assert pack["candidate_commitment_sha256"] == LOCKED_CANDIDATE_COMMITMENT
    supervised_pack = json.loads(
        (SERVICE / "content" / "pilot_pack.supervised_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert supervised_pack["target_participants"] == 2
    assert supervised_pack["max_provider_runs"] == 12
    assert supervised_pack["tasks"] == pack["tasks"]


def test_migration_has_checksum_runner_and_cascading_retention() -> None:
    migration = (SERVICE / "migrations" / "0001_pilot_staging.sql").read_text(
        encoding="utf-8"
    )
    telemetry_migration = (
        SERVICE / "migrations" / "0002_attempt_telemetry_checks.sql"
    ).read_text(encoding="utf-8")
    normalized_telemetry_migration = " ".join(telemetry_migration.split())
    runner = (SERVICE / "src/pilot_staging/migrate.py").read_text(encoding="utf-8")
    retention = (SERVICE / "src/pilot_staging/retention.py").read_text(
        encoding="utf-8"
    )
    assert "source_sha256" in migration
    assert "pg_advisory_lock" in runner
    assert "checksum drift" in runner
    assert "config.migrations_path" in runner
    assert "RESEARCHOPS_PILOT_MIGRATIONS_PATH=/app/pilot/migrations" in (
        SERVICE / ".env.example"
    ).read_text(encoding="utf-8")
    assert "COPY services/pilot_staging/migrations /app/pilot/migrations" in (
        SERVICE / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "ON DELETE CASCADE" in migration
    assert "pilot_events is append-only" in migration
    for field in (
        "model_call_count",
        "model_requested_tool_call_count",
        "backend_executed_tool_call_count",
    ):
        assert f"{field} IS NULL OR {field} >= 0" in normalized_telemetry_migration
    assert telemetry_migration.count("VALIDATE CONSTRAINT") == 3
    assert "timedelta(days=6)" in retention
    assert "now + timedelta(days=1)" in retention
    assert "participant_id" not in re.search(
        r'event_type="participant_withdrew",\s+payload=\{([^}]*)\}',
        (SERVICE / "src/pilot_staging/postgres.py").read_text(encoding="utf-8"),
    ).group(1)
