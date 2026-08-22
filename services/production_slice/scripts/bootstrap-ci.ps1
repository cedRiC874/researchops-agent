[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:CI -ne "true") {
    throw "bootstrap-ci.ps1 may only run when CI=true."
}

function New-RandomBytes([int]$Count) {
    $bytes = [byte[]]::new($Count)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return $bytes
}

function ConvertTo-Base64Url([byte[]]$Bytes) {
    return [Convert]::ToBase64String($Bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function ConvertTo-Hex([byte[]]$Bytes) {
    return -join ($Bytes | ForEach-Object { $_.ToString("x2") })
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = [Text.UTF8Encoding]::new($false)
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $bytes = $encoding.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

$serviceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$servicesRoot = [IO.Path]::GetFullPath((Join-Path $serviceRoot ".."))
$repoRoot = [IO.Path]::GetFullPath((Join-Path $servicesRoot ".."))
$envTemplate = Join-Path $serviceRoot ".env.example"
$envTarget = Join-Path $serviceRoot ".env"
$secretsTarget = Join-Path $serviceRoot "secrets"
$dataTarget = Join-Path (Join-Path (Join-Path $repoRoot "artifacts") "self_pilot_data") "run-01"

foreach ($target in @($envTarget, $secretsTarget, $dataTarget)) {
    if (Test-Path -LiteralPath $target) {
        throw "CI bootstrap target already exists; refusing overwrite."
    }
}
if (-not (Test-Path -LiteralPath $envTemplate -PathType Leaf)) {
    throw "Missing .env.example."
}

[IO.File]::Copy($envTemplate, $envTarget, $false)
New-Item -ItemType Directory -Path $secretsTarget | Out-Null
New-Item -ItemType Directory -Path $dataTarget -Force | Out-Null

$secretValues = [ordered]@{
    "postgres_password.txt" = ConvertTo-Base64Url (New-RandomBytes 32)
    "minio_access_key.txt" = (ConvertTo-Hex (New-RandomBytes 12)).ToUpperInvariant()
    "minio_secret_key.txt" = ConvertTo-Base64Url (New-RandomBytes 32)
    "api_token.txt" = ConvertTo-Base64Url (New-RandomBytes 32)
    "idempotency_hmac_key.txt" = ConvertTo-Base64Url (New-RandomBytes 48)
}
foreach ($entry in $secretValues.GetEnumerator()) {
    Write-Utf8NoBom -Path (Join-Path $secretsTarget $entry.Key) -Content $entry.Value
}
if (-not $IsWindows) {
    $composeSecretMode = (
        [IO.UnixFileMode]::UserRead -bor
        [IO.UnixFileMode]::GroupRead -bor
        [IO.UnixFileMode]::OtherRead
    )
    foreach ($entry in $secretValues.GetEnumerator()) {
        [IO.File]::SetUnixFileMode(
            (Join-Path $secretsTarget $entry.Key),
            $composeSecretMode
        )
    }
}

$datasetId = "palmer_penguins_v0_1_0"
$csvName = "$datasetId.csv"
$csvPath = Join-Path $dataTarget $csvName
$rows = [Collections.Generic.List[string]]::new()
$rows.Add("species,island,bill_length_mm,bill_depth_mm,flipper_length_mm,body_mass_g,sex,year")
$species = @("Adelie", "Chinstrap", "Gentoo")
$islands = @("Biscoe", "Dream", "Torgersen")
$sexes = @("female", "male")
$culture = [Globalization.CultureInfo]::InvariantCulture
for ($index = 0; $index -lt 344; $index++) {
    $billLength = [string]::Format($culture, "{0:F1}", 35.0 + (($index * 7) % 180) / 10.0)
    $billDepth = [string]::Format($culture, "{0:F1}", 13.0 + (($index * 5) % 80) / 10.0)
    $flipper = 170 + (($index * 3) % 65)
    $mass = 2800 + (($index * 37) % 3600)
    $year = 2007 + ($index % 3)
    $rows.Add("$($species[$index % 3]),$($islands[$index % 3]),$billLength,$billDepth,$flipper,$mass,$($sexes[$index % 2]),$year")
}
Write-Utf8NoBom -Path $csvPath -Content (($rows -join "`n") + "`n")

$csvItem = Get-Item -LiteralPath $csvPath
$csvSha256 = (Get-FileHash -LiteralPath $csvPath -Algorithm SHA256).Hash.ToLowerInvariant()
$manifestSeed = [Text.Encoding]::UTF8.GetBytes("researchops-ci-synthetic-palmer-v1")
$hashAlgorithm = [Security.Cryptography.SHA256]::Create()
try {
    $manifestSha256 = ConvertTo-Hex ($hashAlgorithm.ComputeHash($manifestSeed))
}
finally {
    $hashAlgorithm.Dispose()
}

$registry = [ordered]@{
    schema_version = "1.0"
    registry_id = "researchops-e2e-ci-registry-v1"
    dataset_manifest_sha256 = $manifestSha256
    entries = @(
        [ordered]@{
            dataset_id = $datasetId
            relative_path = $csvName
            prepared_sha256 = $csvSha256
            prepared_bytes = $csvItem.Length
            row_count = 344
            column_count = 8
            source_asset_sha256 = $csvSha256
            preparation_version = "1.0"
            privacy_class = "synthetic_ci_fixture"
            model_access = "aggregate_tools_only"
            domain = "ecology_synthetic_ci"
            repeated_subjects = $false
            analysis_boundaries = @("Synthetic CI fixture; aggregate-only contract testing.")
            transformations = @("deterministic_ci_fixture_generation")
        }
    )
}
$registryPath = Join-Path $dataTarget "logical_dataset_registry.json"
Write-Utf8NoBom -Path $registryPath -Content (($registry | ConvertTo-Json -Depth 10) + "`n")

if ($IsLinux -or $IsMacOS) {
    & chmod 600 $envTarget
    & chmod 700 $secretsTarget
    foreach ($secretFile in Get-ChildItem -LiteralPath $secretsTarget -File) {
        & chmod 600 $secretFile.FullName
    }
}

$secretValues.Clear()
[GC]::Collect()

[ordered]@{
    status = "created"
    environment = "ci"
    dataset_id = $datasetId
    row_count = 344
    column_count = 8
    secret_values_printed = $false
    secret_files = @(
        "postgres_password.txt",
        "minio_access_key.txt",
        "minio_secret_key.txt",
        "api_token.txt",
        "idempotency_hmac_key.txt"
    )
} | ConvertTo-Json
