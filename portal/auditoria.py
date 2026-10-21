"""Monitoramento do suporte: registro e consulta das ações dos perfis auditados.

Desligado por padrão desde a versão 1.5 (config.MONITORING_ENABLED). Desligado, record() não grava nada.
"""
from __future__ import annotations

import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import resumo_sped
from portal import config
from portal.db import connect
from portal.sped import DOCUMENT_TYPES

AUDITED_ROLES = ("support",)
AUDIT_ACTIONS = {
    # ação: (rótulo curto do selo, categoria do filtro)
    "login": ("Acesso", "acessos"),
    "logout": ("Acesso", "acessos"),
    "login_falhou": ("Alerta", "alertas"),
    "abriu_central": ("Consulta", "consultas"),
    "abriu_loja": ("Consulta", "consultas"),
    "baixou_arquivo": ("Download", "downloads"),
    "acao_bloqueada": ("Alerta", "alertas"),
}
AUDIT_FILTERS = {"": "Todas as ações", "acessos": "Entradas e saídas", "consultas": "Lojas consultadas", "downloads": "Arquivos baixados", "alertas": "Alertas"}
AUDIT_PERIODS = {"hoje": ("Hoje", 0), "7d": ("Últimos 7 dias", 6), "30d": ("Últimos 30 dias", 29), "tudo": ("Todo o período", None)}
WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
_machine_cache: dict[str, str] = {}
_machine_resolver = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nome-maquina")


def machine_name(ip: str) -> str:
    """Nome do computador de onde veio o acesso, pela rede (DNS/NetBIOS). Vazio quando a rede não informa."""
    if ip in _machine_cache:
        return _machine_cache[ip]
    if ip in ("127.0.0.1", "::1"):
        name = socket.gethostname().upper()
    else:
        try:
            name = _machine_resolver.submit(socket.gethostbyaddr, ip).result(timeout=1.5)[0].split(".")[0].upper()
        except Exception:
            name = ""
    _machine_cache[ip] = name
    return name


def browser_label(agent: str) -> str:
    browser = ("Edge" if "Edg/" in agent else "Opera" if "OPR/" in agent else "Chrome" if "Chrome/" in agent
               else "Firefox" if "Firefox/" in agent else "Safari" if "Safari/" in agent else "Navegador não identificado")
    system = ("Windows" if "Windows" in agent else "Android" if "Android" in agent else "iPhone" if ("iPhone" in agent or "iPad" in agent)
              else "macOS" if "Mac OS" in agent else "Linux" if "Linux" in agent else "")
    return f"{browser} no {system}" if system else browser


def record(user: sqlite3.Row | dict | None, action: str, ip: str, user_agent: str, store: sqlite3.Row | dict | None = None,
           document_type: str | None = None, competence: str | None = None, file_name: str | None = None, detail: str | None = None) -> None:
    """Registra uma ação de um perfil monitorado (suporte): quem, o quê, qual loja, quando e de qual máquina.

    Abrir a mesma tela de novo em menos de 1 minuto (recarregar, voltar) não gera registro repetido.
    """
    if not config.MONITORING_ENABLED or not user or user["role"] not in AUDITED_ROLES:
        return
    now = datetime.now().astimezone()
    store_id = store["id"] if store else None
    with connect() as db:
        if action in ("abriu_loja", "abriu_central"):
            last = db.execute("SELECT action, store_id, document_type, competence, created_at FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
            if (last and (last["action"], last["store_id"], last["document_type"], last["competence"]) == (action, store_id, document_type, competence or None)
                    and (now - datetime.fromisoformat(last["created_at"])).total_seconds() < 60):
                return
        db.execute(
            "INSERT INTO audit_log (created_at, user_id, user_name, login_name, action, store_id, store_name, store_cnpj, document_type, competence, file_name, detail, ip, machine, browser) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), user["id"], user["name"], user["login_name"], action, store_id, store["name"] if store else None,
             store["cnpj"] if store else None, document_type, competence or None, file_name, detail, ip, machine_name(ip), browser_label(user_agent)))


def audit_sentence(entry: sqlite3.Row) -> tuple[str, str]:
    """Frase principal e complemento que descrevem uma ação registrada, em linguagem simples."""
    type_label = DOCUMENT_TYPES.get(entry["document_type"] or "", ("", "", ""))[0]
    month = resumo_sped.month_label(entry["competence"]).capitalize() if entry["competence"] else ""
    context = " · ".join(part for part in (type_label, month) if part)
    action = entry["action"]
    if action == "login":
        return "Entrou no portal", ""
    if action == "logout":
        return "Saiu do portal", ""
    if action == "login_falhou":
        return "Tentou entrar com a senha errada", "O acesso foi recusado"
    if action == "abriu_central":
        return "Abriu a lista de todas as lojas", type_label
    if action == "abriu_loja":
        return "Consultou a loja", context
    if action == "baixou_arquivo":
        return f"Baixou o arquivo {entry['file_name']}", context
    if action == "acao_bloqueada":
        return f"Tentou {entry['detail']}", "Bloqueado: o suporte só consulta e baixa arquivos"
    return action, ""


def audit_entries(user_id: str = "", period: str = "7d", category: str = "", limit: int | None = 500) -> list[sqlite3.Row]:
    clauses, params = [], []
    if user_id.isdigit():
        clauses.append("user_id=?")
        params.append(int(user_id))
    days = AUDIT_PERIODS.get(period, AUDIT_PERIODS["7d"])[1]
    if days is not None:
        clauses.append("created_at >= ?")
        params.append((date.today() - timedelta(days=days)).isoformat())
    actions = [action for action, (_, group) in AUDIT_ACTIONS.items() if group == category]
    if actions:
        clauses.append(f"action IN ({','.join('?' * len(actions))})")
        params.extend(actions)
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as db:
        return db.execute(sql, params).fetchall()


def csv_cell(value: object) -> str:
    """Célula de CSV para Excel (separador ;), protegida contra fórmulas injetadas."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"' if any(char in text for char in ';"\n\r') else text
