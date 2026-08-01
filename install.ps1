[CmdletBinding()]
param(
    [switch]$NonInteractive,
    [switch]$SkipTelegram,
    [switch]$NoStart
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
& $python.Source @arguments
exit $LASTEXITCODE
