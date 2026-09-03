@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\focus_shield_client.cmd" start "%~dp0"

title REVALIDACAO DE NAO ENCONTRADOS

echo ================================================================
echo  REVALIDACAO DE NAO ENCONTRADOS
echo ================================================================
echo.
echo Este modo ira:
echo.
echo 1. Fotografar todos os Nao encontrados atuais
echo 2. Obter somente CPF, nome, mae e nascimento da fonte autorizada
echo 3. Validar os quatro dados obrigatorios
echo 4. Voltar registros aptos para Pendentes
echo 5. Consultar novamente o TSE
echo 6. Encontrado - salvar local - Validado
echo 7. Nao encontrado REAL - Revisar
echo 8. Erro tecnico - Repasse
echo.
echo Nenhuma falha tecnica sera tratada como Nao encontrado.
echo A fotografia sera concluida antes de qualquer alteracao no CRM.
echo.
echo ================================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "revalidar-nao-encontrados\crm_tse_bot_revalidar.py"
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "revalidar-nao-encontrados\crm_tse_bot_revalidar.py"
) else (
    echo Ambiente Python nao encontrado. Instale as dependencias na pasta principal.
)

call "%~dp0scripts\focus_shield_client.cmd" stop
echo.
pause
