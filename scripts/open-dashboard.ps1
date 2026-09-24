param([string]$PythonPath, [ValidateRange(1024, 65535)][int]$Port = 8766, [switch]$NoOpen)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = $null
$prefix = @()
if ($PythonPath) {
    $python = (Get-Command $PythonPath -ErrorAction Stop).Source
} else {
    foreach ($name in @((Join-Path $root '.venv/Scripts/python.exe'), 'python', 'python3', 'py')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($candidate) {
            $testPrefix = if ($name -eq 'py') { @('-3') } else { @() }
            & $candidate.Source @testPrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $python = $candidate.Source; $prefix = $testPrefix; break }
        }
    }
}
if (-not $python) { throw 'Install Python 3.10 or newer and add it to PATH, or pass -PythonPath.' }
$arguments = $prefix + @((Join-Path $root 'desktop/server.py'), '--port', [string]$Port)
if ($NoOpen) { $arguments += '--no-open' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw "Dashboard exited with code $LASTEXITCODE." }
