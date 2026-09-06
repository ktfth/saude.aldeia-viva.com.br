"""
TDD - Fase 1: Taxas de incidência por 100k habitantes

Tests for the new `domain/rates.py` module.
All tests written BEFORE the implementation (RED phase).

Covers:
- incidence_per_100k: pure function for epidemiological incidence rate calculation
- Edge cases: None population, zero population, negative population, zero cases,
  large numbers, rounding precision
"""

import unittest


class TestIncidencePer100k(unittest.TestCase):
    """Unit tests for incidence_per_100k in domain/rates.py."""

    def _get_fn(self):
        """Lazy import so the test file itself loads even before the module exists."""
        from domain.rates import incidence_per_100k
        return incidence_per_100k

    # -------------------------------------------------------------------------
    # Happy path
    # -------------------------------------------------------------------------

    def test_returns_correct_rate_for_basic_inputs(self):
        """Standard case: 50 cases / 100_000 population = 50.0 per 100k."""
        fn = self._get_fn()
        result = fn(50, 100_000)
        self.assertEqual(result, 50.0)

    def test_returns_correct_rate_small_population(self):
        """10 cases in a population of 1_000 = 1000.0 per 100k."""
        fn = self._get_fn()
        result = fn(10, 1_000)
        self.assertEqual(result, 1000.0)

    def test_rounds_to_two_decimal_places(self):
        """1 case in 3_000 population = 33.33 per 100k (not 33.333...)."""
        fn = self._get_fn()
        result = fn(1, 3_000)
        self.assertEqual(result, 33.33)

    def test_rounding_example_two(self):
        """7 cases in 80_000 = round(7/80000*100000, 2) = 8.75."""
        fn = self._get_fn()
        result = fn(7, 80_000)
        self.assertEqual(result, 8.75)

    def test_returns_float(self):
        """Return type must be float, not int."""
        fn = self._get_fn()
        result = fn(100, 200_000)
        self.assertIsInstance(result, float)

    # -------------------------------------------------------------------------
    # Zero cases
    # -------------------------------------------------------------------------

    def test_zero_cases_with_positive_population_returns_zero_float(self):
        """0 cases and a valid population returns 0.0 (not None)."""
        fn = self._get_fn()
        result = fn(0, 500_000)
        self.assertEqual(result, 0.0)
        self.assertIsInstance(result, float)

    # -------------------------------------------------------------------------
    # Invalid / missing population — graceful degradation
    # -------------------------------------------------------------------------

    def test_none_population_returns_none(self):
        """Cannot compute rate without population data."""
        fn = self._get_fn()
        result = fn(10, None)
        self.assertIsNone(result)

    def test_zero_population_returns_none(self):
        """Division by zero must be avoided — return None."""
        fn = self._get_fn()
        result = fn(10, 0)
        self.assertIsNone(result)

    def test_negative_population_returns_none(self):
        """Negative population is semantically invalid."""
        fn = self._get_fn()
        result = fn(10, -1)
        self.assertIsNone(result)

    def test_none_population_with_zero_cases_returns_none(self):
        """Even zero cases cannot be rated without population."""
        fn = self._get_fn()
        result = fn(0, None)
        self.assertIsNone(result)

    def test_zero_population_with_zero_cases_returns_none(self):
        """Zero/zero is still undefined — return None."""
        fn = self._get_fn()
        result = fn(0, 0)
        self.assertIsNone(result)

    # -------------------------------------------------------------------------
    # Large numbers / precision
    # -------------------------------------------------------------------------

    def test_large_population_brazil_scale(self):
        """Brazil-scale: 215 million population, 100k cases."""
        fn = self._get_fn()
        result = fn(100_000, 215_000_000)
        expected = round(100_000 / 215_000_000 * 100_000, 2)
        self.assertEqual(result, expected)

    def test_large_case_count(self):
        """High incidence scenario: 50k cases in 100k population."""
        fn = self._get_fn()
        result = fn(50_000, 100_000)
        self.assertEqual(result, 50_000.0)

    def test_single_case_large_population(self):
        """Single case in a large city: precise rounding matters."""
        fn = self._get_fn()
        result = fn(1, 12_325_232)
        expected = round(1 / 12_325_232 * 100_000, 2)
        self.assertEqual(result, expected)

    def test_very_large_population_does_not_raise(self):
        """No exception for extremely large populations."""
        fn = self._get_fn()
        result = fn(1, 10 ** 9)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, float)

    # -------------------------------------------------------------------------
    # Type contract: no exception on valid non-negative int inputs
    # -------------------------------------------------------------------------

    def test_does_not_raise_on_valid_inputs(self):
        """Contract: no exception raised for non-negative cases and positive population."""
        fn = self._get_fn()
        for cases in [0, 1, 100, 10_000]:
            for pop in [1, 1_000, 1_000_000]:
                with self.subTest(cases=cases, pop=pop):
                    # Must not raise
                    fn(cases, pop)

class TestIncidenceIntegration(unittest.TestCase):
    """Integração da taxa com a autoridade que de fato a produz.

    As classes que existiam aqui verificavam que `create_municipality_summary`
    guardava `populacao` vinda de `lookup_item` e que
    `finalize_municipality_rows` gravava `taxa_incidencia_100k`. Esse caminho
    passava nos testes e nunca funcionou em produção: o lookup do IBGE
    (`ingestion/municipality_lookup.py`) monta apenas
    `{municipio, estado, codigo_ibge}`, então `populacao` era sempre None e a
    taxa era sempre None — medido: 0 de 5.339 municípios no relatório real.

    O denominador passou a ser responsabilidade de
    `aggregation/population_enrichment.py`, aplicado na entrada do relatório
    em memória, o que faz valer também para o cache em disco e o snapshot
    embarcado. Estes testes acompanham a mudança de autoridade.
    """

    POPULATIONS = {"355030": 11_911_337, "520870": 1_536_097, "999999": 800}

    def _report(self, codigo="355030", casos=10_225):
        return {
            "municipios": [
                {
                    "codigo_municipio": codigo,
                    "municipio": "TestCity",
                    "estado": "SP",
                    "total_casos_provaveis": casos,
                    "total_obitos": 0,
                    "risk_score": 1.0,
                    "nivel_risco": "baixo",
                    "doencas": [],
                    "doencas_altas": [],
                }
            ],
            "metadata": {},
        }

    def _enrich(self, **kwargs):
        from aggregation.population_enrichment import enrich_with_population

        return enrich_with_population(self._report(**kwargs), self.POPULATIONS)[
            "municipios"
        ][0]

    def test_population_reaches_the_municipality(self):
        self.assertEqual(self._enrich()["populacao"], 11_911_337)

    def test_rate_is_computed_when_population_is_known(self):
        self.assertAlmostEqual(self._enrich()["incidencia"]["por_100k"], 85.84, places=1)

    def test_rate_is_none_when_population_is_unknown(self):
        row = self._enrich(codigo="111111")
        self.assertIsNone(row["populacao"])
        self.assertIsNone(row["incidencia"]["por_100k"])
        self.assertIn("sem população", row["incidencia"]["ressalva"])

    def test_zero_cases_with_known_population_is_zero_not_none(self):
        row = self._enrich(casos=0)
        self.assertEqual(row["incidencia"]["por_100k"], 0.0)

    def test_existing_fields_are_untouched(self):
        row = self._enrich()
        for field in (
            "codigo_municipio",
            "municipio",
            "estado",
            "total_casos_provaveis",
            "total_obitos",
            "risk_score",
            "nivel_risco",
            "doencas",
            "doencas_altas",
        ):
            self.assertIn(field, row)
        self.assertEqual(row["total_casos_provaveis"], 10_225)

    def test_small_municipality_rate_is_flagged(self):
        row = self._enrich(codigo="999999", casos=1)
        self.assertIsNotNone(row["incidencia"]["por_100k"])
        self.assertFalse(row["incidencia"]["confiavel"])

    def test_report_builder_no_longer_claims_population(self):
        """A autoridade saiu de lá; nada deve fingir que ainda está."""
        from aggregation.report_builder import create_municipality_summary

        summary = create_municipality_summary(
            "999999", {"ID_MN_RESI": "999999"}, 2025, {"municipio": "X", "estado": "SP"}
        )
        self.assertNotIn("populacao", summary)


if __name__ == "__main__":
    unittest.main()
