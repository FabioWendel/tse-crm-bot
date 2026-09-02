@echo off
setlocal
pushd "%~dp0"
set "TSE_NAVEGADOR=brave"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "somente-pendentes\crm_tse_bot_pendentes.py" %*
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "somente-pendentes\crm_tse_bot_pendentes.py" %*
) else (
    echo Ambiente Python nao encontrado. Veja a instalacao no README.md.
)

popd
pause
