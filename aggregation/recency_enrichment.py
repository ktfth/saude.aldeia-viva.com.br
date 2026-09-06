"""
Aplica a recência do sinal a um relatório epidemiológico inteiro.

Roda uma única vez, quando o relatório entra em memória (`apply_report_state`),
de modo que funciona igual para as três origens do dado: carga nova, cache em
disco e snapshot embarcado. Custo O(municípios × agravos) por carga, não por
request.

TRÊS RELÓGIOS, deliberadamente separados — cada um responde a uma pergunta
diferente, e confundi-los produz diagnóstico falso:

  1. CARGA  (`metadata.carga`)   — há quanto tempo o serviço buscou dados?
                                   Falha operacional do serviço.
  2. FONTE  (`doenca.fonte`)     — de que ano é o arquivo que temos deste
                                   agravo? Cobertura do acervo.
  3. SINAL  (`doenca.recencia`)  — dentro do que temos deste agravo, até
                                   quando este município notificou? Único dos
                                   três que é fato epidemiológico sobre o
                                   município.

Medido no relatório real: o arquivo rotulado "2026" reúne Dengue de 2026,
Leptospirose de 2024, Hanseníase de 2023 e Meningite de 2022. Medir o sinal
contra um horizonte único fazia 100% da Meningite parecer fóssil e 100% da
Dengue parecer viva — o que descreve qual arquivo foi baixado, não o
comportamento de município algum.
"""

from datetime import date
from typing import Any, Iterable, Mapping

from domain.recency import (
    FRESHNESS_ORDER,
    LIVE,
    UNKNOWN,
    describe_signal,
    freshest_date,
    humanize_age,
    parse_signal_date,
    signal_age_days,
    with_recency,
)

DISEASE_COLLECTIONS = ("doencas", "doencas_altas")

# Acima disso a carga deixa de descrever o presente e passa a ser histórico.
LOAD_FRESH_MAX_DAYS = 7

DISEASE_CODE_FIELDS = ("codigo", "codigo_doenca")


def disease_code(record: Mapping[str, Any]) -> str | None:
    """Código do agravo, tolerando os dois nomes usados no projeto."""
    for field in DISEASE_CODE_FIELDS:
        value = record.get(field)
        if value:
            return str(value)
    return None


def disease_horizons(report: Mapping[str, Any]) -> dict[str, date]:
    """Horizonte de cada agravo: a notificação mais recente que existe dele.

    É o limite do que o sistema sabe sobre aquele agravo. Medir um município
    contra esse limite responde "ele notificou até o fim do que temos?" em vez
    de "o arquivo dele é velho?".
    """
    candidates: dict[str, list[Any]] = {}

    def collect(record: Mapping[str, Any], *fields: str) -> None:
        code = disease_code(record)
        if not code:
            return
        bucket = candidates.setdefault(code, [])
        bucket.extend(record.get(field) for field in fields)

    for municipality in report.get("municipios") or []:
        for collection in DISEASE_COLLECTIONS:
            for disease in municipality.get(collection) or []:
                collect(disease, "ultima_notificacao", "ultimo_inicio_sintomas")
    for alert in report.get("alertas_altos") or []:
        collect(alert, "ultima_notificacao")

    horizons: dict[str, date] = {}
    for code, values in candidates.items():
        parsed = parse_signal_date(freshest_date(values))
        if parsed:
            horizons[code] = parsed
    return horizons


def describe_source(horizon: date | None, today: date) -> dict[str, Any]:
    """Idade do arquivo-fonte de um agravo. Cobertura, não epidemiologia."""
    if horizon is None:
        return {
            "ano": None,
            "horizonte": None,
            "idade_dias": None,
            "rotulo": "sem fonte",
            "do_ano_corrente": False,
        }
    age = signal_age_days(horizon, today)
    return {
        "ano": horizon.year,
        "horizonte": horizon.isoformat(),
        "idade_dias": age,
        "rotulo": humanize_age(age),
        "do_ano_corrente": horizon.year == today.year,
    }


def enrich_disease(
    disease: Mapping[str, Any],
    horizons: Mapping[str, date],
    today: date,
    fallback_horizon: date,
) -> dict[str, Any]:
    """Agravo com os relógios de FONTE e de SINAL, cada um no seu lugar."""
    code = disease_code(disease)
    horizon = horizons.get(code) if code else None
    enriched = with_recency(disease, horizon or fallback_horizon)
    enriched["fonte"] = describe_source(horizon, today)
    return enriched


def enrich_alert(
    alert: Mapping[str, Any],
    horizons: Mapping[str, date],
    today: date,
    fallback_horizon: date,
) -> dict[str, Any]:
    """Alerta com os mesmos dois relógios de um agravo."""
    return enrich_disease(alert, horizons, today, fallback_horizon)


def _freshest_level(levels: Iterable[str]) -> str:
    seen = {level for level in levels if level in FRESHNESS_ORDER}
    for level in FRESHNESS_ORDER:
        if level in seen:
            return level
    return UNKNOWN


def enrich_municipality(
    municipality: Mapping[str, Any],
    horizons: Mapping[str, date],
    today: date,
    fallback_horizon: date,
) -> dict[str, Any]:
    """Município e seus agravos, cada agravo no seu próprio relógio.

    O município herda o frescor do agravo mais vivo — é esse sinal que aciona.
    E declara quantos dos seus agravos têm fonte do ano corrente, que é a
    medida honesta de quanto do painel dele descreve o presente.
    """
    enriched = dict(municipality)
    for collection in DISEASE_COLLECTIONS:
        enriched[collection] = [
            enrich_disease(item, horizons, today, fallback_horizon)
            for item in (municipality.get(collection) or [])
        ]

    diseases = enriched.get("doencas") or []
    enriched["recencia"] = describe_signal(
        freshest_date(item["recencia"]["data"] for item in diseases), fallback_horizon
    )
    if diseases:
        enriched["recencia"]["frescor"] = _freshest_level(
            item["recencia"]["frescor"] for item in diseases
        )
    enriched["agravos_total"] = len(diseases)
    enriched["agravos_com_sinal_vivo"] = sum(
        1 for item in diseases if item["recencia"]["frescor"] == LIVE
    )
    enriched["agravos_com_fonte_atual"] = sum(
        1 for item in diseases if item["fonte"]["do_ano_corrente"]
    )
    return enriched


def freshness_distribution(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Contagem por faixa de frescor, sempre com todas as faixas presentes."""
    counts = {level: 0 for level in FRESHNESS_ORDER}
    for item in items:
        level = (item.get("recencia") or {}).get("frescor", UNKNOWN)
        counts[level] = counts.get(level, 0) + 1
    return counts


def describe_load(
    metadata: Mapping[str, Any], horizon: date | None, today: date
) -> dict[str, Any]:
    """Idade da CARGA — há quanto tempo o serviço buscou dados novos.

    Falha operacional do serviço, não fato epidemiológico. Separá-la impede
    que "o pipeline parou" seja lido como "os municípios pararam de adoecer".
    """
    loaded_at = parse_signal_date(metadata.get("carregado_em"))
    age = signal_age_days(loaded_at, today) if loaded_at else None
    lag = (today - horizon).days if horizon else None
    fresh = age is not None and age <= LOAD_FRESH_MAX_DAYS
    return {
        "carregado_em": loaded_at.isoformat() if loaded_at else None,
        "idade_dias": age,
        "horizonte_dado": horizon.isoformat() if horizon else None,
        "defasagem_dias": max(0, lag) if lag is not None else None,
        "atualizada": fresh,
        "referencia": today.isoformat(),
        "aviso": None
        if fresh
        else (
            "A carga não é atualizada há mais tempo que o recomendado; "
            "os números descrevem o momento da carga, não hoje."
        ),
    }


def _source_year_histogram(sources: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    years: dict[str, int] = {}
    for source in sources.values():
        if source["ano"] is not None:
            key = str(source["ano"])
            years[key] = years.get(key, 0) + 1
    return years


def enrich_report(
    report: Mapping[str, Any],
    reference: date,
    *,
    horizon: date | None = None,
) -> dict[str, Any]:
    """Relatório com os três relógios explícitos e separados."""
    horizons = disease_horizons(report)
    detected = max(horizons.values()) if horizons else None
    global_horizon = horizon or detected or reference

    municipalities = [
        enrich_municipality(item, horizons, reference, global_horizon)
        for item in (report.get("municipios") or [])
    ]
    alerts = [
        enrich_alert(item, horizons, reference, global_horizon)
        for item in (report.get("alertas_altos") or [])
    ]

    sources = {
        code: describe_source(value, reference) for code, value in horizons.items()
    }

    metadata = dict(report.get("metadata") or {})
    metadata["recencia"] = {
        "referencia": global_horizon.isoformat(),
        "horizonte_dado": detected.isoformat() if detected else None,
        "medido_contra": "horizonte da fonte de cada agravo",
        "alertas_total": len(alerts),
        "alertas": freshness_distribution(alerts),
        "alertas_confiaveis_como_atuais": sum(
            1 for alert in alerts if alert["recencia"]["confiavel_como_atual"]
        ),
        "municipios": freshness_distribution(municipalities),
        "agravos_total": len(sources),
        "agravos_com_fonte_do_ano_corrente": sum(
            1 for source in sources.values() if source["do_ano_corrente"]
        ),
        "fontes_por_ano": _source_year_histogram(sources),
        "fontes": sources,
        "definicao": {
            "vivo": "notificou até 30 dias antes do fim da fonte daquele agravo",
            "esfriando": "entre 31 e 90 dias antes do fim da fonte",
            "dormente": "entre 91 e 365 dias antes do fim da fonte",
            "fossil": "mais de 365 dias antes do fim da fonte",
            "desconhecido": "sem data de notificação utilizável",
            "aviso": (
                "Frescor é medido dentro da fonte de cada agravo. Um agravo "
                "vivo cuja fonte é de 2022 continua sendo dado de 2022 — "
                "consulte fonte.ano antes de tratar como situação atual."
            ),
        },
    }
    metadata["carga"] = describe_load(metadata, detected, reference)

    enriched = dict(report)
    enriched["municipios"] = municipalities
    enriched["alertas_altos"] = alerts
    enriched["metadata"] = metadata
    return enriched
