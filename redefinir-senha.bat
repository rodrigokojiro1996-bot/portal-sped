@echo off
rem Gera uma senha nova para uma loja do Portal Fiscal e mostra na tela.
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
echo ============================================
echo   Portal Fiscal - redefinir senha de loja
echo ============================================
echo.
set /p "CNPJ=Digite o CNPJ da loja e tecle Enter: "
echo.
"%PYTHON%" app.py redefinir-senha "%CNPJ%"
echo.
echo Anote a senha acima. Ela nao fica salva em nenhum arquivo.
pause
