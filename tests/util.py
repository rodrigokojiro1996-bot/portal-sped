"""Ajudantes comuns dos testes: armazenamento temporário, servidor de teste e amostras de SPED."""
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import app

STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ = "11222333000181", "11444777000161", "45543915000181"


def restore_storage():
    """Volta os caminhos do portal para data/ sem abrir nem alterar o banco real."""
    app.DATA_DIR = app.ROOT / "data"
    app.DATABASE = app.DATA_DIR / "portal.sqlite3"
    app.DOCUMENTS = app.DATA_DIR / "documentos"
    app.APP_SECRET = None


class TestServer:
    """Portal rodando numa porta livre, para testes que fazem requisições HTTP."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


FISCAL_SAMPLE = "\r\n".join((
    "|0000|020|0|01092026|30092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|0000000000|4314902|00000||A|1|",
    "|0150|11444777000161|DISTRIBUIDORA EXEMPLO SA|1058|11444777000161||||||||",
    "|C100|1|0||65|00|1|101||02092026|02092026|150|0|0|0|150|9|0|0|0|100|17|0|0|0|0|0|0|0|",
    "|C190|060|5405|0|100|0|0|0|0|0|0||",
    "|C190|000|5102|17|50|50|8,5|0|0|0|0||",
    "|C100|1|0||65|02|1|102||02092026|02092026|999|0|0|0|999|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C190|000|5102|17|999|999|169,83|0|0|0|0||",
    "|C100|0|1|11444777000161|55|00|1|555||01092026|03092026|80|0|0|0|80|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C190|060|1403|0|60|0|0|0|0|0|0||",
    "|C190|000|1102|17|20|20|3,4|0|0|0|0||",
    "|C100|1|0||55|00|1|103||05092026|05092026|30|0|0|0|30|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C190|000|5929|17|30|30|5,1|0|0|0|0||",
    "|E110|8,5|0|0|0|3,4|0|0|0|0|5,1|0|5,1|0|0|",
    "|E116|000|5,1|12102026|0221|||||092026|",
    "|E210|1|0|0|0|0|0|0|0|0|0|0|1,5|0|0|",
))

CONTRIBUTIONS_SAMPLE = "\r\n".join((
    "|0000|006|0|0||01092026|30092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|4314902||00|",
    "|0140|0001|LOJA MATRIZ|11222333000181|RS||4314902|||",
    "|C010|11222333000181|2|",
    "|C100|1|0||65|00|1|1||02092026|02092026|200|0|0|0|200|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C175|5405|200|10|04|0|0|||0|04|0|0|||0||",
    "|C100|0|1|11444777000161|55|00|1|2||01092026|01092026|120|0|0|0|120|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|M200|0|0|0|0|0|0|0|0,65|0|0|0,65|0,65|",
    "|M210|51|100|100|0|0|100|0,65|0||0,65|0|0|0|0|0,65|",
    "|M400|04|300||",
    "|M600|0|0|0|0|0|0|0|3|0|0|3|3|",
))


def sped(sample, cnpj=STORE_A_CNPJ, period="01092026|30092026"):
    """Amostra SPED com outro CNPJ e período no registro 0000 (as demais linhas ficam como estão)."""
    first, rest = sample.split("\r\n", 1)
    fields = first.split("|")
    start, end = period.split("|")
    if fields[2] == "006":  # Contribuições
        fields[6], fields[7], fields[9] = start, end, cnpj
    else:
        fields[4], fields[5], fields[7] = start, end, cnpj
    return ("|".join(fields) + "\r\n" + rest).encode()


def temporary_storage(test):
    """Banco e documentos numa pasta temporária, desfeitos no fim do teste."""
    import tempfile
    temporary = tempfile.TemporaryDirectory()
    app.configure_storage(Path(temporary.name))
    test.addCleanup(temporary.cleanup)
    test.addCleanup(restore_storage)
    return Path(temporary.name)
