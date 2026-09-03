@echo off
cd /d "%~dp0"

echo ================================================================
echo  DESATIVANDO FOCUS SHIELD
echo ================================================================
echo.

call "%~dp0scripts\focus_shield_client.cmd" stop

echo.
echo Focus Shield desativado.
echo Agora voce pode usar o Brave normalmente.
echo.
pause