"""
Bairros homônimos entre cidades.

`build_locality_aliases()` montava um dicionário plano por nome normalizado.
Quando duas cidades compartilham um bairro, `aliases[key] = {...}`
sobrescreve, e a última cidade iterada vence — Rio de Janeiro, no caso.

Três nomes são homônimos no registro atual: `penha` e `campo grande`
existem em São Paulo e no Rio; `vila sônia` estava listado duas vezes dentro de
São Paulo, inflando a contagem publicada. Efeito medido na API:

    municipio=penha&estado=SP   -> SEM RESULTADO
    municipio=penha             -> Rio de Janeiro, silenciosamente
    municipio=campo grande&estado=SP -> SEM RESULTADO

O primeiro caso é o pior: `/v1/bairros` anuncia `penha` na lista de São
Paulo, e a consulta que segue exatamente essa documentação devolve vazio.
O segundo esconde uma escolha arbitrária atrás de uma resposta que parece
certa — e `campo grande` é também a capital do Mato Grosso do Sul.

A correção: o mapa guarda TODOS os candidatos por nome, a UF desambigua, e
quando não há UF a ambiguidade é declarada em vez de escondida.
"""

import unittest

from aggregation.filters import (
    CITY_NEIGHBORHOODS,
    LOCALITY_ALIASES,
    ambiguous_locality_names,
    resolve_locality_alias,
)


class TestAliasMapKeepsEveryCandidate(unittest.TestCase):
    def test_a_homonym_keeps_both_cities(self) -> None:
        self.assertEqual(
            {entry["estado"] for entry in LOCALITY_ALIASES["penha"]}, {"SP", "RJ"}
        )

    def test_a_unique_name_has_a_single_candidate(self) -> None:
        self.assertEqual(len(LOCALITY_ALIASES["copacabana"]), 1)
        self.assertEqual(LOCALITY_ALIASES["copacabana"][0]["estado"], "RJ")

    def test_candidates_are_ordered_deterministically(self) -> None:
        """A ordem de iteração de um dicionário não pode decidir qual cidade
        ganha — a mesma consulta tem que devolver o mesmo município sempre."""
        estados = [entry["estado"] for entry in LOCALITY_ALIASES["penha"]]
        self.assertEqual(estados, sorted(estados))

    def test_every_declared_neighborhood_is_resolvable(self) -> None:
        for city in CITY_NEIGHBORHOODS.values():
            for bairro in city["bairros"]:
                resolved = resolve_locality_alias(bairro, city["uf"])
                self.assertIsNotNone(
                    resolved,
                    f"{bairro!r} está anunciado em {city['municipio']}/{city['uf']} "
                    "mas não resolve com essa UF",
                )
                self.assertEqual(resolved["municipio_resolvido"], city["municipio"])

    def test_ambiguous_names_are_enumerable(self) -> None:
        self.assertEqual(ambiguous_locality_names(), ["campo grande", "penha"])


class TestResolutionRespectsTheStateFilter(unittest.TestCase):
    def test_uf_disambiguates_a_homonym(self) -> None:
        self.assertEqual(
            resolve_locality_alias("penha", "SP")["municipio_resolvido"], "São Paulo"
        )
        self.assertEqual(
            resolve_locality_alias("penha", "RJ")["municipio_resolvido"],
            "Rio de Janeiro",
        )

    def test_campo_grande_resolves_in_both_cities(self) -> None:
        self.assertEqual(
            resolve_locality_alias("campo grande", "SP")["municipio_resolvido"],
            "São Paulo",
        )
        self.assertEqual(
            resolve_locality_alias("campo grande", "RJ")["municipio_resolvido"],
            "Rio de Janeiro",
        )

    def test_a_uf_with_no_candidate_resolves_to_nothing(self) -> None:
        """Campo Grande/MS é município, não bairro: o alias não deve capturá-lo."""
        self.assertIsNone(resolve_locality_alias("campo grande", "MS"))

    def test_without_uf_the_ambiguity_is_declared_not_hidden(self) -> None:
        resolved = resolve_locality_alias("penha", "")
        self.assertIn("ambiguidade", resolved)
        self.assertEqual(
            [item["estado"] for item in resolved["ambiguidade"]], ["RJ", "SP"]
        )
        self.assertIn("aviso", resolved)

    def test_an_unambiguous_name_carries_no_ambiguity_block(self) -> None:
        self.assertNotIn("ambiguidade", resolve_locality_alias("ipanema", ""))

    def test_unknown_name_still_returns_none(self) -> None:
        self.assertIsNone(resolve_locality_alias("bairro que nao existe", ""))
        self.assertIsNone(resolve_locality_alias("", "SP"))

    def test_prefix_variations_still_work(self) -> None:
        self.assertEqual(
            resolve_locality_alias("bairro copacabana", "")["municipio_resolvido"],
            "Rio de Janeiro",
        )


class TestEndpointReportsTheTruth(unittest.TestCase):
    def test_bairros_endpoint_declares_distinct_and_ambiguous(self) -> None:
        from aggregation.filters import get_supported_bairros

        payload = get_supported_bairros()
        # 136 entradas reais: a lista de São Paulo repetia "Vila Sônia" e a
        # contagem publicada dizia 137. Dos 136, dois nomes são homônimos
        # entre cidades, logo 134 nomes distintos buscáveis.
        self.assertEqual(payload["total_bairros"], 136)
        self.assertEqual(payload["total_nomes_distintos"], 134)
        self.assertEqual(payload["nomes_ambiguos"], ["campo grande", "penha"])

    def test_the_api_returns_the_right_city_for_each_uf(self) -> None:
        from fastapi.testclient import TestClient

        import app

        app.rate_limiter.reset()
        with TestClient(app.app) as client:
            for uf, esperado in (("SP", "São Paulo"), ("RJ", "Rio de Janeiro")):
                app.rate_limiter.reset()
                rows = client.get(
                    f"/v1/risk-index?municipio=penha&estado={uf}&limite=1"
                ).json()
                self.assertIsInstance(rows, list, f"penha/{uf} devolveu vazio")
                self.assertTrue(rows, f"penha/{uf} devolveu lista vazia")
                self.assertEqual(rows[0]["municipio"], esperado)


if __name__ == "__main__":
    unittest.main()
