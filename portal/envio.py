"""Envio de um e-mail pela API do Resend, só com a biblioteca padrão, como o resto do portal.

send() nunca lança. Devolve o id da mensagem no Resend ou um motivo seguro de gravar: só o tipo do erro, porque a
mensagem do Resend pode repetir endereços e trechos do e-mail.

A chave anti-duplicação (Idempotency-Key) é o que torna o reenvio seguro: com a mesma chave e o mesmo conteúdo, o
Resend devolve a resposta original sem mandar de novo. Se a resposta se perder no caminho, a fila tenta outra vez com
a mesma chave e o e-mail não sai duas vezes. O Resend guarda as chaves por 24 horas.

Cada resultado diz duas coisas que a fila precisa para não duplicar:
    reached    a requisição pode ter chegado ao Resend (a chave passou a valer a partir daí);
    uncertain  o e-mail pode ter saído sem confirmação: sem resposta, erro do servidor ou resposta sem o id.
Falha de nome ou conexão recusada não chegou a lugar nenhum: nem uma coisa nem outra.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import NamedTuple

from portal import config

API_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 15
# O Resend aceita 10 requisições por segundo por conta; com folga, para outro sistema da mesma conta não ficar sem.
MIN_INTERVAL_SECONDS = 0.25
RETRY_WAITS = (1, 2, 4)
# Erros passageiros: vale esperar e tentar de novo. Os outros só mudam com outra configuração ou outro conteúdo.
TRANSIENT = {"rate_limit_exceeded", "concurrent_idempotent_requests", "resource_locked", "application_error", "service_unavailable"}
# Passageiros em que o e-mail pode ter sido aceito antes da falha.
MAYBE_ACCEPTED = {"concurrent_idempotent_requests", "resource_locked", "application_error", "service_unavailable"}
CONFLICT = "invalid_idempotent_request"  # mesma chave com conteúdo diferente
NO_ANSWER = "sem confirmação do Resend"
NOT_REACHED = "sem conexão com o Resend"

_urlopen = urllib.request.urlopen
_sleep = time.sleep
_pace = threading.Lock()
_last_request = 0.0


class SendResult(NamedTuple):
    provider_id: str | None = None
    error: str | None = None
    conflict: bool = False
    reached: bool = True
    uncertain: bool = False


class Transient(NamedTuple):
    name: str
    maybe_accepted: bool


def _wait_turn() -> None:
    """Espaça as requisições: a fila, o botão e a varredura podem pedir envios ao mesmo tempo."""
    global _last_request
    with _pace:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request)
        if wait > 0:
            _sleep(wait)
        _last_request = time.monotonic()


def _error_name(error: urllib.error.HTTPError) -> str:
    try:
        name = json.loads(error.read().decode("utf-8")).get("name")
    except (ValueError, AttributeError, OSError):
        name = None
    return name if isinstance(name, str) and name.replace("_", "").isalnum() else f"HTTP {error.code}"


def _request(message: dict, idempotency_key: str) -> SendResult | Transient:
    """Uma requisição. Devolve o resultado final, ou um erro passageiro para tentar de novo."""
    request = urllib.request.Request(API_URL, data=json.dumps(message).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {config.RESEND_API_KEY}",
        "Content-Type": "application/json",
        # Sem User-Agent o Resend recusa com 403, o que pareceria chave inválida.
        "User-Agent": f"PortalFiscal/{config.PORTAL_VERSION}",
        "Idempotency-Key": idempotency_key,
    })
    _wait_turn()
    try:
        with _urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            provider_id = json.loads(response.read().decode("utf-8")).get("id")
        if provider_id:
            return SendResult(provider_id=str(provider_id))
        return SendResult(error=f"{NO_ANSWER} (resposta sem o id da mensagem)", uncertain=True)  # aceitou, mas não disse qual
    except urllib.error.HTTPError as error:
        name = _error_name(error)
        if name in TRANSIENT or error.code >= 500:
            return Transient(name, name in MAYBE_ACCEPTED or error.code >= 500)
        return SendResult(error=f"recusado pelo Resend ({name})", conflict=name == CONFLICT)
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.gaierror, ConnectionRefusedError)):
            return SendResult(error=f"{NOT_REACHED} ({type(error.reason).__name__})", reached=False)
        return SendResult(error=f"{NO_ANSWER} ({type(error).__name__})", uncertain=True)
    except (http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
        # Pode ter saído e a resposta se perdido: a nova tentativa usa a mesma chave e não duplica.
        return SendResult(error=f"{NO_ANSWER} ({type(error).__name__})", uncertain=True)


def send(message: dict, idempotency_key: str) -> SendResult:
    """Envia, esperando e tentando de novo nos erros passageiros. Nunca lança."""
    maybe_accepted = False
    for wait in (*RETRY_WAITS, None):
        result = _request(message, idempotency_key)
        if isinstance(result, SendResult):
            return result._replace(uncertain=result.uncertain or maybe_accepted)
        maybe_accepted = maybe_accepted or result.maybe_accepted
        if wait is None:
            if maybe_accepted:
                return SendResult(error=f"{NO_ANSWER} ({result.name})", uncertain=True)
            return SendResult(error=f"o Resend continua indisponível ({result.name})")
        _sleep(wait)
    return SendResult(error="o Resend continua indisponível")
