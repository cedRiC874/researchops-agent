from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from pilot_staging.application import task_pack_commitment_sha256
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
    assert "litellm==1.83.0" in lines
    assert "propcache==0.5.2" in lines
    dockerfile = (SERVICE / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-deps /app/core" in dockerfile
    assert "python -m pip check" in dockerfile
    assert "USER 10002:10002" in dockerfile
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert rules[0] == "**"
    assert "!services/pilot_staging/src/**" in rules
    assert not any(line == "!artifacts/**" for line in rules)


def test_contract_json_and_pack_are_parseable() -> None:
    contract_paths = sorted((SERVICE / "contracts").glob("*.schema.json"))
    assert len(contract_paths) == 5
    for path in contract_paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert value["additionalProperties"] is False
    historical_v3 = "22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9"
    historical_v4 = "1741c2b0df53d06a299a5a89dfa91e68eade4c71cef7931d367115c07f6399c7"
    current_v5 = LOCKED_CANDIDATE_COMMITMENT

    def candidate_enums(value):
        matches = []
        if isinstance(value, dict):
            enum = value.get("enum")
            if isinstance(enum, list) and current_v5 in enum:
                matches.append(enum)
            for child in value.values():
                matches.extend(candidate_enums(child))
        elif isinstance(value, list):
            for child in value:
                matches.extend(candidate_enums(child))
        return matches

    for name in (
        "participant_lifecycle.schema.json",
        "pilot_manifest.schema.json",
        "pilot_summary.v1.2.schema.json",
        "task_feedback.schema.json",
    ):
        schema = json.loads((SERVICE / "contracts" / name).read_text(encoding="utf-8"))
        enums = candidate_enums(schema)
        assert len(enums) == 1
        assert historical_v3 in enums[0]
        assert historical_v4 in enums[0]
    pack = json.loads(
        (SERVICE / "content" / "pilot_pack.public_v1.json").read_text(encoding="utf-8")
    )
    assert len(pack["tasks"]) == 6
    assert len({item["dataset_id"] for item in pack["tasks"]}) == 3
    assert len({item["scenario"] for item in pack["tasks"]}) >= 5
    assert pack["candidate_commitment_sha256"] == (
        "7744770aa4a36c131476b95d6ed9be248cdefc3ab0f4f2a18d5111b85c9f0d11"
    )
    supervised_pack = json.loads(
        (SERVICE / "content" / "pilot_pack.supervised_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert hashlib.sha256(
        (SERVICE / "content" / "pilot_pack.supervised_v4.json").read_bytes()
    ).hexdigest() == "f764786a984a4b65bacf7f403019057c4230535e0f7c6849f3461c35cc74125d"
    assert hashlib.sha256(
        (SERVICE / "content" / "pilot_pack.supervised_v4.review.json").read_bytes()
    ).hexdigest() == "cc077115a4ba92362350456a4ab8301fce0080502b1e35e8842ffae991a49db5"
    assert hashlib.sha256(
        (SERVICE / "content" / "pilot_pack.supervised_v5.json").read_bytes()
    ).hexdigest() == "6cc4b5ee59d40e5009b6d7660cf7cd27c6c42bdee8450957f0730aee798c022f"
    assert hashlib.sha256(
        (SERVICE / "content" / "pilot_pack.supervised_v5.review.json").read_bytes()
    ).hexdigest() == "40841d3af62f64bf8456e4a788eae99c30bd1910c19809f38a37e1adc7b50017"
    assert supervised_pack["target_participants"] == 2
    assert supervised_pack["max_provider_runs"] == 12
    assert supervised_pack["tasks"] == pack["tasks"]
    regression_pack = json.loads(
        (SERVICE / "content" / "pilot_pack.supervised_v6.json").read_text(
            encoding="utf-8"
        )
    )
    review = json.loads(
        (SERVICE / "content" / "pilot_pack.supervised_v6.review.json").read_text(
            encoding="utf-8"
        )
    )
    predecessor_pack = json.loads(
        (SERVICE / "content" / "pilot_pack.supervised_v5.json").read_text(
            encoding="utf-8"
        )
    )
    public_tasks = {
        item["task_id"]: item
        for line in (ROOT / "evals" / "v2" / "public_tasks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
        for item in (json.loads(line),)
    }
    assert regression_pack["target_participants"] == 1
    assert regression_pack["max_provider_runs"] == 6
    regression_commitment = task_pack_commitment_sha256(regression_pack["tasks"])
    assert regression_commitment == (
        "83363291f30c7edd62d30e88da38fcf966b7d01c5ac16d3a2964ee9571555d72"
    )
    assert regression_commitment == task_pack_commitment_sha256(
        predecessor_pack["tasks"]
    )
    assert regression_commitment != task_pack_commitment_sha256(supervised_pack["tasks"])
    assert regression_pack["candidate_commitment_sha256"] == LOCKED_CANDIDATE_COMMITMENT
    assert "Kimi Models preflight" in regression_pack["title"]
    assert regression_pack["provider"] == predecessor_pack["provider"]
    assert regression_pack["tasks"] == predecessor_pack["tasks"]
    assert (
        regression_pack["candidate_commitment_sha256"]
        != predecessor_pack["candidate_commitment_sha256"]
    )
    regression_source_ids = {
        item["source_task_id"] for item in regression_pack["tasks"]
    }
    assert len(regression_source_ids) == 6
    assert regression_source_ids.isdisjoint(
        {item["source_task_id"] for item in supervised_pack["tasks"]}
    )
    assert len({item["dataset_id"] for item in regression_pack["tasks"]}) == 3
    assert {item["scenario"] for item in regression_pack["tasks"]} == {
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "prompt_injection",
        "unauthorized_resource",
        "approval_pause",
    }
    for item in regression_pack["tasks"]:
        source = public_tasks[item["source_task_id"]]
        assert source["split"] == "public_regression"
        assert source["lifecycle_status"] == "ready"
        assert source["review_status"] == "internal_reviewed"
        assert item["dataset_id"] == source["dataset_id"]
        assert item["scenario"] == source["scenario"]
        assert item["prompt_en"] == source["prompt"]
        assert item["context"] == source["context"]
        assert bool(item["prompt_zh"].strip())
        assert item["clarification_expected"] is (
            source["scenario"] == "clarification_required"
        )
    assert review["review_status"] == "internal_reviewed"
    assert review["pack_file"] == "pilot_pack.supervised_v6.json"
    assert review["purpose"] == (
        "kimi_models_preflight_successor_supervised_usability_only"
    )
    assert review["predecessor_pack_file"] == "pilot_pack.supervised_v5.json"
    assert review["task_selection_changed"] is False
    assert review["translation_review"] == {
        "status": "internal_reviewed",
        "english_prompt_exact": True,
        "chinese_prompt_scope_preserving": True,
    }
    assert review["source_task_ids"] == [
        item["source_task_id"] for item in regression_pack["tasks"]
    ]
    assert review["selection_policy"] == {
        "public_regression_only": True,
        "internal_reviewed_only": True,
        "exclude_supervised_v1_tasks": True,
        "dataset_and_scenario_coverage_precommitted": True,
        "model_performance_used_for_selection": False,
        "private_holdout_used": False,
        "repo_local_holdout_used": False,
    }
    assert review["evidence_boundaries"][
        "independent_participant_evidence_allowed"
    ] is False
    assert review["evidence_boundaries"]["cross_campaign_aggregation_allowed"] is False
    assert review["evidence_boundaries"]["prior_pilot_results_inherited"] is False
    assert review["evidence_boundaries"]["online_run_performed_for_v5_candidate"] is False
    assert review["evidence_boundaries"]["kimi_models_preflight_live_call_performed"] is False
    assert review["evidence_boundaries"]["kimi_online_run_performed"] is False
    composition = (SERVICE / "src" / "pilot_staging" / "composition.py").read_text(
        encoding="utf-8"
    )
    assert "pilot_pack.supervised_v6.json" in composition
    assert "pilot_pack.supervised_v5.json" not in composition
    assert "pilot_pack.supervised_v4.json" not in composition
    assert "pilot_pack.supervised_v3.json" not in composition
    assert "pilot_pack.supervised_v2.json" not in composition
    assert "pilot_pack.supervised_v1.json" not in composition


def test_migration_has_checksum_runner_and_cascading_retention() -> None:
    migration = (SERVICE / "migrations" / "0001_pilot_staging.sql").read_text(
        encoding="utf-8"
    )
    telemetry_migration = (
        SERVICE / "migrations" / "0002_attempt_telemetry_checks.sql"
    ).read_text(encoding="utf-8")
    normalized_telemetry_migration = " ".join(telemetry_migration.split())
    completion_migration = (
        SERVICE / "migrations" / "0003_completion_failure_source.sql"
    ).read_text(encoding="utf-8")
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
    for source in (
        "final_output_missing",
        "response_output_item_incomplete",
        "response_not_completed",
        "output_limit_suspected",
    ):
        assert source in completion_migration
    assert "outcome = 'controlled_failure'" in completion_migration
    assert completion_migration.count("VALIDATE CONSTRAINT") == 2
    assert "timedelta(days=6)" in retention
    assert "now + timedelta(days=1)" in retention
    assert "participant_id" not in re.search(
        r'event_type="participant_withdrew",\s+payload=\{([^}]*)\}',
        (SERVICE / "src/pilot_staging/postgres.py").read_text(encoding="utf-8"),
    ).group(1)
