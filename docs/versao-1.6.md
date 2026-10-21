# Portal Fiscal 1.6 — documentação técnica

Branch: `melhorias-1.6` · Versão: `1.6.0` · Base: `main` em `v1.5.0` (`7a45700`)

A 1.6 executa o plano de [docs/ideias-melhorias.txt](ideias-melhorias.txt):
- divide o `app.py` em módulos;
- passa a usar migrações numeradas;
- separa os testes por módulo;
- cria a rota `/saude` e a checagem na inicialização;
- adiciona backup agendado;
- organiza os scripts do Windows.

Além disso, passa a aceitar SPED de **parte do mês** (ex.: SPED Fiscal quinzenal). O portal continua usando só a biblioteca padrão do Python 3.11+.

## 1. Estrutura

| Módulo | Responsabilidade | Depende de |
| --- | --- | --- |
| `portal/config.py` | versão, caminhos, variáveis de ambiente, `.env` | — |
| `portal/db.py` | estado do armazenamento, conexão, segredo, migrações | config |
| `portal/sped.py` | tipos de SPED, CNPJ, leitura do `0000` (sem banco) | — |
| `portal/auth.py` | perfis, senhas, contas, hash de token | db, sped |
| `portal/calendario.py` | dias úteis, feriados, prazos | config |
| `portal/publicacao.py` | `publish_sped`, resumo por arquivo e por competência | db, sped, resumo_sped |
| `portal/auditoria.py` | monitoramento do suporte (`record`, consultas, CSV) | config, db, sped |
| `portal/backup.py` | backup, conferência e retenção | config, db |
| `portal/views/*` | HTML: `layout`, `acesso`, `painel`, `dialogos`, `monitoramento` | módulos acima, sem acessar o banco |
| `portal/handler.py` | `PortalHandler`: rotas, sessão, consultas ao banco | todos |
| `portal/cli.py` | comandos de `python app.py` e checagem de inicialização | todos |
| `app.py` | ponto de entrada (8 linhas) | `portal.main` |

**Estado do armazenamento.** `DATA_DIR`, `DATABASE`, `DOCUMENTS` e `APP_SECRET` vivem só em `portal/db.py` e são lidos na hora do uso (`storage.DOCUMENTS`). Como o código usa `with connect() as db:` em toda parte, o módulo é importado com o apelido `storage` onde as duas coisas convivem. `configure_storage(pasta)` troca a pasta; os testes usam `db.use_paths()` para voltar a `data/` sem abrir o banco real.

**Views.** As funções em `portal/views/` recebem dados prontos e devolvem HTML. As consultas ficam no handler: por exemplo, `handler.summary_section` calcula o resumo e chama `painel.summary_section`.

A divisão foi feita em passos pequenos, um commit por módulo, com a suíte verde a cada passo.

## 2. SPED de parte do mês

### Regra
`sped.parse_sped_header` aceita qualquer período `DT_INI..DT_FIN` contido num único mês:

| Período no `0000` | Resultado |
| --- | --- |
| 01/09 a 30/09 | competência 2026-09, mês inteiro |
| 01/09 a 15/09 | competência 2026-09, parcial |
| 16/09 a 30/09 (ou 15/09 a 30/09) | competência 2026-09, parcial |
| 10/02 a 10/02 | competência 2026-02, parcial |
| 16/09 a 15/10 | recusado: "não corresponde a um único mês" |
| 16/09 a 15/09 | recusado: "data final anterior à inicial" |

O retorno ganha `period_start`, `period_end` (ISO) e `partial`. A regra vale para os dois tipos. No Contribuições, períodos menores que o mês aparecem em situações especiais, e aceitá-los evita bloquear esses casos.

### Banco
A migração `004_periodo_do_arquivo.sql` adiciona `documents.period_start` e `documents.period_end`. Arquivos publicados antes ficam com esses campos vazios e contam como mês inteiro (`publicacao.is_partial`).

### Tela
- **Pop-up:** "...competência Setembro/2026 (período parcial de 01/09/2026 a 15/09/2026)." mais a linha **Período** no quadro de detalhes.
- **Lista de arquivos do mês:** "Período 01/09/2026 a 15/09/2026 · Publicado em ...", com os arquivos ordenados por período.
- **Últimas publicações:** o período aparece ao lado do mês.
- **Formulário de envio:** avisa que arquivos de parte do mês são aceitos.

### Resumo do mês
Antes, quando havia vários arquivos na competência, cada seção do resumo vinha do arquivo mais recente. Com duas quinzenas, isso mostraria só a segunda. Agora, em `publicacao.competence_summary`:

1. Os arquivos do mês inteiro entram como antes.
2. Os parciais formam um conjunto sem sobreposição (`partial_set`): do mais recente para o mais antigo, cada período entra se não cruzar um já escolhido. Assim, o reenvio corrigido da 1ª quinzena substitui o anterior.
3. O conjunto é resumido como se fosse um arquivo só: as linhas são concatenadas em ordem de período (`combined_summary`, com cache em memória) e entram no `merge` na posição do mais recente.

Para a soma ficar certa, o `resumo_sped` passou a acumular apurações repetidas (`add_period`):
- E110, M200 e M600 são somados campo a campo;
- o saldo credor anterior vem do primeiro período, e o saldo a transportar, do último;
- `SUMMARY_VERSION` foi para 2, então os resumos guardados são recalculados na próxima consulta.

Isso também corrige arquivos com mais de um período de apuração do ICMS (vários E100/E110), que antes ficavam só com o último.

## 3. Migrações numeradas

- **Arquivos:** `migrations/NNN_nome.sql` (`001_inicial`, `002_perfil_suporte`, `003_indice_publicacoes`, `004_periodo_do_arquivo`). A numeração precisa ser contínua, e um nome fora do padrão impede o portal de subir.
- **Controle:** a tabela `schema_version(version, name, applied_at)`.
- **`db.migrate()`:** aplica em ordem as migrações acima da versão do banco. Cada uma roda dentro de `BEGIN IMMEDIATE ... COMMIT`, com as chaves estrangeiras desligadas (exigência do SQLite para recriar tabelas, como na 002) e `PRAGMA foreign_key_check` antes do COMMIT. Se algo falhar, a migração é desfeita (ROLLBACK) e a exceção sobe.
- **Bancos anteriores à 1.6** (sem `schema_version`, com tabelas): passam uma única vez pelo reparo antigo da 1.5 (`_adopt_pre_migration_database`), que leva bancos da 1.1 e da 1.5 ao esquema da 1.5. Depois são marcados nas versões 1 e 2 e seguem para a 003.
- **Conferência:**
  - teste que compara o esquema normalizado (sem aspas nem `IF NOT EXISTS`) de um banco novo com o de um banco da 1.5 adotado;
  - execução numa **cópia** do banco real: esquema igual e as mesmas contagens de usuários, documentos, resumos e sessões.

## 4. Rota `/saude` e checagem na inicialização

- **`GET /saude`, sem login:** faz `SELECT 1` e compara `schema_version()` com `latest_version()`.
  - Tudo certo: `200 {"ok": true, "versao": "1.6.0", "migracao": 4}`.
  - Banco atrasado: `503` com a migração esperada.
  - Falha do banco: `503 {"erro": "Banco de dados indisponível."}`. O detalhe técnico vai só para o stderr, sem expor caminhos.
- **`cli.startup_problems()`**, antes de qualquer comando:
  - `config.PROBLEMS` (ex.: `PORTAL_PORT` inválida);
  - `configure_storage()`, que cria as pastas e aplica as migrações;
  - teste de gravação em `data/` e `documentos/`;
  - `.secret` com pelo menos 32 bytes;
  - banco na migração esperada.

  Se houver algum problema, a lista vai para o stderr e o comando sai com código 2.
- **`iniciar-portal.bat`:**
  - se `curl /saude` responde, o portal já está no ar e o script não abre outra cópia;
  - se a porta está ocupada sem `/saude`, avisa e sai com código 1;
  - se o Python sai com código 2, para o laço em vez de reiniciar a cada 10 s;
  - gira `logs/portal.log` acima de 5 MB.

## 5. Backup

`portal/backup.run_backup(destino, manter_dias)`:
1. Recusa um destino dentro de `data/`.
2. Grava em `portal-AAAA-MM-DD_HHMMSS.parcial/`: o banco por `sqlite3.Connection.backup` (seguro com o portal rodando), `documentos/` e `.secret`.
3. Confere a cópia: `integrity_check`, um `.txt` para cada linha de `documents` e o `.secret`. Se falhar, apaga a pasta parcial e levanta `BackupError`.
4. Escreve um `LEIA-ME.txt` e renomeia a pasta para o nome final.
5. Apaga só pastas `portal-AAAA-MM-DD_HHMMSS` mais antigas que a retenção (14 dias por padrão).

O formato da pasta é o mesmo de `data/`, então a restauração é feita com `PORTAL_DATA_DIR`. Isso foi conferido em teste e com os dados reais: backup com o portal no ar e a cópia restaurada subindo na porta 8001.

**Agendamento:** `scripts\windows\instalar-backup.bat` cria a tarefa **Portal Fiscal - Backup** (diária às 3h, `StartWhenAvailable`, limite de 2 h, como SYSTEM). O registro vai para `logs/backup.log`.

## 6. Scripts do Windows

- Todos os scripts ficam em `scripts/windows/`, menos o `iniciar-portal.bat`, que continua na raiz.
- **`python.bat`:** escolhe o Python para todos os `.bat`, nesta ordem:
  1. `PORTAL_PYTHON`;
  2. `..\..\..\.venv`;
  3. `python` do PATH;
  4. `py`.
- **Tarefas como SYSTEM:** o SYSTEM não enxerga o Python instalado só para o usuário, e esse problema já existia na 1.5 com o `instalar-servico.bat`. Agora `servico-windows.ps1` descobre o caminho real do interpretador (`sys.executable`) ao instalar e grava na ação da tarefa: `cmd /c set "PORTAL_PYTHON=..." && "<bat>"`.
- **`servico-windows.ps1 -Acao status`:** mostra as duas tarefas (estado, última execução e resultado) e a resposta da `/saude`.

## 7. Configuração (`.env`)

`config.load_env_file` lê `CHAVE=valor` do `.env` na raiz:
- ignora comentários e linhas sem `=`;
- tira aspas simples ou duplas;
- não sobrescreve variáveis que já existem no ambiente.

O `.env` está no `.gitignore`. O modelo `.env.example` entra no git por uma exceção (`!.env.example`). Novas variáveis: `PORTAL_BACKUP_DIR` e `PORTAL_PYTHON`.

## 8. Testes

São 88 testes, todos passando. Rode com `python -m unittest discover -s tests -v`.

| Arquivo | Testes novos ou movidos |
| --- | --- |
| `test_sped.py` | CNPJ, cabeçalho (inclui período parcial e Contribuições parcial), detecção do tipo, resumo, soma de apurações |
| `test_publicacao.py` | regras de publicação (sem HTTP) e 6 testes de período parcial (duas quinzenas, soma, reenvio corrigido, recusa de dois meses) |
| `test_auth.py` | senhas, redefinição, formulário de login |
| `test_calendario.py` | prazos, feriados, linha do tempo com `FORECAST_UNTIL` configurável |
| `test_db.py` | migrações: banco novo, idempotência, adoção da 1.5 com esquema igual, banco da 1.1, rollback, numeração |
| `test_backup.py` | cópia completa, destino dentro de `data/`, retenção, documento faltando, restauração subindo o portal, comando |
| `test_cli.py` | checagem de inicialização (porta inválida, pasta impossível, migração atrasada), `.env`, `/saude` (200, 503, sem vazar detalhes) |
| `test_handler.py` | perfis, matriz/filial, monitoramento, pop-up (inclui período parcial na tela) |

**Não coberto por teste automático:** instalar as tarefas do Windows, porque exige administrador. Foram verificados manualmente o `backup.bat`, o `status` do `.ps1`, o `python.bat` com e sem `PORTAL_PYTHON` e o `iniciar-portal.bat` subindo o portal.

## 9. Itens do plano que ficaram de fora

- **Templates HTML com `string.Template`** (item 8 do plano): o HTML continua em funções Python, agora separadas em `portal/views/`. Passar para templates é uma mudança grande só de forma; vale fazer junto com uma revisão do visual.
- **HTTPS por proxy reverso** e revisão de segurança: decisões de implantação, descritas no README.
- **Tornar o repositório privado:** o repositório é público no GitHub, e isso é uma configuração de lá. Nenhum dado de loja, banco, `.secret` ou credencial está no git.

## 10. Compatibilidade e atualização

- **Atualizar:** pare o portal e atualize o código. Na primeira inicialização, o banco existente é reconhecido e recebe as migrações 003 e 004. Recomendado: fazer um backup antes (`python app.py backup`).
- **Caminhos dos scripts:** os `.bat` mudaram de lugar. Quem instalou a tarefa **Portal Fiscal** na 1.5 deve reinstalar com `scripts\windows\instalar-servico.bat`, para gravar o `PORTAL_PYTHON`. A tarefa antiga aponta para o `iniciar-portal.bat` da raiz, que continua existindo.
- **Voltar para a 1.5:** o código da 1.5 abre um banco migrado normalmente. As colunas e a tabela novas são ignoradas, e o reparo da 1.5 não mexe nelas. Os arquivos parciais publicados na 1.6 continuam visíveis na 1.5, mas o resumo deles volta a mostrar só o mais recente.
