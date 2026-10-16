@echo off
rem Remove o inicio automatico do Portal Fiscal e para o servidor.
rem Pede permissao de administrador automaticamente.
net session >nul 2>&1
if errorlevel 1 (
    echo Solicitando permissao de administrador...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\servico-windows.ps1" -Acao remover
echo.
pause
