-- Esquema inicial (versão 1.1): administração e lojas, sessões, documentos SPED e resumos.
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    cnpj TEXT UNIQUE,
    login_name TEXT UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'store')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES users(id),
    document_type TEXT NOT NULL CHECK(document_type IN ('fiscal', 'contribuicoes')),
    competence TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE(store_id, document_type, sha256)
);

CREATE TABLE login_attempts (
    ip TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    window_start INTEGER NOT NULL
);

CREATE TABLE document_summaries (
    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
