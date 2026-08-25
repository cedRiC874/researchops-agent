[CmdletBinding()]
param(
    [string]$CampaignId,

    [ValidateRange(1, 72)]
    [int]$TtlHours = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_supervised-common.ps1")

$null = Assert-DockerDesktopAndCompose
$tailscale = Get-TailscaleSupervisedIdentity
$environment = Read-PilotEnvironment
$null = Assert-SupervisedSecretsExist
$configuration = Assert-SupervisedEnvironment $environment $tailscale
Wait-PilotApiReady 30
if (-not (Test-PilotWorkerHeartbeat $environment $configuration.CandidateCommitment)) {
    throw "Refusing to create an invite while the locked-candidate worker is not ready."
}
if (-not (Test-PilotFunnelActive)) {
    throw "Refusing to create an invite while the HTTPS Funnel is not active."
}
$null = Assert-RunningPilotIdentity $configuration

if ($CampaignId -and $CampaignId -notmatch '^EXT-PILOT-[A-Za-z0-9-]{1,64}$') {
    throw "CampaignId format is invalid."
}
if (-not $CampaignId) {
    $bootstrapJson = Invoke-PilotCompose -Arguments @(
        "exec", "-T", "api",
        "researchops-pilot-admin",
        "--api-base", "http://127.0.0.1:8090",
        "--admin-token-file", "/run/secrets/admin_token",
        "bootstrap",
        "--project-root", "/app/core",
        "--pack", "/app/core/services/pilot_staging/content/pilot_pack.supervised_v4.json",
        "--deployment-git-sha", $configuration.DeploymentGitSha,
        "--deployment-image-digest", $configuration.DeploymentImageDigest
    ) -Capture
    try {
        $campaign = $bootstrapJson | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "Pilot campaign bootstrap returned invalid JSON."
    }
    $CampaignId = [string]$campaign.campaign_id
    if ($CampaignId -notmatch '^EXT-PILOT-[A-Za-z0-9-]{1,64}$' -or
        [string]$campaign.status -ne "frozen" -or
        [string]$campaign.execution_environment -ne "supervised") {
        throw "Pilot campaign was not created and frozen safely."
    }
    Write-Output "Created and froze supervised campaign: $CampaignId"
}

$summaryJson = Invoke-PilotCompose -Arguments @(
    "exec", "-T", "api",
    "researchops-pilot-admin",
    "--api-base", "http://127.0.0.1:8090",
    "--admin-token-file", "/run/secrets/admin_token",
    "summary", "--campaign-id", $CampaignId
) -Capture
try {
    $campaignSummary = $summaryJson | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw "Pilot campaign environment verification returned invalid JSON."
}
if ([string]$campaignSummary.execution_environment -ne "supervised" -or
    -not [bool]$campaignSummary.supervised_pretest -or
    [bool]$campaignSummary.external_validation_claim_allowed -or
    [string]$campaignSummary.commitments.deployment_git_sha -ne
        [string]$configuration.DeploymentGitSha -or
    [string]$campaignSummary.commitments.deployment_image_digest -ne
        [string]$configuration.DeploymentImageDigest -or
    [string]$campaignSummary.commitments.candidate_commitment_sha256 -ne
        [string]$configuration.CandidateCommitment) {
    throw "Refusing to reuse a campaign that is not a fail-closed supervised pretest."
}

Write-Output "The following command prints one invite link once. It is not written to a file."
Invoke-PilotCompose -Arguments @(
    "exec", "-T", "api",
    "researchops-pilot-admin",
    "--api-base", "http://127.0.0.1:8090",
    "--admin-token-file", "/run/secrets/admin_token",
    "invite",
    "--campaign-id", $CampaignId,
    "--ttl-hours", [string]$TtlHours,
    "--public-base", $configuration.PublicBase
)
Write-Output "Invite TTL: $TtlHours hour(s). Copy it directly to exactly one participant; no local copy was persisted."
