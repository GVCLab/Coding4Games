<#
.SYNOPSIS
Test the selected frozen program from each development directory, serially.
.EXAMPLE
./scripts/launchers/test_code.ps1 -RunDirs @('out/experiments/run1', 'out/experiments/run2')
.EXAMPLE
./scripts/launchers/test_code.ps1 -RunDirs 'out/experiments/run1' -Repeats 3 -Execute
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [Alias('RunDir')]
    [string[]]$RunDirs,
    [ValidateRange(1, 10000)]
    [int]$Repeats = 3,
    [ValidateRange(1, 2147483647)]
    [int]$MaxTics,
    [string]$Python,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher_common.ps1')
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$Python = Get-C4GPython $repo $Python
$scenarios = @(Get-C4GRegisteredScenarios $repo $Python)
# Resolve caller-relative inputs before switching to the repository directory.
$jobs = foreach ($runDir in $RunDirs) {
    $source = (Resolve-Path -LiteralPath $runDir).Path
    $config = Get-Content -LiteralPath (Join-Path $source 'config.json') -Raw | ConvertFrom-Json
    $programs = Join-Path $source 'programs'
    $selected = Get-Content -LiteralPath (Join-Path $programs 'final.json') -Raw | ConvertFrom-Json
    foreach ($field in @('profile', 'scenario', 'windows_input', 'tic_budget')) {
        if ($null -eq $config.$field -or "$($config.$field)" -eq '') {
            throw "Missing config field '$field' in $source"
        }
    }
    if ($scenarios -cnotcontains $config.scenario) {
        throw "Scenario '$($config.scenario)' is not registered for independent tests: $source"
    }
    if ($selected.id -notmatch '^[a-f0-9]{64}$') { throw "Invalid selected program in $programs" }
    if (-not (Test-Path -LiteralPath (Join-Path $programs $selected.id) -PathType Container)) {
        throw "Selected program archive is missing in $programs"
    }
    $runtimeBackend = if ($config.runtime_backend) { "$($config.runtime_backend)" } else { 'unsupported-legacy' }
    if ($runtimeBackend -ne 'wsl2') { throw "Only WSL2 source runs are supported; historical evidence is unchanged: $source" }
    if (-not (Test-Path -LiteralPath $config.profile -PathType Leaf)) {
        throw "Runtime profile is missing: $($config.profile)"
    }
    [pscustomobject]@{ Source = $source; Config = $config; Programs = $programs; Id = $selected.id; RuntimeBackend = $runtimeBackend }
}

$batchId = (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8)
Push-Location $repo
try {
    $jobIndex = 0
    foreach ($job in $jobs) {
        $jobIndex++
        $config = $job.Config
        $wall = if ($null -eq $config.wall_seconds) { 'unlimited' } else { "$($config.wall_seconds)" }
        for ($repeat = 1; $repeat -le $Repeats; $repeat++) {
            $outDir = Join-Path $job.Source "test/${batchId}_job_${jobIndex}/repeat_${repeat}_input-$($config.windows_input)"
            $cliArgs = @(
                '-m', 'coding4game_gui.benchmark', 'test',
                '--profile', "$($config.profile)",
                '--runtime-backend', $job.RuntimeBackend,
                '--scenario', "$($config.scenario)",
                '--windows-input', "$($config.windows_input)",
                '--source-programs', $job.Programs,
                '--program-id', $job.Id,
                '--tic-budget', "$($config.tic_budget)",
                '--wall-seconds', $wall,
                '--out-dir', $outDir
            )
            $episodeCap = @($MaxTics, $config.max_tics) | Where-Object { $_ -gt 0 } | Measure-Object -Minimum
            if ($null -ne $episodeCap.Minimum) { $cliArgs += @('--max-tics', "$($episodeCap.Minimum)") }
            if ($Execute) { $cliArgs += '--execute' }
            Write-Host "Source: $($job.Source) | Test $repeat/$Repeats | Execute: $Execute"
            & $Python @cliArgs
            if ($LASTEXITCODE -ne 0) { throw "Test process failed with exit code $LASTEXITCODE. Output: $outDir" }
            if ($Execute) {
                $result = Get-Content -LiteralPath (Join-Path $outDir 'config.json') -Raw | ConvertFrom-Json
                $episode = $result.result
                $programFailed = $null -ne $episode.program_returncode -and $episode.program_returncode -gt 0
                if ($result.status -ne 'complete' -or $result.result_valid -ne $true -or $programFailed) {
                    throw "Test did not complete validly (status: $($result.status); valid: $($result.result_valid); program return code: $($episode.program_returncode); reason: $($episode.end_reason); tics: $($episode.tics)/$($episode.resolved_max_tics); overshoot: $($episode.budget_overshoot_tics)). Inspect $outDir"
                }
            }
        }
    }
}
finally {
    Pop-Location
}
