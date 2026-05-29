"""
TDD Safety Net for Aggregation Filters - Fase 0

Protects the filtering logic and especially the São Paulo district
alias resolution (critical for agents searching by neighborhood).
"""

import unittest

from aggregation.filters import (
    filter_risk_index,
    filter_alerts,
    resolve_locality_alias,
    level_at_least,
    LOCALITY_ALIASES,
    SAO_PAULO_DISTRICTS,
    CITY_NEIGHBORHOODS,
    get_supported_bairros,
)


class TestLocalityAliases(unittest.TestCase):
    def test_perus_resolves_to_sao_paulo(self):
        alias = LOCALITY_ALIASES.get("perus")
        self.assertIsNotNone(alias)
        self.assertEqual(alias["municipio_resolvido"], "São Paulo")
        self.assertEqual(alias["estado"], "SP")

    def test_vila_mariana_resolves(self):
        alias = LOCALITY_ALIASES.get("vila mariana")
        self.assertIsNotNone(alias)
        self.assertEqual(alias["codigo_municipio"], "355030")

    def test_sao_paulo_districts_count(self):
        # We have a large list of known districts
        self.assertGreater(len(SAO_PAULO_DISTRICTS), 80)


class TestFilterRiskIndex(unittest.TestCase):
    def setUp(self):
        self.sample_rows = [
            {
                "codigo_municipio": "355030",
                "municipio": "São Paulo",
                "estado": "SP",
                "nivel_risco": "alto",
                "doencas_altas": [{"nome": "Dengue"}],
            },
            {
                "codigo_municipio": "352940",
                "municipio": "Campinas",
                "estado": "SP",
                "nivel_risco": "baixo",
                "doencas_altas": [],
            },
            {
                "codigo_municipio": "330455",
                "municipio": "Rio de Janeiro",
                "estado": "RJ",
                "nivel_risco": "critico",
                "doencas_altas": [{"nome": "Dengue"}],
            },
        ]

    def test_filter_by_estado(self):
        result = filter_risk_index(self.sample_rows, estado="SP")
        self.assertEqual(len(result), 2)

    def test_filter_somente_altos(self):
        result = filter_risk_index(self.sample_rows, somente_altos=True)
        self.assertEqual(len(result), 2)  # SP + RJ have alerts

    def test_filter_by_nivel_minimo(self):
        result = filter_risk_index(self.sample_rows, nivel_minimo="alto")
        self.assertEqual(len(result), 2)

    def test_perus_alias_resolution(self):
        # Searching "perus" should resolve to São Paulo (355030)
        result = filter_risk_index(self.sample_rows, municipio="perus")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["codigo_municipio"], "355030")


class TestLevelAtLeast(unittest.TestCase):
    def test_ordering(self):
        self.assertTrue(level_at_least("critico", "alto"))
        self.assertTrue(level_at_least("alto", "moderado"))
        self.assertFalse(level_at_least("baixo", "moderado"))
        self.assertTrue(level_at_least("critico", "critico"))


if __name__ == "__main__":
    unittest.main()

class TestMultiCityLocalityAliases(unittest.TestCase):
    """TDD tests for multi-city neighborhood support (other capitals beyond SP)."""

    def test_city_neighborhoods_registry_exists_and_has_sp(self):
        self.assertIn("SP:São Paulo", CITY_NEIGHBORHOODS)
        sp = CITY_NEIGHBORHOODS["SP:São Paulo"]
        self.assertEqual(sp["uf"], "SP")
        self.assertEqual(sp["codigo_municipio"], "355030")
        self.assertGreater(len(sp["bairros"]), 80)

    def test_localidade_aliases_now_multi_city(self):
        self.assertIn("copacabana", LOCALITY_ALIASES)
        rio_alias = LOCALITY_ALIASES["copacabana"]
        self.assertEqual(rio_alias["municipio_resolvido"], "Rio de Janeiro")
        self.assertEqual(rio_alias["estado"], "RJ")
        self.assertEqual(rio_alias["codigo_municipio"], "330455")

        self.assertIn("savassi", LOCALITY_ALIASES)
        bh_alias = LOCALITY_ALIASES["savassi"]
        self.assertEqual(bh_alias["municipio_resolvido"], "Belo Horizonte")
        self.assertEqual(bh_alias["estado"], "MG")

        self.assertIn("boa viagem", LOCALITY_ALIASES)
        rec_alias = LOCALITY_ALIASES["boa viagem"]
        self.assertEqual(rec_alias["municipio_resolvido"], "Recife")
        self.assertEqual(rec_alias["estado"], "PE")
        self.assertEqual(rec_alias["codigo_municipio"], "261160")

    def test_resolve_locality_alias_supports_other_cities(self):
        alias = resolve_locality_alias("ipanema", "")
        self.assertIsNotNone(alias)
        self.assertEqual(alias["municipio_resolvido"], "Rio de Janeiro")
        self.assertEqual(alias["estado"], "RJ")

        alias2 = resolve_locality_alias("bairro de savassi", "MG")
        self.assertIsNotNone(alias2)
        self.assertEqual(alias2["municipio_resolvido"], "Belo Horizonte")

        alias3 = resolve_locality_alias("distrito de boa viagem", "")
        self.assertIsNotNone(alias3)
        self.assertEqual(alias3["estado"], "PE")

    def test_filter_risk_index_resolves_rio_bairro(self):
        sample = [
            {"codigo_municipio": "330455", "municipio": "Rio de Janeiro", "estado": "RJ", "nivel_risco": "alto"},
            {"codigo_municipio": "355030", "municipio": "São Paulo", "estado": "SP", "nivel_risco": "baixo"},
        ]
        result = filter_risk_index(sample, municipio="leblon")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["codigo_municipio"], "330455")
        self.assertIn("filtro_localidade", result[0])
        self.assertEqual(result[0]["filtro_localidade"]["localidade"], "Leblon")

    def test_get_supported_bairros_default_returns_grouped_structure(self):
        data = get_supported_bairros()
        self.assertIn("versao", data)
        self.assertGreaterEqual(data["versao"], 2)
        self.assertIn("cidades", data)
        self.assertGreaterEqual(len(data["cidades"]), 4)
        sp_entry = next((c for c in data["cidades"] if c["uf"] == "SP"), None)
        self.assertIsNotNone(sp_entry)
        self.assertIn("bairros", sp_entry)
        self.assertGreater(len(sp_entry["bairros"]), 80)

    def test_get_supported_bairros_filters_by_uf(self):
        data = get_supported_bairros(uf="RJ")
        self.assertEqual(data["uf"], "RJ")
        self.assertEqual(data["municipio"], "Rio de Janeiro")
        self.assertIn("bairros", data)
        self.assertGreater(len(data["bairros"]), 5)

    def test_get_supported_bairros_filters_by_municipio_name(self):
        data = get_supported_bairros(municipio="recife")
        self.assertEqual(data["estado"], "PE")
        self.assertIn("Boa Viagem", data["bairros"])

    def test_sp_backward_compatibility_preserved(self):
        alias = LOCALITY_ALIASES.get("perus")
        self.assertEqual(alias["codigo_municipio"], "355030")
        self.assertEqual(alias["estado"], "SP")
        sp_direct = get_supported_bairros(uf="SP", municipio="São Paulo")
        self.assertEqual(sp_direct["estado"], "SP")
        self.assertGreater(len(sp_direct.get("bairros", [])), 80)
