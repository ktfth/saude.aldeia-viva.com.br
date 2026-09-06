"""
População municipal estimada pelo IBGE.

O lookup de municípios (`municipality_lookup.py`) usa a API de localidades,
que traz nome e UF mas não traz população. Sem denominador,
`domain/rates.py` nunca produziu um número: medido no relatório real, 0 de
5.339 municípios tinham população, e ordenar por `risk_score` era
aproximadamente ordenar por tamanho de cidade.

Esta fonte é a tabela 6579 do SIDRA (estimativas populacionais), variável
9324, no nível N6 (município). Uma requisição, ~673 KB, 5.571 municípios.
"""

import gzip
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

IBGE_POPULATION_URL = (
    "https://servicodados.ibge.gov.br/api/v3/agregados/6579"
    "/periodos/-1/variaveis/9324?localidades=N6[all]"
)

CACHE_FILENAME = "ibge_population.json"


def parse_population_payload(payload: Any) -> dict[str, int]:
    """Extrai `{código de 6 dígitos: população}` da resposta do SIDRA.

    O código municipal do SINAN tem 6 dígitos; o do IBGE tem 7 (o último é
    verificador). A chave é truncada para casar com o resto do projeto.
    """
    populations: dict[str, int] = {}
    for aggregate in payload or []:
        for result in aggregate.get("resultados") or []:
            for entry in result.get("series") or []:
                code = str((entry.get("localidade") or {}).get("id") or "").strip()
                if len(code) < 6:
                    continue
                series = entry.get("serie") or {}
                if not series:
                    continue
                latest_year = max(series)
                raw = str(series[latest_year]).strip()
                if not raw.isdigit():
                    continue
                populations[code[:6]] = int(raw)
    return populations


def _cache_path() -> Path:
    from app import SINAN_CACHE_DIR

    return SINAN_CACHE_DIR / CACHE_FILENAME


def _load_from_cache() -> dict[str, int]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): int(value) for key, value in data.items()}
    except Exception as error:
        logger.warning("Falha ao ler cache de população: %s", error)
        return {}


def _save_to_cache(populations: Mapping[str, int]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(populations, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("População cacheada em disco (%d municípios)", len(populations))
    except Exception as error:
        logger.warning("Falha ao gravar cache de população: %s", error)


def load_population_lookup(force_refresh: bool = False) -> dict[str, int]:
    """População por município, com cache em disco e degradação elegante.

    Nunca levanta: se a fonte falhar, devolve o cache; se não houver cache,
    devolve vazio e o consumidor publica a taxa como indisponível — o que é
    honesto e é exatamente o que a interface sabe representar.
    """
    if not force_refresh:
        cached = _load_from_cache()
        if cached:
            return cached

    from app import REQUEST_TIMEOUT_SECONDS

    logger.info("Buscando estimativas populacionais do IBGE...")
    try:
        request = urllib.request.Request(
            IBGE_POPULATION_URL, headers={"User-Agent": "aldeia-viva-saude/1.0"}
        )
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
    except Exception as error:
        logger.warning("Falha ao buscar população no IBGE: %s", error)
        return _load_from_cache()

    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as error:
        logger.error("Falha ao interpretar resposta de população do IBGE: %s", error)
        return _load_from_cache()

    populations = parse_population_payload(payload)
    if populations:
        _save_to_cache(populations)
        logger.info("População carregada para %d municípios", len(populations))
    else:
        logger.warning("IBGE não devolveu população utilizável")
    return populations
