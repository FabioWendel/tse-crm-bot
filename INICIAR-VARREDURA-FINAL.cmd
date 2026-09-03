@echo off
setlocal
cd /d "%~dp0"

title CRM x TSE - VARREDURA FINAL

echo ================================================================
echo  CRM x TSE - VARREDURA FINAL
echo ================================================================
echo.
echo Esta rotina vai:
echo.
echo  1. Ler os consultas.csv dos operadores 0, 1, 2 e 3
echo  2. Fotografar a base total e os Pendentes atuais pela API do CRM
echo  3. Reconciliar o historico dos 4 operadores com o estado atual
echo  4. Validar nome, CPF, nome da mae e nascimento
echo  5. Montar apenas o residual e gerar os arquivos de auditoria
echo.
echo Nenhuma consulta ao TSE sera iniciada sem confirmacao.
echo ================================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "varredura-final\crm_tse_bot_varredura.py"
) else (
    python "varredura-final\crm_tse_bot_varredura.py"
)

echo.
echo ================================================================
echo Processo encerrado.
echo ================================================================
pause
