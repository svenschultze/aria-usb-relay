param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GradleArguments = @('assembleDebug')
)

$ErrorActionPreference = 'Stop'
$previousJavaHome = $env:JAVA_HOME
$previousJavaOptions = $env:JAVA_TOOL_OPTIONS
$buildExitCode = 1

try {
    if (-not $env:JAVA_HOME) {
        $studioJdk = Join-Path $env:ProgramFiles 'Android\Android Studio\jbr'
        if (Test-Path -LiteralPath (Join-Path $studioJdk 'bin\java.exe')) {
            $env:JAVA_HOME = $studioJdk
        }
    }

    if (-not $env:JAVA_HOME -or -not (Test-Path -LiteralPath (Join-Path $env:JAVA_HOME 'bin\java.exe'))) {
        throw 'Set JAVA_HOME to a JDK 17 or newer, or install Android Studio.'
    }

    # A local socket path avoids a Windows JDK failure with some redirected TEMP paths.
    $socketDirectory = Join-Path $PSScriptRoot '.gradle\sockets'
    New-Item -ItemType Directory -Force -Path $socketDirectory | Out-Null
    $socketOption = '-Djdk.net.unixdomain.tmpdir="' + $socketDirectory + '"'
    $env:JAVA_TOOL_OPTIONS = ($previousJavaOptions + ' ' + $socketOption).Trim()

    Push-Location $PSScriptRoot
    try {
        & (Join-Path $PSScriptRoot 'gradlew.bat') @GradleArguments
        $buildExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:JAVA_HOME = $previousJavaHome
    $env:JAVA_TOOL_OPTIONS = $previousJavaOptions
}

exit $buildExitCode
