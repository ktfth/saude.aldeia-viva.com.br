"""
Ate que ano cada agravo pode ser atual.

`DiseaseSource.latest_year` e um numero cravado no codigo sobre um fato do
mundo exterior. Ele nao pode ser conferido por teste unitario -- exige rede --
e por isso apodrece calado. `scripts/auditar_fontes.py` faz a conferencia
contra o servidor; este arquivo protege as invariantes que valem sem rede.

O que a auditoria mediu em 2026-09-06, e que muda o entendimento do produto:

    DBC "FINAIS" (publicacao com anos de atraso, e a defasagem E DA FONTE)
      MENI  ate 2022      ANIM  ate 2022
      TOXC  ate 2023      TOXG  ate 2023      HANS  ate 2023
      LEPT  ate 2024      BOTU  ate 2024

    CSV preliminares (arquivo continuo, cresce durante o ano corrente)
      DENG 2026 -> 13,7 MB      CHIK 2026 -> 3,2 MB      ZIKA 2026 -> 0,2 MB

Seis dos sete tetos conferiam. O BOTU estava em 2023 com 2024 publicado: o
carregador comecava ja defasado e nunca alcancava o arquivo novo.

A conclusao que importa: nenhum pipeline torna Meningite atual, porque o
DATASUS nao publicou. O que da para fazer e declarar a idade -- que e o que o
modelo de recencia faz -- e manter atuais os tres agravos cuja fonte permite.
"""

import unittest
from pathlib import Path

from domain.disease_sources import DISEASE_SOURCES

RAIZ = Path(__file__).resolve().parent.parent

# Agravos cuja fonte e continua e por isso pode estar no ano corrente.
FONTES_CONTINUAS = {"DENG", "CHIK", "ZIKA"}


class TestOsTetosDeAnoSaoCoerentes(unittest.TestCase):
    def test_toda_fonte_dbc_declara_um_teto(self) -> None:
        """Sem teto, o carregador tenta o ano corrente e desce ano a ano.

        Cada tentativa falha com uma requisicao de rede. Um agravo publicado
        so ate 2022 gastaria quatro idas ao servidor antes de acertar, a cada
        recarga.
        """
        sem_teto = [
            f.codigo
            for f in DISEASE_SOURCES.values()
            if f.dbc_prefix and f.latest_year is None
        ]
        self.assertEqual(sem_teto, [], f"fonte DBC sem teto declarado: {sem_teto}")

    def test_fonte_continua_nao_declara_teto(self) -> None:
        """Um teto aqui congelaria no passado um arquivo que cresce sozinho."""
        com_teto = [
            f.codigo
            for f in DISEASE_SOURCES.values()
            if f.codigo in FONTES_CONTINUAS and f.latest_year is not None
        ]
        self.assertEqual(
            com_teto,
            [],
            f"fonte continua com teto congelaria o ano corrente: {com_teto}",
        )

    def test_as_fontes_continuas_existem_no_catalogo(self) -> None:
        """Se um desses codigos sumir, a invariante acima vira decoracao."""
        for codigo in FONTES_CONTINUAS:
            self.assertIn(codigo, DISEASE_SOURCES)

    def test_nenhum_teto_esta_no_futuro(self) -> None:
        """Teto acima do publicado gasta requisicoes ate encontrar o real."""
        from datetime import date

        limite = date.today().year
        adiantados = {
            f.codigo: f.latest_year
            for f in DISEASE_SOURCES.values()
            if f.latest_year and f.latest_year > limite
        }
        self.assertEqual(adiantados, {})

    def test_o_teto_do_botu_alcanca_o_que_o_servidor_publica(self) -> None:
        """O defeito concreto, para nao ser desfeito em silencio."""
        self.assertGreaterEqual(DISEASE_SOURCES["BOTU"].latest_year, 2024)

    def test_a_auditoria_existe_e_e_referenciada(self) -> None:
        """O teto so e manutenivel se houver como reconferi-lo."""
        script = RAIZ / "scripts" / "auditar_fontes.py"
        self.assertTrue(script.exists())
        fonte = (RAIZ / "domain" / "disease_sources.py").read_text(encoding="utf-8")
        self.assertIn("auditar_fontes.py", fonte)


if __name__ == "__main__":
    unittest.main()
