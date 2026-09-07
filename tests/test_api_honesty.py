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
from tests import PREMIUM_KEY, ensure_real_report


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


class TestOMetadataContinuaBarato(unittest.TestCase):
    """`/v1/metadata` existe para ser chamado antes de decidir buscar o resto.

    É o endpoint que um agente consulta para conferir a idade do dado. Ao
    acrescentar as curvas semanais ele passou de 9 KB para **248 KB**, e 90%
    disso eram as 27 curvas estaduais — que nada consumia e nada documentava.
    O endpoint barato tinha deixado de ser barato, e nada acusava.

    As curvas estaduais não foram descartadas: são exatas, custam 29 KB
    comprimidos no snapshot, e passaram a ser servidas em
    `/v1/professional-report?estado=XX`, onde alguém nomeia o estado.

    O teto abaixo não é uma preferência de estilo: é o que separa "consulto
    antes de decidir" de "consulto e já paguei o preço da decisão".
    """

    # Folga sobre os 25 KB atuais, e ainda uma ordem de grandeza abaixo do que
    # o endpoint chegou a pesar.
    TETO_KB = 60

    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_o_metadata_cabe_no_teto(self) -> None:
        corpo = self.client.get("/v1/metadata").content
        tamanho = len(corpo) / 1024
        self.assertLess(
            tamanho,
            self.TETO_KB,
            f"/v1/metadata está em {tamanho:.0f} KB; ele existe para ser "
            "consultado antes de buscar o resto",
        )

    def test_as_curvas_estaduais_nao_viajam_nele(self) -> None:
        curvas = self.client.get("/v1/metadata").json().get("curvas") or {}
        self.assertIn("nacional", curvas, "a curva nacional é o que ele deve trazer")
        self.assertNotIn("por_uf", curvas)

    def test_a_curva_estadual_existe_onde_se_pede_o_estado(self) -> None:
        """Remover de um lugar só vale se ela estiver disponível no outro."""
        app.rate_limiter.reset()
        resposta = self.client.get(
            "/v1/professional-report?estado=GO", headers={"X-API-Key": PREMIUM_KEY}
        )
        curva = (resposta.json().get("metadata") or {}).get("curva_do_estado")
        self.assertTrue(curva, "curva do estado ausente de onde ela deveria estar")
        self.assertIn("DENG", curva)


if __name__ == "__main__":
    unittest.main()
