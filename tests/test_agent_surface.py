"""
Agentes como usuários de primeira classe.

Um agente autônomo consome esta API sem humano no meio. Se o manifesto não
declarar a idade das fontes, o agente vai afirmar com toda a confiança que
"São Paulo tem meningite em nível crítico em 2026" — quando o arquivo de
meningite é de 2022 e a carga parou em abril.

O painel humano já ganhou essa informação. Estes testes garantem que o
contrato de máquina não fique para trás, porque é ele que se propaga em
escala e sem revisão.
"""

import unittest

from fastapi.testclient import TestClient

import app


class AgentSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)


class TestAgentManifest(AgentSurfaceTest):
    def manifest(self) -> dict:
        return self.client.get("/agent.json").json()

    def test_declares_source_age_per_disease(self) -> None:
        data = self.manifest()["freshness"]
        self.assertIn("sources", data)
        self.assertIn("current_year_sources", data)
        self.assertIn("total_sources", data)

    def test_declares_load_age_in_days(self) -> None:
        data = self.manifest()["freshness"]
        self.assertIn("load_age_days", data)
        self.assertIn("is_current", data)

    def test_declares_the_ano_parameter_that_already_existed(self) -> None:
        """`ano` existia no endpoint e era usado pelo proprio painel, mas o
        manifesto nunca o declarou — um agente não tinha como descobri-lo."""
        query = self.manifest()["endpoints"]["/v1/risk-index"]["query"]
        self.assertIn("ano", query)

    def test_declares_the_tier_ceiling(self) -> None:
        """Pedir limite=100 sem chave devolve 5. Isso precisa estar escrito."""
        manifest = self.manifest()
        self.assertIn("limits", manifest)
        self.assertEqual(manifest["limits"]["anonymous"]["max_results"], 5)

    def test_declares_the_result_count_headers(self) -> None:
        manifest = self.manifest()
        self.assertIn("X-Total-Results", manifest["limits"]["response_headers"])

    def test_interpretation_rules_warn_about_source_age(self) -> None:
        rules = " ".join(self.manifest()["interpretation"])
        self.assertIn("fonte", rules.lower())
        self.assertIn("recencia", rules.lower().replace("ê", "e").replace("ó", "o"))

    def test_limitations_mention_the_multi_year_patchwork(self) -> None:
        limitations = " ".join(self.manifest()["limitations"]).lower()
        self.assertIn("ano", limitations)


class TestLlmsTxt(AgentSurfaceTest):
    def text(self) -> str:
        return self.client.get("/llms.txt").text

    def test_is_written_in_the_same_language_as_the_rest_of_the_site(self) -> None:
        """Todo o produto é pt-BR; o llms.txt estava em inglês."""
        self.assertIn("Regras de interpretação", self.text())

    def test_explains_the_two_temporal_fields(self) -> None:
        text = self.text()
        self.assertIn("recencia", text)
        self.assertIn("fonte", text)

    def test_warns_that_a_live_signal_may_come_from_an_old_source(self) -> None:
        self.assertIn("fonte.ano", self.text())

    def test_declares_the_anonymous_ceiling(self) -> None:
        self.assertIn("limite", self.text().lower())

    def test_bairro_support_is_not_understated(self) -> None:
        """Dizia suportar apenas São Paulo; o código suporta quatro cidades."""
        text = self.text()
        self.assertIn("RJ", text)
        self.assertIn("PE", text)


class TestMetadataEndpoint(AgentSurfaceTest):
    def test_metadata_exposes_both_clocks(self) -> None:
        data = self.client.get("/v1/metadata").json()
        self.assertIn("recencia", data)
        self.assertIn("carga", data)

    def test_health_reports_load_age(self) -> None:
        data = self.client.get("/health").json()
        self.assertIn("carga", data)


if __name__ == "__main__":
    unittest.main()
