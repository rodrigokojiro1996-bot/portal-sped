@echo off
rem Cria um usuario de suporte do Portal Fiscal (ve todas as lojas, so consulta e baixa arquivos).
rem A credencial e mostrada na tela e acrescentada em admin-credentials.txt na Area de Trabalho.
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
echo ============================================
echo   Portal Fiscal - criar usuario de suporte
echo ============================================
echo.
set "LOGIN=suporte"
set /p "LOGIN=Usuario de login [suporte]: "
set "NOME=Suporte Farmacias Associadas"
set /p "NOME=Nome exibido [Suporte Farmacias Associadas]: "
echo.
"%PYTHON%" app.py criar-suporte "%LOGIN%" "%NOME%" --salvar-em "%USERPROFILE%\Desktop\admin-credentials.txt"
echo.
pause
