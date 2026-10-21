-- Perfil de suporte e monitoramento das ações do suporte.
-- O SQLite não altera um CHECK no lugar: a tabela de usuários é recriada com o perfil 'support'.
-- O executor das migrações desliga as chaves estrangeiras durante a troca e confere as referências no fim.
CREATE TABLE users_migration (
    id INTEGER PRIMARY KEY,
    cnpj TEXT UNIQUE,
    login_name TEXT UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'support', 'store')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
INSERT INTO users_migration (id, cnpj, login_name, name, role, password_hash, active, created_at)
    SELECT id, cnpj, login_name, name, role, password_hash, active, created_at FROM users;
DROP TABLE users;
ALTER TABLE users_migration RENAME TO users;

-- Monitoramento: cada ação dos perfis auditados. Nomes ficam copiados no registro
-- para o histórico continuar legível mesmo se uma loja ou usuário mudar depois.
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    user_name TEXT NOT NULL,
    login_name TEXT,
    action TEXT NOT NULL,
    store_id INTEGER,
    store_name TEXT,
    store_cnpj TEXT,
    document_type TEXT,
    competence TEXT,
    file_name TEXT,
    detail TEXT,
    ip TEXT,
    machine TEXT,
    browser TEXT
);
CREATE INDEX audit_log_created ON audit_log(created_at);
