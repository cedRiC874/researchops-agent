function Invoke-Item6NativeOnce {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Executable,
          [Parameter(Mandatory)][string[]]$Arguments,
          [Parameter(Mandatory)][string]$Receipt)
    $ErrorActionPreference='Stop'
    $PSNativeCommandUseErrorActionPreference=$false
    $path=[IO.Path]::GetFullPath($Receipt)
    $stream=[IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    $record=[ordered]@{schema='item6-native-process-exit/1';actual_exit_code=$null;invocation_completed=$false;invocation_error_type=$null;exception_body_recorded=$false;retry_attempted=$false}
    try {
        & $Executable @Arguments 2>$null | Out-Host
        $record.actual_exit_code=$LASTEXITCODE
        $record.invocation_completed=$true
    } catch { $record.invocation_error_type=$_.Exception.GetType().FullName }
    finally {
        try {
            $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($record | ConvertTo-Json -Compress))
            $stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)
        } finally { $stream.Dispose() }
    }
    return [pscustomobject]$record
}
Export-ModuleMember -Function Invoke-Item6NativeOnce
