# Portal Fiscal | Farmácias Associadas

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

## Publicar um SPED

1. Entre como administrador e cadastre a loja com nome, CNPJ válido e senha inicial.
2. Abra a loja pela lista ou selecione-a na lateral e escolha `SPED Fiscal` ou `SPED Contribuições`.
3. Escolha a competência e envie o arquivo `.txt` na área do tipo correspondente.
4. O portal lê o registro `0000` no leiaute do tipo escolhido, valida os dígitos verificadores do CNPJ e confirma que o CNPJ e o mês do arquivo correspondem à loja e à competência selecionadas.
5. Só então o arquivo é armazenado. Arquivos idênticos são bloqueados por hash; arquivos complementares diferentes podem coexistir na mesma loja, tipo e competência. Fiscal e Contribuições também podem coexistir.
6. A loja acessa a conta com seu CNPJ, escolhe o módulo e baixa os arquivos autorizados.

O arquivo precisa conter, no início, um registro `0000` SPED EFD com período de mês-calendário completo. O parser usa os campos próprios de cada leiaute: EFD Fiscal e EFD Contribuições. Arquivos acima de 40 MB, períodos incompatíveis, CNPJ divergente e arquivos sem esse registro são recusados. A plataforma não substitui a validação do PVA/SPED nem confere todas as regras fiscais do arquivo.

### Matriz e filiais

Contas de loja cujos CNPJs compartilham os mesmos 8 primeiros dígitos são tratadas como estabelecimentos da mesma empresa. Após entrar, o usuário pode escolher uma dessas filiais no seletor lateral; a sessão continua aberta, mas a lista e os downloads mostram somente os documentos da filial selecionada. O servidor também aplica esse escopo na rota de download, impedindo acesso a CNPJs de outra raiz. O vínculo é automático pelo CNPJ-base e não exige cadastrar relações separadas.

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

A suíte cobre dígitos verificadores do CNPJ, extração do cabeçalho/competência, recusas de período inválido, política de senha e criação de conta.
