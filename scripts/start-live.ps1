param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$Device,
    [string]$Distro, [string]$LinuxUser, [string]$SdkPath, [string]$AriaSerial,
    [string]$Profile = 'profile12',
    [ValidateRange(1024, 65535)][int]$LinuxAdbPort = 5038,
    [string]$AdbPath
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'adb-common.ps1')
. (Join-Path $PSScriptRoot 'wsl-common.ps1')
if ($LinuxAdbPort -eq 5037) { throw 'LinuxAdbPort must differ from the phone ADB server on 5037.' }
$adb = $null
$context = $null
$runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.research/live-run'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$guard = $null
$relay = $null
$process = $null
$processStarted = $false
$launch = $null
try {
    try { $guard = [IO.File]::Open((Join-Path $runtime 'windows-launch.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another Windows launcher already owns this workspace. Stop that session before starting another.' }
    $launch = New-AriaLaunchRecord -Runtime $runtime -Distro $Distro
    Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
    $adb = Find-AriaAdb -ExplicitPath $AdbPath
    $context = Get-AriaWslContext -Distro $Distro -LinuxUser $LinuxUser
    Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
    # This check precedes app launches, tunnel changes, and USB actions.
    $state = Get-AriaLiveState -Context $context
    Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
    if ($state.running) { throw 'This workspace already has a live session. Stop it before starting another.' }
    $preflight = & (Join-Path $PSScriptRoot 'doctor.ps1') -Device $Device -Distro $Distro -LinuxUser $context.User -SdkPath $SdkPath -AdbPath $adb -Json
    $report = $preflight | ConvertFrom-Json
    Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
    if (-not $report.ready) {
        $failures = @($report.checks | Where-Object status -eq 'fail' | ForEach-Object { "$($_.label): $($_.detail)" })
        throw ($failures -join [Environment]::NewLine)
    }
    $relay = & (Join-Path $PSScriptRoot 'start-relay.ps1') -Device $Device -AdbPath $adb -LaunchToken $launch.token
    Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
    $arguments = @($context.Prefix) + @('-u', 'root', '--cd', $context.Root, '--exec', 'bash',
        'scripts/wsl-live.sh', '--port', [string]$relay.UsbIpPort, '--user', $context.User,
        '--profile', $Profile, '--adb-port', [string]$LinuxAdbPort, '--launch-token', $launch.token)
    if ($SdkPath) { $arguments += @('--sdk-path', $SdkPath) }
    if ($AriaSerial) { $arguments += @('--serial', $AriaSerial) }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $context.Executable
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    foreach ($argument in $arguments) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
    if (-not $process.Start()) { throw 'Could not launch the Linux USB transport.' }
    $processStarted = $true
    Write-Host 'Starting live sensors. Accept USB permission on the phone; data is displayed on this laptop.'
    $lastConnectionMessage = $null
    while (-not $process.WaitForExit(5000)) {
        Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $launch.token
        # Recreate only our exact forward if an ADB reconnect removed it.
        try {
            $listed = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('forward', '--list') -TimeoutMilliseconds 2500
            if ($listed.ExitCode -ne 0) { throw 'Phone ADB connection is unavailable; waiting for it to reconnect.' }
            if (-not (Test-AriaForwardMapping -Output $listed.Output -Device $Device -Port $relay.UsbIpPort)) {
                $restored = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('forward', '--no-rebind', "tcp:$($relay.UsbIpPort)", 'tcp:3240') -TimeoutMilliseconds 3000
                if ($restored.ExitCode -ne 0) { throw 'Phone tunnel is unavailable; reconnect the phone ADB connection.' }
            }
            $lastConnectionMessage = $null
        }
        catch {
            if ($lastConnectionMessage -ne $_.Exception.Message) { Write-Warning $_.Exception.Message; $lastConnectionMessage = $_.Exception.Message }
        }
    }
    if ($process.ExitCode -notin @(0, 130, 143)) { throw "Live session exited with code $($process.ExitCode). See .research/live-run logs." }
}
finally {
    if ($processStarted -and -not $process.HasExited) {
        # Ask the verified Linux owner to stop before removing its transport.
        try {
            Request-AriaLaunchCancellation -Runtime $runtime | Out-Null
            $stopArgs = @($context.Prefix) + @('-u', 'root', '--cd', $context.Root, '--exec', 'python3', 'scripts/wsl-session.py')
            $stopped = Invoke-AriaProcess -FilePath $context.Executable -Arguments $stopArgs -TimeoutMilliseconds 55000
            if ($stopped.ExitCode -ne 0) { Write-Warning $stopped.Error.Trim() }
            $process.WaitForExit(15000) | Out-Null
        }
        catch { Write-Warning "Session cleanup: $($_.Exception.Message)" }
    }
    if ($null -ne $relay) {
        try {
            $listed = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('forward', '--list')
            if ($listed.ExitCode -eq 0 -and (Test-AriaForwardMapping -Output $listed.Output -Device $Device -Port $relay.UsbIpPort)) {
                $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('forward', '--remove', "tcp:$($relay.UsbIpPort)")
                if ($result.ExitCode -ne 0) { Write-Warning "Owned tunnel cleanup: $($result.Error.Trim())" }
            }
        }
        catch { Write-Warning "Owned tunnel cleanup: $($_.Exception.Message)" }
    }
    if ($null -ne $launch) {
        $keepCancel = $processStarted -and -not $process.HasExited
        Clear-AriaLaunchRecord -Runtime $runtime -Token $launch.token -KeepCancellation:$keepCancel
    }
    if ($null -ne $process) { $process.Dispose() }
    if ($null -ne $guard) { $guard.Dispose() }
}
