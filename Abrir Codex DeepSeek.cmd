@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo Python nao encontrado. Verifique a instalacao do Python.
    pause
    exit /b 1
)
python "%~dp0run_codex.py" -- %*
set "result=%errorlevel%"
if not "%result%"=="0" (
    echo.
    echo Codex finalizou com erro %result%.
    pause
)
exit /b %result%
