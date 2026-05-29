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


class TestIncidencePer100kIntegrationWithReportBuilder(unittest.TestCase):
    """
    Integration tests: municipality summaries carry taxa_incidencia_100k
    after finalize_municipality_rows is called.
    """

    def _build_municipality(self, populacao: int | None) -> dict:
        """Helper: build a minimal municipality dict as create_municipality_summary would."""
        from aggregation.report_builder import create_municipality_summary
        lookup_item = {"municipio": "TestCity", "estado": "SP"}
        if populacao is not None:
            lookup_item["populacao"] = populacao  # type: ignore[assignment]
        record = {"ID_MN_RESI": "999999", "SG_UF": "35"}
        return create_municipality_summary("999999", record, 2025, lookup_item)

    def test_municipality_summary_stores_populacao_from_lookup(self):
        """create_municipality_summary must store populacao when present in lookup."""
        mun = self._build_municipality(populacao=500_000)
        self.assertEqual(mun["populacao"], 500_000)

    def test_municipality_summary_populacao_is_none_when_absent(self):
        """create_municipality_summary must store None when lookup has no populacao."""
        mun = self._build_municipality(populacao=None)
        self.assertIn("populacao", mun)
        self.assertIsNone(mun["populacao"])

    def _finalize_with_disease(
        self,
        populacao: int | None,
        casos_provaveis: int = 10,
    ) -> dict:
        """Helper: run a minimal finalize_municipality_rows cycle."""
        from aggregation.report_builder import finalize_municipality_rows
        municipality = self._build_municipality(populacao=populacao)
        # Inject a fake disease with the given case count
        municipality["doencas_por_codigo"] = {
            "DENG": {
                "codigo": "DENG",
                "nome": "Dengue",
                "virus": "DENV",
                "tipo": "Arbovirose urbana",
                "perfil_risco": "arbovirus",
                "formula_risco": "casos_provaveis + 4*sinais_alarme",
                "periodo": {"ano": 2025},
                "total_notificacoes": casos_provaveis,
                "casos_provaveis": casos_provaveis,
                "casos_descartados": 0,
                "sinais_alarme": 0,
                "casos_graves": 0,
                "hospitalizacoes": 0,
                "obitos": 0,
                "risk_score": 0.0,
                "nivel_risco": "baixo",
                "ultima_notificacao": None,
                "ultimo_inicio_sintomas": None,
                "classificacoes": {},
            }
        }
        rows = finalize_municipality_rows([municipality])
        return rows[0]

    def test_taxa_incidencia_present_when_populacao_known(self):
        """finalize_municipality_rows adds taxa_incidencia_100k when population is known."""
        row = self._finalize_with_disease(populacao=200_000, casos_provaveis=10)
        self.assertIn("taxa_incidencia_100k", row)
        expected = round(10 / 200_000 * 100_000, 2)
        self.assertEqual(row["taxa_incidencia_100k"], expected)

    def test_taxa_incidencia_is_none_when_populacao_unknown(self):
        """finalize_municipality_rows sets taxa_incidencia_100k=None when population is missing."""
        row = self._finalize_with_disease(populacao=None, casos_provaveis=10)
        self.assertIn("taxa_incidencia_100k", row)
        self.assertIsNone(row["taxa_incidencia_100k"])

    def test_existing_fields_unchanged_after_adding_taxa(self):
        """Adding taxa_incidencia_100k must not alter any previously existing fields."""
        row = self._finalize_with_disease(populacao=500_000, casos_provaveis=5)
        # Check several pre-existing required fields
        self.assertIn("codigo_municipio", row)
        self.assertIn("municipio", row)
        self.assertIn("estado", row)
        self.assertIn("periodo", row)
        self.assertIn("total_casos_provaveis", row)
        self.assertIn("total_obitos", row)
        self.assertIn("risk_score", row)
        self.assertIn("nivel_risco", row)
        self.assertIn("doencas", row)
        self.assertIn("doencas_altas", row)
        self.assertEqual(row["total_casos_provaveis"], 5)

    def test_taxa_zero_when_cases_zero_and_population_known(self):
        """0 cases with known population yields 0.0 rate (not None)."""
        row = self._finalize_with_disease(populacao=100_000, casos_provaveis=0)
        self.assertEqual(row["taxa_incidencia_100k"], 0.0)


class TestMunicipalityLookupPopulacao(unittest.TestCase):
    """
    Tests that the municipality lookup layer correctly propagates
    the optional 'populacao' field from lookup items.
    """

    def test_lookup_item_with_populacao_is_accepted(self):
        """A lookup dict containing 'populacao' is a valid item."""
        lookup_item: dict = {
            "municipio": "Curitiba",
            "estado": "PR",
            "codigo_ibge": "4106902",
            "populacao": 1_948_626,
        }
        # The lookup item is just a plain dict — verify it carries the field
        self.assertEqual(lookup_item["populacao"], 1_948_626)

    def test_lookup_item_without_populacao_defaults_to_none_in_summary(self):
        """When lookup item has no 'populacao', municipality summary must have None."""
        from aggregation.report_builder import create_municipality_summary
        lookup_item = {"municipio": "Curitiba", "estado": "PR"}
        record = {"ID_MN_RESI": "410690"}
        summary = create_municipality_summary("410690", record, 2024, lookup_item)
        self.assertIn("populacao", summary)
        self.assertIsNone(summary["populacao"])

    def test_lookup_item_with_populacao_propagates_to_summary(self):
        """When lookup item has 'populacao', municipality summary must carry it."""
        from aggregation.report_builder import create_municipality_summary
        lookup_item = {
            "municipio": "Curitiba",
            "estado": "PR",
            "populacao": 1_948_626,
        }
        record = {"ID_MN_RESI": "410690"}
        summary = create_municipality_summary("410690", record, 2024, lookup_item)
        self.assertEqual(summary["populacao"], 1_948_626)


if __name__ == "__main__":
    unittest.main()
