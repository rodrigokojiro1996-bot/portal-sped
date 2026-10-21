@echo off
rem Faz um backup do Portal Fiscal agora (banco, documentos e segredo) e apaga os com mais de 14 dias.
rem Destino: PORTAL_BACKUP_DIR (ou o .env); sem ele, a pasta backups\ do projeto.
rem E o que a tarefa "Portal Fiscal - Backup" roda todo dia. Registro em logs\backup.log.
setlocal
cd /d "%~dp0..\.."
call "%~dp0python.bat"
set "PYTHONIOENCODING=utf-8"
if not exist logs mkdir logs
echo [%date% %time%] Iniciando backup >> logs\backup.log
"%PYTHON%" app.py backup %* >> logs\backup.log 2>&1
set "CODIGO=%errorlevel%"
echo [%date% %time%] Backup encerrado (codigo %CODIGO%) >> logs\backup.log
if not "%CODIGO%"=="0" echo O backup falhou. Veja logs\backup.log.
exit /b %CODIGO%
