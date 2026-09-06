"""
O corte de resultados precisa ser declarado, não sofrido em silêncio.

Medido antes desta mudança: o dashboard público pedia `limite=25`,
`get_api_user` rebaixava anônimo para 5 sem avisar, e a linha de status
exibia "5 município(s) retornado(s)" — de um total de 5.339. Nem o humano
nem um agente tinham como saber que aquilo era um teto e não um total.
"""

import unittest

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report


class TestResultCountHeaders(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_risk_index_declares_total_and_applied_limit(self) -> None:
        response = self.client.get("/v1/risk-index?limite=25")
        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Total-Results", response.headers)
        self.assertIn("X-Returned-Results", response.headers)
        self.assertIn("X-Limit-Applied", response.headers)

    def test_returned_count_matches_the_payload(self) -> None:
        response = self.client.get("/v1/risk-index?limite=25")
        payload = response.json()
        returned = int(response.headers["X-Returned-Results"])
        self.assertEqual(returned, len(payload) if isinstance(payload, list) else 0)

    def test_total_is_larger_than_the_page_when_truncated(self) -> None:
        response = self.client.get("/v1/risk-index?limite=25")
        total = int(response.headers["X-Total-Results"])
        returned = int(response.headers["X-Returned-Results"])
        self.assertGreaterEqual(total, returned)

    def test_applied_limit_reveals_the_tier_downgrade(self) -> None:
        """Anônimo pede 25 e recebe 5: o teto real precisa estar no header."""
        response = self.client.get("/v1/risk-index?limite=25")
        self.assertEqual(
            int(response.headers["X-Limit-Applied"]),
            len(response.json()) if isinstance(response.json(), list) else 5,
        )

    def test_high_alerts_declares_the_same_contract(self) -> None:
        response = self.client.get("/v1/high-alerts?limite=25")
        self.assertIn("X-Total-Results", response.headers)
        self.assertIn("X-Limit-Applied", response.headers)


class TestRefreshIsProtected(unittest.TestCase):
    """POST /v1/refresh reprocessa a carga inteira e estava sem autenticação
    e sem rate limit — o único endpoint pesado do serviço, aberto."""

    def setUp(self) -> None:
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_anonymous_cannot_trigger_a_full_reload(self) -> None:
        response = self.client.post("/v1/refresh")
        self.assertEqual(response.status_code, 403)

    def test_invalid_key_is_rejected(self) -> None:
        response = self.client.post("/v1/refresh", headers={"X-API-Key": "nope"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
