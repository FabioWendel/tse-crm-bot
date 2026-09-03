@echo off

if /i "%~1"=="start" goto :start
if /i "%~1"=="stop" goto :stop
exit /b 2

:start
if not defined BOT_FOCUS_SHIELD set "BOT_FOCUS_SHIELD=1"
if "%BOT_FOCUS_SHIELD%"=="0" exit /b 0
if defined BOT_FOCUS_SHIELD_TOKEN exit /b 0
if not exist "%~dp0focus_shield.ps1" exit /b 0

for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0focus_shield.ps1" -Mode Acquire -ProjectRoot "%~f2"`) do set "BOT_FOCUS_SHIELD_TOKEN=%%I"
exit /b 0

:stop
if not defined BOT_FOCUS_SHIELD_TOKEN exit /b 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0focus_shield.ps1" -Mode Release -Token "%BOT_FOCUS_SHIELD_TOKEN%" >nul 2>nul
set "BOT_FOCUS_SHIELD_TOKEN="
exit /b 0
