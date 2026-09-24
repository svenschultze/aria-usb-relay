param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Device,
    [string]$AdbPath,
    [string]$LaunchToken
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'adb-common.ps1')
. (Join-Path $PSScriptRoot 'wsl-common.ps1')
$runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.research/live-run'
$adb = Find-AriaAdb -ExplicitPath $AdbPath
Assert-AriaAdbDevice -Adb $adb -Device $Device

function Invoke-RelayAdb {
    param([string[]]$Arguments)
    $result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments $Arguments -TimeoutMilliseconds 15000
    if ($result.ExitCode -ne 0) { throw "ADB failed: $($result.Error) $($result.Output)" }
    return $result.Output.Trim()
}

# The Activity handles this idempotently. Keep any active USB relay connected.
if ($LaunchToken) { Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $LaunchToken }
Invoke-RelayAdb -Arguments @('shell', 'am', 'start', '-n', 'dev.aria.usb/.MainActivity', '--ez', 'relay_usb', 'true') | Out-Null

# Allocate a session-owned forward; never borrow or remove another session's.
# Port 3240 may be reserved by Hyper-V/WSL and must not be hardcoded locally.
$forwardPort = 0
if ($forwardPort -eq 0) {
    if ($LaunchToken) { Assert-AriaLaunchNotCancelled -Runtime $runtime -Token $LaunchToken }
    $allocated = Invoke-RelayAdb -Arguments @('forward', 'tcp:0', 'tcp:3240')
    if (-not [int]::TryParse($allocated, [ref]$forwardPort) -or $forwardPort -lt 1 -or $forwardPort -gt 65535) {
        throw "ADB returned an invalid dynamic forwarding port: $allocated"
    }
}

Write-Host 'USB relay requested. Accept any USB permission prompt on the phone.'
Write-Host "Windows USB/IP tunnel: 127.0.0.1:$forwardPort -> phone loopback:3240"
[PSCustomObject]@{
    Device = $Device
    UsbIpPort = $forwardPort
    PreviewUrl = 'http://127.0.0.1:8765/'
}
