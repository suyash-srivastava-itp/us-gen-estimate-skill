# Finds a Python 3.8 or newer interpreter on Windows, checks numpy, and prints where this skill lives.
# Use on Windows PowerShell. On macOS, Linux or Git Bash use find_python.sh.
#
#   powershell -ExecutionPolicy Bypass -File <skill>\scripts\find_python.ps1 [-Python <interpreter>]
#
# With no -Python it tries the py launcher (py -3), then python, then python3.
# With -Python, only that interpreter is tried, for example a virtual environment's python.exe.
#
# Output, one value per line:
#   PYTHON=<absolute path of the interpreter>
#   PYTHON_VERSION=3.11
#   NUMPY=2.4.6                      or NUMPY=missing, with INSTALL=... and NOTE=...
#   SKILL_DIR=<absolute path of this skill's folder, with forward slashes>
# Exit status: 0 ready, 2 Python found but numpy missing, 1 no Python 3.8 or newer.

param([string]$Python = "")

$scripts = $PSScriptRoot
$skill = (Resolve-Path (Join-Path $scripts "..")).Path
$probe = Join-Path $scripts "python_probe.py"

# Runs the probe with one interpreter command; returns its details if it is Python 3.8 or newer.
function Test-Python {
    param([string[]]$Command)
    if (-not (Get-Command $Command[0] -ErrorAction SilentlyContinue)) { return $null }
    $rest = @()
    if ($Command.Count -gt 1) { $rest = $Command[1..($Command.Count - 1)] }
    $global:LASTEXITCODE = 0
    $out = @(& $Command[0] @rest $probe)
    if ($global:LASTEXITCODE -ne 0 -or $out.Count -lt 3) { return $null }
    try {
        $ver = $out[1] -split ' '
        if ([int]$ver[0] -ne 3 -or [int]$ver[1] -lt 8) { return $null }
        return [pscustomobject]@{
            Exe     = $out[0]
            Version = "$($ver[0]).$($ver[1])"
            Numpy   = $out[2]
        }
    } catch {
        return $null
    }
}

$found = $null
if ($Python) {
    $found = Test-Python @($Python)
} else {
    foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
        $found = Test-Python $candidate
        if ($found) { break }
    }
}

if (-not $found) {
    Write-Output "NO_PYTHON=1"
    Write-Output "HINT=Install Python 3.8 or newer from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), or run: winget install Python.Python.3.11"
    exit 1
}

Write-Output "PYTHON=$($found.Exe)"
Write-Output "PYTHON_VERSION=$($found.Version)"
if ($found.Numpy -eq "missing") {
    Write-Output "NUMPY=missing"
    Write-Output "INSTALL=`"$($found.Exe)`" -m pip install -r `"$skill\requirements.txt`""
    Write-Output "NOTE=Ask the person before installing. If pip reports externally-managed-environment, create a virtual environment and run this script with -Python <venv>\Scripts\python.exe."
    $status = 2
} else {
    Write-Output "NUMPY=$($found.Numpy)"
    $status = 0
}
Write-Output "SKILL_DIR=$($skill -replace '\\', '/')"
exit $status
