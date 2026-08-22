[CmdletBinding()]
param(
    [switch]$ConfirmOnline,
    [switch]$ConfirmRetentionSchedule,
    [switch]$BackgroundFunnel,
    [ValidateRange(30, 600)][int]$ReadyTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_supervised-common.ps1")

if (-not $ConfirmOnline) {
    throw "Refusing to start the online worker without -ConfirmOnline; queued work can spend Provider budget."
}
if (-not $ConfirmRetentionSchedule) {
    throw "Refusing supervised exposure without -ConfirmRetentionSchedule."
}

$null = Assert-WindowsAdministrator
$docker = Assert-DockerDesktopAndCompose
$tailscaleIdentity = Get-TailscaleSupervisedIdentity
$tailscaleExecutable = Get-TailscaleExecutable
$envCreated = Ensure-PilotEnvironmentFile
$secrets = Assert-SupervisedSecretsExist
$deploymentGitSha = Get-CleanDeploymentGitSha
Assert-PilotFunnelNotConflicting

$preparation = @{
    RESEARCHOPS_PILOT_ENVIRONMENT = "supervised"
    RESEARCHOPS_PILOT_PUBLIC_BASE_URL = $tailscaleIdentity.PublicBase
    RESEARCHOPS_PILOT_ALLOWED_HOSTS = "$($tailscaleIdentity.Hostname),127.0.0.1,localhost"
    RESEARCHOPS_PILOT_SECURE_COOKIES = "true"
    RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED = "true"
    RESEARCHOPS_PILOT_RETENTION_SCHEDULE_CONFIRMED = "true"
    RESEARCHOPS_PILOT_DEPLOYMENT_GIT_SHA = $deploymentGitSha
    RESEARCHOPS_PILOT_CANDIDATE_COMMITMENT_SHA256 = $script:PilotCandidateCommitment
    RESEARCHOPS_PILOT_PROVIDER_ID = "deepseek"
    RESEARCHOPS_PILOT_MODEL_ID = "deepseek-v4-flash"
    RESEARCHOPS_PILOT_PROVIDER_API_KEY_FILE = "/run/secrets/provider_api_key"
    RESEARCHOPS_PILOT_DATABASE_HOST = "postgres"
    RESEARCHOPS_PILOT_DATABASE_PORT = "5432"
    RESEARCHOPS_PILOT_DATABASE_NAME = "researchops_pilot"
    RESEARCHOPS_PILOT_DATABASE_USER = "researchops_pilot"
    RESEARCHOPS_PILOT_DATABASE_PASSWORD_FILE = "/run/secrets/postgres_password"
    RESEARCHOPS_PILOT_DATABASE_SSLMODE = "disable"
    RESEARCHOPS_PILOT_ADMIN_TOKEN_FILE = "/run/secrets/admin_token"
    RESEARCHOPS_PILOT_TOKEN_PEPPER_FILE = "/run/secrets/token_pepper"
    RESEARCHOPS_PILOT_REGISTRY_PATH = "/data/logical_dataset_registry.json"
    RESEARCHOPS_PILOT_PROJECT_ROOT = "/app/core"
}
Set-PilotEnvironmentValuesAtomic $preparation

$deploymentImageDigest = Prepare-PilotApplicationImages
Set-PilotEnvironmentValuesAtomic @{
    RESEARCHOPS_PILOT_DEPLOYMENT_IMAGE_DIGEST = $deploymentImageDigest
}

$environment = Read-PilotEnvironment
$configuration = Assert-SupervisedEnvironment $environment $tailscaleIdentity
$null = Assert-DeploymentGitIdentity $configuration.DeploymentGitSha
Invoke-PilotCompose -Arguments @(
    "--profile", "online", "up", "-d", "postgres", "migrate", "api", "worker"
)

Wait-PilotWorkerHeartbeat $environment $configuration.CandidateCommitment $ReadyTimeoutSeconds
Wait-PilotApiReady $ReadyTimeoutSeconds
$images = Assert-RunningPilotIdentity $configuration

[ordered]@{
    status = "services_ready"
    public_url = $configuration.PublicBase + "/pilot"
    candidate_commitment_sha256 = $configuration.CandidateCommitment
    deployment_git_sha = $configuration.DeploymentGitSha
    deployment_image_digest = $images.ApiImage
    environment_file_created = $envCreated
    environment_file_prepared_atomically = $true
    docker_engine = $docker.EngineVersion
    docker_compose = $docker.ComposeVersion
    secure_cookies = $true
    provider_execution_enabled = $true
    provider_secret_present = $secrets.ProviderSecretPresent
    secret_values_printed = $false
} | ConvertTo-Json

if ($BackgroundFunnel) {
    & $tailscaleExecutable funnel --bg --yes --https=443 $script:PilotLocalTarget
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to start Tailscale Funnel. Pilot services remain local-only."
    }
    Wait-PilotFunnelActive
    Write-Output "Funnel is running in the background. Stop it with services/pilot_staging/scripts/stop-supervised.ps1."
    exit 0
}

Write-Output "Starting foreground HTTPS Funnel. Press Ctrl+C to close the public route."
Write-Output "Run stop-supervised.ps1 afterward to stop containers while preserving the PostgreSQL volume."
try {
    & $tailscaleExecutable funnel --https=443 $script:PilotLocalTarget
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale Funnel exited with an error."
    }
}
finally {
    & $tailscaleExecutable funnel --https=443 off 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Foreground Funnel ended, but route removal was not confirmed. Run stop-supervised.ps1 immediately."
    }
}
