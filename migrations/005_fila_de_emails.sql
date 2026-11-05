-- Quem recebe o aviso de publicação de cada loja e a fila de saída desses avisos.
-- O contato é guardado pelo CNPJ, e não pelo usuário: a lista pode ser importada antes de a loja ter acesso ao portal.
-- Endereços ficam separados por vírgula e espaço.
CREATE TABLE store_contacts (
    cnpj TEXT PRIMARY KEY,
    recipients TEXT NOT NULL,
    copies TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Uma linha por arquivo publicado. Ela nasce na mesma transação do documento: se o processo cair antes do
-- processamento, a linha continua pendente e é retomada depois.
-- 'ensaio' é final: um aviso processado em modo de teste nunca vira e-mail de verdade.
CREATE TABLE email_notifications (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pendente' CHECK(status IN ('pendente', 'ensaio', 'enviado', 'erro')),
    recipients TEXT,
    copies TEXT,
    subject TEXT,
    body TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX email_notifications_status ON email_notifications(status, created_at);
