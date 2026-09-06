"""
Toda página pública precisa renderizar.

Três vezes nesta série de iterações um símbolo ficou para trás num refactor e
só apareceu quando o caminho foi exercitado: `load_latest_available_records`
sumiu do `fetch_epidemiology_report`, os estilos de `.link-card` não foram
portados na extração do CSS, e `ambiguous_locality_names` foi usado em
`render_agents_page` antes de ser importado.

Um teste por página, escrito à mão, não protege a próxima página. Este
percorre todas as rotas HTML declaradas e falha se qualquer uma quebrar.
"""

import unittest

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report

# Rotas que devolvem HTML para humanos.
HTML_ROUTES = ("/", "/dashboard", "/sobre", "/planos", "/agentes")

# Rotas legíveis por máquina, que também não podem quebrar.
MACHINE_ROUTES = (
    "/agent.json",
    "/llms.txt",
    "/robots.txt",
    "/sitemap.xml",
    "/health",
    "/v1/metadata",
    "/v1/diseases",
    "/v1/bairros",
    "/v1/risk-index?limite=1",
    "/v1/high-alerts?limite=1",
    "/openapi.json",
)


class TestEveryPageRenders(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_html_routes_return_a_complete_document(self) -> None:
        for route in HTML_ROUTES:
            with self.subTest(route=route):
                app.rate_limiter.reset()
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, route)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertIn("<!doctype html>", response.text.lower())
                self.assertIn("</html>", response.text)
                self.assertIn('id="conteudo-principal"', response.text)

    def test_machine_routes_respond(self) -> None:
        for route in MACHINE_ROUTES:
            with self.subTest(route=route):
                app.rate_limiter.reset()
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, route)
                self.assertTrue(response.content, f"{route} devolveu corpo vazio")

    def test_no_page_leaks_an_unrendered_placeholder(self) -> None:
        """Uma f-string que escapou fica visível na página como texto cru."""
        for route in HTML_ROUTES:
            with self.subTest(route=route):
                app.rate_limiter.reset()
                text = self.client.get(route).text
                for suspeito in ("{render_", "{escape_html(", "{format_", "None</"):
                    self.assertNotIn(suspeito, text, f"{route} vazou {suspeito!r}")

    def test_no_page_carries_inline_styles(self) -> None:
        """Cor escrita à mão no HTML fura o sistema de tokens.

        O painel e /planos já foram limpos; /agentes tinha dez, com cores da
        paleta slate que não pertencem ao projeto.
        """
        for route in HTML_ROUTES:
            with self.subTest(route=route):
                app.rate_limiter.reset()
                self.assertNotIn('style="', self.client.get(route).text)


if __name__ == "__main__":
    unittest.main()
