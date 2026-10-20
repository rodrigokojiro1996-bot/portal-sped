# Portal Fiscal | Farmácias Associadas

Versão atual: **1.5.0**. Veja o [CHANGELOG](CHANGELOG.md) e a [documentação técnica da 1.5](docs/versao-1.5.md).

Aplicação privada para publicar arquivos SPED EFD por loja, tipo e competência. Depois do acesso, a lateral identifica a loja e o CNPJ e oferece as áreas SPED Fiscal e SPED Contribuições. O administrador cadastra os acessos e envia os arquivos; cada loja pode consultar os próprios documentos e, quando vinculada a filiais, alternar entre elas sem encerrar a sessão.

> A primeira versão roda com Python 3.11 ou superior, sem pacotes externos. O portal é uma base funcional para validação do fluxo e não passou por auditoria independente de segurança. Não o exponha à internet nem use dados fiscais reais em produção antes de seguir as recomendações de implantação abaixo.

## Requisitos

- Python 3.11 ou superior.
- Navegador moderno.
- Para produção: servidor HTTPS gerenciado por proxy reverso, backups protegidos e controle de acesso ao servidor.

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

Cada instalação deve criar seu próprio administrador e cadastrar suas lojas. Nenhum usuário, senha, arquivo fiscal ou conjunto de dados local é fornecido pelo repositório.

## Perfis de acesso

| Perfil | Entra com | Vê | Pode |
| --- | --- | --- | --- |
| Administração | usuário (ex.: `admin.fa`) | todas as lojas (e o monitoramento do suporte, quando ativo) | publicar arquivos, cadastrar lojas, baixar |
| Suporte | usuário (ex.: `suporte`) | todas as lojas | só consultar e baixar arquivos |
| Loja | CNPJ | as lojas do próprio grupo (mesma raiz de CNPJ) | consultar e baixar |

Para criar um usuário de suporte, use `criar-usuario-suporte.bat`. A senha aparece na tela e é acrescentada em `admin-credentials.txt` na Área de Trabalho. Pela linha de comando:

```powershell
python app.py criar-suporte suporte "Suporte Farmácias Associadas" --salvar-em "$env:USERPROFILE\Desktop\admin-credentials.txt"
```

Para trocar a senha de uma loja ou do suporte, use `redefinir-senha.bat`, informando o CNPJ da loja ou o usuário do suporte.

### Monitoramento do suporte (desativado na 1.5)

A partir da versão 1.5 o monitoramento vem **desligado**: não aparece na lateral, `/monitoramento` responde "Página não encontrada", nenhuma ação do suporte é gravada e o suporte não vê o aviso de registro. O código, a tabela `audit_log` e o histórico já gravado continuam no lugar. Para reativar, inicie o portal com a variável `PORTAL_MONITORAMENTO=1`:

```powershell
$env:PORTAL_MONITORAMENTO = '1'
python app.py
```

No `iniciar-portal.bat`, acrescente `set "PORTAL_MONITORAMENTO=1"` junto das outras variáveis.

Com o monitoramento ativo, tudo o que um usuário de suporte faz fica registrado. A administração consulta em **Monitoramento**, na barra lateral:

- entradas e saídas do portal e tentativas com a senha errada;
- cada loja consultada, com o tipo de SPED e o mês;
- cada arquivo baixado;
- tentativas bloqueadas, como publicar, cadastrar ou abrir o monitoramento.

Cada registro guarda data e hora, usuário, loja e CNPJ, arquivo, nome da máquina, IP e navegador. Há filtros por usuário, período e tipo de ação, e o botão **Exportar para Excel** gera um CSV com o histórico filtrado. O nome da máquina é o informado pela rede (DNS ou NetBIOS); quando a rede não informa, aparece só o IP. O suporte vê um aviso de que suas ações são registradas.

## Servidor provisório no Windows

Enquanto o portal não tem hospedagem definitiva, ele pode rodar neste computador:

| Arquivo | O que faz |
| --- | --- |
| `iniciar-portal.bat` | Sobe o portal em http://127.0.0.1:8000. Se o processo cair, reinicia em 10 segundos. Se o portal já estiver no ar, não abre uma segunda cópia. Registro em `logs/portal.log`. |
| `instalar-servico.bat` | Cria a tarefa do Windows **Portal Fiscal**, que roda o `iniciar-portal.bat` como SYSTEM sempre que o computador liga, mesmo sem ninguém fazer login. Pede permissão de administrador. |
| `remover-servico.bat` | Remove a tarefa e para o portal. |
| `criar-usuario-suporte.bat` | Cria um usuário de suporte e guarda a credencial na Área de Trabalho. |
| `redefinir-senha.bat` | Gera uma senha nova para uma loja (CNPJ) ou para o suporte (usuário). |

Para ver se a tarefa está instalada e o portal no ar:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\servico-windows.ps1 -Acao status
```

A tarefa não tem limite de tempo de execução e também roda na bateria. Se falhar, o Windows tenta de novo a cada minuto. O servidor escuta só em `127.0.0.1`. Para abrir na rede interna, defina `PORTAL_HOST=0.0.0.0` no `iniciar-portal.bat` e libere a porta no firewall. Faça isso apenas em rede confiável, seguindo as recomendações de implantação abaixo.

## Publicar um SPED

1. Entre como administrador e cadastre a loja com nome, CNPJ válido e senha inicial.
2. No painel, em **Enviar arquivo SPED**, escolha o arquivo `.txt` e clique em **Validar e publicar**. Não é preciso escolher loja, tipo nem mês.
3. O portal lê o registro `0000` e identifica sozinho:
   - o **tipo**: SPED Fiscal (ICMS/IPI) ou SPED Contribuições (PIS/COFINS), pela posição do período e do CNPJ, que é diferente em cada leiaute;
   - a **loja**, pelo CNPJ do arquivo (com os dígitos verificadores conferidos), entre as lojas ativas;
   - a **competência**, pelo período, que precisa cobrir um mês-calendário completo.
4. O arquivo é anexado na aba do tipo certo da loja, e um pop-up confirma o tipo, a loja, o CNPJ e a competência. Se algo não bater (CNPJ sem loja cadastrada, período parcial, arquivo que não é SPED EFD), o pop-up explica o motivo e nada é gravado.
5. Arquivos idênticos são bloqueados por hash; arquivos complementares diferentes podem coexistir na mesma loja, tipo e competência. Fiscal e Contribuições também podem coexistir.
6. A loja acessa a conta com seu CNPJ, escolhe o módulo e baixa os arquivos autorizados.

O arquivo precisa conter, no início, um registro `0000` SPED EFD com período de mês-calendário completo. O parser usa os campos próprios de cada leiaute: EFD Fiscal e EFD Contribuições. Arquivos acima de 40 MB, períodos incompatíveis, CNPJ divergente e arquivos sem esse registro são recusados. A plataforma não substitui a validação do PVA/SPED nem confere todas as regras fiscais do arquivo.

### Matriz e filiais

Contas de loja cujos CNPJs compartilham os mesmos 8 primeiros dígitos são tratadas como estabelecimentos da mesma empresa. Após entrar, o usuário pode escolher uma dessas filiais no seletor lateral; a sessão continua aberta, mas a lista e os downloads mostram somente os documentos da filial selecionada. O servidor também aplica esse escopo na rota de download, impedindo acesso a CNPJs de outra raiz. O vínculo é automático pelo CNPJ-base e não exige cadastrar relações separadas.

### Resumo fiscal da competência

Ao abrir uma loja, acima da lista de arquivos, o portal mostra um resumo gerencial da competência escolhida, calculado a partir dos SPEDs publicados:

- **SPED Fiscal:** saídas e entradas (C100/C190), apuração do ICMS (E110), ICMS-ST (E210), vencimento (E116) e inventário (H005). Traz movimento diário, vendas por tributação do ICMS, principais CFOPs e maiores fornecedores.
- **SPED Contribuições:** receita tributada e sem incidência (M210/M400), PIS e COFINS apurados e a recolher (M200/M600), créditos (M100/M500) e movimento por estabelecimento (C010/C100).

Documentos cancelados são ignorados, e notas com CFOP x.929 ficam fora das vendas para não duplicar cupons e NFC-e. Quando há mais de um arquivo na competência, cada seção vem do arquivo mais recente que a contém. Com duas ou mais competências publicadas, aparece também a evolução mensal.

O resumo é calculado no upload e guardado na tabela `document_summaries`. Para gerar os resumos de arquivos publicados antes desta versão:

```powershell
python app.py gerar-resumos
```

Os gráficos são SVG e CSS gerados no servidor, sem JavaScript. O resumo é gerencial e não substitui a apuração oficial.

### Previsão de publicação

O painel mostra os meses até dezembro/2026 (ajustável por `PORTAL_PREVISAO_ATE`, no formato `AAAA-MM`). Os meses sem arquivo aparecem como previstos, com o prazo de publicação:

- grupos com até 3 lojas (mesma raiz de CNPJ): até o **3º dia útil** do mês seguinte à competência;
- grupos com mais de 3 lojas: até o **5º dia útil**, com um ícone "?" explicando que o volume exige mais tempo e que correções entram na fila depois das postagens regulares.

A regra vale para o SPED Fiscal e o SPED Contribuições. Os dias úteis descontam fins de semana, feriados nacionais, Carnaval e Sexta-feira Santa. Feriados estaduais e municipais não são considerados.

## Dados locais

Por padrão, banco, segredo de sessão e arquivos ficam em `data/`:

- `data/portal.sqlite3`: usuários, hashes de senha, sessões e metadados dos documentos.
- `data/.secret`: segredo local usado na proteção do token antirrepetição do login.
- `data/documentos/`: arquivos SPED privados, nomeados internamente por identificador aleatório.
`data/` e os bancos SQLite estão no `.gitignore`. Não mova documentos para uma pasta servida como conteúdo estático nem inclua dados fiscais, credenciais ou bancos locais no Git. Para apontar os dados a um diretório protegido diferente, configure `PORTAL_DATA_DIR` antes de iniciar o servidor:

```powershell
$env:PORTAL_DATA_DIR = 'D:\DadosProtegidos\PortalFiscal'
python app.py
```

Proteja o diretório com permissões do sistema operacional e faça backups criptografados, com acesso restrito e retenção definida. Guarde o backup do banco e dos documentos de forma coordenada. Uma restauração precisa manter também o arquivo `.secret` para preservar as sessões existentes; alternativamente, invalide todas as sessões após a restauração.

## Configuração

| Variável | Padrão | Uso |
| --- | --- | --- |
| `PORTAL_HOST` | `127.0.0.1` | Interface de rede de escuta. Só altere em instalação controlada atrás de proxy. |
| `PORTAL_PORT` | `8000` | Porta HTTP local. |
| `PORTAL_DATA_DIR` | `data/` | Diretório privado para banco, segredo e arquivos. |
| `COOKIE_SECURE` | `0` | Use `1` somente quando HTTPS estiver configurado no proxy reverso. |
| `PORTAL_MONITORAMENTO` | `0` | Use `1` para reativar o monitoramento do suporte (desligado desde a 1.5). |
| `PORTAL_PREVISAO_ATE` | `2026-12` | Último mês (`AAAA-MM`) mostrado na previsão de publicação. |

Exemplo PowerShell para HTTPS terminado em um proxy local:

```powershell
$env:PORTAL_HOST = '127.0.0.1'
$env:PORTAL_PORT = '8000'
$env:COOKIE_SECURE = '1'
python app.py
```

O proxy deve forçar HTTPS, não deve ser acessível por HTTP público e precisa encaminhar para o serviço local. Configure limites de corpo de requisição compatíveis com o limite de 40 MB. Restrinja também o acesso administrativo e os backups. O servidor HTTP embutido no Python é destinado a desenvolvimento/uso local, não a servir tráfego público em produção. O portal ainda não possui recuperação de senha, autenticação multifator, trilha de auditoria exportável ou integração de identidade; estabeleça esses controles e obtenha revisão técnica antes de disponibilizar dados fiscais reais.

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

A suíte cobre dígitos verificadores do CNPJ, extração do cabeçalho/competência, identificação do tipo de SPED, publicação automática na loja e aba certas (com o pop-up de confirmação e as recusas), recusas de período inválido, política de senha e criação de conta.
