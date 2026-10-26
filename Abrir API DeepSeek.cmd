@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo Python nao encontrado. Verifique a instalacao do Python.
    pause
    exit /b 1
)
echo API DeepSeek para outros programas:
echo OpenAI Base URL: http://127.0.0.1:8000/v1
echo Anthropic Base URL: http://127.0.0.1:8000
echo Modelo: deepseek-web
echo Feche esta janela ou pressione Ctrl+C para encerrar o servidor.
echo.
python "%~dp0openai_server.py" --host 127.0.0.1 --port 8000 %*
set "result=%errorlevel%"
if not "%result%"=="0" (
    echo.
    echo A API encerrou com erro %result%.
    pause
)
exit /b %result%
