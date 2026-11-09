-- Nome reverso do IP de cada acesso por certificado (DNS reverso, confirmado de volta para o mesmo IP).
-- Pela internet costuma ser o nome da operadora; só em rede interna é o nome do computador.
-- Registros anteriores ficam com o campo vazio.
ALTER TABLE certificate_access ADD COLUMN hostname TEXT;
