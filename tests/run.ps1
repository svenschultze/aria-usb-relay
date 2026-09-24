param([string]$JavaHome = $env:JAVA_HOME)
$ErrorActionPreference = 'Stop'
if (-not $JavaHome) {
    $studioJdk = Join-Path $env:ProgramFiles 'Android\Android Studio\jbr'
    if (Test-Path -LiteralPath (Join-Path $studioJdk 'bin\javac.exe')) { $JavaHome = $studioJdk }
}
$testArguments = @((Join-Path $PSScriptRoot 'run_jvm.py'))
if ($JavaHome) { $testArguments += @('--java-home', $JavaHome) }
python @testArguments
if ($LASTEXITCODE -ne 0) { throw 'JVM tests failed.' }
