# Instala ou remove as tarefas do Windows do Portal Fiscal:
#   "Portal Fiscal"          - inicia o portal junto com o computador (iniciar-portal.bat), como SYSTEM;
#   "Portal Fiscal - Backup" - faz o backup todo dia as 3h (scripts\windows\backup.bat), como SYSTEM.
# Uso (como administrador): servico-windows.ps1 -Acao instalar | remover | instalar-backup | remover-backup | status
param(
    [ValidateSet('instalar', 'remover', 'instalar-backup', 'remover-backup', 'status')]
    [string]$Acao = 'status'
)
$ErrorActionPreference = 'Stop'
$nome = 'Portal Fiscal'
$nomeBackup = 'Portal Fiscal - Backup'
$raiz = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$bat = Join-Path $raiz 'iniciar-portal.bat'
$batBackup = Join-Path $PSScriptRoot 'backup.bat'
$porta = 8000

function Caminho-Python {
    # A tarefa roda como SYSTEM, que nao enxerga o Python instalado so para o usuario (PATH do usuario).
    # Por isso o caminho real do interpretador e descoberto agora e passado para a tarefa em PORTAL_PYTHON.
    $venv = Join-Path (Split-Path -Parent $raiz) '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    foreach ($comando in @('python', 'py')) {
        if (Get-Command $comando -ErrorAction SilentlyContinue) {
            $caminho = & $comando -c "import sys; print(sys.executable)" 2>$null
            if ($caminho -and (Test-Path $caminho)) { return $caminho.Trim() }
        }
    }
    throw 'Python nao encontrado. Instale o Python 3.11+ ou crie a pasta .venv ao lado de portal-sped.'
}

function Acao-Tarefa([string]$arquivo) {
    $python = Caminho-Python
    Write-Host "Python usado pela tarefa: $python"
    return New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c set `"PORTAL_PYTHON=$python`" && `"$arquivo`"" -WorkingDirectory $raiz
}

function Parar-Portal {
    # Fecha janelas do iniciar-portal.bat abertas manualmente (senao elas religariam o portal)...
    Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -like '*iniciar-portal.bat*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # ...e o Python que estiver na porta (encerrar a tarefa nem sempre encerra o processo filho).
    $processos = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($id in $processos) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}

function Saude-Portal {
    try {
        $resposta = Invoke-WebRequest -Uri "http://127.0.0.1:$porta/saude" -UseBasicParsing -TimeoutSec 5
        return "no ar ($($resposta.Content))"
    } catch {
        if (Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue) { return 'porta ocupada, mas /saude nao respondeu bem' }
        return 'parado'
    }
}

switch ($Acao) {
    'instalar' {
        if (-not (Test-Path $bat)) { throw "Arquivo nao encontrado: $bat" }
        $gatilho = New-ScheduledTaskTrigger -AtStartup
        $conta = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        # Sem limite de tempo (o padrao do Windows encerraria em 3 dias), roda na bateria
        # e tenta de novo a cada minuto se a tarefa falhar.
        $config = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $nome -Action (Acao-Tarefa $bat) -Trigger $gatilho -Principal $conta -Settings $config `
            -Description 'Mantem o Portal Fiscal (SPED) no ar desde a inicializacao do Windows.' -Force | Out-Null
        Parar-Portal
        Start-ScheduledTask -TaskName $nome
        Start-Sleep -Seconds 5
        Write-Host "Servico '$nome' instalado. O portal inicia sozinho sempre que o computador ligar."
        Write-Host "Portal: $(Saude-Portal)"
    }
    'remover' {
        if (Get-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $nome -Confirm:$false
        }
        Parar-Portal
        Write-Host "Servico '$nome' removido e portal parado."
    }
    'instalar-backup' {
        if (-not (Test-Path $batBackup)) { throw "Arquivo nao encontrado: $batBackup" }
        $gatilho = New-ScheduledTaskTrigger -Daily -At '03:00'
        $conta = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        # StartWhenAvailable: se o computador estava desligado as 3h, o backup roda assim que ele ligar.
        $config = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $nomeBackup -Action (Acao-Tarefa $batBackup) -Trigger $gatilho -Principal $conta -Settings $config `
            -Description 'Backup diario do Portal Fiscal (banco, documentos e segredo), com retencao de 14 dias.' -Force | Out-Null
        Write-Host "Backup agendado: todo dia as 3h. Registro em $(Join-Path $raiz 'logs\backup.log')."
        Write-Host 'Para testar agora: Start-ScheduledTask -TaskName "Portal Fiscal - Backup"'
    }
    'remover-backup' {
        if (Get-ScheduledTask -TaskName $nomeBackup -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $nomeBackup -Confirm:$false
        }
        Write-Host "Backup agendado removido. Os backups ja feitos continuam na pasta de destino."
    }
    'status' {
        foreach ($tarefa in @($nome, $nomeBackup)) {
            $agendada = Get-ScheduledTask -TaskName $tarefa -ErrorAction SilentlyContinue
            if (-not $agendada) { Write-Host "Tarefa '$tarefa': nao instalada." ; continue }
            $info = Get-ScheduledTaskInfo -TaskName $tarefa
            Write-Host ("Tarefa '{0}': {1} (ultima execucao: {2}, resultado: {3})" -f $tarefa, $agendada.State, $info.LastRunTime, $info.LastTaskResult)
        }
        Write-Host "Portal na porta ${porta}: $(Saude-Portal)"
    }
}
