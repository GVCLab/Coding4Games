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
    [string]$Python,
    [switch]$Dev,
    [switch]$Ocr,
    [switch]$Execute
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$venv = Join-Path $repo '.venv'
$hostPython = Join-Path $venv 'Scripts/python.exe'
function Get-HostInterpreter {
    param([string]$Executable, [string[]]$LauncherArguments = @())

    # Calling the Microsoft Store alias can open a window on an unconfigured host.
    if ($Executable -match '[\\/]Microsoft[\\/]WindowsApps[\\/](python(3(\.\d+)?)?|py)\.exe$') { return }
    $probe = @'
import json, sys, sysconfig
if sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 11) or sysconfig.get_platform() != 'win-amd64':
    sys.exit(1)
print(json.dumps(dict(Path=sys.executable, Version=sys.version.split()[0])))
'@
    try {
        $result = & $Executable @LauncherArguments -I -B -S -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) {
            $info = $result | ConvertFrom-Json
            if (Test-Path -LiteralPath $info.Path -PathType Leaf) { return $info }
        }
    }
    catch {
        # An unusable candidate is reported with its context by the caller.
    }
}

$interpreter = $null
$existing = Test-Path -LiteralPath $venv
if ($existing -and -not (Test-Path -LiteralPath $hostPython -PathType Leaf)) {
    throw "Incomplete environment at '$venv'. Preserve it and move it aside before retrying setup."
}
if ($Python) {
    $requested = if (Test-Path -LiteralPath $Python -PathType Leaf) {
        (Get-Item -LiteralPath $Python).FullName
    } else {
        Get-Command -Name $Python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty Source
    }
    if ($requested) { $interpreter = Get-HostInterpreter $requested }
    if (-not $interpreter) {
        throw "Supply the full path to an installed Python 3.11 x64 (CPython) executable. Cannot use -Python '$Python'."
    }
}
if ($existing) {
    $interpreter = Get-HostInterpreter $hostPython
    if (-not $interpreter) {
        throw "Existing environment at '$venv' must use Python 3.11 x64 (CPython). Preserve it and move it aside before retrying setup."
    }
}
if (-not $interpreter) {
    $previousAllowInstall = $env:PYLAUNCHER_ALLOW_INSTALL
    $previousAlwaysInstall = $env:PYLAUNCHER_ALWAYS_INSTALL
    try {
        # Python's legacy launcher can install from the Store when these are set.
        $env:PYLAUNCHER_ALLOW_INSTALL = $null
        $env:PYLAUNCHER_ALWAYS_INSTALL = $null
        foreach ($launcher in (Get-Command -Name py.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $interpreter = Get-HostInterpreter $launcher.Source @('-3.11')
            if ($interpreter) { break }
        }
    }
    finally {
        $env:PYLAUNCHER_ALLOW_INSTALL = $previousAllowInstall
        $env:PYLAUNCHER_ALWAYS_INSTALL = $previousAlwaysInstall
    }
}
if (-not $interpreter) {
    foreach ($command in (Get-Command -Name python.exe,python3.11.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $interpreter = Get-HostInterpreter $command.Source
        if ($interpreter) { break }
    }
}
if (-not $interpreter) {
    throw 'Python 3.11 x64 (CPython) was not found. Install it and reopen PowerShell, or pass -Python C:/path/to/python.exe.'
}

$extras = @()
if ($Dev) { $extras += 'dev' }
if ($Ocr) { $extras += 'ocr' }
$target = if ($extras.Count) { '.[' + ($extras -join ',') + ']' } else { '.' }
$environmentAction = if ($existing) { 'Reuse' } else { 'Create' }
Write-Host "Python $($interpreter.Version) x64: $($interpreter.Path)"
Write-Host "${environmentAction} environment: $venv"
Write-Host "Install: `"$hostPython`" -m pip install -e `"$target`""
Write-Host 'Create missing launcher.local.json, games.local.json and litellm.config.yaml from docs/examples.'
if ($Ocr) {
    Write-Host 'Includes the OCR packages and more than 2 GB of PaddlePaddle and CUDA downloads on a fresh install.'
}
Write-Host 'Downloads Python dependencies. Does not install system drivers/WSL, start games or call models.'
if (-not $Execute) {
    Write-Host 'Preview only. No packages or local files were changed.'
    return
}
if (-not $existing) {
    & $interpreter.Path -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
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
