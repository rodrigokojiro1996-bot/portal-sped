@echo off
rem ==========================================================================
rem  Portal Fiscal - servidor provisorio
rem  Mantem o portal no ar: se o processo cair, ele e reiniciado em 10 segundos.
rem  Registro de execucao: logs\portal.log
rem ==========================================================================
setlocal
cd /d "%~dp0"

rem Python do ambiente virtual do projeto (pasta .venv ao lado de portal-sped).
set "PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

rem 127.0.0.1 = acesso somente neste computador.
rem Para liberar na rede interna, troque por 0.0.0.0 (veja o README antes).
if not defined PORTAL_HOST set "PORTAL_HOST=127.0.0.1"
if not defined PORTAL_PORT set "PORTAL_PORT=8000"

if not exist logs mkdir logs

rem Evita duas copias do portal disputando a mesma porta.
netstat -ano | findstr /r /c:":%PORTAL_PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo O Portal Fiscal ja esta no ar na porta %PORTAL_PORT%.
    echo Acesse: http://127.0.0.1:%PORTAL_PORT%
    exit /b 0
)

echo Portal Fiscal no ar em http://%PORTAL_HOST%:%PORTAL_PORT%
echo Registro em: %~dp0logs\portal.log
echo Para parar, feche esta janela.

:loop
echo [%date% %time%] Iniciando Portal Fiscal em http://%PORTAL_HOST%:%PORTAL_PORT% >> logs\portal.log
"%PYTHON%" -u app.py >> logs\portal.log 2>&1
echo [%date% %time%] Portal encerrado (codigo %errorlevel%). Reiniciando em 10 segundos... >> logs\portal.log
rem "ping" serve de pausa: funciona tambem quando o .bat roda sem janela (tarefa do Windows).
ping -n 11 127.0.0.1 >nul
goto loop
