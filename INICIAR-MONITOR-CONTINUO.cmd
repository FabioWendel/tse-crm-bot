@echo off
setlocal
cd /d "%~dp0"

title MONITOR CONTINUO CRM x TSE

echo ================================================================
echo  MONITOR CONTINUO CRM x TSE
echo ================================================================
echo.
echo  - monitora novos Pendentes
echo  - consulta automaticamente
echo  - usa o fluxo normal do CRM
echo  - Nao achei real -^> marca Nao achei
echo  - erro tecnico -^> repasse
echo  - falhas repetidas -^> quarentena
echo  - Ctrl+C encerra com seguranca
echo.
echo Os dados deste modo ficam em monitor-continuo\dados.
echo ================================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "monitor-continuo\crm_tse_bot_monitor.py"
) else (
    python "monitor-continuo\crm_tse_bot_monitor.py"
)

echo.
echo ================================================================
echo Monitor encerrado.
echo ================================================================
pause
