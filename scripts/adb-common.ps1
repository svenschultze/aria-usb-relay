function Find-AriaAdb {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "ADB executable not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    foreach ($sdk in @($env:ANDROID_SDK_ROOT, $env:ANDROID_HOME, (Join-Path $env:LOCALAPPDATA 'Android\Sdk'))) {
        if (-not $sdk) { continue }
        $candidate = Join-Path $sdk 'platform-tools\adb.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $commandInfo = Get-Command adb -ErrorAction SilentlyContinue
    if ($commandInfo) { return $commandInfo.Source }
    throw 'ADB not found. Install Android SDK Platform-Tools, set ANDROID_SDK_ROOT, or pass -AdbPath.'
}

function Assert-AriaAdbDevice {
    param([string]$Adb, [string]$Device)
    $state = Invoke-AriaAdbBounded -Adb $Adb -Device $Device -Arguments @('get-state')
    if ($state.ExitCode -ne 0 -or $state.Output.Trim() -ne 'device') {
        throw "ADB device '$Device' is not connected and authorized. Run adb pair/connect first. $($state.Error)"
    }
}

function Invoke-AriaAdbBounded {
    param(
        [string]$Adb,
        [string]$Device,
        [string[]]$Arguments,
        [int]$TimeoutMilliseconds = 5000,
        [AllowNull()][string]$InputText
    )
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Adb
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = $true
    foreach ($argument in (@('-s', $Device) + $Arguments)) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Could not start ADB.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputText) { $process.StandardInput.WriteLine($InputText) }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $process.Kill()
            $process.WaitForExit(2000) | Out-Null
            throw "ADB operation timed out for '$Device'."
        }
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            Output = $stdout.GetAwaiter().GetResult()
            Error = $stderr.GetAwaiter().GetResult()
        }
    }
    finally { $process.Dispose() }
}
