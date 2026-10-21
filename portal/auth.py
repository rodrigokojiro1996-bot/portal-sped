"""Contas, senhas e tokens de sessão.

Perfis de acesso:
    admin   - vê todas as lojas, publica arquivos, cadastra lojas e, com o monitoramento ativo, vê as ações do suporte;
    support - vê todas as lojas e baixa arquivos (somente consulta); com o monitoramento ativo, tudo o que faz é registrado;
    store   - vê só as lojas do próprio grupo (mesma raiz de CNPJ).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from portal.db import connect
from portal.sped import normalize_cnpj, valid_cnpj

USER_ROLES = ("admin", "support", "store")
STAFF_ROLES = ("admin", "support")
ROLE_LABELS = {"admin": "Administração", "support": "Suporte", "store": "Acesso da loja"}
PASSWORD_ITERATIONS = 600_000
# Senhas geradas pelo portal: sem caracteres que se confundem ao ler ou digitar (0/O, 1/l/I).
READABLE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def password_digest(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_user(cnpj: str | None, name: str, role: str, password: str, login_name: str | None = None) -> None:
    if role not in USER_ROLES:
        raise ValueError("Perfil de acesso desconhecido.")
    normalized = normalize_cnpj(cnpj or "") if role == "store" else None
    if role == "store" and not valid_cnpj(normalized or ""):
        raise ValueError("Informe um CNPJ válido.")
    username = (login_name or "").strip().lower() if role in STAFF_ROLES else None
    if role in STAFF_ROLES and (not username or not re.fullmatch(r"[a-z0-9._-]{3,40}", username)):
        raise ValueError("O usuário deve ter 3 a 40 caracteres: letras, números, ponto, hífen ou sublinhado.")
    if len(password) < 12:
        raise ValueError("A senha deve ter pelo menos 12 caracteres.")
    with connect() as db:
        db.execute(
            "INSERT INTO users (cnpj, login_name, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (normalized, username, name.strip(), role, password_digest(password), datetime.now(timezone.utc).isoformat()),
        )


def readable_password() -> str:
    """Senha no formato Kp7m-Xq4t-Rw9z: 12 caracteres sem 0/O e 1/l/I, fácil de ler e digitar."""
    return "-".join("".join(secrets.choice(READABLE_ALPHABET) for _ in range(4)) for _ in range(3))


def reset_store_password(identifier: str) -> tuple[str, str]:
    """Gera uma senha nova (loja pelo CNPJ ou suporte pelo usuário), encerra as sessões e libera bloqueios de login."""
    digits = normalize_cnpj(identifier)
    password = readable_password()
    with connect() as db:
        if len(digits) == 14:
            account = db.execute("SELECT id, name FROM users WHERE role='store' AND cnpj=?", (digits,)).fetchone()
            missing = "Nenhuma loja cadastrada com esse CNPJ."
        else:
            account = db.execute("SELECT id, name FROM users WHERE role='support' AND login_name=?", (identifier.strip().lower(),)).fetchone()
            missing = "Nenhum usuário de suporte com esse nome. Informe o CNPJ da loja ou o usuário do suporte."
        if not account:
            raise ValueError(missing)
        db.execute("UPDATE users SET password_hash=?, active=1 WHERE id=?", (password_digest(password), account["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (account["id"],))
        db.execute("DELETE FROM login_attempts")
    return account["name"], password


def create_support_user(login_name: str, name: str) -> str:
    """Cria um usuário de suporte (somente consulta e download) e devolve a senha gerada."""
    password = readable_password()
    try:
        create_user(None, name, "support", password, login_name)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Já existe um usuário chamado '{login_name}'.") from error
    return password
