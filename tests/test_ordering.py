"""
Ordenação do índice de risco.

Medido no relatório real (5.331 municípios com denominador): a correlação de
Pearson entre `risk_score` e população é 0,822 e a de postos é 0,658. Ordenar
por score é, em boa medida, ordenar por tamanho de cidade — e o produto
apresentava isso como priorização de risco.

O efeito concreto: São Paulo aparecia em 1º com 86 casos por 100 mil,
enquanto Sete Quedas/MS, com 6.612 por 100 mil (6,6% da população notificada),
não aparecia em lugar nenhum. Onze dos quinze municípios de maior taxa não
estavam no top 50 por contagem.

Mostrar a taxa sem poder ordenar por ela não mudaria decisão alguma.
"""

import unittest

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report
from aggregation.ordering import ORDERINGS, sort_municipalities


def _rows():
    return [
        {
            "codigo_municipio": "355030",
            "municipio": "São Paulo",
            "risk_score": 31948.0,
            "total_casos_provaveis": 10225,
            "total_obitos": 175,
            "incidencia": {"por_100k": 85.84, "confiavel": True},
        },
        {
            "codigo_municipio": "500627",
            "municipio": "Sete Quedas",
            "risk_score": 900.0,
            "total_casos_provaveis": 750,
            "total_obitos": 0,
            "incidencia": {"por_100k": 6612.0, "confiavel": True},
        },
        {
            "codigo_municipio": "999999",
            "municipio": "Vilarejo",
            "risk_score": 10.0,
            "total_casos_provaveis": 5,
            "total_obitos": 1,
            "incidencia": {"por_100k": 9000.0, "confiavel": False},
        },
    ]


class TestSortMunicipalities(unittest.TestCase):
    def test_default_is_score(self) -> None:
        got = sort_municipalities(_rows(), None)
        self.assertEqual(got[0]["municipio"], "São Paulo")

    def test_unknown_key_falls_back_to_score(self) -> None:
        got = sort_municipalities(_rows(), "inexistente")
        self.assertEqual(got[0]["municipio"], "São Paulo")

    def test_by_rate_promotes_the_small_town_with_a_real_outbreak(self) -> None:
        got = sort_municipalities(_rows(), "taxa")
        self.assertEqual(got[0]["municipio"], "Sete Quedas")

    def test_by_rate_ranks_unreliable_rates_below_reliable_ones(self) -> None:
        """Uma taxa de 9.000 por 100 mil vinda de 5 casos numa população de
        800 habitantes não pode encabeçar o painel."""
        got = sort_municipalities(_rows(), "taxa")
        self.assertEqual(got[-1]["municipio"], "Vilarejo")

    def test_by_cases(self) -> None:
        got = sort_municipalities(_rows(), "casos")
        self.assertEqual(got[0]["municipio"], "São Paulo")

    def test_by_deaths(self) -> None:
        got = sort_municipalities(_rows(), "obitos")
        self.assertEqual(got[0]["municipio"], "São Paulo")

    def test_does_not_mutate_the_input(self) -> None:
        rows = _rows()
        original = [row["municipio"] for row in rows]
        sort_municipalities(rows, "taxa")
        self.assertEqual([row["municipio"] for row in rows], original)

    def test_rows_without_the_incidence_block_do_not_break(self) -> None:
        rows = _rows() + [{"municipio": "Antigo", "risk_score": 1.0}]
        got = sort_municipalities(rows, "taxa")
        self.assertEqual(got[-1]["municipio"], "Antigo")

    def test_every_declared_ordering_has_a_key(self) -> None:
        """A invariante real, no lugar da lista escrita à mão.

        A versão anterior enumerava os nomes e reprovava a cada ordenação
        nova — sem verificar o que importa: que `ORDERINGS`, que o endpoint
        aceita, e `_KEYS`, que ordena de fato, digam a mesma coisa. Divergir
        aqui faria o endpoint aceitar um nome e ordenar por outro critério,
        em silêncio.
        """
        from aggregation.ordering import _KEYS

        self.assertEqual(set(ORDERINGS), set(_KEYS))

    def test_ordering_by_current_source_rate_exists(self) -> None:
        """`taxa` soma todos os anos-fonte; `taxa_atual` só o corrente."""
        self.assertIn("taxa_atual", ORDERINGS)

    def test_current_rate_ordering_ignores_historical_only_rows(self) -> None:
        """Município cuja taxa inteira vem de fonte antiga não encabeça."""
        rows = [
            {
                "municipio": "So historico",
                "incidencia": {
                    "por_100k": 3727.0,
                    "por_100k_fonte_atual": None,
                    "confiavel": True,
                },
            },
            {
                "municipio": "Atual",
                "incidencia": {
                    "por_100k": 500.0,
                    "por_100k_fonte_atual": 500.0,
                    "confiavel": True,
                },
            },
        ]
        got = sort_municipalities(rows, "taxa_atual")
        self.assertEqual(got[0]["municipio"], "Atual")
        # E pela taxa total a ordem se inverte, que é o ponto de haver duas.
        self.assertEqual(
            sort_municipalities(rows, "taxa")[0]["municipio"], "So historico"
        )


class TestOEndpointAceitaTodaOrdenacaoDeclarada(unittest.TestCase):
    """A terceira autoridade, que a versão anterior desta rede não cobria.

    O endpoint validava `ordenar` com um padrão cravado à mão —
    `^(score|taxa|casos|obitos)$`. Acrescentar `taxa_atual` ao domínio e ao
    `_KEYS` deixou o serviço sabendo ordenar por ela e recusando o pedido com
    422. Descoberto EM PRODUÇÃO, depois de deployar: a rede conferia
    `ORDERINGS` contra `_KEYS` e parava aí.

    Aqui a verificação é de comportamento, e não de introspecção do
    validador: o que importa é a requisição ser aceita, não como o padrão
    está representado por dentro.
    """

    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_nenhuma_ordenacao_declarada_e_recusada(self) -> None:
        recusadas = {}
        for nome in ORDERINGS:
            app.rate_limiter.reset()
            resposta = self.client.get(f"/v1/risk-index?limite=1&ordenar={nome}")
            if resposta.status_code >= 400:
                recusadas[nome] = resposta.status_code
        self.assertEqual(
            recusadas,
            {},
            "ordenação que o serviço sabe fazer e o endpoint recusa",
        )

    def test_ordenacao_desconhecida_continua_recusada(self) -> None:
        """Derivar o padrão não pode ter aberto o parâmetro para qualquer coisa."""
        app.rate_limiter.reset()
        resposta = self.client.get("/v1/risk-index?limite=1&ordenar=inventada")
        self.assertEqual(resposta.status_code, 422)


class TestOrderingEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_accepts_the_ordenar_parameter(self) -> None:
        response = self.client.get("/v1/risk-index?ordenar=taxa&limite=5")
        self.assertEqual(response.status_code, 200)

    def test_rejects_an_unknown_ordering_explicitly(self) -> None:
        """Erro legível por máquina, não fallback silencioso."""
        response = self.client.get("/v1/risk-index?ordenar=magica")
        self.assertEqual(response.status_code, 422)

    def test_ordering_by_rate_changes_the_first_result(self) -> None:
        por_score = self.client.get("/v1/risk-index?limite=5").json()
        por_taxa = self.client.get("/v1/risk-index?ordenar=taxa&limite=5").json()
        if isinstance(por_score, list) and isinstance(por_taxa, list) and por_score and por_taxa:
            self.assertNotEqual(
                por_score[0]["codigo_municipio"], por_taxa[0]["codigo_municipio"]
            )

    def test_manifest_declares_the_parameter(self) -> None:
        manifest = self.client.get("/agent.json").json()
        self.assertIn("ordenar", manifest["endpoints"]["/v1/risk-index"]["query"])


if __name__ == "__main__":
    unittest.main()
