# Changelog

Mudanças do Portal Fiscal por versão. Detalhes técnicos da 1.5 em [docs/versao-1.5.md](docs/versao-1.5.md).

## 1.5.0 — 2026-10-19

### Adicionado
- **Publicação automática do SPED.** O formulário **Enviar arquivo SPED** pede só o arquivo `.txt`. O portal lê o registro `0000` e identifica sozinho:
  - o **tipo**: SPED Fiscal (ICMS/IPI) ou SPED Contribuições (PIS/COFINS), pela posição do período e do CNPJ em cada leiaute;
  - a **loja**: a loja ativa com o CNPJ do arquivo;
  - a **competência**: o mês do período do arquivo.

  Em seguida, anexa o arquivo na aba certa da loja.
- **Pop-up de confirmação.** Mostra o tipo, a loja, o CNPJ, a competência e o nome do arquivo publicado. Quando o arquivo é recusado, o pop-up fica vermelho, explica o motivo e nada é gravado. Fecha pelo botão **OK, entendi** ou pela tecla **Esc**.
- `static/portal.js`: script mínimo, servido pelo próprio portal e compatível com a CSP, que abre o pop-up como modal. É o que faz o Esc funcionar e prende o foco no pop-up.
- Versão do portal (`PORTAL_VERSION = "1.5.0"`) no rodapé das páginas e na mensagem de início do servidor.
- Versão no endereço do CSS e do script (`?v=<hash>`), para o navegador não usar uma cópia antiga do cache depois de uma atualização.
- 25 testes novos: detecção do tipo, regras da publicação, tela e pop-up, Esc, cache do CSS e monitoramento desligado. A suíte foi de 29 para 54 testes.

### Alterado
- O formulário de envio não tem mais os campos **Loja** e **Mês de referência**, nem o tipo de SPED preso à aba aberta.
- As recusas de envio deixaram de cair na mensagem genérica "Não foi possível concluir" e passaram a ter mensagens específicas: CNPJ sem loja, loja inativa, arquivo duplicado, período parcial, CNPJ inválido ou arquivo que não é SPED EFD.
- `parse_sped_header` passou a usar a tabela `SPED_LAYOUTS`, a mesma usada na detecção do tipo.

### Desativado
- **Monitoramento do suporte.** Agora fica desligado por padrão:
  - sem o item na lateral;
  - `/monitoramento` e `/monitoramento.csv` respondem 404;
  - nenhuma gravação em `audit_log`;
  - sem o aviso de registro para o suporte.

  O código e o histórico já gravado foram mantidos. Para reativar, use `PORTAL_MONITORAMENTO=1`.

### Corrigido
- O pop-up continuava visível depois de clicar em **OK, entendi**, porque o `display: grid` do CSS anulava o `display: none` do `<dialog>` fechado.

## Perfil de suporte — 2026-10-16 (commit `5afaa80`, publicado sem número de versão)
- Perfil **Suporte**: consulta e download em todas as lojas, sem publicar nem cadastrar.
- Monitoramento das ações do suporte, com filtros e exportação em CSV.
- `criar-usuario-suporte.bat`; `redefinir-senha.bat` passou a aceitar também o usuário do suporte.

## 1.1.0 — 2026-10-16
- Resumo fiscal do mês (SPED Fiscal e Contribuições), com indicadores e gráficos em SVG.
- Previsão de publicação: prazo no 3º ou no 5º dia útil, conforme o tamanho do grupo.
- Servidor provisório no Windows: `iniciar-portal.bat` e a tarefa agendada.
- Correção do login descartado ao recarregar a página ou abrir outra aba.

## Versão inicial — 2026-10-14
- Estrutura inicial: login por CNPJ ou usuário, cadastro de lojas, publicação de SPED por loja, tipo e competência, acesso de matriz e filiais pela raiz do CNPJ.
