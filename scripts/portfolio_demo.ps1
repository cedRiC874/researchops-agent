<#
.SYNOPSIS
Runs the fully offline ResearchOps Agent portfolio demo on Windows.

.DESCRIPTION
This is a thin PowerShell wrapper around scripts/portfolio_demo.py. The shared
Python implementation owns corpus validation, the pinned numerical baseline,
artifact verification and overwrite protection while selecting the explicitly
frozen Windows or Linux evidence lineage.

.PARAMETER OutputDirectory
Optional new child directory under artifacts.

.PARAMETER PythonPath
Python executable. Defaults to .venv\Scripts\python.exe.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\scripts\portfolio_demo.ps1
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$OutputDirectory,

    [Parameter()]
    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        $pythonExecutable = Join-Path $repoRoot ".venv\Scripts\python.exe"
    }
    elseif ([System.IO.Path]::IsPathRooted($PythonPath)) {
        $pythonExecutable = [System.IO.Path]::GetFullPath($PythonPath)
    }
    else {
        $pythonExecutable = [System.IO.Path]::GetFullPath(
            (Join-Path $repoRoot $PythonPath)
        )
    }
    if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw "Python executable not found: $pythonExecutable"
    }

    $demoArguments = @(Join-Path $repoRoot "scripts\portfolio_demo.py")
    if (-not [string]::IsNullOrWhiteSpace($OutputDirectory)) {
        $demoArguments += @("--output-dir", $OutputDirectory)
    }

    $nativeErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5 may promote successful native stderr diagnostics
        # to ErrorRecord. Stream both channels and trust the process exit code.
        $ErrorActionPreference = "Continue"
        & $pythonExecutable @demoArguments 2>&1 |
            ForEach-Object { Write-Host ([string]$_) }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $nativeErrorPreference
    }
    if ($exitCode -ne 0) {
        throw "Shared portfolio demo failed with exit code $exitCode."
    }
}
catch {
    Write-Error ("Portfolio demo failed: {0}" -f $_.Exception.Message) `
        -ErrorAction Continue
    exit 1
}
