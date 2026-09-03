@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo ================================================================
    echo  EXECUTAR RECUPERACAO SEGURA DA RODADA - 4 OPERADORES
    echo ================================================================
    echo.
    echo Le somente recuperar-rodada\dados\fila_recuperacao.csv.
    echo A fila sera dividida em quatro fatias exclusivas.
    echo Cada janela usa perfil, porta, logs e repasse separados.
    echo Antes de cada CPF, o motor confirma pela API se continua Pendente.
    echo A fila original nao sera refeita nem alterada.
    echo.
    echo Abrindo operadores 0, 1, 2 e 3...
    start "Recuperacao - Operador 0" cmd /c call "%~f0" 0
    timeout /t 2 /nobreak >nul
    start "Recuperacao - Operador 1" cmd /c call "%~f0" 1
    timeout /t 2 /nobreak >nul
    start "Recuperacao - Operador 2" cmd /c call "%~f0" 2
    timeout /t 2 /nobreak >nul
    start "Recuperacao - Operador 3" cmd /c call "%~f0" 3
    exit /b 0
)

set "OPERADOR=%~1"
title RECUPERACAO SEGURA - OPERADOR %OPERADOR%

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "recuperar-rodada\crm_tse_bot_executar_recuperacao.py" "%OPERADOR%"
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "recuperar-rodada\crm_tse_bot_executar_recuperacao.py" "%OPERADOR%"
) else (
    echo Ambiente Python nao encontrado. Instale as dependencias na pasta principal.
)

echo.
pause
