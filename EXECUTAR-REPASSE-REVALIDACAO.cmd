@echo off
setlocal
cd /d "%~dp0"

echo ================================================================
echo  REPASSE - REVALIDACAO DE NAO ENCONTRADOS
echo ================================================================
echo.
echo Este modo le somente:
echo revalidar-nao-encontrados\dados\repasse.csv
echo.
echo Nao refaz a fila completa.
echo.

if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" "revalidar-nao-encontrados\executar_repasse.py"
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "revalidar-nao-encontrados\executar_repasse.py"
) else (
    echo Ambiente Python nao encontrado.
)

echo.
pause