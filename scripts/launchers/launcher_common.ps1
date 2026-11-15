# Locate the host interpreter for the generic launchers.
function Get-C4GPython([string]$Repo, [string]$Explicit) {
    if ($Explicit) { return $Explicit }
    $venv = Join-Path $Repo '.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $venv -PathType Leaf) { return $venv }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw 'Python is missing. Install Python 3.11 and open ./scripts/start.ps1.' }
    return $command.Source
}

function Get-C4GRegisteredScenarios([string]$Repo, [string]$Python) {
    Push-Location $Repo
    try {
        $scenarios = & $Python -B -c "from coding4game_gui.catalog import registered_scenario_paths; print(*[p.stem for p in registered_scenario_paths()], sep=chr(10))"
        if ($LASTEXITCODE -ne 0 -or -not $scenarios) { throw 'Cannot load the formal scenario catalog.' }
        return $scenarios
    }
    finally { Pop-Location }
}
