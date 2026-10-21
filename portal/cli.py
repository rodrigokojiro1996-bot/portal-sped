"""Linha de comando do portal: python app.py [comando].

Comandos:
    (nenhum)            sobe o servidor
    create-admin        cria um administrador (pede a senha sem mostrar)
    criar-suporte       cria um usuário de suporte: [usuario] [nome] [--salvar-em arquivo.txt]
    redefinir-senha     gera senha nova para uma loja (CNPJ) ou suporte (usuário)
    gerar-resumos       recalcula os resumos fiscais dos arquivos publicados
"""
from __future__ import annotations

import getpass
import sqlite3
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

from portal import config
from portal.auth import create_support_user, create_user, reset_store_password
from portal.db import configure_storage, connect
from portal.handler import PortalHandler
from portal.publicacao import document_summary
from portal.sped import format_cnpj, normalize_cnpj


def create_admin() -> None:
    username = input("Usuário administrador: ").strip()
    name = input("Nome do administrador: ").strip()
    password = getpass.getpass("Senha (mínimo 12 caracteres): ")
    confirm = getpass.getpass("Confirme a senha: ")
    if password != confirm:
        raise SystemExit("As senhas não conferem.")
    try:
        create_user(None, name, "admin", password, username)
    except (ValueError, sqlite3.IntegrityError) as error:
        raise SystemExit(str(error)) from error
    print("Administrador criado. Inicie o portal com: python app.py")


def create_support(args: list[str]) -> None:
    target = None
    if "--salvar-em" in args:
        position = args.index("--salvar-em")
        target = Path(args[position + 1]) if position + 1 < len(args) else None
        args = args[:position] + args[position + 2:]
    login_name = (args[0] if args else input("Usuário do suporte (ex.: suporte): ")).strip().lower() or "suporte"
    name = args[1] if len(args) > 1 else "Suporte Farmácias Associadas"
    try:
        password = create_support_user(login_name, name)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    recorded = " As ações ficam registradas no monitoramento da administração." if config.MONITORING_ENABLED else ""
    print(f"Usuário de suporte criado: {login_name} ({name})")
    print(f"Senha: {password}")
    print("Acesso: todas as lojas, somente consulta e download." + recorded)
    if target:
        block = (f"\nPORTAL FISCAL - ACESSO SUPORTE\nUsuário: {login_name}\nSenha: {password}\n"
                 "Perfil: consulta de todas as lojas e download de arquivos. Não publica nem cadastra." + (" Ações registradas no monitoramento." if config.MONITORING_ENABLED else "") + "\n")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(block)
        print(f"Credencial adicionada em: {target}")


def reset_password(args: list[str]) -> None:
    identifier = args[0] if args else input("CNPJ da loja: ")
    try:
        name, password = reset_store_password(identifier)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Loja: {name}")
    print(f"CNPJ: {format_cnpj(identifier)}" if len(normalize_cnpj(identifier)) == 14 else f"Usuário: {identifier.strip().lower()}")
    print(f"Nova senha: {password}")
    print("Sessões antigas encerradas e bloqueios de tentativa liberados.")


def rebuild_summaries() -> None:
    with connect() as db:
        documents = db.execute("SELECT * FROM documents ORDER BY uploaded_at").fetchall()
    failures = sum(1 for document in documents if document_summary(document) is None)
    print(f"Resumos prontos: {len(documents) - failures} de {len(documents)} arquivos.")


def serve() -> None:
    server = ThreadingHTTPServer((config.HOST, config.PORT), PortalHandler)
    print(f"Portal Fiscal {config.PORTAL_VERSION} ativo em http://{config.HOST}:{config.PORT} (monitoramento do suporte {'ativo' if config.MONITORING_ENABLED else 'desativado'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando portal...")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    command, rest = (args[0], args[1:]) if args else ("", [])
    configure_storage()
    if command == "create-admin":
        create_admin()
    elif command == "criar-suporte":
        create_support(rest)
    elif command == "redefinir-senha":
        reset_password(rest)
    elif command == "gerar-resumos":
        rebuild_summaries()
    elif command == "":
        serve()
    else:
        raise SystemExit(f"Comando desconhecido: {command}\n{__doc__}")
