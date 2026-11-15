<#
.SYNOPSIS
Prepare a checkout's Windows Python environment and missing local settings.
.EXAMPLE
./scripts/launchers/setup.ps1 -Python C:/Python311/python.exe
.EXAMPLE
./scripts/launchers/setup.ps1 -Python C:/Python311/python.exe -Execute
#>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [switch]$Dev,
    [switch]$Execute
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$venv = Join-Path $repo '.venv'
$hostPython = Join-Path $venv 'Scripts/python.exe'
$target = if ($Dev) { '.[dev]' } else { '.' }
Write-Host "Environment: $venv (existing environments are reused, never replaced)"
Write-Host "Install: $hostPython -m pip install -e $target"
Write-Host 'Create missing launcher.local.json, games.local.json and litellm.config.yaml from docs/examples.'
Write-Host 'Downloads Python dependencies. Does not install system drivers/WSL, start games or call models.'
if (-not $Execute) {
    Write-Host 'Preview only. Install Python 3.11 x64, then add -Execute.'
    return
}
if (-not (Test-Path -LiteralPath $hostPython -PathType Leaf)) {
    & $Python -c "import sys; assert sys.version_info[:2] == (3, 11), 'Use Python 3.11 x64 for the documented host dependencies'; assert sys.maxsize > 2**32, '64-bit Python required'"
    if ($LASTEXITCODE -ne 0) { throw 'Select a Python 3.11 x64 interpreter with -Python.' }
    if (Test-Path -LiteralPath $venv) { throw 'Incomplete .venv exists; inspect it before choosing a new environment.' }
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $hostPython -c "import sys; assert sys.version_info[:2] == (3, 11), 'Existing .venv must use Python 3.11'; assert sys.maxsize > 2**32"
if ($LASTEXITCODE -ne 0) { throw 'Existing .venv is incompatible; preserve it and use the manual installation guide.' }
Push-Location $repo
try {
    $previousVigemSkip = $env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL
    try {
        $env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = 'true'
        & $hostPython -m pip install -e $target
        if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    }
    finally { $env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = $previousVigemSkip }
    & $hostPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
    & $hostPython -c "from coding4game_gui.operator_config import initialize; print('Created: ' + ', '.join(initialize()))"
    if ($LASTEXITCODE -ne 0) { throw 'Local configuration initialization failed.' }
    Write-Host 'Python setup complete. Next: ./.venv/Scripts/python.exe -m coding4game_gui.doctor'
}
finally { Pop-Location }
