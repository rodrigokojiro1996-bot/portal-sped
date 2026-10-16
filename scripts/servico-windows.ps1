# Instala ou remove a tarefa do Windows que inicia o Portal Fiscal junto com o computador.
# Uso (como administrador): servico-windows.ps1 -Acao instalar | remover | status
param(
    [ValidateSet('instalar', 'remover', 'status')]
    [string]$Acao = 'status'
)
$ErrorActionPreference = 'Stop'
$nome = 'Portal Fiscal'
$raiz = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $raiz 'iniciar-portal.bat'
$porta = 8000

function Parar-Portal {
    # Fecha janelas do iniciar-portal.bat abertas manualmente (senao elas religariam o portal)...
    Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -like '*iniciar-portal.bat*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # ...e o Python que estiver na porta (encerrar a tarefa nem sempre encerra o processo filho).
    $processos = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($id in $processos) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}

switch ($Acao) {
    'instalar' {
        if (-not (Test-Path $bat)) { throw "Arquivo nao encontrado: $bat" }
        $acaoTarefa = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$bat`"" -WorkingDirectory $raiz
        $gatilho = New-ScheduledTaskTrigger -AtStartup
        $conta = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        # Sem limite de tempo (o padrao do Windows encerraria em 3 dias), roda na bateria
        # e tenta de novo a cada minuto se a tarefa falhar.
        $config = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $nome -Action $acaoTarefa -Trigger $gatilho -Principal $conta -Settings $config `
            -Description 'Mantem o Portal Fiscal (SPED) no ar desde a inicializacao do Windows.' -Force | Out-Null
        Parar-Portal
        Start-ScheduledTask -TaskName $nome
        Start-Sleep -Seconds 4
        Write-Host "Servico '$nome' instalado. O portal inicia sozinho sempre que o computador ligar."
        Write-Host "Acesse: http://127.0.0.1:$porta"
    }
    'remover' {
        if (Get-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $nome -Confirm:$false
        }
        Parar-Portal
        Write-Host "Servico '$nome' removido e portal parado."
    }
    'status' {
        $tarefa = Get-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue
        if (-not $tarefa) { Write-Host "Servico '$nome' nao esta instalado." } else { Write-Host "Servico '$nome': $($tarefa.State)" }
        $ativo = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue
        Write-Host ("Portal na porta {0}: {1}" -f $porta, $(if ($ativo) { 'no ar' } else { 'parado' }))
    }
}
