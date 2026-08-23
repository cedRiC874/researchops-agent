from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from pilot_staging.application import PilotApplication
from pilot_staging.domain import LOCKED_CANDIDATE_COMMITMENT
from pilot_staging.memory import InMemoryPilotStore, StaticDatasetCatalog
from pilot_staging.telemetry import validate_provider_execution_telemetry


SERVICE = Path(__file__).resolve().parents[1]
CONSENT = "# Contract consent\nAll terms are visible."
HASHES = {
    "protocol_sha256": "1" * 64,
    "consent_sha256": hashlib.sha256(CONSENT.encode()).hexdigest(),
    "feedback_schema_sha256": "2" * 64,
    "dataset_manifest_sha256": "3" * 64,
}
DATASETS = (
    "palmer_penguins_v0_1_0",
    "uci_parkinsons_telemonitoring_189",
    "uci_heart_disease_cleveland_45",
)


def test_application_summary_matches_published_schema() -> None:
    store = InMemoryPilotStore()
    application = PilotApplication(
        store=store,
        dataset_catalog=StaticDatasetCatalog(DATASETS),
        token_pepper=b"p" * 32,
        consent_document=CONSENT,
        expected_commitments=HASHES,
        provider_execution_enabled=False,
        clock=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    )
    scenarios = (
        "standard_analysis",
        "clarification_required",
        "safe_refusal",
        "approval_pause",
        "prompt_injection",
        "unauthorized_resource",
    )
    tasks = []
    for index, scenario in enumerate(scenarios, 1):
        dataset_id = DATASETS[(index - 1) % 3]
        context = {"dataset_id": dataset_id}
        if scenario == "approval_pause":
            context.update(bundle_id="bundle", release_name="release")
        tasks.append(
            {
                "task_id": f"PILOT-TASK-{index:03d}",
                "source_task_id": f"V2-PUB-{index:03d}",
                "dataset_id": dataset_id,
                "scenario": scenario,
                "prompt_en": f"Prepared task {index}",
                "prompt_zh": f"预设任务 {index}",
                "context": context,
                "clarification_expected": scenario == "clarification_required",
            }
        )
    campaign = application.create_campaign(
        {
            "title": "Schema contract",
            **HASHES,
            "deployment_git_sha": None,
            "deployment_image_digest": None,
            "candidate_commitment_sha256": LOCKED_CANDIDATE_COMMITMENT,
            "provider": {
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "transport_id": "openai_compatible_responses",
            },
            "target_participants": 3,
            "max_provider_runs": 18,
            "tasks": tasks,
        }
    )
    application.freeze_campaign(campaign["campaign_id"])
    summary = application.summary(campaign["campaign_id"])
    schema = json.loads(
        (SERVICE / "contracts" / "pilot_summary.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(summary)
    assert summary["external_validation_claim_allowed"] is False
    assert summary["pilot_success_criteria_met"] is None
    assert summary["schema_version"] == "external-pilot-summary/1.1"
    telemetry = summary["provider_execution_telemetry"]
    assert telemetry["telemetry_coverage_status"] == "no_attempts"
    assert telemetry["executor_model_call_count"]["observed_sum"] is None
    assert telemetry["model_planning_accuracy_claim_allowed"] is False
    assert telemetry["append_only_event_binding_status"] == "not_applicable"
    assert summary["retention_status"][
        "participant_projection_binding_status"
    ] == "not_applicable"
    assert "artifact_integrity_invalid" in summary[
        "external_validation_claim_reason_codes"
    ]
    invalid_telemetry = deepcopy(telemetry)
    invalid_telemetry["executor_model_call_count"]["unknown_attempt_count"] = 1
    with pytest.raises(ValueError, match="denominator mismatch"):
        validate_provider_execution_telemetry(invalid_telemetry)

    positive = deepcopy(summary)
    positive["status"] = "complete"
    positive["external_validation_claim_allowed"] = True
    positive["external_validation_claim_reason_codes"] = []
    positive["pilot_success_criteria_met"] = True
    positive["commitments"]["deployment_git_sha"] = "a" * 40
    positive["commitments"]["deployment_image_digest"] = "sha256:" + "b" * 64
    positive["cohort"].update(
        eligible_external_participant_count=4,
        started_participant_count=4,
        completed_participant_count=4,
    )
    positive["interactions"].update(
        planned_count=24,
        started_count=24,
        answer_displayed_count=24,
        feedback_completed_count=24,
        seeded_count=24,
    )
    positive["provider_execution_telemetry"].update(
        worker_started_attempt_count=24,
        terminal_attempt_count=24,
        telemetry_coverage_status="complete",
        append_only_event_binding_status="valid",
    )
    for name in (
        "executor_model_call_count",
        "model_requested_tool_call_count",
        "backend_executed_tool_call_count",
    ):
        positive["provider_execution_telemetry"][name] = {
            "observed_sum": 24,
            "observed_attempt_count": 24,
            "unknown_attempt_count": 0,
            "coverage_rate": 1.0,
        }
    positive["coverage"].update(
        dataset_count=3,
        dataset_counts={
            "palmer_penguins_v0_1_0": 8,
            "uci_parkinsons_telemonitoring_189": 8,
            "uci_heart_disease_cleveland_45": 8,
        },
        scenario_counts={
            "standard_analysis": 4,
            "clarification_required": 4,
            "safe_refusal": 4,
            "approval_pause": 4,
            "prompt_injection": 4,
            "unauthorized_resource": 4,
        },
        feedback_rate=1.0,
        max_participant_contribution_rate=0.25,
    )
    positive["retention_status"]["scheduled_purge_confirmed"] = True
    positive["retention_status"]["participant_projection_binding_status"] = "valid"
    Draft202012Validator(schema).validate(positive)
    for invalid_binding_status in ("invalid", "not_applicable"):
        invalid_binding = deepcopy(positive)
        invalid_binding["provider_execution_telemetry"][
            "append_only_event_binding_status"
        ] = invalid_binding_status
        with pytest.raises(Exception):
            Draft202012Validator(schema).validate(invalid_binding)
    for invalid_projection_status in ("invalid", "not_applicable"):
        invalid_projection = deepcopy(positive)
        invalid_projection["retention_status"][
            "participant_projection_binding_status"
        ] = invalid_projection_status
        with pytest.raises(Exception):
            Draft202012Validator(schema).validate(invalid_projection)

    supervised = deepcopy(positive)
    supervised["execution_environment"] = "supervised"
    supervised["supervised_pretest"] = True
    supervised["external_pilot"] = False
    supervised["external_participant_pretest"] = True
    supervised["evidence_status"] = "supervised_external_user_pretest_only"
    supervised["external_validation_claim_allowed"] = False
    supervised["external_validation_claim_reason_codes"] = [
        "supervised_environment_not_claim_eligible"
    ]
    supervised["pilot_success_criteria_met"] = None
    Draft202012Validator(schema).validate(supervised)
