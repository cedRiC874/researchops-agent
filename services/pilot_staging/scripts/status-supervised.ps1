[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_supervised-common.ps1")

$docker = Assert-DockerDesktopAndCompose
$tailscale = Get-TailscaleSupervisedIdentity
$environment = Read-PilotEnvironment
$secrets = Assert-SupervisedSecretsExist
$configuration = Assert-SupervisedEnvironment $environment $tailscale

$apiId = Get-PilotContainerId "api" -Optional
$workerId = Get-PilotContainerId "worker" -Optional
$apiState = Get-PilotContainerState $apiId
$workerState = Get-PilotContainerState $workerId
$apiImage = if ($apiState -eq "running") { Get-PilotContainerImageId $apiId } else { $null }
$workerImage = if ($workerState -eq "running") { Get-PilotContainerImageId $workerId } else { $null }
$apiReady = Test-PilotApiReady
$workerReady = Test-PilotWorkerHeartbeat $environment $configuration.CandidateCommitment
$funnelActive = Test-PilotFunnelActive

[ordered]@{
    schema_version = "supervised-status/1.0"
    public_url = $configuration.PublicBase + "/pilot"
    funnel_active = $funnelActive
    api_container_status = $apiState
    worker_container_status = $workerState
    api_ready = $apiReady
    worker_locked_candidate_heartbeat = $workerReady
    candidate_commitment_sha256 = $configuration.CandidateCommitment
    deployment_git_sha = $configuration.DeploymentGitSha
    expected_image_digest = $configuration.DeploymentImageDigest
    api_image_digest = $apiImage
    worker_image_digest = $workerImage
    image_identity_matches = ($apiImage -eq $configuration.DeploymentImageDigest -and $workerImage -eq $configuration.DeploymentImageDigest)
    docker_engine = $docker.EngineVersion
    docker_compose = $docker.ComposeVersion
    secure_cookies = $true
    provider_execution_enabled = $true
    provider_secret_present = $secrets.ProviderSecretPresent
    secret_values_printed = $false
} | ConvertTo-Json
