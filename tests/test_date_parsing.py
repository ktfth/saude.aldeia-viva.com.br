"""
Datas: falhar fechado em vez de propagar lixo.

Defeito LATENTE — não se manifesta no dado de hoje (medido: 0 de 20.047 pares
município-agravo têm data fora do formato ISO), mas está na fundação da
dimensão de recência, que sustenta a leitura temporal inteira do produto.

`parse_date_value` tentava dois formatos e, falhando, **devolvia a string
crua**:

    parse_date_value("2026-04-21 10:33:00") -> "2026-04-21 10:33:00"
    parse_date_value("31/02/2026")          -> "31/02/2026"

E `update_latest_date` comparava com `>` entre strings, o que é ordem
lexicográfica e não temporal:

    "21/04/2020" > "2026-04-21"  ->  True   (uma data de 2020 vence uma de 2026)
    "9999-99-99" vence qualquer data real

Basta um arquivo do SINAN com data-hora, ou com o sentinela `9999-99-99`, para
`ultima_notificacao` sair corrompida de forma plausível — e a recência inteira
daquele município passa a mentir sem que nada falhe.

É o mesmo padrão que esta série vem eliminando: degradar em silêncio.
"""

import unittest

from aggregation.utils import parse_date_value, update_latest_date


class TestParseDateValue(unittest.TestCase):
    def test_iso(self) -> None:
        self.assertEqual(parse_date_value("2026-04-21"), "2026-04-21")

    def test_brazilian(self) -> None:
        self.assertEqual(parse_date_value("21/04/2026"), "2026-04-21")

    def test_iso_with_time(self) -> None:
        """O SINAN entrega data-hora em alguns arquivos."""
        self.assertEqual(parse_date_value("2026-04-21 10:33:00"), "2026-04-21")
        self.assertEqual(parse_date_value("2026-04-21T10:33:00"), "2026-04-21")

    def test_brazilian_with_time(self) -> None:
        self.assertEqual(parse_date_value("21/04/2026 10:33"), "2026-04-21")

    def test_compact(self) -> None:
        self.assertEqual(parse_date_value("20260421"), "2026-04-21")

    def test_empty(self) -> None:
        self.assertEqual(parse_date_value(""), "")
        self.assertEqual(parse_date_value(None), "")

    def test_impossible_date_fails_closed(self) -> None:
        """31 de fevereiro não existe. Antes voltava como string crua."""
        self.assertEqual(parse_date_value("31/02/2026"), "")

    def test_sentinel_fails_closed(self) -> None:
        """`9999-99-99` vencia toda comparação e virava a última notificação."""
        self.assertEqual(parse_date_value("9999-99-99"), "")
        self.assertEqual(parse_date_value("0000-00-00"), "")

    def test_garbage_fails_closed(self) -> None:
        for lixo in ("abril de 2026", "sem data", "--", "N/A"):
            with self.subTest(valor=lixo):
                self.assertEqual(parse_date_value(lixo), "")

    def test_never_returns_a_non_iso_string(self) -> None:
        """A garantia que faltava: ou sai ISO, ou sai vazio."""
        import re

        entradas = [
            "2026-04-21", "21/04/2026", "2026-04-21 10:33:00", "20260421",
            "31/02/2026", "9999-99-99", "abril", "", "N/A", "2026/04/21",
        ]
        for valor in entradas:
            with self.subTest(valor=valor):
                saida = parse_date_value(valor)
                self.assertTrue(
                    saida == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", saida),
                    f"{valor!r} produziu {saida!r}",
                )


class TestUpdateLatestDate(unittest.TestCase):
    def test_keeps_the_newer_date(self) -> None:
        summary = {}
        update_latest_date(summary, "d", "2026-04-21")
        update_latest_date(summary, "d", "2026-06-01")
        self.assertEqual(summary["d"], "2026-06-01")

    def test_ignores_the_older_date(self) -> None:
        summary = {}
        update_latest_date(summary, "d", "2026-06-01")
        update_latest_date(summary, "d", "2026-04-21")
        self.assertEqual(summary["d"], "2026-06-01")

    def test_a_brazilian_older_date_does_not_win_lexicographically(self) -> None:
        """O caso que corrompia: "21/04/2020" > "2026-04-21" como texto."""
        summary = {}
        update_latest_date(summary, "d", "2026-04-21")
        update_latest_date(summary, "d", "21/04/2020")
        self.assertEqual(summary["d"], "2026-04-21")

    def test_a_sentinel_never_wins(self) -> None:
        summary = {}
        update_latest_date(summary, "d", "2026-04-21")
        update_latest_date(summary, "d", "9999-99-99")
        self.assertEqual(summary["d"], "2026-04-21")

    def test_garbage_never_becomes_the_stored_value(self) -> None:
        summary = {}
        update_latest_date(summary, "d", "abril de 2026")
        self.assertNotIn("d", summary)

    def test_garbage_does_not_overwrite_a_good_value(self) -> None:
        summary = {"d": "2026-04-21"}
        update_latest_date(summary, "d", "N/A")
        self.assertEqual(summary["d"], "2026-04-21")

    def test_empty_candidate_is_ignored(self) -> None:
        summary = {"d": "2026-04-21"}
        update_latest_date(summary, "d", "")
        self.assertEqual(summary["d"], "2026-04-21")

    def test_a_datetime_candidate_is_normalized_before_comparing(self) -> None:
        summary = {}
        update_latest_date(summary, "d", "2026-04-21")
        update_latest_date(summary, "d", "2026-06-01 09:00:00")
        self.assertEqual(summary["d"], "2026-06-01")

    def test_the_stored_value_is_always_iso(self) -> None:

        summary = {}
        for candidato in ("21/04/2026", "2026-06-01 09:00", "20260715"):
            update_latest_date(summary, "d", candidato)
        self.assertRegex(summary["d"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(summary["d"], "2026-07-15")


if __name__ == "__main__":
    unittest.main()
