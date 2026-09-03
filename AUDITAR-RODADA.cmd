@echo off
setlocal
cd /d "%~dp0"

title AUDITAR E RECUPERAR RODADA

echo ================================================================
echo  AUDITORIA SOMENTE LEITURA DA VARREDURA FINAL MULTI
echo ================================================================
echo.
echo  - le os arquivos dos operadores 0, 1, 2 e 3
echo  - identifica erros, repasses e suspeitos de falso skip
echo  - confirma cada CPF pela API autenticada de Pendentes
echo  - gera auditoria, fila de recuperacao e indeterminados
echo  - NAO consulta o TSE
echo  - NAO altera o CRM
echo.
echo ================================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "recuperar-rodada\crm_tse_bot_recuperar_rodada.py"
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "recuperar-rodada\crm_tse_bot_recuperar_rodada.py"
) else (
    echo Ambiente Python nao encontrado. Instale as dependencias na pasta principal.
)

echo.
pause
