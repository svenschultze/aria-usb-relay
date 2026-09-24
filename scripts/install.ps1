param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Device,
    [string]$ApkPath,
    [string]$AdbPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'adb-common.ps1')
$sessionRecord = Join-Path (Split-Path -Parent $PSScriptRoot) '.research/live-run/session.pid'
if (Test-Path -LiteralPath $sessionRecord) {
    throw 'A live session record exists. Stop the transport before updating the phone app.'
}
$adb = Find-AriaAdb -ExplicitPath $AdbPath
Assert-AriaAdbDevice -Adb $adb -Device $Device

if (-not $ApkPath) {
    $ApkPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'app\build\outputs\apk\debug\app-debug.apk'
}
if (-not (Test-Path -LiteralPath $ApkPath -PathType Leaf)) {
    throw "APK not found: $ApkPath. Run .\build.ps1 assembleDebug first."
}
$apk = (Resolve-Path -LiteralPath $ApkPath).Path
$result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('install', '-r', $apk) -TimeoutMilliseconds 120000
if ($result.ExitCode -ne 0) { throw "Installing APK failed: $($result.Error) $($result.Output)" }
$result = Invoke-AriaAdbBounded -Adb $adb -Device $Device -Arguments @('shell', 'am', 'start', '-n', 'dev.aria.usb/.MainActivity') -TimeoutMilliseconds 15000
if ($result.ExitCode -ne 0) { throw "APK installed, but opening the phone bridge failed: $($result.Error)" }
Write-Output 'Phone bridge installed and opened. Start the laptop dashboard, then allow USB access on the phone.'
