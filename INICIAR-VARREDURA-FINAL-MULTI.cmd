@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo ================================================================
    echo  VARREDURA FINAL - 4 INSTANCIAS
    echo ================================================================
    echo.
    echo A fila salva sera dividida em quatro fatias exclusivas.
    echo Cada janela usa perfil, porta, logs e repasse separados.
    echo Nao regenere fila_varredura.csv durante esta execucao.
    echo.
    echo Abrindo operadores 0, 1, 2 e 3...
    start "Varredura Final - Operador 0" cmd /c call "%~f0" 0
    timeout /t 2 /nobreak >nul
    start "Varredura Final - Operador 1" cmd /c call "%~f0" 1
    timeout /t 2 /nobreak >nul
    start "Varredura Final - Operador 2" cmd /c call "%~f0" 2
    timeout /t 2 /nobreak >nul
    start "Varredura Final - Operador 3" cmd /c call "%~f0" 3
    exit /b 0
)

set "OPERADOR=%~1"
title VARREDURA FINAL MULTI - OPERADOR %OPERADOR%

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "varredura-final-multi\crm_tse_bot_varredura_multi.py" "%OPERADOR%"
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "varredura-final-multi\crm_tse_bot_varredura_multi.py" "%OPERADOR%"
) else (
    echo Ambiente Python nao encontrado. Instale as dependencias na pasta principal.
)

echo.
pause
