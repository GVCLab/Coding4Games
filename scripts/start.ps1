<#
.SYNOPSIS
Open the operator menu and offer host setup on the first launch.
.EXAMPLE
./scripts/start.ps1
.EXAMPLE
./scripts/start.ps1 -Setup -Python C:/Python311/python.exe
.EXAMPLE
./scripts/start.ps1 -SetupWsl2
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Setup,
    [switch]$SetupWsl2,
    [switch]$Dev,
    [switch]$Ocr,
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$hostPython = Join-Path $repo '.venv/Scripts/python.exe'
if ($Python -and -not $Setup) {
    $requested = if (Test-Path -LiteralPath $Python -PathType Leaf) {
        (Get-Item -LiteralPath $Python).FullName
    } else {
        Get-Command -Name $Python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty Source
    }
    if (-not $requested -or $requested -match '[\\/]Microsoft[\\/]WindowsApps[\\/](python(3(\.\d+)?)?|py)\.exe$') {
        throw "Supply the full path to a prepared Python executable. Cannot use -Python '$Python'."
    }
    $Python = $requested
}
if ($SetupWsl2) {
    if ($Setup) { throw 'Run -Setup before -SetupWsl2.' }
    if (-not $Python) {
        if (-not (Test-Path -LiteralPath $hostPython -PathType Leaf)) {
            throw 'Run ./scripts/start.ps1 -Setup to prepare the Windows Python environment first.'
        }
        $Python = $hostPython
    }
    $wslSetupArgs = @((Join-Path $PSScriptRoot 'setup_wsl2_runtime.py'), '--first-run',
                      '--distro', 'C4G-WSL2-Base-LOCAL')
    & $Python @wslSetupArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($Preview -or (Read-Host 'Prepare the WSL 2 runtime? [y/N]') -notin @('y', 'yes')) { return }
    & $Python @wslSetupArgs --execute
    exit $LASTEXITCODE
}
if ($Setup -or $Ocr -or (-not $Python -and -not (Test-Path -LiteralPath $hostPython -PathType Leaf))) {
    $installer = Join-Path $PSScriptRoot 'launchers/setup.ps1'
    $setupArgs = @{ Dev = $Dev; Ocr = $Ocr }
    if ($Python) { $setupArgs.Python = $Python }
    & $installer @setupArgs
    if ($Preview) { return }
    if ((Read-Host 'Install the host environment and open the operator menu? [y/N]') -notin @('y', 'yes')) {
        return
    }
    & $installer @setupArgs -Execute
    $Python = $hostPython
}
if (-not $Python) { $Python = $hostPython }
if ($Preview) {
    Write-Host "Operator menu: $Python $(Join-Path $PSScriptRoot 'game_tui.py')"
    return
}
& $Python (Join-Path $PSScriptRoot 'game_tui.py')
exit $LASTEXITCODE
