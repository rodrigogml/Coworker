$ErrorActionPreference = "Stop"
if ($args.Count -ne 0) {
    [Console]::Error.WriteLine("Este iniciador nao aceita argumentos; use a configuracao da propria instancia.")
    exit 2
}
Set-Location -LiteralPath $PSScriptRoot
& python (Join-Path $PSScriptRoot "scripts\codex_interactive.py")
exit $LASTEXITCODE
