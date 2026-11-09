"""Telas de acesso: login e entrada pelo certificado digital."""
from __future__ import annotations

from portal import certificado
from portal.views.layout import h, icon

CHOOSE_AGAIN = "Feche todas as janelas do navegador, abra o link do e-mail de novo e escolha o certificado da loja. O navegador guarda a escolha enquanto está aberto."
TALK_TO_SUPPORT = "Se o problema continuar, fale com a equipe das Farmácias Associadas."

# Para cada recusa: título e o que a pessoa pode fazer. Quem lê é a loja ou a contabilidade, não a equipe técnica.
REFUSALS = {
    certificado.NO_CERTIFICATE: ("Nenhum certificado recebido", [
        "Use o computador em que o certificado da loja está instalado. No celular, o link normalmente não funciona.",
        "Se o certificado for token ou cartão, conecte-o antes de abrir o link.",
        "Quando o navegador pedir, escolha o certificado da loja. Se já tiver cancelado, feche todas as janelas do navegador e abra o link de novo."]),
    certificado.OTHER_STORE: ("Certificado de outra loja", [CHOOSE_AGAIN, TALK_TO_SUPPORT]),
    certificado.NO_CNPJ: ("Certificado de pessoa física", [CHOOSE_AGAIN, TALK_TO_SUPPORT]),
    certificado.EXPIRED: ("Certificado vencido", ["Renove o certificado com a certificadora da loja.", "Se a loja já tem um certificado novo, instale-o neste computador. " + CHOOSE_AGAIN]),
    certificado.NOT_YET_VALID: ("Certificado ainda não vale", ["Confira se a data e a hora deste computador estão certas.", TALK_TO_SUPPORT]),
    certificado.NO_STORE: ("Loja não encontrada", ["Confira se o certificado é da loja certa. " + CHOOSE_AGAIN, TALK_TO_SUPPORT]),
    certificado.UNREADABLE: ("Certificado não reconhecido", [CHOOSE_AGAIN, TALK_TO_SUPPORT]),
}


def entry_card(mark: str, title: str, text: str, steps: list[str], action: str = "") -> str:
    items = "".join(f"<li>{h(step)}</li>" for step in steps)
    return (f"<main class='entry-page'><section class='entry-card'><span class='entry-mark'>{icon(mark)}</span><h1>{h(title)}</h1><p>{h(text)}</p>"
            f"{f'<ul class=entry-steps>{items}</ul>' if items else ''}{action}</section></main>")


def certificate_refused(message: str) -> str:
    """Recusa da entrada por certificado: o que aconteceu e o que fazer."""
    title, steps = REFUSALS[message]
    return entry_card("lock", title, message, steps)


def certificate_exit() -> str:
    """Saída de quem entrou por certificado. Sem esta tela, a entrada automática desfaria o "Sair"."""
    action = f"<a class='btn btn-outline' href='/certificado'>{icon('arrow')}<span>Entrar de novo</span></a>"
    return entry_card("lock", "Você saiu do portal", "Sua sessão foi encerrada.",
                      ["Em computador compartilhado, feche todas as janelas do navegador. Enquanto ele estiver aberto, o certificado continua disponível para entrar de novo."], action)


def login_page(csrf: str, message: str = "", credential: str = "") -> str:
    """Com mensagem, mostra o erro acima do formulário e mantém o CNPJ digitado."""
    notice = f"<div class='notice notice-error' role='alert'>{h(message)}</div>" if message else ""
    kicker = "TENTE NOVAMENTE" if message else "BEM-VINDA, LOJA"
    return ("<main class='login-layout'><section class='login-brand'><div class='eyebrow'>ÁREA EXCLUSIVA</div><h1>Fiscal em dia.<br><em>Sem complicação.</em></h1>"
            "<p>Seus arquivos fiscais, organizados por competência e protegidos em um só lugar.</p><div class='login-foot'>ACESSO SEGURO <i></i> FARMÁCIAS ASSOCIADAS</div></section>"
            f"<section class='login-panel'>{notice}<div class='login-heading'><span class='section-kicker'>{kicker}</span><h2>Acesse seu portal</h2><p>Entre com o CNPJ cadastrado e sua senha.</p></div>"
            f"<form method='post' action='/login' class='stack-form'><input type='hidden' name='csrf' value='{h(csrf)}'>"
            f"<label>CNPJ ou usuário<input name='cnpj' autocomplete='username' placeholder='CNPJ da loja ou usuário admin' maxlength='40' value='{h(credential)}' required></label>"
            "<label>Senha<input name='password' type='password' autocomplete='current-password' placeholder='Sua senha de acesso' required></label>"
            "<button class='button button-primary button-wide'>Entrar no portal <span>→</span></button></form>"
            "<div class='login-note'><span class='lock-icon'>▣</span> Conexão protegida. Seus arquivos são privados.</div></section></main>")
