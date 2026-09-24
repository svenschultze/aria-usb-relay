param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Device,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Command,
    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 30,
    [string]$AdbPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'adb-common.ps1')
$adb = Find-AriaAdb -ExplicitPath $AdbPath
$commandBytes = [Text.Encoding]::UTF8.GetBytes($Command.Trim())
if ($commandBytes.Length -eq 0 -or $commandBytes.Length -gt 4096) {
    throw 'The probe command must contain between 1 and 4096 UTF-8 bytes.'
}
if ($Command.Contains([char]0)) { throw 'The probe command cannot contain a NUL character.' }

# Serialize this helper's requests per phone; the app has one private mailbox.
$deviceHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Device))).Substring(0, 16)
$mutex = [Threading.Mutex]::new($false, "Local\AriaUsbLabProbe-$deviceHash")
$ownsMutex = $false
$requestQueued = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Another probe is already running for this phone.' }
    $state = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('get-state')
    if ($state.ExitCode -ne 0 -or $state.Output.Trim() -ne 'device') {
        throw "ADB device '$Device' is not connected and authorized. $($state.Error)"
    }
    $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'test', '-d', 'files')
    if ($result.ExitCode -ne 0) {
        throw 'Open the debug app and connect its USB session first. The private debug mailbox is unavailable.'
    }
    $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'test', '-f', 'files/probe-request.txt')
    if ($result.ExitCode -eq 0) { throw 'A probe is already queued in the app. Let it complete before retrying.' }
    $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'rm', '-f', 'files/probe-response.txt')
    if ($result.ExitCode -ne 0) { throw 'Could not clear the previous debug response.' }

    # Command content travels on stdin as base64, never as host/phone shell syntax.
    # The fixed remote shell only writes and atomically publishes the mailbox file.
    $encodedCommand = [Convert]::ToBase64String($commandBytes)
    $requestQueued = $true
    $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'sh', '-c', "'base64 -d > files/probe-request.tmp && mv files/probe-request.tmp files/probe-request.txt'") -InputText $encodedCommand
    if ($result.ExitCode -ne 0) { throw 'Could not publish the debug request.' }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $remainingMs = [Math]::Max(1, [Math]::Min(5000, [int](($TimeoutSeconds - $timer.Elapsed.TotalSeconds) * 1000)))
        $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'test', '-f', 'files/probe-response.txt') -TimeoutMilliseconds $remainingMs
        if ($result.ExitCode -eq 0) {
            $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'cat', 'files/probe-response.txt')
            if ($result.ExitCode -ne 0) { throw 'The probe completed, but reading its response failed.' }
            Write-Output $result.Output
            $requestQueued = $false
            return
        }
        Start-Sleep -Milliseconds 400
    }
    throw "No response within $TimeoutSeconds seconds. Check that Aria USB Lab has an active USB session."
}
finally {
    if ($requestQueued) {
        # Remove an unconsumed request so it cannot execute during a later session.
        # A command already being executed by the app cannot be cancelled here.
        try {
            $cleanup = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'run-as', 'dev.aria.usb', 'rm', '-f', 'files/probe-request.txt', 'files/probe-request.tmp') -TimeoutMilliseconds 2000
            if ($cleanup.ExitCode -ne 0) { Write-Warning 'Could not remove the queued probe. It may run when the USB session resumes.' }
        }
        catch { Write-Warning 'ADB is unavailable; the queued probe could not be removed and may run when the USB session resumes.' }
    }
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
