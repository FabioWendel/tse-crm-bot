@echo off
setlocal
pushd "%~dp0"

echo Abrindo quatro operadores independentes neste computador...
start "CRM TSE - Operador 0" cmd /c call "%~dp0INICIAR-OPERADOR.cmd" 0
timeout /t 2 /nobreak >nul
start "CRM TSE - Operador 1" cmd /c call "%~dp0INICIAR-OPERADOR.cmd" 1
timeout /t 2 /nobreak >nul
start "CRM TSE - Operador 2" cmd /c call "%~dp0INICIAR-OPERADOR.cmd" 2
timeout /t 2 /nobreak >nul
start "CRM TSE - Operador 3" cmd /c call "%~dp0INICIAR-OPERADOR.cmd" 3

popd
