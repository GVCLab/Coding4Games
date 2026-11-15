<#
.SYNOPSIS
Run the stepped Direct baseline. Preview unless -Execute is supplied.
.EXAMPLE
./scripts/launchers/run_baseline.ps1 -Games balatro -Models MODEL -Episodes 3
.EXAMPLE
./scripts/launchers/run_baseline.ps1 -Games balatro -Models MODEL -Execute
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Models,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Games,

    [ValidateRange(1, 2147483647)]
    [int]$TicBudget = 10000,
    [ValidateRange(1, 2147483647)]
    [int]$Episodes = 3,
    [string]$OutputRoot,
    [string]$Python,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher_common.ps1')
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$Python = Get-C4GPython $repo $Python
# Reuse the batch scheduler so locking, evidence and failure handling stay shared.
$batchScript = Join-Path $repo 'scripts/run_all_baselines.py'
if (-not (Test-Path -LiteralPath $batchScript -PathType Leaf)) {
    throw "Batch runner not found: $batchScript"
}
$batchArguments = @($batchScript, '--games')
$batchArguments += $Games
$batchArguments += '--models'
$batchArguments += $Models
$batchArguments += @('--tic-budget', "$TicBudget", '--episodes', "$Episodes")
if ($OutputRoot) { $batchArguments += @('--output-root', $OutputRoot) }
if ($Execute) { $batchArguments += '--execute' }

& $Python @batchArguments
exit $LASTEXITCODE

