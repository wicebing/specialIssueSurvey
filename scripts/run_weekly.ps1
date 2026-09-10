param(
    [switch]$NoPush,
    [switch]$Offline,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot
$RepoPath = $RepoRoot.Path

function Resolve-Python {
    param([string]$Preferred)

    if ($Preferred) {
        return $Preferred
    }

    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        return $PythonCommand.Source
    }

    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $PyLauncher.Source
        }
    }

    $CodexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $CodexPython) {
        return $CodexPython
    }

    throw "Cannot find Python. Install Python 3.11+ or pass -Python C:\Path\To\python.exe."
}

$PythonExe = Resolve-Python -Preferred $Python

if (!(Test-Path -LiteralPath ".venv")) {
    if ((Split-Path -Leaf $PythonExe) -ieq "py.exe") {
        & $PythonExe -3 -m venv .venv
    } else {
        & $PythonExe -m venv .venv
    }
}

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

$Args = @("scripts\fetch_cfps.py")
if ($Offline) {
    $Args += "--offline"
}
& $VenvPython @Args

if (!(Test-Path -LiteralPath ".git")) {
    Write-Host "This folder is not a git repository yet. Run: git init; git add .; git commit -m 'Initial CFP tracker'"
    exit 0
}

function Invoke-RepoGit {
    git -c "safe.directory=$RepoPath" @args
}

Invoke-RepoGit add README.md config data docs reports scripts .github requirements.txt .gitignore
Invoke-RepoGit diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "No report changes to commit."
    exit 0
}

$Stamp = Get-Date -Format "yyyy-MM-dd"
Invoke-RepoGit commit -m "chore: weekly CFP report $Stamp"

if (!$NoPush) {
    Invoke-RepoGit push
}
