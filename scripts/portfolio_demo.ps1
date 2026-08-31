<#
.SYNOPSIS
运行 ResearchOps Agent 的完全离线作品集演示。

.DESCRIPTION
校验 Python 环境和两阶段评测语料，运行固定 50 题离线评测，并独立验证
产物哈希、审计链和脱敏规则。脚本不调用在线评测入口，不读取任何凭据。

.PARAMETER OutputDirectory
新的评测产物目录。相对路径按项目根目录解析，且必须位于 artifacts 下。
省略时自动生成唯一目录；任何已存在的目标都会被拒绝，不会覆盖。

.PARAMETER PythonPath
Python 可执行文件。相对路径按项目根目录解析；默认使用 .venv\Scripts\python.exe。

.EXAMPLE
pwsh -File .\scripts\portfolio_demo.ps1

.EXAMPLE
pwsh -File .\scripts\portfolio_demo.ps1 -OutputDirectory artifacts\portfolio_demo_interview
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

function Write-Section {
    param([Parameter(Mandatory)][string]$Title)

    Write-Host ""
    Write-Host ("=== {0} ===" -f $Title) -ForegroundColor Cyan
}

function Invoke-PythonStep {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$ShowOutput
    )

    Write-Section $Title
    $stepErrorActionPreference = $ErrorActionPreference
    try {
        # A successful native process may emit a warning on stderr. Windows
        # PowerShell 5 promotes that stream to an ErrorRecord when the global
        # preference is Stop, so capture it first and judge success by exit code.
        $ErrorActionPreference = "Continue"
        $capturedOutput = @(& $script:PythonExecutable @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $stepErrorActionPreference
    }
    $renderedOutput = ($capturedOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine

    if ($exitCode -ne 0) {
        if (-not [string]::IsNullOrWhiteSpace($renderedOutput)) {
            Write-Host $renderedOutput
        }
        throw ("步骤 [{0}] 失败（Python 退出码 {1}）。请先处理上方错误，再重新运行；脚本不会复用或覆盖本次目标目录。" -f $Title, $exitCode)
    }

    if ($ShowOutput -and -not [string]::IsNullOrWhiteSpace($renderedOutput)) {
        Write-Host $renderedOutput
    }
    Write-Host "通过" -ForegroundColor Green
}

try {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $artifactsRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "artifacts"))
    $sourceRoot = Join-Path $repoRoot "src"

    if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        $script:PythonExecutable = Join-Path $repoRoot ".venv\Scripts\python.exe"
    }
    elseif ([System.IO.Path]::IsPathRooted($PythonPath)) {
        $script:PythonExecutable = [System.IO.Path]::GetFullPath($PythonPath)
    }
    else {
        $script:PythonExecutable = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $PythonPath))
    }

    $requiredFiles = @(
        $script:PythonExecutable,
        (Join-Path $repoRoot "pyproject.toml"),
        (Join-Path $repoRoot "data\synthetic_trial.csv"),
        (Join-Path $repoRoot "data\synthetic_trial_design.json"),
        (Join-Path $repoRoot "evals\tasks.jsonl"),
        (Join-Path $repoRoot "evals\phase6_agent_tasks.jsonl"),
        (Join-Path $repoRoot "evals\phase6_splits.json"),
        (Join-Path $repoRoot "scripts\verify_phase5_artifacts.py")
    )
    $missingFiles = @($requiredFiles | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missingFiles.Count -gt 0) {
        throw ("演示所需文件缺失：{0}" -f ($missingFiles -join ", "))
    }

    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        $runId = "portfolio_demo_{0}_{1}" -f `
            [DateTime]::UtcNow.ToString("yyyyMMdd_HHmmssfff"), `
            ([Guid]::NewGuid().ToString("N").Substring(0, 8))
        $resolvedOutput = [System.IO.Path]::GetFullPath((Join-Path $artifactsRoot $runId))
    }
    elseif ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
        $resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
    }
    else {
        $resolvedOutput = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
    }

    $artifactPrefix = $artifactsRoot.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (
        $resolvedOutput.Equals($artifactsRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $resolvedOutput.StartsWith($artifactPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "输出目录必须是项目 artifacts 目录下的新子目录。"
    }
    if (Test-Path -LiteralPath $resolvedOutput) {
        throw ("输出目录已存在，拒绝覆盖：{0}" -f $resolvedOutput)
    }

    Write-Host "ResearchOps Agent 作品集离线演示" -ForegroundColor Yellow
    Write-Host ("项目：{0}" -f $repoRoot)
    Write-Host ("输出：{0}" -f $resolvedOutput)
    Write-Host "模式：完全离线；固定 50 题组件与控制面评测"
    Write-Host "说明：本脚本不会调用 phase6-run-online，也不会检查或展示任何凭据。"

    $previousLocation = (Get-Location).Path
    $hadPythonPath = Test-Path Env:PYTHONPATH
    $previousPythonPath = if ($hadPythonPath) { $env:PYTHONPATH } else { $null }
    $numericalEnvironment = [ordered]@{
        OPENBLAS_CORETYPE = "NEHALEM"
        OPENBLAS_NUM_THREADS = "1"
        OMP_NUM_THREADS = "1"
        MKL_NUM_THREADS = "1"
        NUMEXPR_NUM_THREADS = "1"
        NPY_DISABLE_CPU_FEATURES = "X86_V3,X86_V4"
    }
    $previousNumericalEnvironment = @{}
    try {
        Set-Location -LiteralPath $repoRoot
        $env:PYTHONPATH = $sourceRoot
        foreach ($name in $numericalEnvironment.Keys) {
            $previousNumericalEnvironment[$name] = [Environment]::GetEnvironmentVariable(
                $name,
                [EnvironmentVariableTarget]::Process
            )
        }
        foreach ($name in $numericalEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $numericalEnvironment[$name],
                [EnvironmentVariableTarget]::Process
            )
        }

        Invoke-PythonStep `
            -Title "1/4 校验 Python 环境" `
            -Arguments @(
                "-c",
                "import sys; print('Python ' + sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3, 11) else 2)"
            ) `
            -ShowOutput

        Write-Section "固定并验证 Nehalem/x86-v2 数值基线"
        $previousOpenBlasVerbose = [Environment]::GetEnvironmentVariable(
            "OPENBLAS_VERBOSE",
            [EnvironmentVariableTarget]::Process
        )
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $env:OPENBLAS_VERBOSE = "2"
            # OpenBLAS reports its selected kernel on stderr even when the probe
            # succeeds. Capture that diagnostic without turning it into a
            # terminating PowerShell error.
            $ErrorActionPreference = "Continue"
            $probeOutput = @(
                & $script:PythonExecutable -c "import numpy as np; np.linalg.svd(np.eye(4))" 2>&1
            )
            $probeExitCode = $LASTEXITCODE
            $probeText = ($probeOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        }
        finally {
            [Environment]::SetEnvironmentVariable(
                "OPENBLAS_VERBOSE",
                $previousOpenBlasVerbose,
                [EnvironmentVariableTarget]::Process
            )
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($probeExitCode -ne 0 -or $probeText -notmatch '(?im)^Core:\s*Nehalem\s*$') {
            throw "固定的 Nehalem OpenBLAS kernel 未激活，拒绝生成不可比较的 evidence ID。"
        }
        Write-Host "OpenBLAS core：Nehalem" -ForegroundColor Green

        Invoke-PythonStep `
            -Title "验证 canonical ANCOVA evidence identity" `
            -Arguments @(
                "-c",
                "import json,pandas as pd; from pathlib import Path; from researchops.analysis_tools import run_ancova; from researchops.contracts import ResearchDesign; from researchops.data_quality import profile_csv; p=Path('data/synthetic_trial.csv'); f=pd.read_csv(p,encoding='utf-8-sig',low_memory=False); d=ResearchDesign.from_dict(json.loads(Path('data/synthetic_trial_design.json').read_text(encoding='utf-8'))); got=run_ancova(f,profile_csv(p),d).evidence_id; print(got); raise SystemExit(0 if got=='E-36034128278C' else 3)"
            ) `
            -ShowOutput

        Invoke-PythonStep `
            -Title "2/4 校验离线评测语料" `
            -Arguments @("-m", "researchops.cli", "eval-validate") `
            -ShowOutput

        Invoke-PythonStep `
            -Title "附加校验：Phase 6 行为语料与 split（仍不联网）" `
            -Arguments @("-m", "researchops.cli", "phase6-validate") `
            -ShowOutput

        Invoke-PythonStep `
            -Title "3/4 运行固定 50 题离线评测" `
            -Arguments @(
                "-m", "researchops.cli", "eval-run",
                "--tasks", (Join-Path $repoRoot "evals\tasks.jsonl"),
                "--output-dir", $resolvedOutput
            )

        Invoke-PythonStep `
            -Title "4/4 独立验证哈希、审计链与脱敏" `
            -Arguments @(
                (Join-Path $repoRoot "scripts\verify_phase5_artifacts.py"),
                $resolvedOutput
            ) `
            -ShowOutput
    }
    finally {
        Set-Location -LiteralPath $previousLocation
        if ($hadPythonPath) {
            $env:PYTHONPATH = $previousPythonPath
        }
        else {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        foreach ($name in $numericalEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $previousNumericalEnvironment[$name],
                [EnvironmentVariableTarget]::Process
            )
        }
    }

    $reportPath = Join-Path $resolvedOutput "eval_report.json"
    $summaryPath = Join-Path $resolvedOutput "eval_summary.md"
    $manifestPath = Join-Path $resolvedOutput "eval_manifest.json"
    $resultsPath = Join-Path $resolvedOutput "eval_results.jsonl"
    $auditPath = Join-Path $resolvedOutput "eval_audit.sqlite3"
    $auditIndexPath = Join-Path $resolvedOutput "eval_audit_index.json"
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json

    Write-Section "演示结果"
    [pscustomobject]@{
        Tasks = $report.task_count
        Passed = $report.passed_count
        Failed = $report.failed_count
        SuccessRate = "{0:P2}" -f [double]$report.success_rate
        UnexpectedToolErrorRate = "{0:P2}" -f [double]$report.unexpected_tool_error_rate
        SafetyViolationRate = "{0:P2}" -f [double]$report.safety_violation_rate
        EvidenceCitationAccuracy = "{0:P2}" -f [double]$report.evidence_citation_accuracy
        LatencyP50Ms = "{0:N2}" -f [double]$report.p50_latency_ms
        LatencyP95Ms = "{0:N2}" -f [double]$report.p95_latency_ms
        CostStatus = $report.cost_status
    } | Format-List | Out-Host

    Write-Host "关键产物：" -ForegroundColor Cyan
    Write-Host ("- 摘要报告：{0}" -f $summaryPath)
    Write-Host ("- 指标 JSON：{0}" -f $reportPath)
    Write-Host ("- 可复现清单：{0}" -f $manifestPath)
    Write-Host ("- 逐题结果：{0}" -f $resultsPath)
    Write-Host ("- 审计数据库：{0}" -f $auditPath)
    Write-Host ("- 审计链索引：{0}" -f $auditIndexPath)
    Write-Host ""
    Write-Host "离线演示完成。" -ForegroundColor Green
}
catch {
    Write-Error ("作品集演示失败：{0}" -f $_.Exception.Message) -ErrorAction Continue
    exit 1
}
