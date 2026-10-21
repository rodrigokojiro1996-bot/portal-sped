@echo off
rem Escolhe o Python do portal e guarda o caminho em PYTHON. Os outros .bat chamam com: call "...\python.bat"
rem Ordem: PORTAL_PYTHON (gravado pelas tarefas do Windows ao instalar), .venv ao lado da pasta portal-sped,
rem "python" do PATH e, por fim, o inicializador "py" do Windows.
if defined PORTAL_PYTHON if exist "%PORTAL_PYTHON%" (
    set "PYTHON=%PORTAL_PYTHON%"
    goto :eof
)
set "PYTHON=%~dp0..\..\..\.venv\Scripts\python.exe"
if exist "%PYTHON%" goto :eof
set "PYTHON=python"
where python >nul 2>&1 && goto :eof
set "PYTHON=py"
