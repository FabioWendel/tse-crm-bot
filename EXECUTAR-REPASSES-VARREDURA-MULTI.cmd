@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo ================================================================
    echo  EXECUTAR REPASSES - VARREDURA FINAL MULTI
    echo ================================================================
    echo.
    echo Vai abrir os operadores 0, 1, 2 e 3.
    echo Cada operador vai ler SOMENTE o seu repasse.csv.
    echo.
    start "Repasse Varredura - Operador 0" cmd /c call "%~f0" 0
    timeout /t 2 /nobreak >nul
    start "Repasse Varredura - Operador 1" cmd /c call "%~f0" 1
    timeout /t 2 /nobreak >nul
    start "Repasse Varredura - Operador 2" cmd /c call "%~f0" 2
    timeout /t 2 /nobreak >nul
    start "Repasse Varredura - Operador 3" cmd /c call "%~f0" 3
    exit /b 0
)

set "OPERADOR=%~1"
title REPASSE VARREDURA MULTI - OPERADOR %OPERADOR%

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "varredura-final-multi\crm_tse_bot_repasse_multi.py" "%OPERADOR%"
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "varredura-final-multi\crm_tse_bot_repasse_multi.py" "%OPERADOR%"
) else (
    echo Ambiente Python nao encontrado.
)

echo.
pause