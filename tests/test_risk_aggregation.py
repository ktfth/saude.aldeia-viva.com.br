"""
Nível de risco municipal: correção do erro de categoria.

`finalize_municipality_rows` chamava `risk_level()` SEM perfil, aplicando o
perfil de arbovirose à soma de até 10 agravos ao longo de 5 anos-fonte. Com
`death_is_critical=True` e `critical_threshold=100` — contra uma mediana de
score 27 e um máximo de 31.948 — o resultado é que 24,3% dos 5.339 municípios
saíam como "crítico". A principal variável de saída do produto quase não
discriminava.

Um município não tem perfil de risco. Cada agravo tem. O nível municipal é,
portanto, o pior nível entre os agravos daquele município — cada um já
calculado com o seu próprio perfil em `finalize_disease_summary`.

Medido: a correção sozinha leva 24,3% para 23,5%. Ela é necessária por
correção, não por efeito — quem move a agulha é o recorte por fonte atual,
que vive em `recency_enrichment` e é uma dimensão deliberadamente separada.
"""

import unittest

from domain.risk import RISK_LEVEL_ORDER, worst_level


class TestWorstLevel(unittest.TestCase):
    def test_picks_the_most_severe(self) -> None:
        self.assertEqual(worst_level(["baixo", "critico", "moderado"]), "critico")

    def test_respects_the_full_ordering(self) -> None:
        self.assertEqual(worst_level(["baixo", "moderado"]), "moderado")
        self.assertEqual(worst_level(["moderado", "alto"]), "alto")
        self.assertEqual(worst_level(["alto", "critico"]), "critico")

    def test_empty_is_baixo(self) -> None:
        self.assertEqual(worst_level([]), "baixo")

    def test_ignores_unknown_levels(self) -> None:
        self.assertEqual(worst_level(["inexistente", "moderado"]), "moderado")

    def test_only_unknown_levels_is_baixo(self) -> None:
        self.assertEqual(worst_level(["inexistente"]), "baixo")

    def test_order_constant_goes_from_worst_to_best(self) -> None:
        self.assertEqual(RISK_LEVEL_ORDER[0], "critico")
        self.assertEqual(RISK_LEVEL_ORDER[-1], "baixo")


class TestMunicipalLevelUsesDiseaseLevels(unittest.TestCase):
    """O nível municipal não pode nascer de um perfil aplicado à soma."""

    def _rows(self, diseases):
        from aggregation.report_builder import finalize_municipality_rows

        return finalize_municipality_rows(
            [
                {
                    "codigo_municipio": "355030",
                    "municipio": "São Paulo",
                    "estado": "SP",
                    "doencas_por_codigo": {
                        d["codigo"]: d for d in diseases
                    },
                }
            ]
        )

    def _disease(self, codigo, perfil, **counts):
        base = {
            "codigo": codigo,
            "nome": codigo,
            "perfil_risco": perfil,
            "casos_provaveis": 0,
            "casos_descartados": 0,
            "sinais_alarme": 0,
            "casos_graves": 0,
            "hospitalizacoes": 0,
            "obitos": 0,
            "classificacoes": {},
        }
        base.update(counts)
        return base

    def test_municipal_level_is_the_worst_disease_level(self) -> None:
        row = self._rows(
            [
                self._disease("DENG", "arbovirus", casos_provaveis=3),
                self._disease("MENI", "meningitis", obitos=1),
            ]
        )[0]
        self.assertEqual(row["nivel_risco"], "critico")

    def test_a_small_municipality_is_not_critical_by_accumulation(self) -> None:
        """Casos espalhados por vários agravos leves não fabricam 'crítico'.

        Com o perfil de arbovirose aplicado à soma, 60 casos prováveis
        distribuídos em três agravos passavam de `critical_threshold=100`
        via score e o município saía como crítico sem nenhum óbito ou caso
        grave em lugar nenhum.
        """
        row = self._rows(
            [
                self._disease("DENG", "arbovirus", casos_provaveis=20),
                self._disease("CHIK", "arbovirus", casos_provaveis=20),
                self._disease("ZIKA", "arbovirus", casos_provaveis=20),
            ]
        )[0]
        self.assertEqual(row["total_casos_provaveis"], 60)
        self.assertEqual(row["total_obitos"], 0)
        self.assertEqual(row["total_casos_graves"], 0)
        self.assertNotEqual(row["nivel_risco"], "critico")

    def test_municipality_without_diseases_is_baixo(self) -> None:
        self.assertEqual(self._rows([])[0]["nivel_risco"], "baixo")

    def test_risk_score_still_sums_the_diseases(self) -> None:
        """O score continua sendo a soma — o que muda é como o nível nasce."""
        row = self._rows(
            [
                self._disease("DENG", "arbovirus", casos_provaveis=10),
                self._disease("CHIK", "arbovirus", casos_provaveis=5),
            ]
        )[0]
        self.assertEqual(row["risk_score"], 15.0)


class TestRealSourceYear(unittest.TestCase):
    """`periodo.ano` carimbava o ano SOLICITADO em todo agravo.

    Medido no relatório real: o arquivo rotulado 2026 reúne Dengue de 2026,
    Febre Amarela de 2025, Leptospirose de 2024, Hanseníase/Toxoplasmose/
    Botulismo de 2023 e Meningite de 2022 — e todos diziam `periodo.ano: 2026`.
    """

    def test_disease_period_uses_the_real_source_year(self) -> None:
        from aggregation.report_builder import create_disease_summary
        from domain.disease_sources import DISEASE_SOURCES

        summary = create_disease_summary(DISEASE_SOURCES["MENI"], 2026, source_year=2022)
        self.assertEqual(summary["periodo"]["ano"], 2022)
        self.assertEqual(summary["periodo"]["ano_solicitado"], 2026)

    def test_falls_back_to_requested_year_when_unknown(self) -> None:
        from aggregation.report_builder import create_disease_summary
        from domain.disease_sources import DISEASE_SOURCES

        summary = create_disease_summary(DISEASE_SOURCES["DENG"], 2026)
        self.assertEqual(summary["periodo"]["ano"], 2026)

    def test_build_report_threads_source_years_through(self) -> None:
        from aggregation.report_builder import build_epidemiology_report

        report = build_epidemiology_report(
            {"MENI": [{"ID_MN_RESI": "355030", "DT_NOTIFIC": "2022-12-30"}]},
            year=2026,
            source_years={"MENI": 2022},
        )
        disease = report["municipios"][0]["doencas"][0]
        self.assertEqual(disease["periodo"]["ano"], 2022)


class TestStateFromNumericCode(unittest.TestCase):
    """SINAN entrega `SG_UF` como código numérico, não como sigla.

    `_state_from_record` fazia `.upper()` no código e devolvia "35" como se
    fosse UF. A versão correta existia em app.py e nunca esteve no caminho
    do relatório.
    """

    def test_resolves_uf_from_the_municipality_code(self) -> None:
        from aggregation.report_builder import state_from_record

        self.assertEqual(state_from_record({"ID_MN_RESI": "355030"}), "SP")

    def test_resolves_uf_from_numeric_sg_uf(self) -> None:
        from aggregation.report_builder import state_from_record

        self.assertEqual(state_from_record({"SG_UF": "33"}), "RJ")

    def test_accepts_an_already_correct_abbreviation(self) -> None:
        from aggregation.report_builder import state_from_record

        self.assertEqual(state_from_record({"SG_UF": "MG"}), "MG")

    def test_unknown_returns_empty(self) -> None:
        from aggregation.report_builder import state_from_record

        self.assertEqual(state_from_record({"SG_UF": "99"}), "")
        self.assertEqual(state_from_record({}), "")

    def test_report_assigns_the_right_uf_without_lookup(self) -> None:
        from aggregation.report_builder import build_epidemiology_report

        report = build_epidemiology_report(
            {"DENG": [{"ID_MN_RESI": "330455", "SG_UF": "33"}]}, year=2026
        )
        self.assertEqual(report["municipios"][0]["estado"], "RJ")


if __name__ == "__main__":
    unittest.main()
