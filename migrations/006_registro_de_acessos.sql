-- Registro de quem usa a entrada por certificado: entradas, recusas e downloads.
-- Os nomes ficam copiados no registro para o histórico continuar legível mesmo se a loja mudar depois.
-- O certificado é identificado pelo CNPJ e pelo número de série. O CPF do responsável, que também vem no
-- e-CNPJ, não é guardado de propósito.
-- session_hash liga cada download à entrada que abriu a sessão.
CREATE TABLE certificate_access (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('entrou', 'recusado', 'baixou')),
    certificate_cnpj TEXT,
    certificate_serial TEXT,
    store_id INTEGER,
    store_name TEXT,
    store_cnpj TEXT,
    document_type TEXT,
    competence TEXT,
    file_name TEXT,
    reason TEXT,
    session_hash TEXT,
    ip TEXT,
    browser TEXT
);
CREATE INDEX certificate_access_created ON certificate_access(created_at);
CREATE INDEX certificate_access_store ON certificate_access(store_id, created_at);
CREATE INDEX certificate_access_session ON certificate_access(session_hash);
