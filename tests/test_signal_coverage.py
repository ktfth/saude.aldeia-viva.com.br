"""
Um zero que significa "não medido" não pode parecer um zero que significa
"não houve".

Medido no relatório real, por agravo, contando em quantos municípios cada
sinal é maior que zero:

    Zika          hospitalizacoes, sinais_alarme, casos_graves  ->  0 de 579
    Chikungunya   sinais_alarme, casos_graves                   ->  0 de 1.920
    Hanseníase    hospitalizacoes, sinais_alarme, casos_graves  ->  0 de 3.088
    Meningite     hospitalizacoes, sinais_alarme                ->  0 de 2.669

Verificado na origem: `ZIKABR26.csv` tem 38 colunas e **nenhuma** `ALRM_` ou
`GRAV_`; `DENGBR26.csv` tem 121 colunas com 9 e 15. E `CHIK`/`ZIKA` declaram
`warning_codes` e `severe_codes` vazios em `disease_sources.py`.

Consequência: `formula_risco` publica

    casos_provaveis + 4*sinais_alarme + 8*casos_graves + 20*obitos + 2*hospitalizacoes

para Dengue e para Zika igualmente, mas para a Zika três dos cinco termos são
estruturalmente zero. Os dois scores não estão na mesma escala, e a página de
metodologia apresenta a fórmula como a explicação do número.

Este módulo não conserta a fonte — ele declara o que a fonte não traz.
"""

import unittest

from aggregation.signal_coverage import (
    TRACKED_SIGNALS,
    describe_coverage,
    signals_without_data,
    with_signal_coverage,
)


def _report(*diseases_per_municipality):
    return {
        "municipios": [
            {"codigo_municipio": str(i), "doencas": list(doencas)}
            for i, doencas in enumerate(diseases_per_municipality)
        ],
        "metadata": {},
    }


def _disease(codigo, **counts):
    base = {
        "codigo": codigo,
        "nome": codigo,
        "casos_provaveis": 0,
        "sinais_alarme": 0,
        "casos_graves": 0,
        "hospitalizacoes": 0,
        "obitos": 0,
    }
    base.update(counts)
    return base


class TestSignalsWithoutData(unittest.TestCase):
    def test_a_signal_never_positive_is_reported(self) -> None:
        report = _report(
            [_disease("ZIKA", casos_provaveis=10)],
            [_disease("ZIKA", casos_provaveis=5)],
        )
        self.assertEqual(
            signals_without_data(report)["ZIKA"],
            ["casos_graves", "hospitalizacoes", "obitos", "sinais_alarme"],
        )

    def test_a_signal_positive_anywhere_counts_as_measured(self) -> None:
        report = _report(
            [_disease("DENG", sinais_alarme=0)],
            [_disease("DENG", sinais_alarme=3)],
        )
        self.assertNotIn("sinais_alarme", signals_without_data(report)["DENG"])

    def test_each_disease_is_evaluated_on_its_own(self) -> None:
        report = _report(
            [_disease("DENG", sinais_alarme=3), _disease("ZIKA", casos_provaveis=2)],
        )
        without = signals_without_data(report)
        self.assertNotIn("sinais_alarme", without["DENG"])
        self.assertIn("sinais_alarme", without["ZIKA"])

    def test_empty_report(self) -> None:
        self.assertEqual(signals_without_data({}), {})

    def test_tracked_signals_are_the_score_terms(self) -> None:
        self.assertEqual(
            set(TRACKED_SIGNALS),
            {"sinais_alarme", "casos_graves", "hospitalizacoes", "obitos"},
        )


class TestWithSignalCoverage(unittest.TestCase):
    def test_attaches_the_list_to_every_disease(self) -> None:
        report = _report([_disease("ZIKA", casos_provaveis=10)])
        got = with_signal_coverage(report)
        disease = got["municipios"][0]["doencas"][0]
        self.assertIn("sinais_alarme", disease["sinais_sem_dados"])

    def test_a_disease_with_full_coverage_gets_an_empty_list(self) -> None:
        report = _report(
            [
                _disease(
                    "DENG",
                    casos_provaveis=10,
                    sinais_alarme=1,
                    casos_graves=1,
                    hospitalizacoes=1,
                    obitos=1,
                )
            ]
        )
        got = with_signal_coverage(report)
        self.assertEqual(got["municipios"][0]["doencas"][0]["sinais_sem_dados"], [])

    def test_does_not_mutate_the_input(self) -> None:
        report = _report([_disease("ZIKA")])
        with_signal_coverage(report)
        self.assertNotIn("sinais_sem_dados", report["municipios"][0]["doencas"][0])

    def test_metadata_summarizes_the_gaps(self) -> None:
        report = _report(
            [
                _disease("DENG", sinais_alarme=1, casos_graves=1, hospitalizacoes=1, obitos=1),
                _disease("ZIKA", casos_provaveis=3),
            ]
        )
        cobertura = with_signal_coverage(report)["metadata"]["cobertura_de_sinais"]
        self.assertEqual(cobertura["agravos_com_lacuna"], ["ZIKA"])
        self.assertIn("sinais_alarme", cobertura["por_agravo"]["ZIKA"])
        self.assertIn("aviso", cobertura)

    def test_the_warning_explains_the_comparability_problem(self) -> None:
        report = _report([_disease("ZIKA", casos_provaveis=3)])
        aviso = with_signal_coverage(report)["metadata"]["cobertura_de_sinais"]["aviso"]
        self.assertIn("compará", aviso.lower())


class TestDescribeCoverage(unittest.TestCase):
    def test_names_the_signals_in_portuguese(self) -> None:
        texto = describe_coverage(["sinais_alarme", "casos_graves"])
        self.assertIn("sinais de alarme", texto)
        self.assertIn("casos graves", texto)

    def test_empty_gap_has_no_text(self) -> None:
        self.assertEqual(describe_coverage([]), "")


if __name__ == "__main__":
    unittest.main()
