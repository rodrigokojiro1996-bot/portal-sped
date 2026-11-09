-- Envio real dos avisos pelo Resend.
--   html, provider_id   o HTML enviado, para conferência, e o id que o Resend devolve, para achar a mensagem no painel dele;
--   idempotency_key     a chave anti-duplicação em uso. Fica gravada: trocar de chave entre tentativas permitiria duplicar;
--   key_sent_at         quando essa chave foi usada pela primeira vez. O Resend só a reconhece por 24 h;
--   uncertain           1 = alguma tentativa com essa chave ficou sem resposta, então o e-mail pode já ter saído;
--   confirmed           1 = a administração confirmou o envio de um aviso parado ou antigo.
ALTER TABLE email_notifications ADD COLUMN html TEXT;
ALTER TABLE email_notifications ADD COLUMN provider_id TEXT;
ALTER TABLE email_notifications ADD COLUMN idempotency_key TEXT;
ALTER TABLE email_notifications ADD COLUMN key_sent_at TEXT;
ALTER TABLE email_notifications ADD COLUMN uncertain INTEGER NOT NULL DEFAULT 0;
ALTER TABLE email_notifications ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0;
