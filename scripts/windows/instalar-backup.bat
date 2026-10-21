@echo off
rem Agenda o backup diario do Portal Fiscal (todo dia as 3h; se o computador estiver desligado,
rem roda assim que ligar). Pede permissao de administrador automaticamente.
net session >nul 2>&1
if errorlevel 1 (
    echo Solicitando permissao de administrador...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0servico-windows.ps1" -Acao instalar-backup
echo.
pause
