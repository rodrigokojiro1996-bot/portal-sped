@echo off
rem Instala o Portal Fiscal para iniciar sozinho junto com o Windows.
rem Pede permissao de administrador automaticamente.
net session >nul 2>&1
if errorlevel 1 (
    echo Solicitando permissao de administrador...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\servico-windows.ps1" -Acao instalar
echo.
pause
