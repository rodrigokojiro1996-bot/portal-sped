"""Tela de acesso (login)."""
from __future__ import annotations

from portal.views.layout import h


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
