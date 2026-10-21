@echo off
rem ==========================================================================
rem  Portal Fiscal - servidor provisorio
rem  Mantem o portal no ar: se o processo cair, ele e reiniciado em 10 segundos.
rem  Registro de execucao: logs\portal.log (acima de 5 MB vira logs\portal.log.1).
rem ==========================================================================
setlocal
cd /d "%~dp0"

rem Python do portal: PORTAL_PYTHON, .venv, "python" do PATH ou "py" (veja scripts\windows\python.bat).
call "%~dp0scripts\windows\python.bat"

rem 127.0.0.1 = acesso somente neste computador.
rem Para liberar na rede interna, troque por 0.0.0.0 (veja o README antes).
if not defined PORTAL_HOST set "PORTAL_HOST=127.0.0.1"
if not defined PORTAL_PORT set "PORTAL_PORT=8000"

if not exist logs mkdir logs

rem Evita duas copias do portal: se a rota /saude responde, o portal ja esta no ar e saudavel.
curl.exe -s -f -m 5 -o nul http://127.0.0.1:%PORTAL_PORT%/saude
if not errorlevel 1 (
    echo O Portal Fiscal ja esta no ar na porta %PORTAL_PORT%.
    echo Acesse: http://127.0.0.1:%PORTAL_PORT%
    exit /b 0
)
rem A porta esta ocupada, mas /saude nao confirma que e o portal (outro programa ou portal com problema).
netstat -ano | findstr /r /c:":%PORTAL_PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo A porta %PORTAL_PORT% esta ocupada, mas o Portal Fiscal nao respondeu em /saude.
    echo Confira com: curl http://127.0.0.1:%PORTAL_PORT%/saude
    exit /b 1
)

echo Portal Fiscal no ar em http://%PORTAL_HOST%:%PORTAL_PORT%
echo Registro em: %~dp0logs\portal.log
echo Para parar, feche esta janela.

:loop
for %%F in (logs\portal.log) do if %%~zF GTR 5242880 move /y logs\portal.log logs\portal.log.1 >nul
echo [%date% %time%] Iniciando Portal Fiscal em http://%PORTAL_HOST%:%PORTAL_PORT% >> logs\portal.log
"%PYTHON%" -u app.py >> logs\portal.log 2>&1
set "CODIGO=%errorlevel%"
rem Codigo 2 = a checagem de inicializacao recusou subir (configuracao, pasta de dados ou banco).
rem Reiniciar nao resolve: para aqui e mostra o motivo, que ficou no log.
if "%CODIGO%"=="2" (
    echo [%date% %time%] Portal nao iniciou: problema de configuracao. Veja as linhas acima. >> logs\portal.log
    echo O Portal Fiscal nao iniciou por um problema de configuracao. Veja logs\portal.log.
    exit /b 2
)
echo [%date% %time%] Portal encerrado (codigo %CODIGO%). Reiniciando em 10 segundos... >> logs\portal.log
rem "ping" serve de pausa: funciona tambem quando o .bat roda sem janela (tarefa do Windows).
ping -n 11 127.0.0.1 >nul
goto loop
