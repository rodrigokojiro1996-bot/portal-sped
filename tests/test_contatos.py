import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from portal import auth, cli, contatos
from portal import db as storage
from util import STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ, temporary_storage

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEADER = ["Fantasia", "CNPJ", "SPED SPA", "CONTATO"]


def write_xlsx(path, rows):
    """Planilha mínima: a primeira coluna vai como texto embutido e as demais pela tabela de textos, como o Excel faz."""
    shared, cells = [], []
    for number, row in enumerate(rows, start=1):
        parts = []
        for index, value in enumerate(row):
            reference = f"{chr(65 + index)}{number}"
            if not value:
                continue
            if index == 0:
                parts.append(f"<c r='{reference}' t='inlineStr'><is><t>{escape(value)}</t></is></c>")
            else:
                shared.append(value)
                parts.append(f"<c r='{reference}' t='s'><v>{len(shared) - 1}</v></c>")
        cells.append(f"<row r='{number}'>{''.join(parts)}</row>")
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("xl/workbook.xml", f"<workbook xmlns='{MAIN}' xmlns:r='{RELS}'><sheets><sheet name='Planilha1' sheetId='1' r:id='rId1'/></sheets></workbook>")
        book.writestr("xl/_rels/workbook.xml.rels", "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Target='worksheets/sheet1.xml'/></Relationships>")
        book.writestr("xl/sharedStrings.xml", f"<sst xmlns='{MAIN}'>" + "".join(f"<si><t>{escape(value)}</t></si>" for value in shared) + "</sst>")
        book.writestr("xl/worksheets/sheet1.xml", f"<worksheet xmlns='{MAIN}'><sheetData>{''.join(cells)}</sheetData></worksheet>")


class SplitContactTests(unittest.TestCase):
    """O campo CONTATO é texto livre: os formatos abaixo são os que aparecem na planilha real, com endereços fictícios."""

    def test_two_addresses_are_both_recipients(self):
        self.assertEqual(contatos.split_contact("loja@example.com / contador@example.com"), (["loja@example.com", "contador@example.com"], []))
        self.assertEqual(contatos.split_contact("Manda para loja@example.com / contador@example.com"), (["loja@example.com", "contador@example.com"], []))

    def test_copy_mark_separates_recipient_from_copy(self):
        self.assertEqual(contatos.split_contact("manda para a@example.com com copia para b@example.com"), (["a@example.com"], ["b@example.com"]))
        self.assertEqual(contatos.split_contact("Envia no a@example.com com cópia para b@example.com"), (["a@example.com"], ["b@example.com"]))

    def test_repeated_address_counts_once(self):
        self.assertEqual(contatos.split_contact("cadastro@example.com <cadastro@example.com>"), (["cadastro@example.com"], []))
        self.assertEqual(contatos.split_contact("a@example.com com cópia para a@example.com"), (["a@example.com"], []))

    def test_words_around_the_address_are_ignored_and_case_is_normalized(self):
        self.assertEqual(contatos.split_contact("pode enviar para: Contato_1@Example.COM"), (["contato_1@example.com"], []))

    def test_text_without_address_gives_nothing(self):
        self.assertEqual(contatos.split_contact("ligar para a loja"), ([], []))
        self.assertEqual(contatos.split_contact(""), ([], []))


class ReadXlsxTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "lojas.xlsx"

    def test_reads_rows_by_header_name_and_skips_empty_rows(self):
        write_xlsx(self.path, [HEADER, ["Loja A & Filha", "11.222.333/0001-81", "SIM", "a@example.com"], ["", "", "", ""], ["Loja B", "11.444.777/0001-61", "", ""]])
        rows = contatos.read_xlsx(self.path)
        self.assertEqual(rows, [{"Fantasia": "Loja A & Filha", "CNPJ": "11.222.333/0001-81", "SPED SPA": "SIM", "CONTATO": "a@example.com"},
                                {"Fantasia": "Loja B", "CNPJ": "11.444.777/0001-61", "SPED SPA": "", "CONTATO": ""}])

    def test_missing_column_is_explained(self):
        write_xlsx(self.path, [["Fantasia", "CNPJ"], ["Loja A", "11.222.333/0001-81"]])
        with self.assertRaisesRegex(contatos.ContactFileError, "Faltam colunas na planilha: SPED SPA, CONTATO"):
            contatos.read_xlsx(self.path)

    def test_file_that_is_not_a_spreadsheet_is_refused(self):
        self.path.write_text("não é uma planilha", encoding="utf-8")
        with self.assertRaisesRegex(contatos.ContactFileError, "não é uma planilha .xlsx válida"):
            contatos.read_xlsx(self.path)


def row(name, cnpj, marked="SIM", contact="", note=""):
    return {"Fantasia": name, "CNPJ": cnpj, "SPED SPA": marked, "CONTATO": contact, "Observação": note}


class SelectContactsTests(unittest.TestCase):
    def test_only_marked_stores_with_email_enter(self):
        result = contatos.select_contacts([row("Loja A", "11.222.333/0001-81", contact="a@example.com"), row("Loja B", "11.444.777/0001-61", marked="", contact="")])
        self.assertEqual(result.contacts, {STORE_A_CNPJ: (["a@example.com"], [])})
        self.assertEqual(result.rows, 2)

    def test_closed_store_never_receives_even_with_email(self):
        result = contatos.select_contacts([row("Loja A", "11.222.333/0001-81", contact="a@example.com", note="Loja Fechada - Sem Funcionamento")])
        self.assertEqual(result.contacts, {})
        self.assertEqual(result.closed, ["Loja A"])

    def test_marked_store_without_email_and_invalid_cnpj_are_reported(self):
        result = contatos.select_contacts([row("Loja A", "11.222.333/0001-81", contact="ligar na loja"), row("Loja B", "11.111.111/1111-11", contact="b@example.com")])
        self.assertEqual(result.contacts, {})
        self.assertEqual(result.without_email, ["Loja A"])
        self.assertEqual(result.invalid_cnpj, ["Loja B"])

    def test_repeated_cnpj_keeps_the_row_with_contact_and_is_reported(self):
        result = contatos.select_contacts([row("Loja A", "11.222.333/0001-81", marked="", contact=""), row("Loja A", "11.222.333/0001-81", contact="a@example.com")])
        self.assertEqual(result.contacts, {STORE_A_CNPJ: (["a@example.com"], [])})
        self.assertEqual(result.repeated, [STORE_A_CNPJ])

    def test_same_cnpj_with_different_contacts_keeps_the_first_and_is_reported(self):
        result = contatos.select_contacts([row("Loja A", "11.222.333/0001-81", contact="a@example.com"), row("Loja A2", "11.222.333/0001-81", contact="b@example.com")])
        self.assertEqual(result.contacts, {STORE_A_CNPJ: (["a@example.com"], [])})
        self.assertEqual(result.conflicts, ["Loja A2"])


class ImportCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = temporary_storage(self)
        self.path = self.folder / "lojas.xlsx"
        write_xlsx(self.path, [HEADER, ["Loja A", "11.222.333/0001-81", "SIM", "manda para a@example.com com copia para b@example.com"],
                               ["Loja Sem Cadastro", "45.543.915/0001-81", "SIM", "c@example.com"]])
        auth.create_user(STORE_A_CNPJ, "Loja A", "store", "senha-loja-a-123")

    def run_command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.import_contacts(list(args))
        return output.getvalue()

    def saved(self):
        with storage.connect() as db:
            return {item["cnpj"]: (item["recipients"], item["copies"]) for item in db.execute("SELECT * FROM store_contacts")}

    def test_without_apply_flag_shows_the_result_and_writes_nothing(self):
        output = self.run_command(str(self.path))
        self.assertIn("Prontas para importar: 2 lojas", output)
        self.assertIn("Nada foi gravado", output)
        self.assertEqual(self.saved(), {})

    def test_apply_flag_writes_and_warns_about_unregistered_store(self):
        output = self.run_command(str(self.path), "--aplicar")
        self.assertIn("Contatos gravados: 2", output)
        self.assertIn("Sem loja ativa no portal: 1", output)
        self.assertEqual(self.saved(), {STORE_A_CNPJ: ("a@example.com", "b@example.com"), UNREGISTERED_CNPJ: ("c@example.com", "")})

    def test_importing_again_updates_without_removing_other_contacts(self):
        contatos.save_contacts({STORE_B_CNPJ: (["fora-da-planilha@example.com"], [])})
        self.run_command(str(self.path), "--aplicar")
        write_xlsx(self.path, [HEADER, ["Loja A", "11.222.333/0001-81", "SIM", "novo@example.com"]])
        self.run_command(str(self.path), "--aplicar")
        saved = self.saved()
        self.assertEqual(saved[STORE_A_CNPJ], ("novo@example.com", ""))
        self.assertEqual(saved[STORE_B_CNPJ], ("fora-da-planilha@example.com", ""))

    def test_missing_file_stops_with_clear_message(self):
        with self.assertRaisesRegex(SystemExit, "Importação não concluída"):
            self.run_command(str(self.folder / "nao-existe.xlsx"))

    def test_command_needs_exactly_one_file(self):
        with self.assertRaisesRegex(SystemExit, "Uso: python app.py importar-contatos"):
            self.run_command("--aplicar")


if __name__ == "__main__":
    unittest.main()
