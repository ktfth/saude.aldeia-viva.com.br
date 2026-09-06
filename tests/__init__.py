"""
Configuração da suíte.

A cache agregada passou a ter prazo de validade, e a cache real do
repositório está vencida — sem isto, cada `TestClient` que sobe o lifespan
tentaria baixar as fontes do SINAN de verdade. A suíte levou 83 segundos e
passou a depender de rede antes desta linha existir.

`0` desliga a expiração. A política de validade em si é exercitada
explicitamente em `test_cache_freshness.py`, que passa o limite por
parâmetro em vez de depender do ambiente.
"""

import os

os.environ.setdefault("SINAN_REPORT_CACHE_MAX_AGE_DAYS", "0")


import contextlib


@contextlib.contextmanager
def preserved_report_state():
    """Restaura `db_clini`, `db_alertas` e `db_metadata` ao sair.

    `apply_report_state` reescreve globais do módulo `app`. Um teste que
    carrega um relatório de fixture com um município deixava esse estado para
    todos os testes seguintes — e qualquer teste que dependesse do volume
    real da base falhava dependendo da ORDEM de execução. Falha de isolamento
    é a pior categoria de teste ruim: passa isolada, quebra em conjunto, e a
    causa aparece longe do efeito.
    """
    import app

    snapshot = (list(app.db_clini), list(app.db_alertas), dict(app.db_metadata))
    try:
        yield
    finally:
        app.db_clini, app.db_alertas, app.db_metadata = (
            snapshot[0],
            snapshot[1],
            snapshot[2],
        )


class ApiTestCase:
    """Mixin para casos que fazem várias requisições anônimas.

    O tier anônimo permite 10 requisições por minuto. Uma suíte que exercita
    a API esbarra nesse limite de verdade, e o teste seguinte falha com 429 em
    vez de falhar pelo que estava verificando — ou pior, passa por acidente.
    """

    def setUp(self) -> None:  # noqa: N802 - contrato do unittest
        import app

        app.rate_limiter.reset()
        super().setUp()
