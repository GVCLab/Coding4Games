<#
.SYNOPSIS
Top up completed developments to three valid final tests and print scores.
.EXAMPLE
./scripts/launchers/test_code_remaining.ps1 -Execute
.EXAMPLE
./scripts/launchers/test_code_remaining.ps1
#>
[CmdletBinding()]
param(
    [string]$Root,
    [ValidateRange(1, 10000)]
    [int]$Repeats = 3,
    [string]$Python,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher_common.ps1')
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$Python = Get-C4GPython $repo $Python
$scenarios = @(Get-C4GRegisteredScenarios $repo $Python)
if (-not $Root) {
    Push-Location $repo
    try {
        $rootJson = & $Python -B -c "import json; from coding4game_gui.operator_config import operator_paths; print(json.dumps(str(operator_paths()['output_root'])))"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the experiment output directory.' }
        $Root = $rootJson | ConvertFrom-Json
    }
    finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        Write-Host "No completed developments found. The configured output directory does not exist: $Root"
        return
    }
}

function Read-RunJson([string]$Path) {
    try { Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { Write-Warning "Cannot read ${Path}: $_" }
}

function Find-Developments([string]$Directory) {
    # Prune archives and evidence trees instead of scanning exported sandboxes.
    $configPath = Join-Path $Directory 'config.json'
    if (Test-Path -LiteralPath $configPath) {
        $config = Read-RunJson $configPath
        $finalPath = Join-Path $Directory 'programs/final.json'
        if ($config.mode -eq 'develop' -and $config.status -eq 'complete' -and
            $config.runtime_backend -eq 'wsl2' -and
            $scenarios -ccontains $config.scenario -and
            (Test-Path -LiteralPath $finalPath)) {
            $final = Read-RunJson $finalPath
            if ($final.id -match '^[a-f0-9]{64}$') {
                [pscustomobject]@{ Source = $Directory; Config = $config; Id = $final.id }
            }
        }
        return
    }
    foreach ($child in Get-ChildItem -LiteralPath $Directory -Directory) {
        if ($child.Name -notin @('archive', 'baseline', 'test', 'programs', 'sandbox_exports', 'wsl2_exports', 'dependencies') -and
            -not ($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Find-Developments $child.FullName
        }
    }
}

function Get-CompletedTests($Job) {
    $testRoot = Join-Path $Job.Source 'test'
    if (-not (Test-Path -LiteralPath $testRoot)) { return }
    $seen = @{}
    # Only the batch/repeat layout written by test_code.ps1 is eligible.
    $files = @(Get-ChildItem -LiteralPath $testRoot -Directory | ForEach-Object {
        Get-ChildItem -LiteralPath $_.FullName -Directory | ForEach-Object {
            $path = Join-Path $_.FullName 'config.json'
            if (Test-Path -LiteralPath $path) { Get-Item -LiteralPath $path }
        }
    } | Sort-Object FullName)
    foreach ($file in $files) {
        $test = Read-RunJson $file.FullName
        if ($test.mode -ne 'test' -or $test.status -ne 'complete' -or
            $test.result_valid -ne $true -or $test.result.complete -ne $true -or
            $test.program_id -ne $Job.Id) { continue }
        $matches = $true
        foreach ($field in @('scenario', 'windows_input', 'tic_budget', 'max_tics', 'wall_seconds')) {
            if ($test.$field -ne $Job.Config.$field) { $matches = $false }
        }
        $sourceBackend = if ($Job.Config.runtime_backend) { "$($Job.Config.runtime_backend)" } else { 'unsupported-legacy' }
        $testBackend = if ($test.runtime_backend) { "$($test.runtime_backend)" } else { 'unsupported-legacy' }
        if ($testBackend -ne $sourceBackend) { $matches = $false }
        if (-not $matches) { continue }
        $uid = $test.result.episode_uid
        if (-not $uid) { $uid = $test.result.episode_id }
        if (-not $uid -or $seen.ContainsKey($uid)) { continue }
        $seen[$uid] = $true
        [pscustomobject]@{ Score = $test.result.score; Path = $file.DirectoryName }
    }
}

function Show-Summary($Jobs) {
    $rows = foreach ($job in $Jobs) {
        $tests = @(Get-CompletedTests $job | Select-Object -First $Repeats)
        $scores = @($tests | Where-Object { $null -ne $_.Score } | ForEach-Object { [double]$_.Score })
        $mean = 'N/A'
        if ($tests.Count -eq $Repeats -and $scores.Count -eq $Repeats) {
            $mean = ([math]::Round(($scores | Measure-Object -Average).Average, 4)).ToString('0.####', [Globalization.CultureInfo]::InvariantCulture)
        }
        [pscustomobject]@{
            Game = $job.Config.scenario
            Model = $job.Config.model
            Completed = "$($tests.Count)/$Repeats"
            Scores = (@($tests | ForEach-Object { if ($null -eq $_.Score) { 'null' } else { "$($_.Score)" } }) -join ', ')
            Mean = $mean
            Source = $job.Source
            Tests = $tests
        }
    }
    Write-Host "`nFinal test summary (earliest matching valid tests; null is never zero):"
    $rows | Format-Table Game, Model, Completed, Scores, Mean -AutoSize | Out-Host
    foreach ($row in $rows) {
        Write-Host "Source: $($row.Source)"
        foreach ($test in $row.Tests) {
            Write-Host "  Score=$($test.Score) | $($test.Path)"
        }
    }
}

$rootPath = (Resolve-Path -LiteralPath $Root).Path
$jobs = @(Find-Developments $rootPath | Sort-Object Source)
if ($jobs.Count -eq 0) { Write-Host 'No completed developments with selected programs found.'; return }
try {
    foreach ($job in $jobs) {
        $completed = @(Get-CompletedTests $job).Count
        $missing = [math]::Max(0, $Repeats - $completed)
        Write-Host "Source: $($job.Source) | Completed: $completed | Missing: $missing | Execute: $Execute"
        if ($missing -gt 0) {
            & (Join-Path $PSScriptRoot 'test_code.ps1') -RunDirs @($job.Source) -Repeats $missing -Python $Python -Execute:$Execute
            if ($Execute -and @(Get-CompletedTests $job).Count -lt $Repeats) {
                throw "Tests remain incomplete for $($job.Source). Inspect evidence before retrying."
            }
        }
    }
}
finally { Show-Summary $jobs }
