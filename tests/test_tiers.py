"""
Planos: a página comercial precisa dizer o que o código faz.

Divergências medidas antes desta mudança, todas com números escritos à mão
no HTML de `/planos`:

  - "Profissional ... 100 req/min" — o tier premium tem `rate_limit: 10000`.
    Cem vezes menos do que a realidade, numa página de preço.
  - "Gratuito ... 5 registros por busca, 10 req/min" — esses são os limites
    do tier ANÔNIMO. O tier `free`, que a própria página oferece logo abaixo
    ("ativamos chaves gratuitas para pesquisadores e ONGs"), tem 20 registros
    e 100 req/min. Duas coisas diferentes chamadas de "Gratuito".
  - "Sem limites: obtenha todos os municípios em uma única chamada" — falso
    em qualquer tier: `Query(le=1000)` limita a mil e existem 5.339
    municípios. Sem paginação, era impossível obter a base completa.

A correção é estrutural, não textual: uma declaração única de tiers passa a
ser a autoridade, e tanto a página quanto o manifesto quanto o enforcement
leem dela. Números escritos à mão no HTML não têm como divergir do código se
não existirem.
"""

import unittest

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report
from domain.tiers import TIERS, tier_by_code, tier_max_results


class TestTierDeclaration(unittest.TestCase):
    def test_declares_the_tiers_the_code_implements(self) -> None:
        self.assertEqual(
            {tier.codigo for tier in TIERS}, {"anonymous", "free", "premium"}
        )

    def test_every_tier_carries_the_numbers_the_page_needs(self) -> None:
        for tier in TIERS:
            self.assertIsInstance(tier.rate_limit, int)
            self.assertIsInstance(tier.nome, str)
            self.assertTrue(tier.nome)
            self.assertIsInstance(tier.preco, str)

    def test_anonymous_matches_what_get_api_user_grants(self) -> None:
        anonymous = tier_by_code("anonymous")
        self.assertEqual(anonymous.max_results, 5)
        self.assertEqual(anonymous.rate_limit, 10)

    def test_free_matches_the_key_shipped_in_users_json(self) -> None:
        import json
        from pathlib import Path

        keys = json.loads(app.api_key_manager.path.read_text(encoding="utf-8"))["keys"]
        free_key = next(v for v in keys.values() if v["tier"] == "free")
        self.assertEqual(tier_by_code("free").rate_limit, free_key["rate_limit"])

    def test_premium_matches_the_key_shipped_in_users_json(self) -> None:
        import json
        from pathlib import Path

        keys = json.loads(app.api_key_manager.path.read_text(encoding="utf-8"))["keys"]
        premium_key = next(v for v in keys.values() if v["tier"] == "premium")
        self.assertEqual(tier_by_code("premium").rate_limit, premium_key["rate_limit"])

    def test_max_results_lookup_matches_enforcement(self) -> None:
        self.assertEqual(tier_max_results("anonymous"), 5)
        self.assertEqual(tier_max_results("free"), 20)
        self.assertIsNone(tier_max_results("premium"))
        self.assertIsNone(tier_max_results("admin"))
        self.assertIsNone(tier_max_results("inexistente"))


class TestPlansPageReadsFromCode(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.html = self.client.get("/planos").text

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_shows_the_real_premium_rate_limit(self) -> None:
        """O cartão Profissional precisa trazer 10.000, não 100.

        "100 req/min" continua aparecendo na página — mas agora no cartão da
        chave gratuita, onde é verdade. A asserção olha o cartão certo.
        """
        import re

        cartao = re.search(
            r"<strong>Profissional</strong>.*?</div>", self.html, re.S
        )
        self.assertIsNotNone(cartao)
        self.assertIn("10.000 req/min", cartao.group(0))
        self.assertNotIn("100 req/min", cartao.group(0))

    def test_the_free_key_card_shows_its_own_real_limit(self) -> None:
        import re

        cartao = re.search(
            r"<strong>Chave gratuita</strong>.*?</div>", self.html, re.S
        )
        self.assertIsNotNone(cartao)
        self.assertIn("100 req/min", cartao.group(0))
        self.assertIn("20 registros", cartao.group(0))

    def test_distinguishes_anonymous_from_the_free_key(self) -> None:
        """Duas ofertas diferentes não podem se chamar a mesma coisa."""
        self.assertIn("Sem chave", self.html)
        self.assertIn("Chave gratuita", self.html)

    def test_does_not_promise_unlimited_results(self) -> None:
        self.assertNotIn("Sem limites", self.html)

    def test_states_the_real_per_call_ceiling_and_pagination(self) -> None:
        self.assertIn("1.000", self.html)
        self.assertIn("página", self.html.lower())

    def test_does_not_advertise_endpoints_that_do_not_exist(self) -> None:
        self.assertNotIn("dados brutos", self.html)


class TestPagination(unittest.TestCase):
    """Sem paginação, "acesso completo" era impossível de cumprir."""

    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_accepts_a_page_parameter(self) -> None:
        response = self.client.get("/v1/risk-index?pagina=2&limite=5")
        self.assertEqual(response.status_code, 200)

    def test_pages_do_not_overlap(self) -> None:
        first = self.client.get("/v1/risk-index?pagina=1&limite=5").json()
        second = self.client.get("/v1/risk-index?pagina=2&limite=5").json()
        if isinstance(first, list) and isinstance(second, list) and first and second:
            codes_first = {row["codigo_municipio"] for row in first}
            codes_second = {row["codigo_municipio"] for row in second}
            self.assertFalse(codes_first & codes_second)

    def test_declares_the_page_and_whether_more_exist(self) -> None:
        response = self.client.get("/v1/risk-index?pagina=1&limite=5")
        self.assertEqual(response.headers["X-Page"], "1")
        self.assertEqual(response.headers["X-Has-More"], "true")

    def test_last_page_says_there_is_no_more(self) -> None:
        response = self.client.get("/v1/risk-index?municipio=355030&limite=5")
        self.assertEqual(response.headers["X-Has-More"], "false")

    def test_page_beyond_the_end_is_empty_not_an_error(self) -> None:
        response = self.client.get("/v1/risk-index?pagina=99999&limite=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Returned-Results"], "0")

    def test_rejects_a_page_below_one(self) -> None:
        self.assertEqual(self.client.get("/v1/risk-index?pagina=0").status_code, 422)

    def test_manifest_declares_pagination(self) -> None:
        manifest = self.client.get("/agent.json").json()
        self.assertIn("pagina", manifest["endpoints"]["/v1/risk-index"]["query"])
        self.assertIn("X-Has-More", manifest["limits"]["response_headers"])


if __name__ == "__main__":
    unittest.main()
