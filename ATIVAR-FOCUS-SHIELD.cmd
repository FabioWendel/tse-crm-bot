@echo off
cd /d "%~dp0"

echo ================================================================
echo  ATIVANDO FOCUS SHIELD
echo ================================================================
echo.
echo O Brave do bot nao podera roubar o foco.
echo.
echo IMPORTANTE:
echo Se precisar mexer manualmente no Brave, execute:
echo DESATIVAR-FOCUS-SHIELD.cmd
echo.

call "%~dp0scripts\focus_shield_client.cmd" start "%~dp0"

echo.
echo Focus Shield solicitado.
echo.
pause