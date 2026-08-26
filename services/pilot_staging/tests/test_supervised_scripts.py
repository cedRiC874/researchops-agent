from __future__ import annotations

import re
from pathlib import Path


SERVICE = Path(__file__).resolve().parents[1]
SCRIPTS = SERVICE / "scripts"


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_supervised_operator_script_set_is_complete() -> None:
    expected = {
        "_supervised-common.ps1",
        "start-supervised.ps1",
        "stop-supervised.ps1",
        "status-supervised.ps1",
        "new-invite.ps1",
        "bootstrap-ci.ps1",
    }
    assert expected.issubset({path.name for path in SCRIPTS.glob("*.ps1")})
    for name in expected - {"_supervised-common.ps1", "bootstrap-ci.ps1"}:
        assert '_supervised-common.ps1' in _text(name)


def test_start_is_explicit_online_foreground_by_default_and_identity_bound() -> None:
    common = _text("_supervised-common.ps1")
    start = _text("start-supervised.ps1")
    assert "Assert-DockerDesktopAndCompose" in start
    assert "Assert-WindowsAdministrator" in start
    assert "Get-TailscaleSupervisedIdentity" in start
    assert "Assert-PilotFunnelNotConflicting" in start
    assert "-ConfirmOnline" in start
    assert "-ConfirmRetentionSchedule" in start
    assert "BackgroundFunnel" in start
    assert "$tailscaleExecutable funnel --https=443" in start
    assert "$tailscaleExecutable funnel --bg --yes --https=443" in start
    assert "Ensure-PilotEnvironmentFile" in start
    assert "Get-CleanDeploymentGitSha" in start
    assert "Set-PilotEnvironmentValuesAtomic" in start
    assert "Prepare-PilotApplicationImages" in start
    assert '$null = Invoke-PilotCompose -Arguments @("build", "migrate")' in common
    assert '@("api", "worker", "retention")' in common
    assert "docker image tag $sourceImageName $targetImageName" in common
    assert 'Get-PilotLocalImageId "retention"' in common
    assert '"build", "migrate", "api", "worker"' not in common
    assert "SkipBuild" not in start
    assert "Wait-PilotWorkerHeartbeat" in start
    assert "Wait-PilotApiReady" in start
    assert "Assert-RunningPilotIdentity" in start
    assert "candidate_commitment_sha256" in start
    assert "deployment_image_digest" in start
    assert "docker version --format" in common
    assert "docker compose version --short" in common
    assert "tailscale status --json" in common
    assert "BackendState" in common and "DNSName" in common
    assert "researchops-pilot" in common
    assert "certificate names must not expose" in common
    assert '${env:ProgramFiles}' in common
    assert '"Tailscale\\tailscale.exe"' in common
    assert "Get-TailscaleExecutable" in common
    assert "& tailscale" not in "\n".join(
        path.read_text(encoding="utf-8") for path in SCRIPTS.glob("*.ps1")
    )
    for required in (
        "RESEARCHOPS_PILOT_SECURE_COOKIES",
        "RESEARCHOPS_PILOT_ALLOWED_HOSTS",
        "RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED",
        "RESEARCHOPS_PILOT_CANDIDATE_COMMITMENT_SHA256",
        "RESEARCHOPS_PILOT_DEPLOYMENT_GIT_SHA",
        "RESEARCHOPS_PILOT_DEPLOYMENT_IMAGE_DIGEST",
        "RESEARCHOPS_PILOT_DATABASE_HOST",
        "RESEARCHOPS_PILOT_DATABASE_SSLMODE",
        "RESEARCHOPS_PILOT_REGISTRY_PATH",
    ):
        assert required in common
        assert required in start or required == "RESEARCHOPS_PILOT_DEPLOYMENT_IMAGE_DIGEST"


def test_secret_preflight_never_opens_or_prints_provider_secret() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in SCRIPTS.glob("*.ps1"))
    assert "provider_api_key.txt" in combined
    assert not re.search(
        r"(?:Get-Content|ReadAllText|ReadAllBytes|OpenRead|WriteAllText|Set-Content|Out-File)"
        r"[^\n]*provider_api_key",
        combined,
        flags=re.IGNORECASE,
    )
    assert "RESEARCHOPS_PILOT_PROVIDER_API_KEY=" not in combined
    assert "secret_values_printed = $false" in combined


def test_stop_preserves_volumes_and_disables_exact_funnel_route() -> None:
    stop = _text("stop-supervised.ps1")
    assert "$tailscaleExecutable funnel --https=443 off" in stop
    assert "Get-PilotFunnelTargetState" in stop
    assert 'funnelState -eq "exact"' in stop
    assert "refusing to modify it" in stop
    assert "Clear-PilotWorkerHeartbeats" in stop
    assert '"stop", "worker"' in stop
    assert '"down", "--remove-orphans"' in stop
    assert "down -v" not in stop.lower()
    assert "postgres_volume_deleted = $false" in stop


def test_invite_defaults_to_two_hours_and_never_persists_link() -> None:
    invite = _text("new-invite.ps1")
    assert re.search(r"\[int\]\$TtlHours\s*=\s*2", invite)
    assert "Mandatory = $true" not in invite.split("[int]$TtlHours", 1)[0]
    assert "Test-PilotFunnelActive" in invite
    assert '"researchops-pilot-admin"' in invite
    assert "pilot_pack.supervised_v5.json" in invite
    assert "pilot_pack.supervised_v3.json" not in invite
    assert "pilot_pack.supervised_v2.json" not in invite
    assert "pilot_pack.supervised_v1.json" not in invite
    assert '"summary", "--campaign-id", $CampaignId' in invite
    assert "execution_environment" in invite
    assert "fail-closed supervised pretest" in invite
    assert "campaignSummary.commitments.deployment_git_sha" in invite
    assert "campaignSummary.commitments.deployment_image_digest" in invite
    assert "campaignSummary.commitments.candidate_commitment_sha256" in invite
    assert '"bootstrap"' in invite
    assert '"--project-root", "/app/core"' in invite
    assert "Created and froze supervised campaign" in invite
    assert '"--admin-token-file", "/run/secrets/admin_token"' in invite
    assert '"--public-base", $configuration.PublicBase' in invite
    for sink in ("Tee-Object", "Out-File", "Set-Content", "Add-Content", "Start-Transcript"):
        assert sink not in invite


def test_status_is_sanitized_and_checks_api_worker_funnel_and_images() -> None:
    status = _text("status-supervised.ps1")
    for field in (
        "funnel_active",
        "api_ready",
        "worker_locked_candidate_heartbeat",
        "candidate_commitment_sha256",
        "expected_image_digest",
        "api_image_digest",
        "worker_image_digest",
        "image_identity_matches",
        "provider_secret_present",
        "secret_values_printed",
    ):
        assert field in status


def test_ci_bootstrap_is_offline_non_overwriting_and_has_three_synthetic_entries() -> None:
    bootstrap = _text("bootstrap-ci.ps1")
    assert '$env:CI -ne "true"' in bootstrap
    assert "refusing overwrite" in bootstrap
    assert "CreateNew" in bootstrap
    assert '"postgres_password.txt", "admin_token.txt", "token_pepper.txt"' in bootstrap
    assert "provider_api_key.txt" not in bootstrap
    assert "provider_secret_created = $false" in bootstrap
    assert "RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED=false" in bootstrap
    assert "RandomNumberGenerator" in bootstrap
    assert "SetUnixFileMode" in bootstrap
    assert "UserRead" in bootstrap
    assert "GroupRead" in bootstrap
    assert "OtherRead" in bootstrap
    assert "UserWrite" not in bootstrap
    assert 'Join-Path $serviceRoot "..\\.."' not in bootstrap
    assert "New-Item -ItemType Directory -Path $datasetRoot -Force" in bootstrap
    assert "logical_dataset_registry.json" in bootstrap
    assert bootstrap.count("New-RegistryEntry \"") == 3
    for dataset in (
        "palmer_penguins_v0_1_0.csv",
        "uci_parkinsons_telemonitoring_189.csv",
        "uci_heart_disease_cleveland_45.csv",
    ):
        assert dataset in bootstrap
