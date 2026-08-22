[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_supervised-common.ps1")

$dockerReady = $true
try {
    $null = Assert-DockerDesktopAndCompose
}
catch {
    $dockerReady = $false
    Write-Warning "Docker Desktop is unavailable; Funnel shutdown will still be attempted."
}

# Remove the reachable backend first even if Tailscale itself is unavailable.
if ($dockerReady) {
    try {
        Invoke-PilotCompose -Arguments @("--profile", "online", "stop", "worker")
        if (Test-Path -LiteralPath $script:PilotEnvFile -PathType Leaf) {
            Clear-PilotWorkerHeartbeats (Read-PilotEnvironment)
        }
        Invoke-PilotCompose -Arguments @("--profile", "online", "stop", "api")
    }
    catch {
        Write-Warning "API/worker stop could not be confirmed; continuing with Funnel shutdown."
    }
}

$funnelStopped = $false
$tailscaleExecutable = Get-TailscaleExecutable -Optional
if ($null -ne $tailscaleExecutable) {
    $funnelState = Get-PilotFunnelTargetState
    if ($funnelState -eq "exact") {
        & $tailscaleExecutable funnel --https=443 off 2>$null
        $funnelStopped = $LASTEXITCODE -eq 0
    }
    elseif ($funnelState -eq "absent") {
        $funnelStopped = $true
    }
    else {
        Write-Warning "Port 443 Funnel is not the ResearchOps 127.0.0.1:8090 route; refusing to modify it."
    }
}

# Volumes are intentionally preserved; teardown removes containers and networks only.
if ($dockerReady) {
    Invoke-PilotCompose -Arguments @("--profile", "online", "down", "--remove-orphans")
}

[ordered]@{
    status = "stopped"
    funnel_stop_confirmed = $funnelStopped
    containers_removed = $dockerReady
    postgres_volume_deleted = $false
    secret_values_printed = $false
} | ConvertTo-Json

if (-not $funnelStopped) {
    Write-Warning "ResearchOps Funnel shutdown was not confirmed; inspect 'tailscale funnel status --json' before taking action."
}
if (-not $dockerReady) {
    Write-Warning "When Docker Desktop is available, rerun this script to remove stopped containers without deleting volumes."
}
