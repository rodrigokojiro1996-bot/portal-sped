"""Configuração do portal: versão, caminhos e variáveis de ambiente.

As variáveis podem vir do ambiente ou de um arquivo .env na raiz do projeto (veja .env.example).
O que já estiver definido no ambiente tem prioridade sobre o .env.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL_VERSION = "1.6.0"


def load_env_file(path: Path) -> None:
    """Lê linhas CHAVE=valor de um .env, sem dependências externas. Ignora comentários e linhas vazias."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")

# Problemas encontrados na leitura da configuração; a checagem de inicialização (cli) os mostra e recusa subir.
PROBLEMS: list[str] = []


def _port(value: str) -> int:
    if value.isdigit() and 0 < int(value) < 65536:
        return int(value)
    PROBLEMS.append(f"PORTAL_PORT inválida: '{value}'. Use um número de 1 a 65535.")
    return 8000


HOST = os.environ.get("PORTAL_HOST", "127.0.0.1")
PORT = _port(os.environ.get("PORTAL_PORT", "8000"))
DATA_DIR = Path(os.environ.get("PORTAL_DATA_DIR", ROOT / "data"))
BACKUP_DIR = Path(os.environ.get("PORTAL_BACKUP_DIR", ROOT / "backups"))
BACKUP_KEEP_DAYS = 14
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
FORECAST_UNTIL = os.environ.get("PORTAL_PREVISAO_ATE", "2026-12")

# Monitoramento do suporte: DESATIVADO a partir da versão 1.5.
# Desligado, o portal não mostra o item "Monitoramento" na lateral, responde "Página não encontrada" em
# /monitoramento e /monitoramento.csv, não grava nada em audit_log e não avisa o suporte de que as ações são
# registradas. O código, a tabela audit_log e o histórico já gravado continuam no lugar.
# Para reativar: PORTAL_MONITORAMENTO=1 no ambiente ou no .env.
MONITORING_ENABLED = os.environ.get("PORTAL_MONITORAMENTO", "0") == "1"

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
SESSION_SECONDS = 8 * 60 * 60
LOGIN_FORM_SECONDS = 2 * 60 * 60
