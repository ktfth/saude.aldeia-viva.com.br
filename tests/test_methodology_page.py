"""
A página de metodologia precisa descrever o método que existe.

`/sobre` é a página mais autoritativa do produto: é para onde o painel manda
quem quer saber como o número foi feito. Depois de cinco iterações mudando o
método, ela contradizia o código em três pontos:

  - "O nível crítico aparece quando há óbitos ou score muito elevado" — o
    limiar sobre o score agregado foi removido justamente por saturar 24,3%
    dos 5.339 municípios em "crítico". O nível municipal passou a ser o pior
    nível entre os agravos, cada um com o seu próprio perfil.
  - "O score organiza prioridade operacional" e "priorize os municípios com
    risco alto ou crítico" — `risk_score` correlaciona 0,822 com a população.
    `/agentes` foi corrigido na iteração 5; esta página continuava ensinando
    a ordenar pelo tamanho da cidade.
  - Nenhuma menção à incidência por 100 mil habitantes, que é a ordenação
    padrão do painel, nem aos três relógios temporais.

Uma página de metodologia errada é pior que nenhuma: ela é citada.
"""

import re
import unittest

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report


class MethodologyPageTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.html = self.client.get("/sobre").text
        self.text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.html))

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)


class TestRiskLevelIsDescribedCorrectly(MethodologyPageTest):
    def test_does_not_claim_the_level_comes_from_the_aggregate_score(self) -> None:
        self.assertNotIn("score muito elevado", self.text)

    def test_explains_the_worst_level_rule(self) -> None:
        self.assertIn("pior nível entre os agravos", self.text)

    def test_explains_the_current_source_cut(self) -> None:
        self.assertIn("nivel_risco_fonte_atual", self.html)


class TestScoreIsNotPresentedAsPriority(MethodologyPageTest):
    def test_does_not_tell_the_reader_to_prioritize_by_score(self) -> None:
        self.assertNotIn("O score organiza prioridade operacional", self.text)

    def test_declares_the_correlation_with_population(self) -> None:
        self.assertIn("0,82", self.text)

    def test_points_to_incidence_as_the_comparable_measure(self) -> None:
        self.assertIn("100 mil", self.text)
        self.assertIn("incidencia", self.html)


class TestTemporalDimensionIsExplained(MethodologyPageTest):
    def test_names_the_three_clocks(self) -> None:
        for termo in ("carga", "fonte", "recência"):
            self.assertIn(termo, self.text.lower())

    def test_warns_that_the_report_mixes_source_years(self) -> None:
        self.assertIn("anos diferentes", self.text)

    def test_shows_the_real_source_year_of_each_disease(self) -> None:
        """Já existia e é o melhor da página — não pode se perder no corte."""
        # A lista de fontes traz o ano REAL do arquivo de cada agravo — é o
        # dado que desmente o "ano-base" único. Buscado pela frase exata para
        # não casar com o "Meningite" que aparece no JSON-LD do cabeçalho.
        self.assertRegex(
            self.text, r"Meningite\s*:\s*[\d.]+ registros, arquivo de 20\d\d"
        )
        self.assertRegex(
            self.text, r"Dengue\s*:\s*[\d.]+ registros, arquivo de 20\d\d"
        )


class TestDenominatorIsExplained(MethodologyPageTest):
    def test_explains_the_reliability_threshold(self) -> None:
        self.assertIn("10.000", self.text)

    def test_names_the_population_source(self) -> None:
        self.assertIn("IBGE", self.text)


class TestFormulasStillComeFromCode(MethodologyPageTest):
    def test_every_enabled_disease_lists_its_own_formula(self) -> None:
        from domain.disease_sources import DISEASE_SOURCES
        from domain.risk import risk_profile_for_source

        for source in DISEASE_SOURCES.values():
            with self.subTest(disease=source.nome):
                self.assertIn(
                    risk_profile_for_source(source).formula.replace("*", "*"),
                    self.html.replace("&#42;", "*"),
                )

    def test_does_not_present_one_universal_formula(self) -> None:
        """A fórmula genérica no topo era só o perfil de arbovirose."""
        self.assertNotIn("Fórmula do score</h2>", self.html)


class TestLocalityAmbiguityIsMentioned(MethodologyPageTest):
    def test_says_that_a_name_can_exist_in_more_than_one_city(self) -> None:
        self.assertIn("mais de uma cidade", self.text)


if __name__ == "__main__":
    unittest.main()
