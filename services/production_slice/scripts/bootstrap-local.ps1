[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$serviceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$envTemplate = Join-Path $serviceRoot ".env.example"
$envTarget = Join-Path $serviceRoot ".env"
$secretsTarget = Join-Path $serviceRoot "secrets"
$secretNames = @(
    "postgres_password.txt",
    "minio_access_key.txt",
    "minio_secret_key.txt",
    "api_token.txt",
    "idempotency_hmac_key.txt"
)

if (-not (Test-Path -LiteralPath $envTemplate -PathType Leaf)) {
    throw "Missing .env.example."
}
if (Test-Path -LiteralPath $envTarget) {
    throw ".env already exists; bootstrap will not overwrite it."
}
if (Test-Path -LiteralPath $secretsTarget) {
    throw "secrets directory already exists; bootstrap will not overwrite it."
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

function Write-SecretCreateNew([string]$Path, [string]$Value) {
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($Value)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

$security = [Security.AccessControl.DirectorySecurity]::new()
$security.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
$propagation = [Security.AccessControl.PropagationFlags]::None
$allow = [Security.AccessControl.AccessControlType]::Allow
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$allowedSids = @($currentSid, $systemSid)
if ($env:COMPUTERNAME -and $env:USERNAME) {
    $interactiveAccount = [Security.Principal.NTAccount]::new($env:COMPUTERNAME, $env:USERNAME)
    $interactiveSid = $interactiveAccount.Translate([Security.Principal.SecurityIdentifier])
    $allowedSids += $interactiveSid
}
foreach ($sid in @($allowedSids | Sort-Object -Property Value -Unique)) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        $propagation,
        $allow
    )
    [void]$security.AddAccessRule($rule)
}

$directory = [IO.Directory]::CreateDirectory($secretsTarget)
$directory.SetAccessControl($security)
[IO.File]::Copy($envTemplate, $envTarget, $false)

$values = [ordered]@{
    "postgres_password.txt" = ConvertTo-Base64Url (New-RandomBytes 32)
    "minio_access_key.txt" = -join ((New-RandomBytes 12) | ForEach-Object { $_.ToString("X2") })
    "minio_secret_key.txt" = ConvertTo-Base64Url (New-RandomBytes 32)
    "api_token.txt" = ConvertTo-Base64Url (New-RandomBytes 32)
    "idempotency_hmac_key.txt" = ConvertTo-Base64Url (New-RandomBytes 48)
}

foreach ($name in $secretNames) {
    Write-SecretCreateNew -Path (Join-Path $secretsTarget $name) -Value $values[$name]
}

$values.Clear()
[GC]::Collect()

[ordered]@{
    status = "created"
    env_file = ".env"
    secret_files = $secretNames
    values_printed = $false
    overwrite_allowed = $false
} | ConvertTo-Json
