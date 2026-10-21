"""Moldura das páginas (topo, rodapé, CSS e script), ícones e pequenos componentes reutilizados."""
from __future__ import annotations

import hashlib
import html
import sqlite3

import resumo_sped
from portal import config
from portal.auth import ROLE_LABELS

# Ícones de traço (24x24) desenhados em SVG inline: nítidos em qualquer tela e permitidos pela CSP.
ICONS = {
    "download": "<path d='M12 4v11'/><path d='m7 10 5 5 5-5'/><path d='M5 20h14'/>",
    "logout": "<path d='M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3'/><path d='m10 16 4-4-4-4'/><path d='M14 12H4'/>",
    "store": "<path d='M4 10v10h16V10'/><path d='m3 10 2-6h14l2 6'/><path d='M9 20v-6h6v6'/>",
    "file": "<path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/>",
    "arrow": "<path d='M5 12h14'/><path d='m13 6 6 6-6 6'/>",
    "upload": "<path d='M12 20V9'/><path d='m7 14 5-5 5 5'/><path d='M5 4h14'/>",
    "plus": "<path d='M12 5v14'/><path d='M5 12h14'/>",
    "lock": "<rect x='5' y='11' width='14' height='9' rx='2'/><path d='M8 11V8a4 4 0 0 1 8 0v3'/>",
    "calendar": "<rect x='4' y='5' width='16' height='15' rx='2'/><path d='M4 10h16'/><path d='M9 3v4'/><path d='M15 3v4'/>",
    "eye": "<path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>",
    "filter": "<path d='M4 5h16l-6 7.5V19l-4-2v-4.5z'/>",
}

# Versão do CSS e do script no endereço de cada um: muda quando o arquivo muda, e o navegador não usa a cópia antiga do cache.
STYLES_VERSION = hashlib.sha256((config.ROOT / "static" / "styles.css").read_bytes()).hexdigest()[:10]
SCRIPT_VERSION = hashlib.sha256((config.ROOT / "static" / "portal.js").read_bytes()).hexdigest()[:10]


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def icon(name: str) -> str:
    return f"<svg class='icon' viewBox='0 0 24 24' aria-hidden='true' focusable='false'>{ICONS[name]}</svg>"


def dashboard_link(store_id: int | None = None, document_type: str = "fiscal", competence: str = "") -> str:
    """Endereço do painel para uma loja, tipo de SPED e mês (já escapado para uso em atributo HTML)."""
    params = ([f"store_id={store_id}"] if store_id else []) + [f"type={document_type}"] + ([f"competence={competence}"] if competence else [])
    return "/dashboard?" + "&amp;".join(params)


def help_tip(text: str) -> str:
    """Ícone "?" que mostra a explicação ao passar o mouse, tocar ou focar pelo teclado."""
    return f"<span class='help' tabindex='0' role='note' aria-label='{h(text)}'>?<span class='help-tip' aria-hidden='true'>{h(text)}</span></span>"


def file_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB".replace(".", ",") if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


def short_store_names(stores: list[sqlite3.Row]) -> dict[int, str]:
    """Nomes curtos para as lojas de um mesmo grupo, sem a parte repetida da razão social.

    "FARMACIA EXEMPLO LTDA - VR" vira "VR"; "LOJA 2 - DROGARIA X LTDA" vira "LOJA 2".
    """
    return dict(zip((store["id"] for store in stores), resumo_sped.short_labels([store["name"].strip() for store in stores])))


def page_heading(kicker: str, title: str, text: str) -> str:
    return f"<div class='page-heading'><div><span class='section-kicker'>{h(kicker.upper())}</span><h1>{h(title)}</h1><p>{h(text)}</p></div></div>"


def message_page(text: str) -> str:
    """Conteúdo de uma página simples de aviso (não encontrado, acesso negado, sessão expirada)."""
    return f"<main class='content'><h1>{h(text)}</h1></main>"


def render_page(title: str, content: str, user: sqlite3.Row | None = None) -> bytes:
    nav = ""
    if user:
        nav = (f"<div class='user-chip'><span class='user-mark'>{h(user['name'][:1].upper())}</span><span class='user-text'>{h(user['name'])}<small>{ROLE_LABELS.get(user['role'], '')}</small></span>"
               f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{h(user['csrf_token'])}'><button class='btn btn-ghost btn-sm' type='submit'>{icon('logout')}<span>Sair</span></button></form></div>")
    brand = "<a class='brand' href='/dashboard'><img class='brand-logo' src='/static/farmacias-associadas-logo.png' alt='Farmácias Associadas'><span class='brand-context'>PORTAL FISCAL</span></a>"
    return (f"<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='theme-color' content='#087e70'><title>{h(title)} · Portal Fiscal</title><link rel='stylesheet' href='/static/styles.css?v={STYLES_VERSION}'><script src='/static/portal.js?v={SCRIPT_VERSION}' defer></script></head><body><header class='topbar'>{brand}{nav}</header>{content}<footer class='site-footer'><span>PORTAL FISCAL <i></i> FARMÁCIAS ASSOCIADAS <i></i> V{config.PORTAL_VERSION}</span><span>Documentos acessíveis somente à loja autorizada.</span></footer></body></html>").encode("utf-8")
