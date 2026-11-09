"""Configuração do portal: versão, caminhos e variáveis de ambiente.

As variáveis podem vir do ambiente ou de um arquivo .env na raiz do projeto (veja .env.example).
O que já estiver definido no ambiente tem prioridade sobre o .env.
"""
from __future__ import annotations

import os
import re
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

# Avisos de publicação por e-mail: DESLIGADOS por padrão. Desligado, nada entra na fila.
# Ligado, a fila funciona em modo de teste: nenhum e-mail sai do portal (veja portal/emails.py).
EMAIL_ENABLED = os.environ.get("PORTAL_EMAIL", "0") == "1"
# Se preenchido, troca todo destinatário por estes endereços. Trava contra disparo acidental.
EMAIL_REDIRECT = os.environ.get("PORTAL_EMAIL_REDIRECIONAR", "").strip()
# Endereço público do portal. Vazio, o aviso sai sem link.
PUBLIC_URL = os.environ.get("PORTAL_URL_PUBLICA", "").strip().rstrip("/")

# Envio real pelo Resend: DESLIGADO por padrão. Desligado, a fila continua em modo de teste e nada sai do portal.
EMAIL_SEND = os.environ.get("PORTAL_EMAIL_ENVIO_REAL", "0") == "1"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
# Sem domínio verificado no Resend, este remetente só entrega para o e-mail dono da conta.
EMAIL_FROM = os.environ.get("PORTAL_EMAIL_REMETENTE", "").strip() or "Portal Fiscal <onboarding@resend.dev>"
# 1 = os avisos vão para os contatos reais das lojas. Sem isto, o envio real só é aceito com PORTAL_EMAIL_REDIRECIONAR.
EMAIL_TO_STORES = os.environ.get("PORTAL_EMAIL_PARA_LOJAS", "0") == "1"
if EMAIL_SEND and not EMAIL_ENABLED:
    PROBLEMS.append("PORTAL_EMAIL_ENVIO_REAL=1 precisa de PORTAL_EMAIL=1: sem a fila ligada, nada seria enviado.")
if EMAIL_SEND and EMAIL_REDIRECT and not re.search(r"[^\s@,;]+@[^\s@,;]+\.[A-Za-z]{2,}", EMAIL_REDIRECT):
    PROBLEMS.append("PORTAL_EMAIL_REDIRECIONAR não tem nenhum endereço de e-mail válido.")
if EMAIL_SEND and not RESEND_API_KEY:
    PROBLEMS.append("PORTAL_EMAIL_ENVIO_REAL=1 precisa de RESEND_API_KEY.")
if EMAIL_SEND and not EMAIL_REDIRECT and not EMAIL_TO_STORES:
    PROBLEMS.append("PORTAL_EMAIL_ENVIO_REAL=1 precisa de PORTAL_EMAIL_REDIRECIONAR (para testar) ou de PORTAL_EMAIL_PARA_LOJAS=1 (para enviar às lojas).")

# Entrada pelo certificado digital: DESLIGADA enquanto não houver segredo. É o valor que o servidor HTTPS da frente
# manda em toda requisição, e é ele que dá ao portal o direito de confiar no certificado repassado.
CERT_PROXY_SECRET = os.environ.get("PORTAL_CERTIFICADO_SEGREDO", "").strip()
if CERT_PROXY_SECRET and (len(CERT_PROXY_SECRET) < 32 or not CERT_PROXY_SECRET.isascii()):
    PROBLEMS.append("PORTAL_CERTIFICADO_SEGREDO inválido. Use pelo menos 32 caracteres aleatórios, sem acentos.")
# 1 = o portal só é alcançado por um proxy, e o endereço real de quem acessa vem no X-Forwarded-For.
# É uma confiança separada do segredo acima: acreditar num endereço não pode dar direito a entrar como loja.
BEHIND_PROXY = os.environ.get("PORTAL_ATRAS_DE_PROXY", "0") == "1"

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
SESSION_SECONDS = 8 * 60 * 60
LOGIN_FORM_SECONDS = 2 * 60 * 60
