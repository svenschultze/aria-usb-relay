function Invoke-AriaProcess {
    param([string]$FilePath, [string[]]$Arguments, [int]$TimeoutMilliseconds = 20000)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FilePath
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw "Could not start $FilePath" }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $process.Kill($true)
            $process.WaitForExit(2000) | Out-Null
            throw "Operation timed out after $TimeoutMilliseconds ms: $FilePath"
        }
        [PSCustomObject]@{ ExitCode = $process.ExitCode; Output = $stdout.GetAwaiter().GetResult(); Error = $stderr.GetAwaiter().GetResult() }
    }
    finally { $process.Dispose() }
}

function Get-AriaWslContext {
    param([string]$Distro, [string]$LinuxUser, [switch]$AllowRootUser)
    $wsl = (Get-Command wsl.exe -ErrorAction Stop).Source
    $prefix = @()
    if ($Distro) { $prefix += @('-d', $Distro) }
    $windowsRoot = (Split-Path -Parent $PSScriptRoot).Replace('\', '/')
    $result = Invoke-AriaProcess -FilePath $wsl -Arguments ($prefix + @('--exec', 'wslpath', '-a', '-u', $windowsRoot))
    $linuxRoot = $result.Output.Trim()
    if ($result.ExitCode -ne 0 -or -not $linuxRoot.StartsWith('/')) { throw "WSL is unavailable or cannot resolve this workspace: $($result.Error.Trim())" }
    if (-not $LinuxUser) {
        $result = Invoke-AriaProcess -FilePath $wsl -Arguments ($prefix + @('--exec', 'id', '-un'))
        $LinuxUser = $result.Output.Trim()
        if ($result.ExitCode -ne 0) { throw 'Could not discover the default WSL user.' }
    }
    if (-not $LinuxUser -or ($LinuxUser -eq 'root' -and -not $AllowRootUser)) { throw 'Set -LinuxUser to the normal WSL user who owns the Aria SDK pairing.' }
    [PSCustomObject]@{ Executable = $wsl; Prefix = $prefix; Root = $linuxRoot; User = $LinuxUser }
}

function Get-AriaLiveState {
    param($Context)
    $arguments = @($Context.Prefix) + @('-u', 'root', '--cd', $Context.Root, '--exec', 'python3', 'scripts/wsl-session.py', '--status-json')
    $result = Invoke-AriaProcess -FilePath $Context.Executable -Arguments $arguments
    if ($result.ExitCode -ne 0) { throw "Could not verify session ownership: $($result.Error.Trim())" }
    $result.Output | ConvertFrom-Json
}

function New-AriaLaunchRecord {
    param([string]$Runtime, [string]$Distro)
    $owner = [Diagnostics.Process]::GetCurrentProcess()
    $record = [PSCustomObject]@{ pid = $PID; startTicks = [string]$owner.StartTime.ToUniversalTime().Ticks; token = [guid]::NewGuid().ToString('D'); distro = $Distro }
    $path = Join-Path $Runtime 'windows-session.json'
    $temporary = $path + '.' + $record.token + '.tmp'
    [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Compress))
    [IO.File]::Move($temporary, $path, $true)
    return $record
}

function Get-AriaLaunchRecord {
    param([string]$Runtime)
    $path = Join-Path $Runtime 'windows-session.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $parsedToken = [guid]::Empty
    if (-not [guid]::TryParse([string]$record.token, [ref]$parsedToken) -or [int]$record.pid -le 1) { throw 'Invalid Windows launch ownership record.' }
    $owner = $null
    try {
        $owner = [Diagnostics.Process]::GetProcessById([int]$record.pid)
        if ([string]$owner.StartTime.ToUniversalTime().Ticks -ne [string]$record.startTicks) { return $null }
    }
    catch [ArgumentException] { return $null }
    finally { if ($owner) { $owner.Dispose() } }
    return $record
}

function Request-AriaLaunchCancellation {
    param([string]$Runtime)
    $record = Get-AriaLaunchRecord -Runtime $Runtime
    if ($null -ne $record) {
        [IO.File]::WriteAllText((Join-Path $Runtime ('windows-cancel.' + $record.token)), $record.token)
    }
    return $record
}

function Assert-AriaLaunchNotCancelled {
    param([string]$Runtime, [string]$Token)
    if (Test-Path -LiteralPath (Join-Path $Runtime ('windows-cancel.' + $Token))) { throw 'Live startup was cancelled.' }
}

function Clear-AriaLaunchRecord {
    param([string]$Runtime, [string]$Token, [switch]$KeepCancellation)
    $path = Join-Path $Runtime 'windows-session.json'
    if (Test-Path -LiteralPath $path) {
        $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ($record.token -eq $Token) { Remove-Item -LiteralPath $path }
    }
    $cancel = Join-Path $Runtime ('windows-cancel.' + $Token)
    if (-not $KeepCancellation -and (Test-Path -LiteralPath $cancel)) { Remove-Item -LiteralPath $cancel }
}

function Test-AriaForwardMapping {
    param([string]$Output, [string]$Device, [int]$Port)
    foreach ($line in ($Output -split '\r?\n')) {
        $parts = $line.Trim() -split '\s+'
        if ($parts.Count -eq 3 -and $parts[0] -eq $Device -and
            $parts[1] -eq "tcp:$Port" -and $parts[2] -eq 'tcp:3240') { return $true }
    }
    return $false
}
