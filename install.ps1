[CmdletBinding()]
param(
    [switch]$NonInteractive,
    [switch]$SkipTelegram,
    [switch]$NoStart,
    [ValidateSet('none','install','remove','start','stop','status')]
    [string]$ServiceAction = 'none',
    [string]$ServiceName = '',
    [ValidateSet('automatic_delayed','automatic','manual')]
    [string]$ServiceStartup = 'automatic_delayed',
    [ValidateSet('current_user','local_system')]
    [string]$ServiceAccountMode = 'current_user'
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw "Python 3 não foi encontrado no PATH. Instale-o antes de configurar a instância."
}

$arguments = @("$projectRoot\scripts\install_instance.py")
if ($NonInteractive) { $arguments += "--non-interactive" }
if ($SkipTelegram) { $arguments += "--skip-telegram" }
if ($NoStart) { $arguments += "--no-start" }
if ($ServiceAction -ne 'none') { $arguments += @('--service-action', $ServiceAction) }
if ($ServiceName) { $arguments += @('--service-name', $ServiceName) }
if ($ServiceStartup) { $arguments += @('--service-startup', $ServiceStartup) }
if ($ServiceAccountMode) { $arguments += @('--service-account-mode', $ServiceAccountMode) }
& $python.Source @arguments
exit $LASTEXITCODE
