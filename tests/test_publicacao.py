import unittest

from portal import auth, publicacao
from portal import db as storage
from portal.sped import PublishError
from util import CONTRIBUTIONS_SAMPLE, FISCAL_SAMPLE, STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ, sped, temporary_storage


class PublishRulesTests(unittest.TestCase):
    """Publicação automática: o portal acha a loja pelo CNPJ, a competência pelo período e a aba pelo tipo do arquivo."""

    def setUp(self):
        temporary_storage(self)
        auth.create_user(STORE_A_CNPJ, "Farmácia Centro", "store", "senha-centro-123")
        auth.create_user(STORE_B_CNPJ, "Farmácia Bairro", "store", "senha-bairro-123")
        with storage.connect() as db:
            ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM users")}
        self.store_a, self.store_b = ids["Farmácia Centro"], ids["Farmácia Bairro"]

    def documents(self):
        with storage.connect() as db:
            return [dict(row) for row in db.execute("SELECT store_id, document_type, competence, original_name FROM documents ORDER BY uploaded_at")]

    def stored_files(self):
        return sorted(path.name for path in storage.DOCUMENTS.iterdir())

    def test_fiscal_file_is_attached_to_store_of_its_cnpj_in_fiscal_tab(self):
        content = sped(FISCAL_SAMPLE, STORE_A_CNPJ)
        published = publicacao.publish_sped(content, "sped-fiscal-centro.txt")
        self.assertEqual((published["store_id"], published["document_type"], published["competence"]), (self.store_a, "fiscal", "2026-09"))
        self.assertEqual(self.documents(), [{"store_id": self.store_a, "document_type": "fiscal", "competence": "2026-09", "original_name": "sped-fiscal-centro.txt"}])
        self.assertEqual((storage.DOCUMENTS / f"{published['id']}.txt").read_bytes(), content)

    def test_contributions_file_is_attached_to_store_of_its_cnpj_in_contributions_tab(self):
        published = publicacao.publish_sped(sped(CONTRIBUTIONS_SAMPLE, STORE_B_CNPJ), "contribuicoes.txt")
        self.assertEqual((published["store_id"], published["document_type"], published["competence"]), (self.store_b, "contribuicoes", "2026-09"))
        self.assertEqual(published["store_name"], "Farmácia Bairro")

    def test_competence_comes_from_the_period_inside_the_file(self):
        published = publicacao.publish_sped(sped(FISCAL_SAMPLE, STORE_B_CNPJ, "01022026|28022026"), "fevereiro.txt")
        self.assertEqual(published["competence"], "2026-02")

    def test_fiscal_and_contributions_of_same_store_and_month_coexist(self):
        publicacao.publish_sped(sped(FISCAL_SAMPLE), "fiscal.txt")
        publicacao.publish_sped(sped(CONTRIBUTIONS_SAMPLE), "contribuicoes.txt")
        self.assertEqual([(doc["store_id"], doc["document_type"]) for doc in self.documents()], [(self.store_a, "fiscal"), (self.store_a, "contribuicoes")])

    def test_file_of_unregistered_cnpj_is_refused_and_nothing_is_saved(self):
        with self.assertRaises(PublishError) as error:
            publicacao.publish_sped(sped(FISCAL_SAMPLE, UNREGISTERED_CNPJ), "outra.txt")
        self.assertIn("45.543.915/0001-81", str(error.exception))
        self.assertIn("FARMACIA EXEMPLO LTDA", str(error.exception))
        self.assertIn("SPED Fiscal (ICMS e IPI)", str(error.exception))
        self.assertEqual((self.documents(), self.stored_files()), ([], []))

    def test_file_of_inactive_store_is_refused(self):
        with storage.connect() as db:
            db.execute("UPDATE users SET active=0 WHERE id=?", (self.store_a,))
        with self.assertRaisesRegex(PublishError, "nenhuma loja ativa"):
            publicacao.publish_sped(sped(FISCAL_SAMPLE), "sped.txt")

    def test_same_file_twice_is_refused_without_duplicating(self):
        publicacao.publish_sped(sped(FISCAL_SAMPLE), "sped.txt")
        with self.assertRaisesRegex(PublishError, "já está publicado no SPED Fiscal .* Farmácia Centro"):
            publicacao.publish_sped(sped(FISCAL_SAMPLE), "copia.txt")
        self.assertEqual((len(self.documents()), len(self.stored_files())), (1, 1))

    def test_invalid_files_are_refused_with_the_reason(self):
        cases = {
            "período em dois meses": (sped(FISCAL_SAMPLE, period="16092026|15102026"), "único mês"),
            "CNPJ inválido": (sped(FISCAL_SAMPLE, "11222333000182"), "CNPJ inválido"),
            "não é SPED": (b"qualquer texto\r\n", "não foi reconhecido"),
            "vazio": (b"", "Escolha um arquivo"),
        }
        for case, (content, reason) in cases.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(PublishError, reason):
                    publicacao.publish_sped(content, "sped.txt")
        self.assertEqual(self.stored_files(), [])


class PartialPeriodTests(unittest.TestCase):
    """SPED de parte do mês (ex.: Fiscal quinzenal): aceito, na competência do mês, e somado no resumo do mês."""

    def setUp(self):
        temporary_storage(self)
        auth.create_user(STORE_A_CNPJ, "Farmácia Centro", "store", "senha-centro-123")

    def documents(self):
        with storage.connect() as db:
            return db.execute("SELECT * FROM documents WHERE document_type='fiscal' ORDER BY uploaded_at").fetchall()

    def test_both_fortnights_are_published_in_the_same_competence(self):
        first = publicacao.publish_sped(sped(FISCAL_SAMPLE, period="01092026|15092026"), "ipi-1a-quinzena.txt")
        second = publicacao.publish_sped(sped(FISCAL_SAMPLE, period="16092026|30092026"), "ipi-2a-quinzena.txt")
        self.assertEqual((first["competence"], first["partial"], first["period_end"]), ("2026-09", True, "2026-09-15"))
        self.assertEqual((second["competence"], second["partial"], second["period_start"]), ("2026-09", True, "2026-09-16"))
        stored = [(doc["competence"], doc["period_start"], doc["period_end"]) for doc in self.documents()]
        self.assertEqual(stored, [("2026-09", "2026-09-01", "2026-09-15"), ("2026-09", "2026-09-16", "2026-09-30")])

    def test_full_month_file_keeps_its_period_and_is_not_partial(self):
        published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "mes.txt")
        self.assertFalse(published["partial"])
        self.assertFalse(publicacao.is_partial(self.documents()[0]))

    def test_month_summary_adds_up_the_fortnights(self):
        publicacao.publish_sped(sped(FISCAL_SAMPLE, period="01092026|15092026"), "1.txt")
        one = publicacao.competence_summary(self.documents(), "2026-09")
        publicacao.publish_sped(sped(FISCAL_SAMPLE, period="16092026|30092026"), "2.txt")
        both = publicacao.competence_summary(self.documents(), "2026-09")
        self.assertEqual(one["operacoes"]["saidas"]["valor"], 150)
        self.assertEqual(both["operacoes"]["saidas"]["valor"], 300)
        self.assertEqual(both["operacoes"]["saidas"]["documentos"], 4)
        self.assertAlmostEqual(both["apuracao"]["icms"]["recolher"], 10.2)
        self.assertAlmostEqual(both["apuracao"]["icms_st_recolher"], 3.0)
        self.assertEqual(len(both["apuracao"]["obrigacoes"]), 2)

    def test_corrected_fortnight_replaces_the_previous_one_instead_of_adding(self):
        publicacao.publish_sped(sped(FISCAL_SAMPLE, period="01092026|15092026"), "1.txt")
        publicacao.publish_sped(sped(FISCAL_SAMPLE, period="16092026|30092026"), "2.txt")
        corrected = sped(FISCAL_SAMPLE, period="01092026|15092026") + b"|C100|1|0||65|00|1|900||10092026|10092026|50|0|0|0|50|9|0|0|0|0|0|0|0|0|0|0|0|0|\r\n|C190|060|5405|0|50|0|0|0|0|0|0||\r\n"
        publicacao.publish_sped(corrected, "1-corrigido.txt")
        summary = publicacao.competence_summary(self.documents(), "2026-09")
        self.assertEqual(len(self.documents()), 3)
        self.assertEqual(summary["operacoes"]["saidas"]["valor"], 350)   # 200 da 1ª corrigida + 150 da 2ª

    def test_partial_contributions_are_accepted_too(self):
        published = publicacao.publish_sped(sped(CONTRIBUTIONS_SAMPLE, period="01092026|15092026"), "contrib-parcial.txt")
        self.assertEqual((published["document_type"], published["competence"], published["partial"]), ("contribuicoes", "2026-09", True))

    def test_period_crossing_two_months_is_still_refused(self):
        with self.assertRaisesRegex(PublishError, "único mês"):
            publicacao.publish_sped(sped(FISCAL_SAMPLE, period="16092026|15102026"), "x.txt")


if __name__ == "__main__":
    unittest.main()
