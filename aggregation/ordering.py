"""
Ordenação do índice de risco.

`risk_score` é soma ponderada de contagens absolutas. Medido no relatório
real, ele tem correlação de Pearson de 0,822 com a população municipal:
ordenar por score é, em boa medida, ordenar por tamanho de cidade. São Paulo
liderava com 86 casos por 100 mil enquanto Sete Quedas/MS, com 6.612 por 100
mil, não aparecia — onze dos quinze municípios de maior taxa estavam fora do
top 50 por contagem.

Este módulo existe para que a taxa seja uma ordenação de primeira classe, e
não uma anotação decorativa ao lado de um ranking que ignora o denominador.
"""

from typing import Any, Iterable, Mapping

ORDERINGS = ("score", "taxa", "taxa_atual", "casos", "obitos")

DEFAULT_ORDERING = "score"


def _current_rate_key(row: Mapping[str, Any]) -> tuple[int, float]:
    """Ordena pela taxa que vem de arquivo do ano corrente.

    `taxa` soma todos os anos-fonte. Para decidir onde atuar AGORA, o que
    importa é a parte da taxa que descreve agora — medido, 16% dos casos
    prováveis do país vêm de fonte anterior, e há município cuja taxa inteira
    vem dela.
    """
    incidence = row.get("incidencia") or {}
    rate = incidence.get("por_100k_fonte_atual")
    if rate is None:
        return (0, 0.0)
    return (2 if incidence.get("confiavel") else 1, float(rate))


def _rate_key(row: Mapping[str, Any]) -> tuple[int, float]:
    """Taxa confiável primeiro, depois taxa marcada, depois sem taxa.

    Uma taxa de 9.000 por 100 mil que vem de 5 casos numa população de 800
    habitantes é aritmeticamente correta e operacionalmente inútil. Ela
    continua sendo publicada — mas não encabeça o painel.
    """
    incidence = row.get("incidencia") or {}
    rate = incidence.get("por_100k")
    if rate is None:
        return (0, 0.0)
    return (2 if incidence.get("confiavel") else 1, float(rate))


def _number(row: Mapping[str, Any], field: str) -> float:
    try:
        return float(row.get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


_KEYS = {
    "score": lambda row: (1, _number(row, "risk_score")),
    "casos": lambda row: (1, _number(row, "total_casos_provaveis")),
    "obitos": lambda row: (1, _number(row, "total_obitos")),
    "taxa": _rate_key,
    "taxa_atual": _current_rate_key,
}


def sort_municipalities(
    rows: Iterable[Mapping[str, Any]], ordering: str | None
) -> list[Mapping[str, Any]]:
    """Nova lista ordenada. Nunca muta a entrada."""
    key = _KEYS.get(ordering or DEFAULT_ORDERING, _KEYS[DEFAULT_ORDERING])
    return sorted(rows, key=key, reverse=True)
