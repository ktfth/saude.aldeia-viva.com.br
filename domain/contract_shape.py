"""Forma da resposta pública: o que um integrador de fato consome.

O contrato para quem embarca esta API em outro software não é o
`/openapi.json`. Medido: os corpos de resposta chegam ao schema como
`schema: {}` — o FastAPI não tem tipo declarado para eles. O que o integrador
lê no código dele são os NOMES DOS CAMPOS, e nada os protegia.

Superfície medida em 2026-09-06, oito endpoints públicos: 1.232 caminhos de
campo. Renomear ou remover qualquer um quebra silenciosamente quem já
integrou — e nada falharia aqui.

Parte desses caminhos é chaveada por dado, não por esquema:

    metadata.formulas_por_doenca.DENG        código de agravo
    metadata.recencia.fontes_por_ano.2022    ano da fonte
    ...por_agravo.CHIK                       código de agravo

Enumerá-los faria o contrato quebrar sempre que a base mudasse — falso
positivo que treina qualquer um a ignorar a rede. Aqui essas chaves viram
`{chave}`, o que reduz 1.232 para 754 caminhos estáveis e mantém a estrutura
sob vigilância.
"""

import re
from typing import Any, Iterable, Mapping

# As rotas sob contrato, declaradas uma vez. O gravador e a rede leem daqui:
# duas listas divergindo fariam a rede vigiar um conjunto e o arquivo gravado
# descrever outro, sem nada acusar.
#
# Os parâmetros existem para a resposta trazer um item — a forma dos itens é
# o que o integrador consome, e uma lista vazia não a revela.
PUBLIC_ROUTES = (
    "/health",
    "/agent.json",
    "/v1/metadata",
    "/v1/diseases",
    "/v1/bairros",
    "/v1/risk-index?limite=1",
    "/v1/high-alerts?limite=1",
    "/v1/professional-report?estado=SP",
)


# Substitui chaves que são dado, e não nome de campo.
DATA_KEY = "{chave}"

# Profundidade além da qual a estrutura deixa de ser contrato e vira detalhe.
# Seis níveis cobrem `municipios[].doencas[].composicao.ressalva` com folga.
MAX_DEPTH = 6

_YEAR = re.compile(r"\d{4}")
_IBGE = re.compile(r"\d{6,7}")
_UF = re.compile(r"[A-Z]{2}")


def is_data_key(key: str, disease_codes: Iterable[str] = ()) -> bool:
    """A chave identifica um dado, e não um campo do contrato?"""
    if key in set(disease_codes):
        return True
    return bool(
        _YEAR.fullmatch(key) or _IBGE.fullmatch(key) or _UF.fullmatch(key)
    )


def field_paths(
    payload: Any, disease_codes: Iterable[str] = (), _prefix: str = "",
    _out: set[str] | None = None, _depth: int = 0,
) -> set[str]:
    """Caminhos de campo de uma resposta, com chaves de dado normalizadas.

    Listas colapsam em `[]`: um integrador depende da forma dos itens, não
    de quantos vieram.
    """
    out = _out if _out is not None else set()
    if _depth > MAX_DEPTH:
        return out

    if isinstance(payload, Mapping):
        codes = set(disease_codes)
        for key, value in payload.items():
            name = DATA_KEY if is_data_key(str(key), codes) else str(key)
            path = f"{_prefix}.{name}" if _prefix else name
            out.add(path)
            field_paths(value, codes, path, out, _depth + 1)
    elif isinstance(payload, list) and payload:
        field_paths(payload[0], disease_codes, _prefix + "[]", out, _depth + 1)

    return out


def breaking_changes(
    recorded: Mapping[str, Iterable[str]], current: Mapping[str, Iterable[str]]
) -> dict[str, list[str]]:
    """O que sumiu do contrato — endpoints e campos.

    Acrescentar não quebra ninguém: um cliente que não conhece o campo novo
    segue funcionando. Remover ou renomear quebra. A assimetria é o ponto.
    """
    perdas: dict[str, list[str]] = {}
    for rota, campos in recorded.items():
        if rota not in current:
            perdas[rota] = ["<endpoint inteiro desapareceu>"]
            continue
        faltando = sorted(set(campos) - set(current[rota]))
        if faltando:
            perdas[rota] = faltando
    return perdas
