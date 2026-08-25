Set-StrictMode -Version Latest

$script:PilotServiceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script:PilotProjectRoot = [IO.Path]::GetFullPath((Join-Path $script:PilotServiceRoot "..\.."))
$script:PilotComposeFile = Join-Path $script:PilotServiceRoot "compose.yaml"
$script:PilotEnvFile = Join-Path $script:PilotServiceRoot ".env"
$script:PilotRegistryFile = Join-Path $script:PilotProjectRoot "artifacts\self_pilot_data\run-01\logical_dataset_registry.json"
$script:PilotCandidateCommitment = "22c985e9cf264df127be42756f708ff5c14e63fe00e5a0d3883efb781c50b2a9"
$script:PilotLocalTarget = "http://127.0.0.1:8090"
$script:PilotFunnelPort = 443
$script:TailscaleExecutable = $null

function Get-TailscaleExecutable {
    param([switch]$Optional)
    if ($script:TailscaleExecutable -and
        (Test-Path -LiteralPath $script:TailscaleExecutable -PathType Leaf)) {
        return $script:TailscaleExecutable
    }
    $fromPath = Get-Command tailscale -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $fromPath -and $fromPath.Path) {
        $script:TailscaleExecutable = $fromPath.Path
        return $script:TailscaleExecutable
    }
    if (${env:ProgramFiles}) {
        $windowsDefault = Join-Path ${env:ProgramFiles} "Tailscale\tailscale.exe"
        if (Test-Path -LiteralPath $windowsDefault -PathType Leaf) {
            $script:TailscaleExecutable = $windowsDefault
            return $script:TailscaleExecutable
        }
    }
    if ($Optional) {
        return $null
    }
    throw "Tailscale CLI is not installed on PATH or under Program Files."
}

function Write-NewFileBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Ensure-PilotEnvironmentFile {
    if (Test-Path -LiteralPath $script:PilotEnvFile -PathType Leaf) {
        return $false
    }
    $example = Join-Path $script:PilotServiceRoot ".env.example"
    if (-not (Test-Path -LiteralPath $example -PathType Leaf)) {
        throw "Pilot .env.example is missing."
    }
    Write-NewFileBytes $script:PilotEnvFile ([IO.File]::ReadAllBytes($example))
    return $true
}

function Set-PilotEnvironmentValuesAtomic {
    param([Parameter(Mandatory = $true)][hashtable]$Updates)
    if (-not (Test-Path -LiteralPath $script:PilotEnvFile -PathType Leaf)) {
        throw "Pilot .env does not exist."
    }
    $seen = @{}
    $output = [Collections.Generic.List[string]]::new()
    foreach ($rawLine in [IO.File]::ReadAllLines($script:PilotEnvFile)) {
        $trimmed = $rawLine.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            $output.Add($rawLine)
            continue
        }
        $separator = $rawLine.IndexOf("=")
        if ($separator -le 0) {
            throw "Pilot .env contains an invalid line."
        }
        $name = $rawLine.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Z][A-Z0-9_]*$' -or $seen.ContainsKey($name)) {
            throw "Pilot .env contains an invalid or duplicate variable name."
        }
        $seen[$name] = $true
        if ($Updates.ContainsKey($name)) {
            $output.Add("$name=$([string]$Updates[$name])")
        }
        else {
            $output.Add($rawLine)
        }
    }
    foreach ($name in @($Updates.Keys | Sort-Object)) {
        if (-not $seen.ContainsKey($name)) {
            $output.Add("$name=$([string]$Updates[$name])")
        }
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($output -join "`n") + "`n")
    $temporary = Join-Path $script:PilotServiceRoot (".env." + [Guid]::NewGuid().ToString("N") + ".tmp")
    $backup = Join-Path $script:PilotServiceRoot (".env." + [Guid]::NewGuid().ToString("N") + ".bak")
    try {
        Write-NewFileBytes $temporary $bytes
        [IO.File]::Replace($temporary, $script:PilotEnvFile, $backup, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            [IO.File]::Delete($temporary)
        }
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            [IO.File]::Delete($backup)
        }
    }
}

function Read-PilotEnvironment {
    if (-not (Test-Path -LiteralPath $script:PilotEnvFile -PathType Leaf)) {
        throw "Missing services/pilot_staging/.env. Create the supervised configuration first."
    }
    $values = @{}
    foreach ($rawLine in [IO.File]::ReadAllLines($script:PilotEnvFile)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            throw "Pilot .env contains an invalid line."
        }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($name -notmatch '^[A-Z][A-Z0-9_]*$' -or $values.ContainsKey($name)) {
            throw "Pilot .env contains an invalid or duplicate variable name."
        }
        $values[$name] = $value
    }
    return $values
}

function Get-RequiredPilotValue {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not $Environment.ContainsKey($Name) -or -not [string]$Environment[$Name]) {
        throw "Pilot .env is missing required variable $Name."
    }
    return [string]$Environment[$Name]
}

function Test-PilotTrue {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Equals("true", [StringComparison]::OrdinalIgnoreCase)
}

function Assert-DockerDesktopAndCompose {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is not installed or is not on PATH."
    }
    $engineVersion = (& docker version --format '{{.Server.Version}}' 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $engineVersion) {
        throw "Docker Desktop engine is not running."
    }
    $composeVersion = (& docker compose version --short 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $composeVersion) {
        throw "Docker Compose v2 is unavailable."
    }
    return [pscustomobject]@{
        EngineVersion = $engineVersion
        ComposeVersion = $composeVersion
    }
}

function Assert-WindowsAdministrator {
    if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [Runtime.InteropServices.OSPlatform]::Windows
    )) {
        return
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run start-supervised.ps1 from an Administrator PowerShell so the HTTPS Funnel can be controlled safely."
    }
}

function Get-TailscaleSupervisedIdentity {
    $tailscale = Get-TailscaleExecutable
    $rawStatus = (& $tailscale status --json 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $rawStatus) {
        throw "Tailscale status is unavailable; sign in before starting the pilot."
    }
    try {
        $status = $rawStatus | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "Tailscale returned invalid status JSON."
    }
    if ([string]$status.BackendState -ne "Running" -or $null -eq $status.Self) {
        throw "Tailscale is not logged in and running."
    }
    if ($status.Self.PSObject.Properties.Name -contains "Online" -and -not [bool]$status.Self.Online) {
        throw "This Tailscale node is offline."
    }
    $hostname = ([string]$status.Self.DNSName).Trim().TrimEnd('.').ToLowerInvariant()
    if ($hostname -notmatch '^[a-z0-9][a-z0-9.-]*\.ts\.net$') {
        throw "Tailscale did not report a valid HTTPS Funnel hostname."
    }
    $machineLabel = $hostname.Split('.')[0]
    if ($machineLabel -notmatch '^researchops-pilot(?:-[a-z0-9]+)?$') {
        throw "Rename the Tailscale machine to researchops-pilot before enabling public HTTPS; certificate names must not expose personal or organization names."
    }
    return [pscustomobject]@{
        Hostname = $hostname
        PublicBase = "https://$hostname"
    }
}

function Assert-SupervisedSecretsExist {
    $secretRoot = Join-Path $script:PilotServiceRoot "secrets"
    $required = @(
        "postgres_password.txt",
        "admin_token.txt",
        "token_pepper.txt",
        "provider_api_key.txt"
    )
    foreach ($name in $required) {
        $path = Join-Path $secretRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing required pilot secret file: $name"
        }
    }
    # Deliberately return names/count only. Secret file contents are never opened here.
    return [pscustomobject]@{
        Count = $required.Count
        ProviderSecretPresent = $true
    }
}

function Assert-SupervisedEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)]$TailscaleIdentity
    )
    if ((Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_ENVIRONMENT") -ne "supervised") {
        throw "RESEARCHOPS_PILOT_ENVIRONMENT must be supervised."
    }
    $publicBase = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_PUBLIC_BASE_URL"
    try {
        $uri = [Uri]$publicBase
    }
    catch {
        throw "RESEARCHOPS_PILOT_PUBLIC_BASE_URL is invalid."
    }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "https" -or $uri.Port -ne 443 -or
        $uri.AbsolutePath -ne "/" -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
        throw "Supervised public base must be an origin-only HTTPS URL on port 443."
    }
    if ($uri.Host.ToLowerInvariant() -ne $TailscaleIdentity.Hostname -or
        $publicBase.TrimEnd('/') -ne $TailscaleIdentity.PublicBase) {
        throw "Pilot public base must match this node's HTTPS Funnel hostname."
    }
    if (-not (Test-PilotTrue (Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_SECURE_COOKIES"))) {
        throw "Supervised pilot requires Secure cookies."
    }
    if (-not (Test-PilotTrue (Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED"))) {
        throw "Supervised pilot online execution flag is not enabled."
    }
    if (-not (Test-PilotTrue (Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_RETENTION_SCHEDULE_CONFIRMED"))) {
        throw "Supervised pilot requires a confirmed retention schedule."
    }
    $allowedHosts = @(
        (Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_ALLOWED_HOSTS").Split(',') |
            ForEach-Object { $_.Trim().ToLowerInvariant() } |
            Where-Object { $_ }
    )
    if ($allowedHosts -contains "*" -or
        $allowedHosts -notcontains $TailscaleIdentity.Hostname -or
        $allowedHosts -notcontains "127.0.0.1") {
        throw "Allowed hosts must contain the Funnel hostname and 127.0.0.1, without a wildcard."
    }
    $candidate = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_CANDIDATE_COMMITMENT_SHA256"
    if ($candidate -ne $script:PilotCandidateCommitment) {
        throw "Pilot candidate commitment does not match the locked candidate."
    }
    if ((Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_PROVIDER_ID") -ne "deepseek" -or
        (Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_MODEL_ID") -ne "deepseek-v4-flash") {
        throw "Pilot Provider/model does not match the locked candidate."
    }
    $gitSha = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DEPLOYMENT_GIT_SHA"
    $imageDigest = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DEPLOYMENT_IMAGE_DIGEST"
    if ($gitSha -notmatch '^[0-9a-f]{40}$') {
        throw "Deployment Git SHA must be a full lowercase commit SHA."
    }
    if ($imageDigest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Deployment image digest must be sha256:<64 lowercase hex>."
    }
    if (-not (Test-Path -LiteralPath $script:PilotRegistryFile -PathType Leaf)) {
        throw "Prepared logical dataset registry is missing."
    }
    if ($Environment.ContainsKey("RESEARCHOPS_PILOT_PROVIDER_API_KEY")) {
        throw "Provider credentials must be supplied by secret file, never inline in .env."
    }
    if ((Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_PROVIDER_API_KEY_FILE") -ne "/run/secrets/provider_api_key") {
        throw "Provider credential must use the controlled container secret-file path."
    }
    $expectedLocalValues = @{
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
        RESEARCHOPS_PILOT_MIGRATIONS_PATH = "/app/pilot/migrations"
    }
    foreach ($entry in $expectedLocalValues.GetEnumerator()) {
        if ((Get-RequiredPilotValue $Environment $entry.Key) -ne $entry.Value) {
            throw "Supervised local infrastructure binding is invalid: $($entry.Key)."
        }
    }
    return [pscustomobject]@{
        PublicBase = $TailscaleIdentity.PublicBase
        Hostname = $TailscaleIdentity.Hostname
        CandidateCommitment = $candidate
        DeploymentGitSha = $gitSha
        DeploymentImageDigest = $imageDigest
    }
}

function Assert-DeploymentGitIdentity {
    param([Parameter(Mandatory = $true)][string]$ExpectedGitSha)
    $actual = (& git -C $script:PilotProjectRoot rev-parse HEAD 2>$null | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $actual -ne $ExpectedGitSha) {
        throw "Current checkout does not match RESEARCHOPS_PILOT_DEPLOYMENT_GIT_SHA."
    }
    $dirty = (& git -C $script:PilotProjectRoot status --porcelain --untracked-files=normal 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw "Supervised deployment requires a clean Git worktree."
    }
    return $actual
}

function Get-CleanDeploymentGitSha {
    $actual = (& git -C $script:PilotProjectRoot rev-parse HEAD 2>$null | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $actual -notmatch '^[0-9a-f]{40}$') {
        throw "Unable to resolve the deployment Git commit."
    }
    $dirty = (& git -C $script:PilotProjectRoot status --porcelain --untracked-files=normal 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw "Supervised deployment requires a clean Git worktree."
    }
    return $actual
}

function Invoke-PilotCompose {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Capture
    )
    $commandArguments = @(
        "compose", "--file", $script:PilotComposeFile,
        "--project-directory", $script:PilotServiceRoot
    ) + $Arguments
    if ($Capture) {
        $output = (& docker @commandArguments 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Docker Compose command failed."
        }
        return $output
    }
    & docker @commandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed."
    }
}

function Get-PilotContainerId {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [switch]$Optional
    )
    $containerId = Invoke-PilotCompose -Arguments @("ps", "-q", $Service) -Capture
    if (-not $containerId -and -not $Optional) {
        throw "Pilot $Service container is not running."
    }
    return $containerId
}

function Get-PilotContainerState {
    param([Parameter(Mandatory = $true)][string]$ContainerId)
    if (-not $ContainerId) {
        return "absent"
    }
    $state = (& docker inspect --format '{{.State.Status}}' $ContainerId 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $state) {
        return "unknown"
    }
    return $state
}

function Get-PilotContainerImageId {
    param([Parameter(Mandatory = $true)][string]$ContainerId)
    $image = (& docker inspect --format '{{.Image}}' $ContainerId 2>$null | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $image -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Unable to verify the running pilot image ID."
    }
    return $image
}

function Get-PilotLocalImageId {
    param([Parameter(Mandatory = $true)][string]$Service)
    if ($Service -notin @("migrate", "api", "worker", "retention")) {
        throw "Unsupported pilot image service."
    }
    $imageName = "researchops-pilot-staging-$Service"
    $image = (& docker image inspect --format '{{.Id}}' $imageName 2>$null | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $image -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Local pilot image is missing for service $Service."
    }
    return $image
}

function Prepare-PilotApplicationImages {
    # Compose assigns a different local tag to every service even though all four use
    # the same Dockerfile and build context. Build exactly once, then bind every
    # application service tag to that one immutable image ID. A later `up` omits
    # `--build`, so Compose cannot silently replace any alias.
    $null = Invoke-PilotCompose -Arguments @("build", "migrate")
    $sourceImageName = "researchops-pilot-staging-migrate"
    foreach ($service in @("api", "worker", "retention")) {
        $targetImageName = "researchops-pilot-staging-$service"
        & docker image tag $sourceImageName $targetImageName
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to bind pilot $service to the immutable migrate image."
        }
    }
    $images = @(
        (Get-PilotLocalImageId "migrate"),
        (Get-PilotLocalImageId "api"),
        (Get-PilotLocalImageId "worker"),
        (Get-PilotLocalImageId "retention")
    )
    if (@($images | Select-Object -Unique).Count -ne 1) {
        throw "Pilot application services do not share one immutable image ID."
    }
    return $images[0]
}

function Test-PilotApiReady {
    try {
        $response = Invoke-RestMethod -Method Get -Uri "$($script:PilotLocalTarget)/health/ready" -TimeoutSec 5
        return [string]$response.status -eq "ready"
    }
    catch {
        return $false
    }
}

function Wait-PilotApiReady {
    param([ValidateRange(10, 600)][int]$TimeoutSeconds = 180)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-PilotApiReady) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Pilot API did not become ready before timeout."
}

function Test-PilotWorkerHeartbeat {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)][string]$CandidateCommitment
    )
    $databaseUser = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DATABASE_USER"
    $databaseName = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DATABASE_NAME"
    if ($databaseUser -notmatch '^[a-z_][a-z0-9_]{0,62}$' -or
        $databaseName -notmatch '^[a-z_][a-z0-9_]{0,62}$') {
        throw "Pilot database identifier is invalid."
    }
    $executionEnvironment = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_ENVIRONMENT"
    $imageDigest = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DEPLOYMENT_IMAGE_DIGEST"
    if ($executionEnvironment -notmatch '^(local|staging|supervised)$' -or
        $imageDigest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Pilot worker identity parameters are invalid."
    }
    $sql = "SELECT count(*) FROM pilot_worker_heartbeats WHERE candidate_commitment_sha256='$CandidateCommitment' AND execution_environment='$executionEnvironment' AND deployment_image_digest='$imageDigest' AND last_seen_at > now() - interval '360 seconds';"
    try {
        $countText = Invoke-PilotCompose -Arguments @(
            "exec", "-T", "postgres", "psql", "-U", $databaseUser,
            "-d", $databaseName, "-At", "-c", $sql
        ) -Capture
        $count = 0
        return [int]::TryParse($countText, [ref]$count) -and $count -gt 0
    }
    catch {
        return $false
    }
}

function Wait-PilotWorkerHeartbeat {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)][string]$CandidateCommitment,
        [ValidateRange(10, 600)][int]$TimeoutSeconds = 180
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-PilotWorkerHeartbeat $Environment $CandidateCommitment) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Pilot worker did not publish a recent locked-candidate heartbeat."
}

function Clear-PilotWorkerHeartbeats {
    param([Parameter(Mandatory = $true)][hashtable]$Environment)
    $databaseUser = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DATABASE_USER"
    $databaseName = Get-RequiredPilotValue $Environment "RESEARCHOPS_PILOT_DATABASE_NAME"
    if ($databaseUser -notmatch '^[a-z_][a-z0-9_]{0,62}$' -or
        $databaseName -notmatch '^[a-z_][a-z0-9_]{0,62}$') {
        throw "Pilot database identifier is invalid."
    }
    $null = Invoke-PilotCompose -Arguments @(
        "exec", "-T", "postgres", "psql", "-U", $databaseUser,
        "-d", $databaseName, "-v", "ON_ERROR_STOP=1", "-c",
        "DELETE FROM pilot_worker_heartbeats;"
    ) -Capture
}

function Assert-RunningPilotIdentity {
    param([Parameter(Mandatory = $true)]$SupervisedConfiguration)
    $apiId = Get-PilotContainerId "api"
    $workerId = Get-PilotContainerId "worker"
    if ((Get-PilotContainerState $apiId) -ne "running" -or
        (Get-PilotContainerState $workerId) -ne "running") {
        throw "Pilot API and worker must both be running."
    }
    $apiImage = Get-PilotContainerImageId $apiId
    $workerImage = Get-PilotContainerImageId $workerId
    if ($apiImage -ne $SupervisedConfiguration.DeploymentImageDigest -or
        $workerImage -ne $SupervisedConfiguration.DeploymentImageDigest) {
        throw "Running API/worker image does not match the committed deployment image digest."
    }
    return [pscustomobject]@{
        ApiImage = $apiImage
        WorkerImage = $workerImage
    }
}

function Test-PilotFunnelActive {
    $tailscale = Get-TailscaleExecutable
    $raw = (& $tailscale funnel status --json 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        return $false
    }
    return $raw.Contains("127.0.0.1:8090") -and
        ($raw.Contains("AllowFunnel") -or $raw -match '"Funnel"\s*:\s*true')
}

function Get-PilotFunnelTargetState {
    $tailscale = Get-TailscaleExecutable
    $raw = (& $tailscale funnel status --json 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -and -not $raw) {
        return "absent"
    }
    if ($LASTEXITCODE -ne 0) {
        return "unknown"
    }
    if (-not $raw -or $raw -eq "{}" -or $raw -eq "null") {
        return "absent"
    }
    if ($raw.Contains("127.0.0.1:8090")) {
        return "exact"
    }
    return "conflict"
}

function Assert-PilotFunnelNotConflicting {
    $tailscale = Get-TailscaleExecutable
    $raw = (& $tailscale funnel status --json 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -and -not $raw) {
        # A signed-in node with no Funnel configuration may report no JSON yet;
        # the first explicit `tailscale funnel` command will request approval.
        return
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the existing Tailscale Funnel configuration."
    }
    if (-not $raw -or $raw -eq "{}" -or $raw -eq "null") {
        return
    }
    if (-not $raw.Contains("127.0.0.1:8090")) {
        throw "This node already has a different Funnel configuration; refusing to replace it."
    }
}

function Wait-PilotFunnelActive {
    param([ValidateRange(5, 120)][int]$TimeoutSeconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-PilotFunnelActive) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Tailscale Funnel did not report the controlled pilot target."
}
