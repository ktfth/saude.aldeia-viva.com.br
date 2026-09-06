"""
População municipal e taxa de incidência.

`domain/rates.py` e os seus 130 linhas de teste existem desde a "Fase 1", mas
`incidence_per_100k` nunca produziu um número em produção: a população vinha
de `lookup_item.get("populacao")` e o lookup do IBGE
(`ingestion/municipality_lookup.py`) só monta `{municipio, estado,
codigo_ibge}`. Resultado medido: 0 de 5.339 municípios com população, e a
chave `taxa_incidencia_100k` nem sequer aparecia no JSON servido.

A consequência é o defeito epistemológico central do produto: sem
denominador, ordenar por `risk_score` é aproximadamente ordenar por
população — e o painel apresentava isso como ranking de risco.
"""

import unittest

from ingestion.population_lookup import parse_population_payload
from aggregation.population_enrichment import (
    MIN_RELIABLE_POPULATION,
    enrich_with_population,
    incidence_block,
)


def _payload(*pairs):
    return [
        {
            "resultados": [
                {
                    "series": [
                        {
                            "localidade": {"id": code, "nome": name},
                            "serie": {"2026": value},
                        }
                        for code, name, value in pairs
                    ]
                }
            ]
        }
    ]


class TestParsePopulationPayload(unittest.TestCase):
    def test_keys_by_six_digit_code(self) -> None:
        got = parse_population_payload(
            _payload(("3550308", "São Paulo - SP", "11911337"))
        )
        self.assertEqual(got["355030"], 11911337)

    def test_ignores_non_numeric_values(self) -> None:
        got = parse_population_payload(_payload(("3550308", "SP", "-")))
        self.assertEqual(got, {})

    def test_ignores_malformed_entries(self) -> None:
        got = parse_population_payload(_payload(("", "x", "10"), ("12", "y", "10")))
        self.assertEqual(got, {})

    def test_empty_payload(self) -> None:
        self.assertEqual(parse_population_payload([]), {})
        self.assertEqual(parse_population_payload(None), {})

    def test_picks_the_most_recent_year(self) -> None:
        payload = [
            {
                "resultados": [
                    {
                        "series": [
                            {
                                "localidade": {"id": "3550308", "nome": "SP"},
                                "serie": {"2024": "100", "2026": "200"},
                            }
                        ]
                    }
                ]
            }
        ]
        self.assertEqual(parse_population_payload(payload)["355030"], 200)


class TestIncidenceBlock(unittest.TestCase):
    """A taxa nunca é publicada como número nu."""

    def test_computes_rate_and_keeps_the_denominator_visible(self) -> None:
        got = incidence_block(500, 100_000)
        self.assertEqual(got["por_100k"], 500.0)
        self.assertEqual(got["populacao"], 100_000)
        self.assertTrue(got["confiavel"])

    def test_small_denominator_is_flagged_not_hidden(self) -> None:
        """Município minúsculo produz taxa instável. O número existe, mas
        vem marcado — suprimir sem dizer é tão desonesto quanto publicar
        sem ressalva."""
        got = incidence_block(3, 800)
        self.assertFalse(got["confiavel"])
        self.assertIn("população", got["ressalva"])

    def test_threshold_boundary(self) -> None:
        self.assertTrue(incidence_block(10, MIN_RELIABLE_POPULATION)["confiavel"])
        self.assertFalse(incidence_block(10, MIN_RELIABLE_POPULATION - 1)["confiavel"])

    def test_missing_population_degrades_gracefully(self) -> None:
        got = incidence_block(500, None)
        self.assertIsNone(got["por_100k"])
        self.assertFalse(got["confiavel"])
        self.assertIn("sem população", got["ressalva"])

    def test_zero_population_is_treated_as_missing(self) -> None:
        self.assertIsNone(incidence_block(5, 0)["por_100k"])


class TestEnrichWithPopulation(unittest.TestCase):
    POP = {"355030": 11_911_337, "520870": 1_536_097, "999999": 800}

    def _report(self):
        return {
            "municipios": [
                {
                    "codigo_municipio": "355030",
                    "municipio": "São Paulo",
                    "total_casos_provaveis": 10_225,
                },
                {
                    "codigo_municipio": "520870",
                    "municipio": "Goiânia",
                    "total_casos_provaveis": 16_925,
                },
                {
                    "codigo_municipio": "111111",
                    "municipio": "Sem população",
                    "total_casos_provaveis": 10,
                },
            ],
            "metadata": {},
        }

    def test_adds_population_and_incidence(self) -> None:
        got = enrich_with_population(self._report(), self.POP)
        sp = got["municipios"][0]
        self.assertEqual(sp["populacao"], 11_911_337)
        self.assertAlmostEqual(sp["incidencia"]["por_100k"], 85.84, places=1)

    def test_reveals_that_the_biggest_city_is_not_the_worst_rate(self) -> None:
        """O ponto inteiro do denominador.

        São Paulo tem mais casos absolutos que Goiânia (10.225 contra
        16.925 — na verdade menos), mas o que importa é a taxa: Goiânia
        tem população oito vezes menor.
        """
        got = enrich_with_population(self._report(), self.POP)
        sp, goiania = got["municipios"][0], got["municipios"][1]
        self.assertGreater(
            goiania["incidencia"]["por_100k"], sp["incidencia"]["por_100k"] * 10
        )

    def test_municipality_without_population_is_explicit(self) -> None:
        got = enrich_with_population(self._report(), self.POP)
        orphan = got["municipios"][2]
        self.assertIsNone(orphan["populacao"])
        self.assertIsNone(orphan["incidencia"]["por_100k"])
        self.assertFalse(orphan["incidencia"]["confiavel"])

    def test_does_not_mutate_input(self) -> None:
        report = self._report()
        enrich_with_population(report, self.POP)
        self.assertNotIn("populacao", report["municipios"][0])

    def test_metadata_declares_coverage(self) -> None:
        meta = enrich_with_population(self._report(), self.POP)["metadata"]
        self.assertEqual(meta["populacao"]["municipios_com_populacao"], 2)
        self.assertEqual(meta["populacao"]["municipios_total"], 3)

    def test_empty_population_map_keeps_the_report_usable(self) -> None:
        got = enrich_with_population(self._report(), {})
        self.assertEqual(got["metadata"]["populacao"]["municipios_com_populacao"], 0)
        self.assertIsNone(got["municipios"][0]["incidencia"]["por_100k"])


if __name__ == "__main__":
    unittest.main()
