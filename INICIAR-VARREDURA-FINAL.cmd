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
echo  2. Consultar a base atual do CRM
echo  3. Varrer todas as abas do CRM
echo  4. Remover quem ja foi concluido
echo  5. Ignorar quem outra maquina ja resolveu
echo  6. Validar nome, CPF, nome da mae e nascimento
echo  7. Montar uma fila somente com novos e falhas tecnicas
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