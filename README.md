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
│   ├── contatos.py        contatos dos avisos, importados de planilha
│   ├── emails.py          fila de avisos de publicação
│   ├── envio.py           envio pela API do Resend
│   ├── certificado.py     entrada pelo certificado digital da loja
│   ├── acessos.py         registro de entradas, recusas e downloads por certificado
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
| `python app.py importar-contatos ARQUIVO.xlsx [--aplicar]` | Lê os contatos dos avisos (veja **Avisos de publicação**). |
| `python app.py conferir-certificado ARQUIVO.cer` | Mostra o CNPJ, a validade e as lojas de um certificado (veja **Entrada pelo certificado digital**). |

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

`GET /saude` responde sem login. Com o banco respondendo e na migração esperada, devolve `200` e `{"ok": true, "versao": "1.6.0", "migracao": 8}`; senão, `503` com `{"ok": false, "erro": "..."}` (o detalhe técnico fica só no log). É o que o `iniciar-portal.bat` e o `status` usam para saber se o portal está no ar de verdade.

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

## Avisos de publicação por e-mail

A fila vem desligada; para ligar, use `PORTAL_EMAIL=1` no `.env` e reinicie o portal. Ligada, ela funciona em **modo de teste**: monta cada aviso e o registra para conferência, sem enviar nada. O envio de verdade, pelo [Resend](https://resend.com), tem uma chave própria (veja **Envio real**).

### Contatos

Os contatos vêm de uma planilha `.xlsx`, com uma linha por loja e as colunas `CNPJ`, `SPED SPA` e `CONTATO` (`Fantasia` é opcional e aparece nos relatórios). Entram as lojas com `SIM` em `SPED SPA` e com e-mail em `CONTATO`. Lojas com "Loja Fechada" em qualquer coluna ficam de fora.

```powershell
python app.py importar-contatos lojas.xlsx            # mostra o resultado, sem gravar
python app.py importar-contatos lojas.xlsx --aplicar  # grava
```

O campo `CONTATO` é texto livre. Vários endereços viram destinatários, e o que vem depois de "com cópia para" vira cópia. Importar de novo atualiza as lojas da planilha e não apaga as demais. O contato é guardado pelo CNPJ e passa a valer quando a loja estiver cadastrada no portal. **A planilha e o banco têm dados reais: não os coloque no repositório.**

### Fila

Ao publicar um arquivo de uma loja com contato, o aviso nasce na mesma transação do documento e é processado em segundo plano, depois da resposta. Loja sem contato não gera aviso. Uma falha na fila nunca atinge quem publicou.

A administração acompanha tudo em **E-mails**, na barra lateral:

| Situação | O que significa |
| --- | --- |
| Pendente | Ainda não foi processado. |
| Teste | Montado e registrado no modo de teste. É final: um aviso de teste nunca vira e-mail de verdade. |
| Enviado | Saiu pelo Resend; a tela mostra o id da mensagem, para achá-la no painel dele. |
| Erro | Não foi montado ou não saiu; o motivo aparece na tela. Tenta de novo até 5 vezes e depois espera uma pessoa. |

Ao iniciar, o portal retoma até 20 avisos com menos de 48 horas. O botão **Reprocessar pendentes** processa até 25 por clique, sem limite de idade. **Ver como e-mail** abre a prévia do aviso numa página isolada, sem scripts.

### Envio real

```env
PORTAL_EMAIL=1
PORTAL_EMAIL_ENVIO_REAL=1
RESEND_API_KEY=re_...                          # chave do painel do Resend, com permissão só de envio
PORTAL_EMAIL_REDIRECIONAR=voce@example.com     # enquanto testa: todo aviso vai só para este endereço
# PORTAL_EMAIL_REMETENTE=Portal Fiscal <avisos@seu-dominio>   # depois de verificar o domínio no Resend
```

- **Proteção na inicialização:** o portal se recusa a iniciar com o envio real ligado se faltar alguma destas peças:
  - a fila ligada (`PORTAL_EMAIL=1`);
  - a chave do Resend;
  - um destino seguro: `PORTAL_EMAIL_REDIRECIONAR` com um endereço válido, para testar, ou `PORTAL_EMAIL_PARA_LOJAS=1`, para enviar às lojas de verdade.
- **Remetente:** sem domínio verificado, o remetente padrão `onboarding@resend.dev` só entrega para o e-mail dono da conta do Resend. Para as lojas, verifique um domínio (SPF e DKIM) e preencha `PORTAL_EMAIL_REMETENTE`.
- **Sem duplicar:** cada aviso tem uma chave anti-duplicação, gravada junto com ele. Com ela, tentar de novo não manda o e-mail duas vezes. O Resend guarda essa chave por 24 h desde o primeiro uso.
  - **Sem confirmação do Resend** quer dizer que o e-mail pode já ter saído: a resposta se perdeu, o Resend falhou no meio ou aceitou sem dizer o id. A fila tenta de novo com a mesma chave.
  - **Sem conexão com o Resend** (nome não encontrado ou conexão recusada) quer dizer que nada chegou lá. Não conta como "pode ter saído".
  - Um aviso sem confirmação que passou de 23 h não é reenviado sozinho. Um aviso sem confirmação que depois teve o conteúdo alterado também não.
  - Nesses casos, o botão diz **Conferi no Resend: reenviar**, e a tela pede para conferir no painel do Resend antes. O reenvio usa uma chave nova e pode duplicar.
- **Avisos antigos:** um aviso com mais de 7 dias que nunca chegou ao Resend não sai sozinho, para que ligar o envio real não despeje nas lojas avisos de semanas atrás. Ele espera **Reenviar este aviso**. Um aviso que já foi tentado continua com a regra das 23 h.
- **Modo de teste depois do envio real:** um aviso que pode já ter saído fica como está. Ele não vira "Teste", porque isso diria que nada foi enviado.
- **Botão Reprocessar pendentes:** roda em segundo plano. A tela não fica presa esperando o Resend.
- **Erros passageiros:** o portal espera e tenta de novo nos limites de envio e nas falhas do Resend. Nos demais erros, a tela mostra só o tipo do erro, porque a mensagem do Resend pode repetir endereços.
- **Ritmo:** no máximo 4 envios por segundo, abaixo do limite de 10 do Resend.

## Entrada pelo certificado digital

O link do aviso abre a página da loja sem senha: quem clica prova que pertence à loja com o **certificado digital dela** (e-CNPJ da ICP-Brasil). O portal compara os 8 primeiros dígitos do CNPJ gravado no certificado com os da loja do link, a mesma regra de matriz e filiais. Por isso o certificado de uma filial abre também a matriz e as outras filiais da mesma raiz. Grupos com mais de uma raiz precisam de um certificado para cada uma.

A sessão aberta assim é da loja: só consulta e baixa. Certificado de pessoa física (e-CPF) é recusado, porque não traz CNPJ. As recusas feitas pelo portal explicam o que fazer. Certificado vencido, revogado ou de fora da ICP-Brasil é barrado antes, pelo servidor da frente: para essas pessoas verem uma explicação, esse servidor precisa de uma página de erro própria.

Nesse endereço não existe senha. Quem perde a sessão entra de novo sozinho e volta à página pedida. Ao clicar em **Sair**, a pessoa vê uma tela de saída, com o aviso de fechar o navegador em computador compartilhado: enquanto ele estiver aberto, o certificado continua disponível.

### Quem faz o quê

| Quem | Responsabilidade |
| --- | --- |
| Servidor HTTPS da frente | Pede o certificado ao navegador, confere a cadeia da ICP-Brasil e a revogação, repassa o certificado e manda o segredo. |
| Portal | Confere o segredo, lê o CNPJ e a validade e decide qual loja abre. |

**O portal não confere a assinatura do certificado nem se ele foi revogado.** Essa parte é do servidor da frente, e sem ela a entrada não é segura. A rota `/certificado` só existe com `PORTAL_CERTIFICADO_SEGREDO` preenchido, e recusa qualquer requisição sem esse segredo: sem ele, bastaria inventar um cabeçalho para entrar como qualquer loja.

### Requisitos do servidor da frente

- Usar um endereço só para esta entrada, que peça o certificado em toda conexão, e apontar `PORTAL_URL_PUBLICA` para ele.
- Aceitar apenas a cadeia da ICP-Brasil e conferir a revogação, mantendo a lista atualizada.
- Recusar certificado inválido, mas deixar chegar ao portal a requisição **sem** certificado: é o portal que explica o que fazer, com uma página melhor que o erro do navegador.
- Sobrescrever `X-Client-Cert`, `X-Portal-Proxy` e `X-Forwarded-For`, sem nunca repassar os valores enviados pelo cliente.
- Mandar o segredo **só neste endereço**. Qualquer outro endereço com o segredo permitiria inventar um certificado e entrar como qualquer loja.
- Manter o portal inacessível por outro caminho (`PORTAL_HOST=127.0.0.1`) e usar `COOKIE_SECURE=1` e `PORTAL_ATRAS_DE_PROXY=1`.

Exemplo em nginx, **ainda não testado**:

```nginx
server {
    listen 443 ssl;
    server_name certificado.example.com;
    ssl_certificate         /etc/nginx/portal.crt;
    ssl_certificate_key     /etc/nginx/portal.key;
    ssl_client_certificate  /etc/nginx/icp-brasil.pem;
    ssl_crl                 /etc/nginx/icp-brasil.crl;
    ssl_verify_client       optional;
    ssl_verify_depth        4;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header X-Client-Cert   $ssl_client_escaped_cert;
        proxy_set_header X-Portal-Proxy  "o mesmo valor de PORTAL_CERTIFICADO_SEGREDO";
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

### Registro de acessos

Com a entrada ligada, a administração acompanha em **Acessos**, na barra lateral, quem entrou pelo certificado, o que baixou e o que foi recusado. Dá para filtrar por loja e período e exportar para Excel.

- **Sem acesso por certificado fora do registro:** a entrada é gravada junto com a sessão, e o download é registrado antes de o arquivo sair. Se o registro falhar, o acesso também falha. Quem tem sessão de senha e abre o link do aviso entra pelo certificado, para o acesso ficar registrado.
- **Recusas:** ficam na loja do link ou, sem link, na loja do certificado. A mesma recusa repetida em menos de 1 minuto conta uma vez.
- **O que fica guardado:** data, CNPJ e número de série do certificado, loja, arquivo, motivo da recusa, IP, nome reverso do IP e navegador. O CPF do responsável, que também vem no e-CNPJ, não é guardado.
- **Nome reverso:** pela internet costuma ser o nome da operadora, e não o do computador, que o navegador não informa. Só vale se apontar de volta para o mesmo IP; se não apontar, aparece com "(não confirmado)", porque quem controla o IP escolhe o nome. A consulta espera no máximo 1,5 s e fica guardada na memória por IP.
- **O que não aparece:** visitas sem certificado, que não identificam ninguém, e downloads de quem entrou com senha.
- **Endereço real:** o IP só é o de quem acessa com `PORTAL_ATRAS_DE_PROXY=1`.

IP, nome reverso e navegador são dados pessoais; em rede interna, o nome reverso pode trazer o nome de quem usa o computador. O registro ainda não tem prazo de retenção: nada é apagado automaticamente.

### Conferir um certificado

Para ver o que o portal lê de um certificado, exporte **só a parte pública** (`.cer` ou `.pem`, nunca o `.pfx`):

```powershell
python app.py conferir-certificado loja.cer
```

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
| `PORTAL_EMAIL` | `0` | Use `1` para ligar a fila de avisos, em modo de teste. |
| `PORTAL_EMAIL_ENVIO_REAL` | `0` | Use `1` para os avisos saírem pelo Resend (veja **Envio real**). |
| `RESEND_API_KEY` | — | Chave da API do Resend. Obrigatória com o envio real. |
| `PORTAL_EMAIL_REMETENTE` | `Portal Fiscal <onboarding@resend.dev>` | Remetente dos avisos. O padrão só entrega para o dono da conta do Resend. |
| `PORTAL_EMAIL_PARA_LOJAS` | `0` | Use `1` para o envio real ir aos contatos das lojas, sem redirecionamento. |
| `PORTAL_EMAIL_REDIRECIONAR` | — | Troca todo destinatário por estes endereços. Valor inválido faz o aviso dar erro, nunca usar o endereço real. |
| `PORTAL_URL_PUBLICA` | — | Endereço do link do aviso: o que pede o certificado digital. Vazio, o aviso sai sem link. |
| `PORTAL_CERTIFICADO_SEGREDO` | — | Segredo do servidor HTTPS da frente, com pelo menos 32 caracteres e sem acentos. Vazio, a entrada por certificado fica desligada. |
| `PORTAL_ATRAS_DE_PROXY` | `0` | Use `1` só com o portal inacessível por fora do proxy: o limite de login passa a usar o endereço real, vindo do `X-Forwarded-For`. |
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
| `test_contatos.py` | leitura da planilha, separação de destinatários e cópias, comando de importação |
| `test_emails.py` | fila de avisos: publicação, modo de teste, envio real com o Resend simulado, HTML, travas, varredura e tela |
| `test_certificado.py` | leitura do certificado, entrada pelo link, segredo do servidor da frente e endereço real |
| `test_acessos.py` | registro de entradas, recusas e downloads, filtros, tela e exportação |
