@echo off
setlocal
set "TSE_URL=https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral#/"

if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" "%TSE_URL%"
    exit /b 0
)

if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    start "" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" "%TSE_URL%"
    exit /b 0
)

if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (
    start "" "%LocalAppData%\Google\Chrome\Application\chrome.exe" "%TSE_URL%"
    exit /b 0
)

echo Google Chrome nao encontrado.
pause
exit /b 1
