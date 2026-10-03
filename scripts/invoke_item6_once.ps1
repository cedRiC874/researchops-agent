[CmdletBinding()]
param([Parameter(Mandatory)][string]$Freeze,
      [Parameter(Mandatory)][string]$Approval,
      [Parameter(Mandatory)][string]$ApprovedDigest,
      [Parameter(Mandatory)][string]$Receipt)
$ErrorActionPreference='Stop'
$repoRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workspaceRoot=[IO.Path]::GetFullPath((Join-Path $repoRoot '..'))
$python=Join-Path $workspaceRoot 'researchops-agent/.venv/Scripts/python.exe'
if(-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'item6_operator_interpreter_unavailable' }
$receiptPath=[IO.Path]::GetFullPath($Receipt)
$receiptRoot=Join-Path $repoRoot 'output/item6-operator-exit'
if ([IO.Path]::GetDirectoryName($receiptPath) -cne $receiptRoot -or
    [IO.Path]::GetFileName($receiptPath) -cnotmatch '^[A-Za-z0-9_-]{1,80}\.json$') { throw 'item6_operator_receipt_scope' }
foreach($part in @((Join-Path $repoRoot 'output'),$receiptRoot)) {
    if (Test-Path -LiteralPath $part) {
        $info=Get-Item -LiteralPath $part
        if(-not $info.PSIsContainer -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'item6_operator_receipt_scope' }
    } else { $null=New-Item -ItemType Directory -Path $part -ErrorAction Stop }
}
if($ApprovedDigest -cnotmatch '^(?!0{64}$)[0-9a-f]{64}$') { throw 'item6_operator_digest' }
Import-Module (Join-Path $PSScriptRoot 'item6_process_exit.psm1') -Force
$env:PYTHONPATH=Join-Path $repoRoot 'src'
$env:PYTHONDONTWRITEBYTECODE='1'
Push-Location -LiteralPath $repoRoot
try {
    # The only online entry is the repository CLI; all original freeze, source,
    # mode, environment, store, approval and winning-claim checks remain there.
    $result=Invoke-Item6NativeOnce -Executable $python -Arguments @('-B','-m','researchops_item6_experiment_v1','run',
        '--freeze',$Freeze,'--approval',$Approval,'--approved-digest',$ApprovedDigest) -Receipt $receiptPath
} finally { Pop-Location }
$result | ConvertTo-Json -Compress
if($null -eq $result.actual_exit_code -or -not $result.invocation_completed) { exit 2 }
exit $result.actual_exit_code
