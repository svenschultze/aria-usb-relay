param(
    [string]$Distro,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wsl-common.ps1')
$runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.research/live-run'
$deadline = [DateTime]::UtcNow.AddSeconds(90)
$launch = $null
if (-not $Check) { $launch = Request-AriaLaunchCancellation -Runtime $runtime }
if (-not $Distro -and $null -ne $launch -and $launch.distro) { $Distro = $launch.distro }
$context = Get-AriaWslContext -Distro $Distro -AllowRootUser
$wslArguments = @($context.Prefix) + @('-u', 'root', '--cd', $context.Root,
    '--exec', 'python3', 'scripts/wsl-session.py')
if ($Check) { $wslArguments += '--check' }
$result = Invoke-AriaProcess -FilePath $context.Executable -Arguments $wslArguments -TimeoutMilliseconds 55000
if ($result.Output) { Write-Output $result.Output.Trim() }
if ($result.ExitCode -ne 0) { throw "Live session stop/check failed: $($result.Error.Trim()). No unrelated process was signaled." }
if ($null -ne $launch) {
    while ([DateTime]::UtcNow -lt $deadline) {
        $current = Get-AriaLaunchRecord -Runtime $runtime
        if ($null -eq $current -or $current.token -ne $launch.token) { return }
        Start-Sleep -Milliseconds 200
    }
    throw 'Cancellation was requested, but startup cleanup is still pending. No unrelated process was killed.'
}
