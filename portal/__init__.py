"""Portal Fiscal (Farmácias Associadas): publicação e consulta de arquivos SPED EFD por loja.

Módulos:
    config       variáveis de ambiente, caminhos e versão
    db           banco SQLite, arquivos privados e migrações numeradas
    auth         senhas, contas e sessões
    sped         leitura do registro 0000 (tipo, CNPJ, período e competência)
    publicacao   publicação de um SPED e resumo fiscal da competência
    calendario   dias úteis e prazos de publicação
    auditoria    monitoramento do suporte (desligado por padrão)
    backup       cópia de segurança do banco, dos documentos e do segredo
    views/       HTML das páginas
    handler      rotas HTTP
    cli          linha de comando (python app.py ...)
"""


def main() -> None:
    from portal.cli import main as run

    run()
