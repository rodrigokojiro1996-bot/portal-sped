# Portal Fiscal 1.5 — documentação técnica

Branch: `publicacao-automatica-sped` · Versão: `1.5.0` · Base: `main` em `5afaa80`

Esta versão traz quatro mudanças:
- a publicação do SPED fica automática: só o arquivo `.txt` e um botão;
- um pop-up confirma a publicação ou explica a recusa;
- os arquivos estáticos ganham versão no endereço;
- o monitoramento do suporte é desligado, sem perder o código.

Não há mudança no esquema do banco nem dependência nova: o portal continua só com a biblioteca padrão do Python 3.11+.

## 1. Arquivos alterados

| Arquivo | O que mudou |
| --- | --- |
| `app.py` | `SPED_LAYOUTS`, `detect_sped_type`, `PublishError`, `publish_sped`, `sped_label`, `publish_dialog`, `PortalHandler.published_dialog`; `upload()` e `admin_tools()` reescritos; `PORTAL_VERSION`, `MONITORING_ENABLED`, `STYLES_VERSION`, `SCRIPT_VERSION`; rota `/static/portal.js` |
| `static/portal.js` | Novo. Abre o pop-up como modal e trata a tecla Esc |
| `static/styles.css` | Estilo do pop-up (`.publish-dialog`, `.publish-facts`, `.publish-mark`) |
| `tests/test_app.py` | 25 testes novos (`SpedTypeDetectionTests`, `AutoPublishTests`, `MonitoringDisabledTests`) e testes do monitoramento ligados por chave |
| `README.md`, `CHANGELOG.md`, `docs/versao-1.5.md` | Documentação |

## 2. Publicação automática

### 2.1 Como o tipo do SPED é identificado

Os dois leiautes começam pelo registro `0000`, mas em posições diferentes. O Contribuições tem três campos a mais antes do período (`TIPO_ESCRIT`, `IND_SIT_ESP`, `NUM_REC_ANTERIOR`), então tudo anda duas casas:

```
Fiscal (ICMS/IPI)          |0000|COD_VER|COD_FIN|DT_INI|DT_FIN|NOME|CNPJ|CPF|UF|...
                             [1]   [2]     [3]    [4]    [5]   [6]  [7]
Contribuições (PIS/COFINS) |0000|COD_VER|TIPO_ESCRIT|IND_SIT_ESP|NUM_REC_ANT|DT_INI|DT_FIN|NOME|CNPJ|UF|...
                             [1]   [2]      [3]          [4]         [5]       [6]    [7]   [8]  [9]
```

Essas posições ficam numa única tabela, usada tanto pela detecção quanto pela leitura do cabeçalho:

```python
SPED_LAYOUTS = {"fiscal": (4, 5, 6, 7), "contribuicoes": (6, 7, 8, 9)}  # DT_INI, DT_FIN, NOME, CNPJ
```

`detect_sped_type(content)` procura o `0000` nas primeiras 500 linhas, aceitando arquivo com BOM UTF-8. Para cada leiaute, testa se as posições de data têm 8 dígitos e se a posição do CNPJ tem 14 dígitos. Só um leiaute pode encaixar:
- no Fiscal, o campo 4 é a data inicial;
- no Contribuições, o campo 4 é `IND_SIT_ESP`, que vem vazio ou com um dígito.

Se nenhum leiaute encaixar, a função levanta `ValueError`. Isso cobre texto comum e também outros SPEDs, como a ECD (`|0000|LECD|...`) e a ECF.

### 2.2 Fluxo de `publish_sped(content, file_name)`

```
arquivo .txt
  └─ vazio ou acima de 40 MB?                  → PublishError
  └─ detect_sped_type                          → "fiscal" | "contribuicoes"  (senão PublishError)
  └─ parse_sped_header(content, tipo)          → CNPJ válido (módulo 11), competência AAAA-MM,
                                                 período = mês-calendário completo  (senão PublishError)
  └─ loja ativa com users.cnpj == CNPJ?        → senão PublishError com CNPJ e razão social do arquivo
  └─ grava data/documentos/<uuid4>.txt (0600)
  └─ INSERT documents (UNIQUE store_id, document_type, sha256)
        └─ IntegrityError                      → apaga o arquivo gravado, PublishError "já está publicado"
  └─ document_summary(document)                → resumo fiscal em cache (document_summaries)
  └─ retorna {id, store_id, store_name, cnpj, document_type, competence}
```

`PublishError` é uma subclasse de `ValueError` cuja mensagem vai direto para o pop-up. Em toda recusa, nada fica gravado nem no banco nem no disco.

| Situação | Mensagem (resumo) |
| --- | --- |
| Arquivo vazio | Escolha um arquivo .txt para publicar. |
| Acima de 40 MB | O arquivo passa do limite de 40 MB. |
| Não é SPED Fiscal nem Contribuições | O arquivo não foi reconhecido como SPED Fiscal (ICMS/IPI) nem como SPED Contribuições (PIS/COFINS). |
| CNPJ com dígito verificador errado | O registro 0000 contém um CNPJ inválido. |
| Período parcial ou com dois meses | O SPED não cobre o mês-calendário completo. / ... não corresponde a um único mês. |
| CNPJ sem loja ativa | O arquivo é um SPED ... do CNPJ xx.xxx.xxx/xxxx-xx (RAZÃO SOCIAL), e nenhuma loja ativa tem esse CNPJ. |
| Arquivo idêntico já publicado | Este arquivo já está publicado no SPED ... da loja X (CNPJ ...). Nada foi alterado. |

### 2.3 Rota HTTP

`POST /upload`, restrita ao perfil admin e com CSRF obrigatório. O formulário envia só `csrf` e `file`.

- **Sucesso:** responde `303` para `/dashboard?store_id=<id>&type=<tipo>&competence=<AAAA-MM>&published=<uuid>`. O padrão POST/redirect/GET evita que recarregar a página reenvie o arquivo. O painel abre na loja, na aba e no mês do arquivo.
- **Recusa:** responde `422` com o painel central e o pop-up de erro, sem redirecionar, porque a mensagem tem dados do arquivo que não devem ir para a URL.

Campos `store_id`, `competence` e `document_type` enviados por formulários antigos são ignorados. Quem decide é sempre o arquivo.

## 3. Pop-up de resultado

- **HTML:** `publish_dialog(success, message, details)` gera um `<dialog open>` com título, mensagem, uma lista `<dl>` com tipo, loja, CNPJ, competência e arquivo, e um `<form method="dialog">` com o botão **OK, entendi**. Esse botão fecha o pop-up sem JavaScript.
- **Pop-up de sucesso:** `PortalHandler.published_dialog(document_id)` monta o pop-up a partir do parâmetro `published`, só para o admin. Ele não reaparece quando a página é aberta sem esse parâmetro.
- **Mensagem de sucesso:** *"O arquivo SPED Fiscal (ICMS e IPI) foi publicado com sucesso na loja X, CNPJ 00.000.000/0000-00, competência Setembro/2026."* No outro tipo, a mensagem diz "SPED Contribuições (PIS e COFINS)".
- **CSS:**
  - o pop-up fica centralizado em `position: fixed`;
  - o véu escuro é feito com `box-shadow: 0 0 0 100vmax`;
  - `.publish-dialog:not([open]){display:none}` é obrigatório, porque o `display: grid` da classe anulava o `display: none` do navegador e o pop-up não sumia ao fechar;
  - `::backdrop` fica transparente, porque o véu já vem do `box-shadow`.
- **Esc e modal (`static/portal.js`):** um `<dialog>` aberto só por HTML não fecha com Esc. O script reabre o pop-up com `showModal()`, e com isso o navegador passa a fechar com Esc, prende o foco e bloqueia o fundo. Para navegadores sem modal nativo, há também um tratamento do Esc no `keydown`.
- **CSP:** a política continua `default-src 'self'`. O script é um arquivo do próprio portal, não inline, e a página não tem `<script>` inline nem `onclick`.

## 4. Versão nos arquivos estáticos

```python
STYLES_VERSION = hashlib.sha256((ROOT / "static" / "styles.css").read_bytes()).hexdigest()[:10]
SCRIPT_VERSION = hashlib.sha256((ROOT / "static" / "portal.js").read_bytes()).hexdigest()[:10]
```

As páginas usam `/static/styles.css?v=<hash>` e `/static/portal.js?v=<hash>`. O roteamento ignora a query string, então o arquivo servido é sempre o atual. Como o endereço muda junto com o conteúdo, o cache de 1 hora (`max-age=3600`) não segura uma versão antiga. O hash é calculado quando o servidor inicia, então reinicie o portal depois de trocar esses arquivos.

## 5. Monitoramento do suporte desativado

```python
MONITORING_ENABLED = os.environ.get("PORTAL_MONITORAMENTO", "0") == "1"
```

Em vez de comentar os blocos, a função ficou atrás de uma chave. Código comentado deixa de ser testado e quebra sem aviso. Com a chave, os testes do monitoramento continuam rodando, e reativar não exige editar código.

| Ponto | Desligado (padrão) | Ligado (`PORTAL_MONITORAMENTO=1`) |
| --- | --- | --- |
| Item **Monitoramento** na lateral do admin | oculto | visível |
| `GET /monitoramento` e `/monitoramento.csv` | 404 | página e CSV, só para o admin |
| `PortalHandler.audit()` | não grava nada | grava em `audit_log` |
| Aviso para o suporte | sem "as ações são registradas" | com o aviso |
| `criar-suporte` (CLI) | sem menção a registro | menciona o monitoramento |

A tabela `audit_log` continua sendo criada e o histórico gravado antes da 1.5 é mantido. O perfil Suporte em si não muda: continua consultando e baixando em todas as lojas, sem publicar nem cadastrar.

Para reativar no servidor provisório, acrescente no `iniciar-portal.bat`:

```bat
if not defined PORTAL_MONITORAMENTO set "PORTAL_MONITORAMENTO=1"
```

## 6. Testes

```powershell
python -m unittest discover -s tests -v
```

São 54 testes, todos passando. Os novos da 1.5:

| Classe | Cobre |
| --- | --- |
| `SpedTypeDetectionTests` | Fiscal, Contribuições, arquivo com BOM; recusa de ECD, texto comum e arquivo vazio |
| `AutoPublishTests`, regras | loja e aba pelo CNPJ e tipo; competência lida do arquivo; Fiscal e Contribuições juntos no mesmo mês; CNPJ não cadastrado; loja inativa; duplicado; período parcial; CNPJ inválido; nada gravado nas recusas |
| `AutoPublishTests`, tela | formulário só com arquivo e botão; pop-up de sucesso de cada tipo na aba certa; pop-up de erro com 422; CSRF obrigatório; pop-up só logo após publicar; fecha ao clicar; Esc (script servido, `showModal`, sem script inline); versão do CSS no endereço |
| `MonitoringDisabledTests` | desligado por padrão e ligado só com `PORTAL_MONITORAMENTO=1`; sem menu e 404 nas rotas; suporte sem registro e sem aviso; versão 1.5.0 no rodapé |

`SupportAccessTests` liga a chave em `setUp` e a restaura em `tearDown`, para manter a função de monitoramento verificada.

Os testes não abrem um navegador. O fechamento do pop-up por clique e por Esc é verificado pelo HTML, CSS e JS entregues; a interação em si deve ser conferida manualmente.

## 7. Teste manual

1. Cadastre uma loja com o CNPJ do arquivo.
2. Em **Enviar arquivo SPED**, escolha o `.txt` e clique em **Validar e publicar**.
3. Confira no pop-up o tipo, a loja, o CNPJ e a competência. Feche com **OK, entendi** e, numa segunda publicação, com **Esc**.
4. Confira que o arquivo aparece na aba e no mês corretos.
5. Envie o mesmo arquivo de novo: deve aparecer o pop-up vermelho de duplicado.
6. Envie um arquivo de CNPJ não cadastrado: deve aparecer o pop-up vermelho, e nada é gravado.

## 8. Limitações conhecidas

- A loja precisa estar cadastrada com o **CNPJ exato** do arquivo antes do envio.
- O SPED Contribuições sai com o CNPJ da matriz e vai para a matriz. As filiais continuam vendo o aviso de que o arquivo está na matriz.
- SPED Fiscal de contribuinte identificado por CPF, com o CNPJ vazio no `0000`, não é reconhecido.
- A validação é estrutural, baseada no registro `0000`, e não substitui o PVA do SPED.
- Arquivos complementares diferentes do mesmo mês continuam sendo aceitos lado a lado, como antes.

## 9. Compatibilidade e reversão

- Não há migração de banco, e arquivos e resumos publicados antes continuam válidos.
- Para voltar à versão anterior, faça checkout de `main` e reinicie o portal. Os documentos publicados pela 1.5 continuam no banco e no disco e aparecem normalmente na versão anterior.
