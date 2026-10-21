# Portal Fiscal | Farmácias Associadas

Versão atual: **1.6.0**. Veja o [CHANGELOG](CHANGELOG.md) e a [documentação técnica da 1.6](docs/versao-1.6.md).

Aplicação privada para publicar arquivos SPED EFD por loja, tipo e competência. Depois do acesso, a lateral identifica a loja e o CNPJ e oferece as áreas SPED Fiscal e SPED Contribuições. O administrador cadastra os acessos e envia os arquivos; cada loja pode consultar os próprios documentos e, quando vinculada a filiais, alternar entre elas sem encerrar a sessão.

> O portal roda com Python 3.11 ou superior, sem pacotes externos. É uma base funcional para validação do fluxo e não passou por auditoria independente de segurança. Não o exponha à internet nem use dados fiscais reais em produção antes de seguir as recomendações de implantação abaixo.

## Requisitos

- Python 3.11 ou superior.
- Navegador moderno.
- Para produção: servidor HTTPS gerenciado por proxy reverso, backups protegidos e controle de acesso ao servidor.

## Estrutura

```
portal-sped/
├── app.py                 ponto de entrada: python app.py [comando]
├── portal/                código do portal
│   ├── config.py          versão, caminhos e variáveis de ambiente (.env opcional)
│   ├── db.py              SQLite, pasta dos documentos, segredo e migrações
│   ├── auth.py            contas, senhas e tokens
│   ├── sped.py            registro 0000: tipo, CNPJ, período e competência
│   ├── publicacao.py      publicação e resumo fiscal da competência
│   ├── calendario.py      dias úteis e prazos de publicação
│   ├── auditoria.py       monitoramento do suporte (desligado)
│   ├── backup.py          cópia de segurança
│   ├── views/             HTML das páginas
│   ├── handler.py         rotas HTTP
│   └── cli.py             comandos de linha
├── resumo_sped.py         leitura dos registros e gráficos do resumo fiscal
├── migrations/            alterações do banco, numeradas (001_inicial.sql, ...)
├── scripts/windows/       serviço, backup agendado, usuário de suporte, senha
├── iniciar-portal.bat     sobe o portal e o mantém no ar
├── static/  tests/  docs/
└── .env.example           modelo de configuração
```

## Iniciar no Windows

Abra o PowerShell nesta pasta (`portal-sped`) e execute:

```powershell
python --version
python -m unittest discover -s tests -v
python app.py create-admin
python app.py
```

No assistente, informe o usuário administrativo, nome e uma senha forte com pelo menos 12 caracteres. A senha é solicitada sem aparecer na tela. Acesse <http://127.0.0.1:8000> e entre com o usuário e a senha criados. O administrador pode cadastrar usuários de loja; a loja entra com seu CNPJ e senha.

O servidor escuta apenas em `127.0.0.1` por padrão. Para encerrar, use `Ctrl+C`. O primeiro administrador é criado explicitamente; não há senha padrão nem conta de demonstração.

Antes de servir (ou de rodar qualquer comando), o portal confere a configuração, se a pasta de dados aceita gravação, o segredo local e se o banco está na migração esperada. Se algo falhar, ele mostra o motivo e sai com código 2, sem subir pela metade.

Cada instalação deve criar seu próprio administrador e cadastrar suas lojas. Nenhum usuário, senha, arquivo fiscal ou conjunto de dados local é fornecido pelo repositório.

### Comandos

| Comando | O que faz |
| --- | --- |
| `python app.py` | Sobe o servidor. |
| `python app.py create-admin` | Cria um administrador. |
| `python app.py criar-suporte [usuario] [nome] [--salvar-em arquivo]` | Cria um usuário de suporte. |
| `python app.py redefinir-senha CNPJ-ou-usuario` | Gera uma senha nova. |
| `python app.py gerar-resumos` | Recalcula os resumos fiscais dos arquivos publicados. |
| `python app.py backup [--destino PASTA] [--manter DIAS]` | Faz um backup conferido (veja **Backup**). |

## Perfis de acesso

| Perfil | Entra com | Vê | Pode |
| --- | --- | --- | --- |
| Administração | usuário (ex.: `admin.fa`) | todas as lojas (e o monitoramento do suporte, quando ativo) | publicar arquivos, cadastrar lojas, baixar |
| Suporte | usuário (ex.: `suporte`) | todas as lojas | só consultar e baixar arquivos |
| Loja | CNPJ | as lojas do próprio grupo (mesma raiz de CNPJ) | consultar e baixar |

Para criar um usuário de suporte, use `scripts\windows\criar-usuario-suporte.bat`. A senha aparece na tela e é acrescentada em `admin-credentials.txt` na Área de Trabalho. **Esse arquivo guarda a senha em texto puro: apague-o depois de entregar a senha ao usuário.** Pela linha de comando, sem `--salvar-em` a senha só aparece na tela:

```powershell
python app.py criar-suporte suporte "Suporte Farmácias Associadas"
```

Para trocar a senha de uma loja ou do suporte, use `scripts\windows\redefinir-senha.bat`, informando o CNPJ da loja ou o usuário do suporte.

### Monitoramento do suporte (desativado desde a 1.5)

O monitoramento vem **desligado**: não aparece na lateral, `/monitoramento` responde "Página não encontrada", nenhuma ação do suporte é gravada e o suporte não vê o aviso de registro. O código, a tabela `audit_log` e o histórico já gravado continuam no lugar. Para reativar, use `PORTAL_MONITORAMENTO=1` no `.env` ou no ambiente:

```powershell
$env:PORTAL_MONITORAMENTO = '1'
python app.py
```

Com o monitoramento ativo, tudo o que um usuário de suporte faz fica registrado (entradas e saídas, senha errada, lojas consultadas, arquivos baixados e tentativas bloqueadas), com data e hora, loja, arquivo, máquina, IP e navegador. A administração consulta em **Monitoramento**, na barra lateral, com filtros e exportação para Excel.

## Servidor provisório no Windows

Enquanto o portal não tem hospedagem definitiva, ele pode rodar neste computador:

| Arquivo | O que faz |
| --- | --- |
| `iniciar-portal.bat` | Sobe o portal em http://127.0.0.1:8000. Se o processo cair, reinicia em 10 segundos. Se a rota `/saude` já responde, não abre uma segunda cópia; se a porta está ocupada por outro programa, avisa e não sobe. Se a checagem de inicialização recusar (código 2), para em vez de reiniciar sem fim. Registro em `logs/portal.log`, girado acima de 5 MB. |
| `scripts\windows\instalar-servico.bat` | Cria a tarefa do Windows **Portal Fiscal**, que roda o `iniciar-portal.bat` como SYSTEM sempre que o computador liga, mesmo sem ninguém fazer login. Pede permissão de administrador. |
| `scripts\windows\remover-servico.bat` | Remove a tarefa e para o portal. |
| `scripts\windows\instalar-backup.bat` | Agenda o backup diário (veja **Backup**). |
| `scripts\windows\remover-backup.bat` | Remove o backup agendado. |
| `scripts\windows\backup.bat` | Faz um backup agora. |
| `scripts\windows\criar-usuario-suporte.bat` | Cria um usuário de suporte e guarda a credencial na Área de Trabalho. |
| `scripts\windows\redefinir-senha.bat` | Gera uma senha nova para uma loja (CNPJ) ou para o suporte (usuário). |

Os `.bat` escolhem o Python em `scripts\windows\python.bat`: primeiro `PORTAL_PYTHON`, depois a pasta `.venv` ao lado de `portal-sped`, o `python` do PATH e, por fim, o inicializador `py`. As tarefas do Windows rodam como SYSTEM, que não enxerga o Python instalado só para o usuário; por isso, ao instalar, o caminho real do Python é gravado na tarefa.

Para ver as tarefas e se o portal está no ar:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\servico-windows.ps1 -Acao status
```

A tarefa do portal não tem limite de tempo de execução e também roda na bateria. Se falhar, o Windows tenta de novo a cada minuto. O servidor escuta só em `127.0.0.1`. Para abrir na rede interna, defina `PORTAL_HOST=0.0.0.0` no `.env` e libere a porta no firewall. Faça isso apenas em rede confiável, seguindo as recomendações de implantação abaixo.

### Rota de saúde

`GET /saude` responde sem login. Com o banco respondendo e na migração esperada, devolve `200` e `{"ok": true, "versao": "1.6.0", "migracao": 4}`; senão, `503` com `{"ok": false, "erro": "..."}` (o detalhe técnico fica só no log). É o que o `iniciar-portal.bat` e o `status` usam para saber se o portal está no ar de verdade.

## Publicar um SPED

1. Entre como administrador e cadastre a loja com nome, CNPJ válido e senha inicial.
2. No painel, em **Enviar arquivo SPED**, escolha o arquivo `.txt` e clique em **Validar e publicar**. Não é preciso escolher loja, tipo nem mês.
3. O portal lê o registro `0000` e identifica sozinho:
   - o **tipo**: SPED Fiscal (ICMS/IPI) ou SPED Contribuições (PIS/COFINS), pela posição do período e do CNPJ, que é diferente em cada leiaute;
   - a **loja**, pelo CNPJ do arquivo (com os dígitos verificadores conferidos), entre as lojas ativas;
   - a **competência**, pelo período do arquivo.
4. O arquivo é anexado na aba do tipo certo da loja, e um pop-up confirma o tipo, a loja, o CNPJ, a competência e, se for parte do mês, o período. Se algo não bater (CNPJ sem loja cadastrada, período em dois meses, arquivo que não é SPED EFD), o pop-up explica o motivo e nada é gravado.
5. Arquivos idênticos são bloqueados por hash; arquivos complementares diferentes podem coexistir na mesma loja, tipo e competência. Fiscal e Contribuições também podem coexistir.
6. A loja acessa a conta com seu CNPJ, escolhe o módulo e baixa os arquivos autorizados.

O arquivo precisa conter, no início, um registro `0000` SPED EFD. O parser usa os campos próprios de cada leiaute: EFD Fiscal e EFD Contribuições. Arquivos acima de 40 MB, períodos que atravessam dois meses, CNPJ sem loja e arquivos sem esse registro são recusados. A plataforma não substitui a validação do PVA/SPED nem confere todas as regras fiscais do arquivo.

### Arquivos de parte do mês (ex.: SPED Fiscal quinzenal)

O período do `0000` pode ser o mês inteiro ou só parte dele: a 1ª quinzena (01 a 15), a 2ª (16 ao fim do mês), do dia 15 ao fim, ou qualquer outro intervalo dentro de um único mês. A competência é sempre o mês do período. No painel, cada arquivo parcial mostra o seu período ("Período 01/09/2026 a 15/09/2026"), e o resumo do mês soma os arquivos parciais. Se uma quinzena for reenviada corrigida, a versão mais recente daquele período substitui a anterior no resumo, sem somar duas vezes.

### Matriz e filiais

Contas de loja cujos CNPJs compartilham os mesmos 8 primeiros dígitos são tratadas como estabelecimentos da mesma empresa. Após entrar, o usuário pode escolher uma dessas filiais no seletor lateral; a sessão continua aberta, mas a lista e os downloads mostram somente os documentos da filial selecionada. O servidor também aplica esse escopo na rota de download, impedindo acesso a CNPJs de outra raiz. O vínculo é automático pelo CNPJ-base e não exige cadastrar relações separadas.

### Resumo fiscal da competência

Ao abrir uma loja, acima da lista de arquivos, o portal mostra um resumo gerencial da competência escolhida, calculado a partir dos SPEDs publicados:

- **SPED Fiscal:** saídas e entradas (C100/C190), apuração do ICMS (E110), ICMS-ST (E210), vencimento (E116) e inventário (H005). Traz movimento diário, vendas por tributação do ICMS, principais CFOPs e maiores fornecedores.
- **SPED Contribuições:** receita tributada e sem incidência (M210/M400), PIS e COFINS apurados e a recolher (M200/M600), créditos (M100/M500) e movimento por estabelecimento (C010/C100).

Documentos cancelados são ignorados, e notas com CFOP x.929 ficam fora das vendas para não duplicar cupons e NFC-e. Quando há mais de um arquivo do mês inteiro na competência, cada seção vem do arquivo mais recente que a contém; arquivos de parte do mês são somados. Apurações de vários períodos (E110, M200/M600) são somadas, com o saldo anterior do primeiro período e o saldo a transportar do último. Com duas ou mais competências publicadas, aparece também a evolução mensal.

O resumo é calculado no upload e guardado na tabela `document_summaries`. Para recalcular os resumos de arquivos publicados antes:

```powershell
python app.py gerar-resumos
```

Os gráficos são SVG e CSS gerados no servidor. O resumo é gerencial e não substitui a apuração oficial.

### Previsão de publicação

O painel mostra os meses até dezembro/2026 (ajustável por `PORTAL_PREVISAO_ATE`, no formato `AAAA-MM`). Os meses sem arquivo aparecem como previstos, com o prazo de publicação:

- grupos com até 3 lojas (mesma raiz de CNPJ): até o **3º dia útil** do mês seguinte à competência;
- grupos com mais de 3 lojas: até o **5º dia útil**, com um ícone "?" explicando que o volume exige mais tempo e que correções entram na fila depois das postagens regulares.

A regra vale para o SPED Fiscal e o SPED Contribuições. Os dias úteis descontam fins de semana, feriados nacionais, Carnaval e Sexta-feira Santa. Feriados estaduais e municipais não são considerados.

## Dados locais

Por padrão, banco, segredo de sessão e arquivos ficam em `data/`:

- `data/portal.sqlite3`: usuários, hashes de senha, sessões, metadados dos documentos e a versão de migração do banco (`schema_version`).
- `data/.secret`: segredo local usado na proteção do token antirrepetição do login.
- `data/documentos/`: arquivos SPED privados, nomeados internamente por identificador aleatório.

`data/`, `backups/` e os bancos SQLite estão no `.gitignore`. Não mova documentos para uma pasta servida como conteúdo estático nem inclua dados fiscais, credenciais ou bancos locais no Git. Para apontar os dados a um diretório protegido diferente, configure `PORTAL_DATA_DIR` no `.env` ou no ambiente antes de iniciar o servidor.

### Migrações do banco

As alterações do banco ficam em `migrations/`, em arquivos numerados (`001_inicial.sql`, `002_perfil_suporte.sql`, ...). Ao iniciar, o portal aplica em ordem só as que ainda não rodaram, cada uma numa transação: se uma falhar, ela é desfeita inteira e o portal não sobe. Bancos criados antes da 1.6 são reconhecidos e marcados na versão 2 na primeira abertura.

Para mudar o banco: crie o próximo arquivo `NNN_descricao.sql` (numeração contínua, sem pular), rode os testes e teste contra uma **cópia** do banco real antes de colocar no ar. Não edite uma migração que já rodou em algum lugar; crie outra.

## Backup

`python app.py backup` (ou `scripts\windows\backup.bat`) copia o banco pela API de backup do SQLite, que é segura com o portal rodando, junto com `documentos/` e `.secret`, para uma pasta `portal-AAAA-MM-DD_HHMMSS`. Antes de concluir, confere a integridade do banco e se há um arquivo para cada documento registrado; um backup incompleto é apagado e o comando falha. Backups com mais de 14 dias são removidos (só as pastas `portal-...`).

- **Destino:** `PORTAL_BACKUP_DIR` (ou `--destino`). Precisa ficar fora de `data/`; o ideal é outro disco. Sem configuração, vai para `backups/` do projeto.
- **Agendado:** `scripts\windows\instalar-backup.bat` cria a tarefa **Portal Fiscal - Backup**, todo dia às 3h (se o computador estava desligado, roda quando ligar). Registro em `logs/backup.log`.
- **Restaurar:** a pasta do backup tem o mesmo formato de `data/`. Copie-a para um lugar protegido e inicie o portal com `PORTAL_DATA_DIR` apontando para a cópia. Para conferir um backup sem mexer no portal principal, use outra porta:

```powershell
$env:PORTAL_DATA_DIR = 'D:\Restauracao\portal-2026-10-20_030000'
$env:PORTAL_PORT = '8001'
python app.py
```

Proteja a pasta de backups com permissões do sistema operacional e, se sair do computador, com criptografia. O `.secret` faz parte do backup para preservar as sessões; alternativamente, invalide todas as sessões após uma restauração.

## Configuração

As variáveis podem ser definidas no ambiente ou num arquivo `.env` na raiz (copie o `.env.example`). O ambiente tem prioridade sobre o `.env`.

| Variável | Padrão | Uso |
| --- | --- | --- |
| `PORTAL_HOST` | `127.0.0.1` | Interface de rede de escuta. Só altere em instalação controlada atrás de proxy. |
| `PORTAL_PORT` | `8000` | Porta HTTP local. Valor inválido impede o portal de subir. |
| `PORTAL_DATA_DIR` | `data/` | Diretório privado para banco, segredo e arquivos. |
| `PORTAL_BACKUP_DIR` | `backups/` | Destino dos backups; precisa ficar fora de `PORTAL_DATA_DIR`. |
| `COOKIE_SECURE` | `0` | Use `1` somente quando HTTPS estiver configurado no proxy reverso. |
| `PORTAL_MONITORAMENTO` | `0` | Use `1` para reativar o monitoramento do suporte. |
| `PORTAL_PREVISAO_ATE` | `2026-12` | Último mês (`AAAA-MM`) mostrado na previsão de publicação. |
| `PORTAL_PYTHON` | — | Caminho do Python usado pelos `.bat` (as tarefas do Windows gravam sozinhas). |

O proxy deve forçar HTTPS, não deve ser acessível por HTTP público e precisa encaminhar para o serviço local. Configure limites de corpo de requisição compatíveis com o limite de 40 MB. Restrinja também o acesso administrativo e os backups. O servidor HTTP embutido no Python é destinado a desenvolvimento/uso local, não a servir tráfego público em produção. O portal ainda não possui recuperação de senha, autenticação multifator ou integração de identidade; estabeleça esses controles e obtenha revisão técnica antes de disponibilizar dados fiscais reais.

## Controles implementados

- Senhas derivadas com PBKDF2-HMAC-SHA256, salt aleatório e 600.000 iterações.
- Sessões aleatórias armazenadas como hash no SQLite, com expiração e cookie `HttpOnly`/`SameSite`.
- Proteção CSRF nos formulários e limitação de tentativas de login por endereço IP.
- CNPJ e competência verificados contra o registro `0000` antes de persistir o arquivo.
- Autorização no servidor por papel e por loja em cada consulta/download.
- Limite de tamanho, cabeçalhos de segurança, política CSP e ausência de URLs públicas para os arquivos.

## Testes

```powershell
python -m unittest discover -s tests -v
```

Rode sempre pelo `discover`, a partir da pasta do projeto (os testes importam ajudantes de `tests/util.py`). Há um arquivo de teste por módulo:

| Arquivo | Cobre |
| --- | --- |
| `test_sped.py` | CNPJ, registro 0000 (tipo, período parcial, competência) e resumo fiscal |
| `test_publicacao.py` | regras da publicação e soma dos arquivos de parte do mês |
| `test_auth.py` | senhas, redefinição e formulário de login |
| `test_calendario.py` | dias úteis, feriados e prazos |
| `test_db.py` | migrações: banco novo, bancos antigos, rollback e numeração |
| `test_backup.py` | backup, retenção, conferência e restauração |
| `test_cli.py` | checagem de inicialização, `.env` e rota `/saude` |
| `test_handler.py` | telas e rotas: perfis, matriz e filiais, pop-up, monitoramento |
