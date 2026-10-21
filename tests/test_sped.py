import unittest

import resumo_sped
from portal import sped
from util import CONTRIBUTIONS_SAMPLE, FISCAL_SAMPLE


class CnpjTests(unittest.TestCase):
    def test_validates_check_digits(self):
        self.assertTrue(sped.valid_cnpj("11.222.333/0001-81"))
        self.assertFalse(sped.valid_cnpj("11.222.333/0001-82"))
        self.assertFalse(sped.valid_cnpj("00.000.000/0000-00"))


class SpedHeaderTests(unittest.TestCase):
    def header(self, cnpj="11222333000181", start="01092026", end="30092026"):
        return f"|0000|020|0|{start}|{end}|FARMACIA EXEMPLO LTDA|{cnpj}||RS|0000000000|4314902|00000||A|1|\r\n|0001|0|\r\n".encode()

    def test_extracts_cnpj_and_competence_from_register_0000(self):
        parsed = sped.parse_sped_header(self.header())
        self.assertEqual(parsed["cnpj"], "11222333000181")
        self.assertEqual(parsed["competence"], "2026-09")
        self.assertEqual(parsed["company"], "FARMACIA EXEMPLO LTDA")

    def test_rejects_invalid_cnpj(self):
        with self.assertRaisesRegex(ValueError, "CNPJ inválido"):
            sped.parse_sped_header(self.header(cnpj="11222333000182"))

    def test_accepts_period_shorter_than_a_month_with_its_competence(self):
        cases = {  # período do 0000 -> (competência, parcial?)
            ("01092026", "15092026"): ("2026-09", True),    # 1ª quinzena
            ("16092026", "30092026"): ("2026-09", True),    # 2ª quinzena
            ("15092026", "30092026"): ("2026-09", True),    # do dia 15 ao fim
            ("10022026", "10022026"): ("2026-02", True),    # um dia só
            ("01022026", "28022026"): ("2026-02", False),   # mês inteiro
        }
        for (start, end), (competence, partial) in cases.items():
            with self.subTest(period=f"{start}-{end}"):
                parsed = sped.parse_sped_header(self.header(start=start, end=end))
                self.assertEqual((parsed["competence"], parsed["partial"]), (competence, partial))
                self.assertEqual(parsed["period_start"], f"{start[4:]}-{start[2:4]}-{start[:2]}")
                self.assertEqual(parsed["period_end"], f"{end[4:]}-{end[2:4]}-{end[:2]}")

    def test_contributions_also_accept_partial_period(self):
        content = b"|0000|006|0|0||01092026|15092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|4314902||00|\r\n"
        parsed = sped.parse_sped_header(content, "contribuicoes")
        self.assertEqual((parsed["competence"], parsed["partial"], parsed["period_end"]), ("2026-09", True, "2026-09-15"))

    def test_rejects_end_before_start(self):
        with self.assertRaisesRegex(ValueError, "anterior à data inicial"):
            sped.parse_sped_header(self.header(start="16092026", end="15092026"))

    def test_period_label_only_for_partial_periods(self):
        self.assertEqual(sped.period_label("2026-09-01", "2026-09-15"), "01/09/2026 a 15/09/2026")
        self.assertEqual(sped.period_label("2026-09-01", "2026-09-30"), "")
        self.assertEqual(sped.period_label(None, None), "")

    def test_rejects_period_spanning_two_months(self):
        with self.assertRaisesRegex(ValueError, "único mês"):
            sped.parse_sped_header(self.header(start="01092026", end="01102026"))

    def test_extracts_contributions_header_using_its_own_field_layout(self):
        content = b"|0000|006|0|0||01092026|30092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|4314902||00|\r\n"
        parsed = sped.parse_sped_header(content, "contribuicoes")
        self.assertEqual(parsed["cnpj"], "11222333000181")
        self.assertEqual(parsed["competence"], "2026-09")
        self.assertEqual(parsed["company"], "FARMACIA EXEMPLO LTDA")


class SpedTypeDetectionTests(unittest.TestCase):
    """O portal precisa saber, só pelo arquivo, se ele é SPED Fiscal (ICMS/IPI) ou SPED Contribuições (PIS/COFINS)."""

    def test_detects_fiscal_icms_ipi(self):
        self.assertEqual(sped.detect_sped_type(FISCAL_SAMPLE.encode()), "fiscal")

    def test_detects_contributions_pis_cofins(self):
        self.assertEqual(sped.detect_sped_type(CONTRIBUTIONS_SAMPLE.encode()), "contribuicoes")

    def test_detects_file_saved_with_utf8_bom(self):
        self.assertEqual(sped.detect_sped_type(b"\xef\xbb\xbf" + CONTRIBUTIONS_SAMPLE.encode()), "contribuicoes")

    def test_rejects_other_sped_layouts_and_plain_text(self):
        ecd = b"|0000|LECD|01012026|31122026|FARMACIA EXEMPLO LTDA|11222333000181|RS||4314902|||0|0|0||0|0||N|N|0|0|\r\n"
        for content in (ecd, b"relatorio de vendas\r\nsem registro 0000\r\n", b""):
            with self.subTest(content=content[:20]):
                with self.assertRaisesRegex(ValueError, "não foi reconhecido"):
                    sped.detect_sped_type(content)


class SpedSummaryTests(unittest.TestCase):
    def test_fiscal_summary_skips_cancelled_and_x929_and_matches_assessment(self):
        summary = resumo_sped.summarize_fiscal(FISCAL_SAMPLE.splitlines())
        operations = summary["operacoes"]
        self.assertEqual(operations["saidas"]["valor"], 150)
        self.assertEqual(operations["saidas"]["documentos"], 2)
        self.assertAlmostEqual(operations["saidas"]["icms"], 8.5)
        self.assertEqual(operations["entradas"]["valor"], 80)
        self.assertEqual(operations["cfop_x929"], 30)
        self.assertEqual(operations["tributacao_saidas"], {"Substituição tributária": 100, "Tributadas": 50})
        self.assertEqual(operations["diario"]["2026-09-03"], [0, 80])
        self.assertEqual(operations["fornecedores"], [["DISTRIBUIDORA EXEMPLO SA", 80]])
        self.assertEqual(summary["apuracao"]["icms"]["recolher"], 5.1)
        self.assertEqual(summary["apuracao"]["icms_st_recolher"], 1.5)
        self.assertEqual(summary["apuracao"]["obrigacoes"], [[5.1, "2026-10-12"]])

    def test_contributions_summary_reads_revenue_and_taxes(self):
        summary = resumo_sped.summarize_contributions(CONTRIBUTIONS_SAMPLE.splitlines())
        self.assertEqual(summary["operacoes"]["estabelecimentos"], [["11222333000181", "LOJA MATRIZ", 200, 120]])
        self.assertEqual(summary["operacoes"]["cfops_saida"], {"5405": 190})
        self.assertEqual(summary["apuracao"]["receita_tributada"], 100)
        self.assertEqual(summary["apuracao"]["receita_sem_incidencia"], {"04": 300})
        self.assertEqual(summary["apuracao"]["pis"]["recolher"], 0.65)
        self.assertEqual(summary["apuracao"]["cofins"]["recolher"], 3)

    def test_assessments_of_several_periods_are_added_up(self):
        two_periods = FISCAL_SAMPLE.splitlines() + ["|E110|10|0|0|0|4|0|0|0|0|6|0|6|2|0|"]
        icms = resumo_sped.summarize_fiscal(two_periods)["apuracao"]["icms"]
        self.assertAlmostEqual(icms["debitos"], 18.5)
        self.assertAlmostEqual(icms["recolher"], 11.1)
        self.assertEqual(icms["saldo_credor_anterior"], 0)    # do primeiro período
        self.assertEqual(icms["saldo_credor"], 2)             # do último período
        contributions = resumo_sped.summarize_contributions(CONTRIBUTIONS_SAMPLE.splitlines() + ["|M200|0|0|0|0|0|0|0|1|0|0|1|1|"])
        self.assertAlmostEqual(contributions["apuracao"]["pis"]["recolher"], 1.65)

    def test_merge_keeps_main_file_when_complement_only_has_inventory(self):
        main = resumo_sped.summarize_fiscal(FISCAL_SAMPLE.splitlines())
        inventory = resumo_sped.summarize_fiscal(["|H005|30092026|1234,5|01|"])
        merged = resumo_sped.merge([main, inventory])
        self.assertEqual(merged["operacoes"]["saidas"]["valor"], 150)
        self.assertEqual(merged["inventario"], {"data": "2026-09-30", "valor": 1234.5})

    def test_render_escapes_names_and_outputs_charts(self):
        summary = resumo_sped.summarize_fiscal(FISCAL_SAMPLE.replace("DISTRIBUIDORA EXEMPLO SA", "<script>x</script>").splitlines())
        page = resumo_sped.render(summary, "2026-09", [("2026-08", {"saidas": 100, "entradas": 50, "imposto": 4}), ("2026-09", resumo_sped.headline(summary))])
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("<svg", page)
        self.assertIn("Evolução mês a mês", page)
        self.assertIn("cresceram", page)
        self.assertLessEqual(page.count("<li>"), resumo_sped.MAX_INSIGHTS)
        contributions = resumo_sped.render(resumo_sped.summarize_contributions(CONTRIBUTIONS_SAMPLE.splitlines()), "2026-09", [])
        self.assertIn("75,0%", contributions)


if __name__ == "__main__":
    unittest.main()
