<#
.SYNOPSIS
Preview or run a serial matrix of coding-agent development experiments.
.EXAMPLE
./scripts/launchers/run_code.ps1 -Games brotato,balatro -Models REPLACE_WITH_CLAUDE_MODEL -TokenLimit 100000 -Execute
#>
[CmdletBinding(DefaultParameterSetName='New')]
param(
    [Parameter(Mandatory, ParameterSetName='New')]
    [ValidateNotNullOrEmpty()]
    [string[]]$Models,
    [Parameter(Mandatory, ParameterSetName='Resume')]
    [string]$ResumeRun,
    [Parameter(ParameterSetName='Resume')]
    [string]$RecoveryRouteAuthorizationFile,
    [Parameter(ParameterSetName='Resume')]
    [string]$DevelopmentContinuationAuthorizationFile,
    [Parameter(ParameterSetName='Resume')]
    [string]$RecoveryBudgetNoticeFile,
    [ValidateNotNullOrEmpty()]
    [string[]]$Games = @('brotato'),
    [ValidateSet('claude', 'codex')]
    [string]$Agent = 'claude',
    [ValidateRange(1, 2147483647)]
    [int]$TicBudget = 10000,
    [ValidateRange(1, 2147483647)]
    [int]$MaxTics,
    [string]$TokenLimit = 'unlimited',
    [string]$WallSeconds = 'unlimited',
    [string]$ReasoningEffort,
    [string]$TaskInstructionsFile,
    [ValidateRange(0.0000001, 1)]
    [double]$MinimumDevelopmentFraction,
    [switch]$StopOnHttp400,
    [ValidateSet('auto', 'gamepad', 'gamepad-kbm')]
    [string]$WindowsInput = 'auto',
    [ValidateRange(1, 10000)]
    [int]$Repeats = 1,
    [string]$Profile,
    [ValidateSet('wsl2')]
    [string]$RuntimeBackend = 'wsl2',
    [string]$ProviderConfig,
    [string]$OutputRoot,
    [string]$Python,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher_common.ps1')
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$Python = Get-C4GPython $repo $Python
$runner = Join-Path $repo 'scripts/run_single_experiment.py'
if ($PSCmdlet.ParameterSetName -eq 'Resume') {
    $forbidden = @('Games','Agent','TicBudget','MaxTics','TokenLimit','WallSeconds',
        'ReasoningEffort','TaskInstructionsFile','WindowsInput','Repeats','Profile','RuntimeBackend','OutputRoot','MinimumDevelopmentFraction','StopOnHttp400')
    foreach ($option in $forbidden) {
        if ($PSBoundParameters.ContainsKey($option)) { throw "ResumeRun retains the original $option; do not supply a replacement." }
    }
    $jobs = @([pscustomobject]@{
        Arguments = @($runner, '--resume-run', $ResumeRun)
        Label = "Continue original development: $ResumeRun"
    })
    if ($ProviderConfig) {
        $jobs[0].Arguments += @('--provider-config', $ProviderConfig)
    }
    if ($RecoveryRouteAuthorizationFile) {
        $jobs[0].Arguments += @('--recovery-route-authorization', $RecoveryRouteAuthorizationFile)
    }
    if ($DevelopmentContinuationAuthorizationFile) {
        $jobs[0].Arguments += @('--development-continuation-authorization', $DevelopmentContinuationAuthorizationFile)
    }
    if ($RecoveryBudgetNoticeFile) {
        $jobs[0].Arguments += @('--recovery-budget-notice', $RecoveryBudgetNoticeFile)
    }
} else {
    $commonArgs = @(
        '--agent', $Agent,
        '--tic-budget', "$TicBudget", '--token-limit', $TokenLimit,
        '--wall-seconds', $WallSeconds, '--windows-input', $WindowsInput,
        '--runtime-backend', $RuntimeBackend
    )
    if ($Profile) { $commonArgs += @('--profile', $Profile) }
    if ($ProviderConfig) { $commonArgs += @('--provider-config', $ProviderConfig) }
    if ($OutputRoot) { $commonArgs += @('--output-root', $OutputRoot) }
    if ($PSBoundParameters.ContainsKey('MinimumDevelopmentFraction')) {
        $commonArgs += @('--minimum-development-fraction', $MinimumDevelopmentFraction.ToString([Globalization.CultureInfo]::InvariantCulture))
    }
    if ($StopOnHttp400) { $commonArgs += '--stop-on-http-400' }
    if ($ReasoningEffort) { $commonArgs += @('--reasoning-effort', $ReasoningEffort) }
    if ($TaskInstructionsFile) { $commonArgs += @('--task-instructions-file', $TaskInstructionsFile) }
    if ($MaxTics) { $commonArgs += @('--max-tics', "$MaxTics") }
    $jobs = foreach ($game in $Games) {
        foreach ($model in $Models) {
            $cliArgs = @($runner, '--game', $game, '--model', $model) + $commonArgs
            for ($repeat = 1; $repeat -le $Repeats; $repeat++) {
                [pscustomobject]@{ Arguments = $cliArgs; Label = "$game / $Agent / $model / repeat $repeat" }
            }
        }
    }
}
# Validate the complete matrix before starting any paid job.
foreach ($job in $jobs) {
    Write-Host "Preview: $($job.Label)"
    & $Python @($job.Arguments) --preview
    if ($LASTEXITCODE -ne 0) { throw "Preflight failed: $($job.Label)" }
}
if ($Execute) {
    foreach ($job in $jobs) {
        Write-Host "Execute: $($job.Label)"
        & $Python @($job.Arguments)
        if ($LASTEXITCODE -ne 0) { throw "Experiment failed: $($job.Label). Remaining jobs were not started." }
    }
}
