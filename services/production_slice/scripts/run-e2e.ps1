[CmdletBinding()]
param(
    [ValidateSet(
        "palmer_penguins_v0_1_0",
        "uci_parkinsons_telemonitoring_189",
        "uci_heart_disease_cleveland_45"
    )]
    [string]$DatasetId = "palmer_penguins_v0_1_0",

    [ValidateRange(30, 900)]
    [int]$TimeoutSeconds = 180,

    [switch]$SkipBuild,
    [switch]$StopAfter,
    [string]$OutputRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $temporary = "$Path.$(New-RandomHex 8).tmp"
    $encoding = [Text.UTF8Encoding]::new($false)
    try {
        [IO.File]::WriteAllText($temporary, $Content, $encoding)
        [IO.File]::Move($temporary, $Path)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-Json([string]$Path, $Value) {
    $content = ($Value | ConvertTo-Json -Depth 20) + "`n"
    Write-Utf8NoBom -Path $Path -Content $content
}

function Get-ComposeStatus([string]$ComposePath) {
    $containerIds = @(& docker compose -f $ComposePath ps -a -q 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "compose_status_command_failed"
    }
    $containerIds = @(
        $containerIds |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { $_ -match "^[0-9a-f]{12,64}$" }
    )
    if ($containerIds.Count -eq 0) {
        throw "compose_status_empty"
    }

    $format = '{{ .Name }}|{{ .State.Status }}|{{ if .State.Health }}{{ .State.Health.Status }}{{ end }}|{{ .State.ExitCode }}|{{ .Config.Image }}'
    $result = @()
    foreach ($containerId in $containerIds) {
        $line = (& docker inspect --format $format $containerId 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "container_inspect_failed"
        }
        $parts = @($line -split "\|", 5)
        if ($parts.Count -ne 5 -or [string]::IsNullOrWhiteSpace($parts[0])) {
            throw "container_inspect_output_invalid"
        }
        $containerName = $parts[0].TrimStart("/")
        if ($containerName -notmatch "^researchops-production-slice-(.+)-[0-9]+$") {
            throw "container_name_invalid"
        }
        $result += [ordered]@{
            service = $Matches[1]
            state = $parts[1]
            status = $parts[1]
            health = $parts[2]
            exit_code = [int]$parts[3]
            image = $parts[4]
        }
    }
    return $result
}

function Get-SanitizedDiagnosticLogs(
    [string]$ComposePath,
    [string]$SecretDirectory,
    [DateTime]$Since
) {
    $logs = (& docker compose -f $ComposePath logs --no-color --since $Since.ToString("yyyy-MM-ddTHH:mm:ssZ") api worker 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($logs)) {
        return $null
    }

    foreach ($file in Get-ChildItem -LiteralPath $SecretDirectory -File) {
        $secretValue = [IO.File]::ReadAllText($file.FullName).Trim()
        if ($secretValue) {
            $logs = $logs.Replace($secretValue, "[REDACTED_SECRET]")
        }
        $secretValue = $null
    }
    $logs = [regex]::Replace(
        $logs,
        "(?im)^.*(?:authorization|bearer\s+[A-Za-z0-9_-]{16,}|api[_-]?key\s*[:=]).*$",
        "[REDACTED_SENSITIVE_LOG_LINE]"
    )

    foreach ($file in Get-ChildItem -LiteralPath $SecretDirectory -File) {
        $secretValue = [IO.File]::ReadAllText($file.FullName).Trim()
        if ($secretValue -and $logs.Contains($secretValue)) {
            throw "diagnostic_log_redaction_failed"
        }
        $secretValue = $null
    }
    if (
        $logs -match "(?i)authorization" -or
        $logs -match "(?i)bearer\s+[A-Za-z0-9_-]{16,}" -or
        $logs -match "(?i)api[_-]?key\s*[:=]\s*\S+"
    ) {
        throw "diagnostic_log_redaction_failed"
    }

    $maximumCharacters = 65536
    if ($logs.Length -gt $maximumCharacters) {
        $logs = "[TRUNCATED_TO_LAST_64_KIB]`n" + $logs.Substring($logs.Length - $maximumCharacters)
    }
    return $logs
}

$dimensions = @{
    "palmer_penguins_v0_1_0" = @(344, 8)
    "uci_parkinsons_telemonitoring_189" = @(5875, 22)
    "uci_heart_disease_cleveland_45" = @(303, 14)
}

$serviceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$servicesRoot = [IO.Path]::GetFullPath((Join-Path $serviceRoot ".."))
$repoRoot = [IO.Path]::GetFullPath((Join-Path $servicesRoot ".."))
$artifactsRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot "artifacts"))
$composePath = Join-Path $serviceRoot "compose.yaml"
$envPath = Join-Path $serviceRoot ".env"
$secretDirectory = Join-Path $serviceRoot "secrets"
$apiTokenPath = Join-Path $secretDirectory "api_token.txt"

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path (Join-Path $artifactsRoot "production_slice") "e2e"
}
$outputParent = [IO.Path]::GetFullPath($OutputRoot)
if (-not $outputParent.StartsWith($artifactsRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputRoot must remain under the repository artifacts directory."
}
New-Item -ItemType Directory -Path $outputParent -Force | Out-Null

$startedAt = [DateTime]::UtcNow
$runId = "E2E-$($startedAt.ToString('yyyyMMddTHHmmssZ'))-$(New-RandomHex 4)"
$finalDirectory = Join-Path $outputParent $runId
$stagingDirectory = Join-Path $outputParent ".$runId.staging"
if ((Test-Path -LiteralPath $finalDirectory) -or (Test-Path -LiteralPath $stagingDirectory)) {
    throw "E2E output directory already exists; refusing overwrite."
}
New-Item -ItemType Directory -Path $stagingDirectory | Out-Null

$stage = "preflight"
$success = $false
$token = $null
$summary = $null
$composeStatus = @()
$safeErrorCode = $null
$created = $null
$job = $null
$result = $null
$reused = $null
$verification = $null
$traceNames = @()
$observedStatuses = @()
$diagnosticLogs = $null

try {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker_not_found"
    }
    & docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "docker_daemon_unavailable"
    }
    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        throw "compose_file_missing"
    }
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        throw "env_file_missing"
    }
    if (-not (Test-Path -LiteralPath $apiTokenPath -PathType Leaf)) {
        throw "api_token_file_missing"
    }
    & docker compose -f $composePath config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "compose_config_invalid"
    }

    $stage = "compose_up"
    if ($SkipBuild) {
        & docker compose -f $composePath up -d
    }
    else {
        & docker compose -f $composePath up --build -d
    }
    if ($LASTEXITCODE -ne 0) {
        throw "compose_up_failed"
    }

    $stage = "readiness"
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $ready = $null
    do {
        try {
            $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health/ready" -TimeoutSec 5
        }
        catch {
            $ready = $null
        }
        if ($null -ne $ready -and $ready.status -eq "ready") {
            break
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($null -eq $ready -or $ready.status -ne "ready") {
        throw "api_readiness_timeout"
    }

    $composeStatus = Get-ComposeStatus -ComposePath $composePath
    $expectedServices = @("api", "migrate", "minio", "otel-collector", "postgres", "worker")
    $observedServices = @($composeStatus | ForEach-Object { $_.service } | Sort-Object -Unique)
    if (($observedServices -join ",") -ne (($expectedServices | Sort-Object) -join ",")) {
        throw "compose_service_scope_mismatch"
    }
    $serviceFailures = @(
        $composeStatus | Where-Object {
            ($_.service -ne "migrate" -and $_.state -ne "running") -or
            ($_.service -eq "migrate" -and ($_.state -ne "exited" -or $_.exit_code -ne 0))
        }
    )
    if ($serviceFailures.Count -ne 0) {
        throw "compose_service_unhealthy"
    }

    $stage = "submit_job"
    $token = [IO.File]::ReadAllText($apiTokenPath).Trim()
    if ($token.Length -lt 24) {
        throw "api_token_invalid"
    }
    $idempotencyKey = "e2e-$(New-RandomHex 16)"
    $headers = @{
        Authorization = "Bearer $token"
        "Idempotency-Key" = $idempotencyKey
    }
    $body = @{ dataset_id = $DatasetId } | ConvertTo-Json -Compress
    $baseUri = "http://127.0.0.1:8080"
    $created = Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUri/v1/inspection-jobs" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 15
    if ($created.job_id -notmatch "^[0-9a-f-]{36}$") {
        throw "job_id_invalid"
    }
    $observedStatuses += [string]$created.status

    $stage = "wait_for_job"
    $jobDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $job = Invoke-RestMethod `
            -Uri "$baseUri/v1/inspection-jobs/$($created.job_id)" `
            -Headers @{ Authorization = "Bearer $token" } `
            -TimeoutSec 15
        if ($observedStatuses -notcontains [string]$job.status) {
            $observedStatuses += [string]$job.status
        }
        if ($job.status -in @("succeeded", "failed", "outcome_unknown")) {
            break
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $jobDeadline)
    if ($job.status -ne "succeeded") {
        throw "job_not_succeeded"
    }

    $stage = "verify_result"
    $result = Invoke-RestMethod `
        -Uri "$baseUri/v1/inspection-jobs/$($created.job_id)/result" `
        -Headers @{ Authorization = "Bearer $token" } `
        -TimeoutSec 15
    $reused = Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUri/v1/inspection-jobs" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 15
    $expectedRows = $dimensions[$DatasetId][0]
    $expectedColumns = $dimensions[$DatasetId][1]
    $contractValid = (
        $result.dataset_id -eq $DatasetId -and
        $result.result_type -eq "aggregate_dataset_profile" -and
        $result.profile.profile.row_count -eq $expectedRows -and
        $result.profile.profile.column_count -eq $expectedColumns -and
        $result.privacy.row_level_data_exposed -eq $false -and
        $result.privacy.filesystem_path_exposed -eq $false -and
        $job.artifact_sha256 -match "^[0-9a-f]{64}$" -and
        $job.artifact_bytes -gt 0 -and
        $reused.job_id -eq $created.job_id -and
        $reused.reused -eq $true
    )
    if (-not $contractValid) {
        throw "aggregate_contract_mismatch"
    }

    $stage = "verify_database_and_object"
    $python = @'
import json
from sqlalchemy import select
from researchops_service.config import Settings
from researchops_service.adapters.postgres import PostgresJobStore, job_events, _event_hash
from researchops_service.adapters.s3 import S3ObjectStore
settings=Settings()
store=PostgresJobStore(settings.database_url())
job=store.get("__JOB_ID__")
with store.engine.connect() as connection:
    rows=connection.execute(select(job_events).where(job_events.c.job_id==job.job_id).order_by(job_events.c.sequence)).mappings().all()
chain_valid=all(row["event_hash"]==_event_hash(job_id=row["job_id"],sequence=row["sequence"],event_type=row["event_type"],payload=row["payload"],previous_hash=row["previous_hash"]) and (row["sequence"]==0 or row["previous_hash"]==rows[row["sequence"]-1]["event_hash"]) for row in rows)
access,secret=settings.object_credentials()
objects=S3ObjectStore(endpoint_url=settings.object_endpoint,region_name=settings.object_region,bucket=settings.object_bucket,access_key=access,secret_key=secret,server_side_encryption=settings.object_server_side_encryption)
head=objects.head_json(object_key=job.expected_object_key)
trace_id=job.traceparent.split("-")[1] if job.traceparent else None
print(json.dumps({"event_count":len(rows),"event_sequence":[row["event_type"] for row in rows],"event_chain_head":rows[-1]["event_hash"] if rows else None,"event_hash_chain_valid":chain_valid,"deterministic_object_key_verified":job.expected_object_key==f"jobs/{job.job_id}/results/{job.artifact_sha256}.json","object_metadata_present":head is not None,"object_sha256_matches":head is not None and head.sha256==job.artifact_sha256,"object_bytes_match":head is not None and head.byte_size==job.artifact_bytes,"object_store_healthy":objects.healthcheck(),"server_side_encryption_enabled":settings.object_server_side_encryption,"trace_id":trace_id}))
store.close()
'@
    $python = $python.Replace("__JOB_ID__", [string]$created.job_id)
    $verificationRaw = $python | & docker compose -f $composePath exec -T api python -
    if ($LASTEXITCODE -ne 0) {
        throw "container_verification_failed"
    }
    $verification = $verificationRaw | ConvertFrom-Json
    if (
        -not $verification.event_hash_chain_valid -or
        -not $verification.deterministic_object_key_verified -or
        -not $verification.object_metadata_present -or
        -not $verification.object_sha256_matches -or
        -not $verification.object_bytes_match -or
        -not $verification.object_store_healthy -or
        $verification.event_sequence.Count -ne 4 -or
        ($verification.event_sequence -join ",") -ne "queued,claimed,publishing,succeeded"
    ) {
        throw "database_or_object_verification_failed"
    }

    $stage = "verify_telemetry"
    $traceDeadline = [DateTime]::UtcNow.AddSeconds(30)
    $collectorLogs = ""
    do {
        $collectorLogs = (& docker compose -f $composePath logs --no-color --since $startedAt.ToString("yyyy-MM-ddTHH:mm:ssZ") otel-collector | Out-String)
        $blocks = [regex]::Matches(
            $collectorLogs,
            "(?ms)Span #\d+.*?(?=otel-collector-1\s+\| Span #|otel-collector-1\s+\| ResourceSpans|\z)"
        )
        $traceNames = @()
        foreach ($blockMatch in $blocks) {
            $block = $blockMatch.Value
            $traceId = [regex]::Match($block, "Trace ID\s+:\s+([0-9a-f]+)").Groups[1].Value
            if ($traceId -eq $verification.trace_id) {
                $name = [regex]::Match($block, "Name\s+:\s+([^\r\n]+)").Groups[1].Value.Trim()
                if ($name -and $traceNames -notcontains $name) {
                    $traceNames += $name
                }
            }
        }
        if (
            $traceNames -contains "POST /v1/inspection-jobs" -and
            $traceNames -contains "inspection.execute"
        ) {
            break
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $traceDeadline)
    $traceMatch = (
        $traceNames -contains "POST /v1/inspection-jobs" -and
        $traceNames -contains "inspection.execute"
    )
    if (-not $traceMatch) {
        throw "cross_process_trace_not_observed"
    }

    $stage = "verify_log_secrecy"
    $allLogs = (& docker compose -f $composePath logs --no-color --since $startedAt.ToString("yyyy-MM-ddTHH:mm:ssZ") | Out-String)
    $leakedSecretFiles = @()
    foreach ($file in Get-ChildItem -LiteralPath $secretDirectory -File) {
        $secretValue = [IO.File]::ReadAllText($file.FullName).Trim()
        if ($secretValue -and $allLogs.Contains($secretValue)) {
            $leakedSecretFiles += $file.Name
        }
        $secretValue = $null
    }
    $authorizationPresent = $allLogs -match "(?i)authorization"
    $bearerPresent = $allLogs -match "(?i)bearer\s+[A-Za-z0-9_-]{16,}"
    $apiKeyPresent = $allLogs -match "(?i)api[_-]?key\s*[:=]\s*\S+"
    if (
        $leakedSecretFiles.Count -ne 0 -or
        $authorizationPresent -or
        $bearerPresent -or
        $apiKeyPresent
    ) {
        throw "secret_leak_detected"
    }

    $composeStatus = Get-ComposeStatus -ComposePath $composePath
    $completedAt = [DateTime]::UtcNow
    $summary = [ordered]@{
        schema_version = "1.0"
        status = "passed"
        run_id = $runId
        started_at_utc = $startedAt.ToString("o")
        completed_at_utc = $completedAt.ToString("o")
        duration_seconds = [math]::Round(($completedAt - $startedAt).TotalSeconds, 3)
        dataset_id = $DatasetId
        job = [ordered]@{
            job_id = $job.job_id
            initial_status = $created.status
            observed_api_statuses = $observedStatuses
            terminal_status = $job.status
            attempt_count = $job.attempt_count
            row_count = $result.profile.profile.row_count
            column_count = $result.profile.profile.column_count
            artifact_sha256_present = ($job.artifact_sha256 -match "^[0-9a-f]{64}$")
            artifact_bytes = $job.artifact_bytes
            row_level_data_exposed = $result.privacy.row_level_data_exposed
            filesystem_path_exposed = $result.privacy.filesystem_path_exposed
            idempotency_reused = $reused.reused
            same_job_id_on_reuse = ($reused.job_id -eq $created.job_id)
        }
        postgres = [ordered]@{
            event_count = $verification.event_count
            event_sequence = $verification.event_sequence
            event_hash_chain_valid = $verification.event_hash_chain_valid
            event_chain_head = $verification.event_chain_head
            deterministic_object_key_verified = $verification.deterministic_object_key_verified
        }
        object_storage = [ordered]@{
            metadata_present = $verification.object_metadata_present
            sha256_matches_database = $verification.object_sha256_matches
            byte_size_matches_database = $verification.object_bytes_match
            healthcheck = $verification.object_store_healthy
            server_side_encryption_enabled = $verification.server_side_encryption_enabled
        }
        telemetry = [ordered]@{
            trace_id = $verification.trace_id
            api_to_worker_trace_match = $traceMatch
            observed_span_names = $traceNames
        }
        security = [ordered]@{
            secret_values_printed = $false
            secret_value_leak_count = $leakedSecretFiles.Count
            authorization_present_in_logs = $authorizationPresent
            bearer_pattern_present_in_logs = $bearerPresent
            api_key_pattern_present_in_logs = $apiKeyPresent
        }
        model_calls = 0
        limitations = @(
            "Single-host development Compose evidence only.",
            "Local MinIO server-side encryption is disabled because no KMS is configured.",
            "No LLM, external publication, or approval recovery is exercised."
        )
    }
    $success = $true
}
catch {
    $caughtMessage = [string]$_.Exception.Message
    $safeErrorCode = if ($caughtMessage -match "^[a-z][a-z0-9_]{2,63}$") {
        $caughtMessage
    }
    else {
        "e2e_failed"
    }
    $completedAt = [DateTime]::UtcNow
    try {
        $composeStatus = Get-ComposeStatus -ComposePath $composePath
    }
    catch {
        $composeStatus = @()
    }
    try {
        $diagnosticLogs = Get-SanitizedDiagnosticLogs `
            -ComposePath $composePath `
            -SecretDirectory $secretDirectory `
            -Since $startedAt
    }
    catch {
        $diagnosticLogs = $null
    }
    $summary = [ordered]@{
        schema_version = "1.0"
        status = "failed"
        run_id = $runId
        started_at_utc = $startedAt.ToString("o")
        completed_at_utc = $completedAt.ToString("o")
        duration_seconds = [math]::Round(($completedAt - $startedAt).TotalSeconds, 3)
        dataset_id = $DatasetId
        failure_stage = $stage
        error_code = $safeErrorCode
        sanitized_diagnostics_persisted = ($null -ne $diagnosticLogs)
        secret_values_printed = $false
        model_calls = 0
    }
}
finally {
    $token = $null
    $headers = $null
    [GC]::Collect()

    if ($null -eq $summary) {
        $summary = [ordered]@{
            schema_version = "1.0"
            status = "failed"
            run_id = $runId
            failure_stage = "evidence_write"
            error_code = "e2e_failed"
            secret_values_printed = $false
            model_calls = 0
        }
    }
    Write-Json -Path (Join-Path $stagingDirectory "e2e_summary.json") -Value $summary
    Write-Json -Path (Join-Path $stagingDirectory "compose_status.json") -Value ([ordered]@{
        schema_version = "1.0"
        run_id = $runId
        services = $composeStatus
    })
    if ($null -ne $diagnosticLogs) {
        Write-Utf8NoBom `
            -Path (Join-Path $stagingDirectory "diagnostic_logs.txt") `
            -Content $diagnosticLogs
    }

    $verificationMarkdown = @"
# Production Slice E2E Verification

- Status: ``$($summary.status)``
- Run ID: ``$runId``
- Dataset: ``$DatasetId``
- Failure stage: ``$(if ($success) { 'none' } else { $stage })``
- Sanitized diagnostics persisted: ``$($null -ne $diagnosticLogs)``
- Secret values printed: ``false``
- Model calls: ``0``

The machine-readable evidence is in ``e2e_summary.json``. This run is a single-host
development Compose check, not HA, production SLA, cloud IAM/KMS/TLS, or load evidence.
"@
    Write-Utf8NoBom -Path (Join-Path $stagingDirectory "verification.md") -Content $verificationMarkdown

    $manifestFiles = @("e2e_summary.json", "compose_status.json", "verification.md")
    if ($null -ne $diagnosticLogs) {
        $manifestFiles += "diagnostic_logs.txt"
    }
    $fileEntries = @()
    foreach ($name in $manifestFiles) {
        $path = Join-Path $stagingDirectory $name
        $fileEntries += [ordered]@{
            file = $name
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            byte_size = (Get-Item -LiteralPath $path).Length
        }
    }
    Write-Json -Path (Join-Path $stagingDirectory "manifest.json") -Value ([ordered]@{
        schema_version = "1.0"
        run_id = $runId
        files = $fileEntries
        secrets_persisted = $false
        diagnostic_logs_sanitized = ($null -ne $diagnosticLogs)
        response_body_persisted = $false
        row_level_data_persisted = $false
    })

    [IO.Directory]::Move($stagingDirectory, $finalDirectory)

    if ($StopAfter) {
        & docker compose -f $composePath down
    }
}

$relativeOutput = $finalDirectory.Substring($repoRoot.Length).TrimStart([char[]]@("\", "/")).Replace("\", "/")
[ordered]@{
    status = $summary.status
    run_id = $runId
    dataset_id = $DatasetId
    job_id = if ($null -ne $job) { $job.job_id } else { $null }
    terminal_status = if ($null -ne $job) { $job.status } else { $null }
    error_code = if ($success) { $null } else { $safeErrorCode }
    output_directory = $relativeOutput
    stopped_after = [bool]$StopAfter
    secret_values_printed = $false
} | ConvertTo-Json

if (-not $success) {
    exit 1
}
