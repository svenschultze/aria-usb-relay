param([string]$Device, [string]$Distro, [string]$LinuxUser, [string]$SdkPath, [string]$AriaSerial, [string]$AdbPath, [switch]$Json)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'adb-common.ps1')
. (Join-Path $PSScriptRoot 'wsl-common.ps1')
$checks = [Collections.Generic.List[object]]::new()
function Add-Check([string]$Id, [string]$Label, [string]$Status, [string]$Detail) {
    $checks.Add([PSCustomObject]@{ id = $Id; label = $Label; status = $Status; detail = $Detail })
}
try {
    $adb = Find-AriaAdb -ExplicitPath $AdbPath
    Add-Check 'adb' 'Android platform-tools' 'pass' $adb
    $helpResult = Invoke-AriaProcess -FilePath $adb -Arguments @('help') -TimeoutMilliseconds 5000
    if (($helpResult.Output + $helpResult.Error) -match '(?m)^\s*pair\s') {
        Add-Check 'adb_pair' 'Wireless phone pairing' 'pass' 'ADB supports wireless pairing.'
    }
    else { Add-Check 'adb_pair' 'Wireless phone pairing' 'fail' 'Install current official Android SDK Platform-Tools: https://developer.android.com/tools/releases/platform-tools' }
    if ($Device) {
        # Query only an existing server. Do not start ADB as a side effect of doctor.
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $task = $client.ConnectAsync('127.0.0.1', 5037)
            if (-not $task.Wait(500) -or -not $client.Connected) { throw 'ADB server is not running; connect the phone with adb pair/connect first.' }
            $state = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('get-state')
            if ($state.ExitCode -ne 0 -or $state.Output.Trim() -ne 'device') { throw 'Phone is not connected and authorized. Run adb pair/connect first.' }
            Add-Check 'phone' 'Phone ADB connection' 'pass' 'Authorized phone connected.'
        }
        catch { Add-Check 'phone' 'Phone ADB connection' 'fail' $_.Exception.Message }
        finally { $client.Dispose() }
    }
    else { Add-Check 'phone' 'Phone selection' 'warn' 'Choose the phone ADB serial in settings before starting.' }
}
catch { Add-Check 'adb' 'Android platform-tools' 'fail' $_.Exception.Message }
try {
    $context = Get-AriaWslContext -Distro $Distro -LinuxUser $LinuxUser
    Add-Check 'wsl' 'WSL Linux runtime' 'pass' "Workspace accessible; normal SDK user: $($context.User)."
    $arguments = @($context.Prefix) + @('-u', $context.User, '--cd', $context.Root, '--exec', 'python3', 'scripts/doctor_linux.py', '--json', '--skip-phone-adb')
    if ($SdkPath) { $arguments += @('--sdk-path', $SdkPath) }
    if ($AriaSerial) { $arguments += @('--serial', $AriaSerial) }
    $result = Invoke-AriaProcess -FilePath $context.Executable -Arguments $arguments -TimeoutMilliseconds 60000
    if ($result.ExitCode -ne 0) { throw $result.Error.Trim() }
    $linux = $result.Output | ConvertFrom-Json
    foreach ($check in $linux.checks) { $checks.Add($check) }
    $mode = Invoke-AriaProcess -FilePath $context.Executable -Arguments (@($context.Prefix) + @('--exec', 'wslinfo', '--networking-mode'))
    if ($mode.ExitCode -eq 0 -and $mode.Output.Trim() -eq 'mirrored') {
        Add-Check 'wsl_network' 'WSL localhost networking' 'pass' 'Mirrored networking is enabled.'
    }
    else { Add-Check 'wsl_network' 'WSL localhost networking' 'fail' 'This Windows transport requires WSL mirrored networking. Set networkingMode=mirrored under [wsl2] in %USERPROFILE%\.wslconfig, then restart WSL yourself when no work is running.' }
}
catch { Add-Check 'wsl_check' 'WSL prerequisites' 'fail' $_.Exception.Message }
$report = [PSCustomObject]@{ ready = @($checks | Where-Object status -eq 'fail').Count -eq 0; checks = @($checks.ToArray()) }
if ($Json) { $report | ConvertTo-Json -Depth 8 -Compress }
else {
    foreach ($check in $checks) { Write-Host "$($check.status.ToUpper().PadRight(4)) $($check.label): $($check.detail)" }
    Write-Host $(if ($report.ready) { 'Ready.' } else { 'Resolve the failed checks before starting.' })
}
