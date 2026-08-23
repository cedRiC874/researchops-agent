[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:CI -ne "true") {
    throw "bootstrap-ci.ps1 may run only when CI=true."
}

$serviceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$servicesRoot = [IO.Path]::GetFullPath((Join-Path $serviceRoot ".."))
$projectRoot = [IO.Path]::GetFullPath((Join-Path $servicesRoot ".."))
$envPath = Join-Path $serviceRoot ".env"
$secretRoot = Join-Path $serviceRoot "secrets"
$artifactsRoot = Join-Path $projectRoot "artifacts"
$selfPilotDataRoot = Join-Path $artifactsRoot "self_pilot_data"
$datasetRoot = Join-Path $selfPilotDataRoot "run-01"
$evalsRoot = Join-Path $projectRoot "evals"
$evalV2Root = Join-Path $evalsRoot "v2"
$manifestPath = Join-Path $evalV2Root "external_datasets.json"
$candidateCommitment = "1f6ac18e1cf4756e2a3ebd34075d2e98f8ab4dd98b316754f4af8b74c7be5ce5"

foreach ($target in @($envPath, $secretRoot, $datasetRoot)) {
    if (Test-Path -LiteralPath $target) {
        throw "CI bootstrap target already exists; refusing overwrite."
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
    try {
        $writer.Write($Value)
    }
    finally {
        $writer.Dispose()
    }
}

function New-RandomSecretText {
    param([ValidateRange(24, 128)][int]$ByteCount)
    return [Convert]::ToHexString(
        [Security.Cryptography.RandomNumberGenerator]::GetBytes($ByteCount)
    ).ToLowerInvariant()
}

function Protect-SecretFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [Runtime.InteropServices.OSPlatform]::Windows
    )) {
        [IO.File]::SetUnixFileMode(
            $Path,
            [IO.UnixFileMode]::UserRead -bor
                [IO.UnixFileMode]::GroupRead -bor
                [IO.UnixFileMode]::OtherRead
        )
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([IO.File]::ReadAllBytes($Path))
    ).ToLowerInvariant()
}

New-Item -ItemType Directory -Path $secretRoot -Force | Out-Null
New-Item -ItemType Directory -Path $datasetRoot -Force | Out-Null

$secretNames = @("postgres_password.txt", "admin_token.txt", "token_pepper.txt")
foreach ($name in $secretNames) {
    $secretPath = Join-Path $secretRoot $name
    Write-Utf8NoBom $secretPath (New-RandomSecretText 32)
    Protect-SecretFile $secretPath
}

$csvFixtures = [ordered]@{
    "palmer_penguins_v0_1_0.csv" = @(
        "species,island,bill_length_mm,bill_depth_mm,flipper_length_mm,body_mass_g,sex,year",
        "Adelie,Torgersen,39.1,18.7,181,3750,male,2007",
        "Adelie,Dream,38.2,18.1,185,3950,female,2008",
        "Gentoo,Biscoe,46.1,13.2,211,4500,female,2009",
        "Chinstrap,Dream,50.0,19.5,196,3900,male,2008"
    ) -join "`n"
    "uci_parkinsons_telemonitoring_189.csv" = @(
        "subject_key,age,sex,test_time,motor_updrs,total_updrs,jitter_percent,jitter_abs,jitter_rap,jitter_ppq5,jitter_ddp,shimmer,shimmer_db,shimmer_apq3,shimmer_apq5,shimmer_apq11,shimmer_dda,nhr,hnr,rpde,dfa,ppe",
        "subject-a,72,0,5.6,28.4,34.3,0.0066,0.000034,0.0040,0.0032,0.0120,0.043,0.42,0.024,0.027,0.036,0.071,0.021,21.6,0.42,0.55,0.24",
        "subject-a,72,0,12.7,28.8,35.1,0.0061,0.000032,0.0037,0.0030,0.0111,0.041,0.40,0.023,0.026,0.035,0.069,0.020,21.9,0.41,0.56,0.23",
        "subject-b,65,1,4.5,19.1,24.2,0.0054,0.000028,0.0031,0.0028,0.0093,0.038,0.37,0.021,0.024,0.032,0.063,0.018,22.4,0.39,0.58,0.21",
        "subject-b,65,1,11.4,19.6,25.0,0.0052,0.000027,0.0030,0.0027,0.0090,0.037,0.36,0.020,0.023,0.031,0.061,0.017,22.7,0.38,0.59,0.20"
    ) -join "`n"
    "uci_heart_disease_cleveland_45.csv" = @(
        "age,sex,chest_pain_type,resting_blood_pressure,cholesterol,fasting_blood_sugar_high,resting_ecg,maximum_heart_rate,exercise_induced_angina,st_depression,st_slope,major_vessels,thalassemia,heart_disease_class",
        "63,1,1,145,233,1,2,150,0,2.3,3,0,6,0",
        "67,1,4,160,286,0,2,108,1,1.5,2,3,3,2",
        "54,0,3,135,250,0,0,160,0,0.0,1,,3,0",
        "59,1,4,140,177,0,0,162,1,0.0,1,1,7,1"
    ) -join "`n"
}

foreach ($fixture in $csvFixtures.GetEnumerator()) {
    Write-Utf8NoBom (Join-Path $datasetRoot $fixture.Key) ($fixture.Value + "`n")
}

$manifestSha = Get-Sha256Hex $manifestPath

function New-RegistryEntry {
    param(
        [string]$DatasetId,
        [string]$RelativePath,
        [int]$RowCount,
        [int]$ColumnCount,
        [string]$Domain,
        [bool]$RepeatedSubjects,
        [string]$PrivacyClass,
        [string[]]$Boundaries
    )
    $path = Join-Path $datasetRoot $RelativePath
    $sha = Get-Sha256Hex $path
    return [ordered]@{
        dataset_id = $DatasetId
        relative_path = $RelativePath
        prepared_sha256 = $sha
        prepared_bytes = [IO.FileInfo]::new($path).Length
        row_count = $RowCount
        column_count = $ColumnCount
        domain = $Domain
        repeated_subjects = $RepeatedSubjects
        privacy_class = $PrivacyClass
        model_access = "aggregate_tools_only"
        preparation_version = "1.0"
        source_asset_sha256 = $sha
        transformations = @("deterministic_synthetic_ci_fixture_only")
        analysis_boundaries = $Boundaries
    }
}

$registry = [ordered]@{
    schema_version = "1.0"
    registry_id = "researchops-eval-v2-logical-datasets-v1"
    dataset_manifest_sha256 = $manifestSha
    entries = @(
        (New-RegistryEntry "palmer_penguins_v0_1_0" "palmer_penguins_v0_1_0.csv" 4 8 "ecology_observational_morphometrics" $false "public_animal_observation" @("observational_not_randomized", "missing_values_present")),
        (New-RegistryEntry "uci_parkinsons_telemonitoring_189" "uci_parkinsons_telemonitoring_189.csv" 4 22 "health_repeated_measure_telemonitoring" $true "public_health_pseudonymized" @("repeated_measurements_within_subject", "rows_are_not_independent")),
        (New-RegistryEntry "uci_heart_disease_cleveland_45" "uci_heart_disease_cleveland_45.csv" 4 14 "health_observational_classification" $false "public_health_deidentified" @("classification_outcome_not_continuous", "missing_values_present"))
    )
}
Write-Utf8NoBom (Join-Path $datasetRoot "logical_dataset_registry.json") (($registry | ConvertTo-Json -Depth 8) + "`n")

$envText = @(
    "RESEARCHOPS_PILOT_ENVIRONMENT=local",
    "RESEARCHOPS_PILOT_PUBLIC_BASE_URL=http://127.0.0.1:8090",
    "RESEARCHOPS_PILOT_ALLOWED_HOSTS=127.0.0.1,localhost,testserver",
    "RESEARCHOPS_PILOT_SECURE_COOKIES=false",
    "RESEARCHOPS_PILOT_DATABASE_HOST=postgres",
    "RESEARCHOPS_PILOT_DATABASE_PORT=5432",
    "RESEARCHOPS_PILOT_DATABASE_NAME=researchops_pilot",
    "RESEARCHOPS_PILOT_DATABASE_USER=researchops_pilot",
    "RESEARCHOPS_PILOT_DATABASE_PASSWORD_FILE=/run/secrets/postgres_password",
    "RESEARCHOPS_PILOT_DATABASE_SSLMODE=disable",
    "RESEARCHOPS_PILOT_ADMIN_TOKEN_FILE=/run/secrets/admin_token",
    "RESEARCHOPS_PILOT_TOKEN_PEPPER_FILE=/run/secrets/token_pepper",
    "RESEARCHOPS_PILOT_PROVIDER_API_KEY_FILE=/run/secrets/provider_api_key",
    "RESEARCHOPS_PILOT_REGISTRY_PATH=/data/logical_dataset_registry.json",
    "RESEARCHOPS_PILOT_PROJECT_ROOT=/app/core",
    "RESEARCHOPS_PILOT_MIGRATIONS_PATH=/app/pilot/migrations",
    "RESEARCHOPS_PILOT_PROVIDER_EXECUTION_ENABLED=false",
    "RESEARCHOPS_PILOT_CANDIDATE_COMMITMENT_SHA256=$candidateCommitment",
    "RESEARCHOPS_PILOT_PROVIDER_ID=deepseek",
    "RESEARCHOPS_PILOT_MODEL_ID=deepseek-v4-flash",
    "RESEARCHOPS_PILOT_WORKER_POLL_SECONDS=1.0",
    "RESEARCHOPS_PILOT_WORKER_LEASE_SECONDS=300",
    "RESEARCHOPS_PILOT_SESSION_TTL_HOURS=8",
    "RESEARCHOPS_PILOT_RETENTION_DAYS=90",
    "RESEARCHOPS_PILOT_RETENTION_SCHEDULE_CONFIRMED=false",
    "RESEARCHOPS_PILOT_DEPLOYMENT_GIT_SHA=",
    "RESEARCHOPS_PILOT_DEPLOYMENT_IMAGE_DIGEST="
) -join "`n"
Write-Utf8NoBom $envPath ($envText + "`n")

[ordered]@{
    status = "created"
    environment = "local"
    non_provider_secret_file_count = $secretNames.Count
    provider_secret_created = $false
    synthetic_dataset_count = $csvFixtures.Count
    registry_entry_count = $registry.entries.Count
    secret_values_printed = $false
} | ConvertTo-Json
