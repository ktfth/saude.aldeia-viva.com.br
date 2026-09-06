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
