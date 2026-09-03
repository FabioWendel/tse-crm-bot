@echo off
setlocal
pushd "%~dp0"
set "TSE_NAVEGADOR=brave"
call "%~dp0scripts\focus_shield_client.cmd" start "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" crm_tse_bot.py %*
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" crm_tse_bot.py %*
) else (
    echo Ambiente Python nao encontrado. Veja a instalacao no README.md.
)

call "%~dp0scripts\focus_shield_client.cmd" stop
popd
pause
