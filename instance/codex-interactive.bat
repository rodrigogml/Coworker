@echo off
setlocal
if not "%~1"=="" (
    echo Este iniciador nao aceita argumentos; use a configuracao da propria instancia. 1>&2
    exit /b 2
)
cd /d "%~dp0"
python scripts\codex_interactive.py
exit /b %ERRORLEVEL%
