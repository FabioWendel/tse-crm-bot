@echo off
setlocal

if "%~1"=="" (
    echo Informe o numero do operador: 0, 1, 2 ou 3.
    pause
    exit /b 2
)

set "OPERADOR=%~1"
set "PASTA_MULTI=%~dp0"
set "RAIZ=%~dp0.."
pushd "%RAIZ%"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "multi-instancia\crm_tse_bot_multi.py" "%OPERADOR%"
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "multi-instancia\crm_tse_bot_multi.py" "%OPERADOR%"
) else (
    echo Ambiente Python nao encontrado. Instale as dependencias na pasta principal.
)

popd
pause
