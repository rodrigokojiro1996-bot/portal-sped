-- Período do registro 0000 de cada arquivo. Permite SPED de parte do mês (ex.: Fiscal quinzenal);
-- a competência continua sendo o mês. Arquivos publicados antes desta versão ficam com o período vazio
-- e são tratados como do mês inteiro.
ALTER TABLE documents ADD COLUMN period_start TEXT;
ALTER TABLE documents ADD COLUMN period_end TEXT;
