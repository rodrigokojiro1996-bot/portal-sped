-- A tela central lista as últimas publicações por data de envio; o índice evita ler a tabela inteira
-- quando o número de arquivos crescer.
CREATE INDEX documents_uploaded_at ON documents(uploaded_at);
