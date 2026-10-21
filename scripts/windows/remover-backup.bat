@echo off
rem Remove o backup diario agendado do Portal Fiscal. Os backups ja feitos continuam na pasta de destino.
net session >nul 2>&1
if errorlevel 1 (
    echo Solicitando permissao de administrador...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0servico-windows.ps1" -Acao remover-backup
echo.
pause
