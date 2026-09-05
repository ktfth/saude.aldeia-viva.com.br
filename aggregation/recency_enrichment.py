"""
Aplica a recência do sinal a um relatório epidemiológico inteiro.

Roda uma única vez, no momento em que o relatório entra em memória
(`apply_report_state`), de modo que funciona igual para as três origens
possíveis do dado: carga nova, cache em disco e snapshot embarcado.

O custo é O(municípios × agravos) uma vez por carga, não por request.
"""

from datetime import date
from typing import Any, Iterable, Mapping

from domain.recency import (
    FRESHNESS_ORDER,
    LIVE,
    UNKNOWN,
    describe_signal,
    freshest_date,
    parse_signal_date,
    signal_age_days,
    with_recency,
)

DISEASE_COLLECTIONS = ("doencas", "doencas_altas")

# Acima disso a carga deixa de descrever o presente e passa a ser histórico.
LOAD_FRESH_MAX_DAYS = 7


def enrich_alert(alert: Mapping[str, Any], reference: date) -> dict[str, Any]:
    """Alerta com bloco de recência. Não muta a entrada."""
    return with_recency(alert, reference)


def enrich_municipality(
    municipality: Mapping[str, Any], reference: date
) -> dict[str, Any]:
    """Município e todos os seus agravos com recência.

    O município herda o frescor do seu agravo mais vivo: é esse sinal que
    determina se há algo a fazer hoje naquele território.
    """
    enriched = dict(municipality)

    for collection in DISEASE_COLLECTIONS:
        items = municipality.get(collection) or []
        enriched[collection] = [with_recency(item, reference) for item in items]

    diseases = enriched.get("doencas") or []
    signal_dates = [disease["recencia"]["data"] for disease in diseases]
    enriched["recencia"] = describe_signal(freshest_date(signal_dates), reference)

    enriched["agravos_total"] = len(diseases)
    enriched["agravos_com_sinal_vivo"] = sum(
        1 for disease in diseases if disease["recencia"]["frescor"] == LIVE
    )
    return enriched


def freshness_distribution(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Contagem por faixa de frescor, sempre com todas as faixas presentes."""
    counts = {level: 0 for level in FRESHNESS_ORDER}
    for item in items:
        level = (item.get("recencia") or {}).get("frescor", UNKNOWN)
        counts[level] = counts.get(level, 0) + 1
    return counts


def data_horizon(report: Mapping[str, Any]) -> date | None:
    """Data da notificação mais recente presente no relatório.

    É o limite do que o sistema sabe. Serve de referência para a idade do
    sinal: um agravo que notificou até esse limite estava vivo no momento em
    que a carga foi feita, por mais antiga que a carga seja.
    """
    candidates: list[Any] = []
    for municipality in report.get("municipios") or []:
        for collection in DISEASE_COLLECTIONS:
            for disease in municipality.get(collection) or []:
                candidates.append(disease.get("ultima_notificacao"))
                candidates.append(disease.get("ultimo_inicio_sintomas"))
    for alert in report.get("alertas_altos") or []:
        candidates.append(alert.get("ultima_notificacao"))
    return parse_signal_date(freshest_date(candidates))


def describe_load(metadata: Mapping[str, Any], horizon: date | None, today: date) -> dict[str, Any]:
    """Idade da CARGA — quanto tempo faz que o serviço buscou dados novos.

    É uma falha operacional do serviço, não um fato epidemiológico. Mantê-la
    separada da recência do sinal impede que "o pipeline parou" seja lido como
    "os municípios pararam de adoecer".
    """
    loaded_at = parse_signal_date(metadata.get("carregado_em"))
    age = signal_age_days(loaded_at, today) if loaded_at else None
    lag = (today - horizon).days if horizon else None
    return {
        "carregado_em": loaded_at.isoformat() if loaded_at else None,
        "idade_dias": age,
        "horizonte_dado": horizon.isoformat() if horizon else None,
        "defasagem_dias": max(0, lag) if lag is not None else None,
        "atualizada": age is not None and age <= LOAD_FRESH_MAX_DAYS,
        "referencia": today.isoformat(),
        "aviso": (
            None
            if age is not None and age <= LOAD_FRESH_MAX_DAYS
            else "A carga não é atualizada há mais tempo que o recomendado; "
                 "os números descrevem o momento da carga, não hoje."
        ),
    }


def enrich_report(
    report: Mapping[str, Any],
    reference: date,
    *,
    horizon: date | None = None,
) -> dict[str, Any]:
    """Relatório com os dois relógios explícitos.

    - A idade do SINAL é medida contra o horizonte do dado (`horizon`).
    - A idade da CARGA é medida contra hoje (`reference`).

    Sem essa separação, um pipeline parado faz 5.339 municípios parecerem
    epidemiologicamente adormecidos.
    """
    effective_horizon = horizon or data_horizon(report) or reference

    municipalities = [
        enrich_municipality(item, effective_horizon)
        for item in (report.get("municipios") or [])
    ]
    alerts = [
        enrich_alert(item, effective_horizon)
        for item in (report.get("alertas_altos") or [])
    ]

    alert_distribution = freshness_distribution(alerts)
    trustworthy = sum(
        1 for alert in alerts if alert["recencia"]["confiavel_como_atual"]
    )

    detected_horizon = horizon or data_horizon(report)

    metadata = dict(report.get("metadata") or {})
    metadata["recencia"] = {
        "referencia": effective_horizon.isoformat(),
        "horizonte_dado": detected_horizon.isoformat() if detected_horizon else None,
        "medido_contra": "horizonte do dado",
        "alertas_total": len(alerts),
        "alertas": alert_distribution,
        "alertas_confiaveis_como_atuais": trustworthy,
        "municipios": freshness_distribution(municipalities),
        "definicao": {
            "vivo": "notificou até 30 dias antes do fim da carga",
            "esfriando": "entre 31 e 90 dias antes do fim da carga",
            "dormente": "entre 91 e 365 dias antes do fim da carga",
            "fossil": "mais de 365 dias antes do fim da carga — não descreve a situação atual",
            "desconhecido": "sem data de notificação utilizável",
        },
    }
    metadata["carga"] = describe_load(metadata, detected_horizon, reference)

    enriched = dict(report)
    enriched["municipios"] = municipalities
    enriched["alertas_altos"] = alerts
    enriched["metadata"] = metadata
    return enriched
