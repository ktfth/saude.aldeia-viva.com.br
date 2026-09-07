"""
Report Builder - Aggregation layer for the Aldeia Viva Saúde BIRO.

Responsible for transforming raw SINAN records (by disease) into
enriched per-municipality epidemiological risk reports.

This module is the heart of the data transformation that powers
the future cockpit (drill-down, year comparisons, maps, etc.).

All functions here should remain as pure as possible.
"""

from datetime import date
from typing import Any, Iterable, Mapping

from domain.disease_sources import DISEASE_SOURCES, classification_label
from domain.epi_week import (
    MINIMO_PARA_TENDENCIA,
    SEMANAS_PROVISORIAS,
    rotulo_semana,
    semana_epidemiologica,
    tendencia,
)
from domain.risk import (
    RISK_FORMULA,
    finalize_disease_summary,
    risk_profile_for_source,
    worst_level,
)

from .utils import (
    any_flag,
    clean_code,
    clean_value,
    first_present,
    has_any_positive_field,
    normalize_text,
    parse_date_value,
    update_latest_date,
)


def is_hospitalized_record(source, record: Mapping[str, Any]) -> bool:
    return has_any_positive_field(record, source.hospitalization_fields)


def is_death_record(record: Mapping[str, Any]) -> bool:
    if clean_code(record.get("EVOLUCAO")) == "2":
        return True
    death_value = normalize_text(clean_value(record.get("OBITO")))
    return death_value in {"sim", "s", "yes"}


# =============================================================================
# Core aggregation functions
# =============================================================================


def create_municipality_summary(
    municipality_code: str,
    record: Mapping[str, Any],
    year: int,
    lookup_item: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state = clean_value(lookup_item.get("estado") if lookup_item else "")
    if not state:
        state = _state_from_record(record)

    return {
        "codigo_municipio": municipality_code,
        "municipio": clean_value(lookup_item.get("municipio") if lookup_item else "")
        or f"Código {municipality_code}",
        "estado": state,
        "periodo": {"ano": year},
        "fonte": "SINAN/OpenDataSUS",
        "formula_risco": RISK_FORMULA,
        "total_notificacoes": 0,
        "total_casos_provaveis": 0,
        "total_casos_descartados": 0,
        "total_sinais_alarme": 0,
        "total_casos_graves": 0,
        "total_hospitalizacoes": 0,
        "total_obitos": 0,
        "risk_score": 0.0,
        "nivel_risco": "baixo",
        "doencas_por_codigo": {},
    }


def create_disease_summary(
    source, year: int, source_year: int | None = None
) -> dict[str, Any]:
    """Resumo vazio de um agravo.

    `periodo.ano` passa a ser o ano do arquivo-fonte de fato usado, não o ano
    solicitado. O relatório reúne anos diferentes por agravo — carimbar o ano
    pedido em todos fazia a Meningite de 2022 se apresentar como dado de 2026.
    """
    return {
        "codigo": source.codigo,
        "nome": source.nome,
        "virus": source.virus,
        "tipo": source.tipo,
        "perfil_risco": source.risk_profile,
        "formula_risco": risk_profile_for_source(source).formula,
        # Casos prováveis por semana epidemiológica de INÍCIO DE SINTOMAS.
        # É a data que o boletim usa para a curva epidêmica: reflete quando
        # houve transmissão, e não quando a notificação foi digitada. A data
        # de notificação entra só como fallback.
        "serie_semanal": {},
        "periodo": {
            "ano": source_year if source_year is not None else year,
            "ano_solicitado": year,
        },
        "total_notificacoes": 0,
        "casos_provaveis": 0,
        "casos_descartados": 0,
        "sinais_alarme": 0,
        "casos_graves": 0,
        "hospitalizacoes": 0,
        "obitos": 0,
        "risk_score": 0.0,
        "nivel_risco": "baixo",
        "ultima_notificacao": None,
        "ultimo_inicio_sintomas": None,
        "classificacoes": {},
    }


def add_record_to_summaries(
    disease: dict[str, Any],
    municipality: dict[str, Any],
    source,
    record: Mapping[str, Any],
) -> None:
    disease["total_notificacoes"] += 1
    municipality["total_notificacoes"] += 1

    classification = clean_code(record.get("CLASSI_FIN"))
    if classification == "5":
        disease["casos_descartados"] += 1
        return

    disease["casos_provaveis"] += 1
    label = classification_label(source.codigo, classification)
    disease["classificacoes"][label] = disease["classificacoes"].get(label, 0) + 1

    severity_code = clean_code(record.get(source.severity_code_field))
    if (
        severity_code in source.warning_codes
        or has_any_positive_field(record, source.warning_fields)
        or any_flag(record, "ALRM_")
    ):
        disease["sinais_alarme"] += 1
    if (
        severity_code in source.severe_codes
        or has_any_positive_field(record, source.severe_fields)
        or any_flag(record, "GRAV_")
    ):
        disease["casos_graves"] += 1
    if is_death_record(record):
        disease["obitos"] += 1
    if is_hospitalized_record(source, record):
        disease["hospitalizacoes"] += 1

    notification_date = parse_date_value(first_present(record, ("DT_NOTIFIC", "DT_IS")))
    symptom_date = parse_date_value(first_present(record, ("DT_SIN_PRI", "DT_IS")))
    update_latest_date(disease, "ultima_notificacao", notification_date)
    update_latest_date(disease, "ultimo_inicio_sintomas", symptom_date)

    # A contagem cai aqui, depois do `return` dos descartados: a curva usa o
    # mesmo numerador da incidência, e não o total de notificações.
    referencia = symptom_date or notification_date
    if referencia:
        try:
            dia = date.fromisoformat(referencia)
        except (TypeError, ValueError):
            return
        rotulo = rotulo_semana(semana_epidemiologica(dia))
        serie = disease["serie_semanal"]
        serie[rotulo] = serie.get(rotulo, 0) + 1


def agregar_curvas(
    municipalities: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Curva por agravo, nacional e por UF, somada ANTES da poda.

    A série de cada município é descartada quando o volume é baixo demais
    para significar algo. Reconstruir o total a partir do que sobra subconta:
    medido, a curva nacional de dengue somada pelos municípios dava 440.685
    contra 449.101 casos reais — 1,9% a menos, justamente os municípios
    pequenos. Aqui a soma acontece antes, e fecha.

    A curva agregada é barata: dez agravos por trinta e cinco semanas, mais
    vinte e sete UFs. O que pesaria seria a série por município, e essa
    continua podada.
    """
    nacional: dict[str, dict[str, int]] = {}
    por_uf: dict[str, dict[str, dict[str, int]]] = {}

    for municipality in municipalities:
        uf = municipality.get("estado") or "??"
        for codigo, disease in (municipality.get("doencas_por_codigo") or {}).items():
            serie = disease.get("serie_semanal") or {}
            if not serie:
                continue
            alvo_nacional = nacional.setdefault(codigo, {})
            alvo_uf = por_uf.setdefault(uf, {}).setdefault(codigo, {})
            for rotulo, contagem in serie.items():
                alvo_nacional[rotulo] = alvo_nacional.get(rotulo, 0) + contagem
                alvo_uf[rotulo] = alvo_uf.get(rotulo, 0) + contagem

    return {
        "nacional": {
            codigo: {
                "serie_semanal": dict(sorted(serie.items())),
                "tendencia": tendencia(serie),
            }
            for codigo, serie in sorted(nacional.items())
        },
        "por_uf": {
            uf: {
                codigo: {
                    "serie_semanal": dict(sorted(serie.items())),
                    "tendencia": tendencia(serie),
                }
                for codigo, serie in sorted(agravos.items())
            }
            for uf, agravos in sorted(por_uf.items())
        },
        "semanas_provisorias": SEMANAS_PROVISORIAS,
        "aviso": (
            "Curva de casos prováveis por semana epidemiológica de início de "
            "sintomas. As últimas semanas estão incompletas — as notificações "
            "ainda chegam — e por isso não entram no cálculo da direção. "
            "Medido: 84% das notificações chegam em uma semana, 93% em duas."
        ),
    }


def _resolver_tendencia(disease: dict[str, Any]) -> None:
    """Direção do agravo naquele município, e o que vale guardar da curva.

    A curva por município é mantida apenas quando há volume para ela
    significar algo. Abaixo do mínimo ela é ruído com aparência de série:
    três casos espalhados em trinta semanas desenham um gráfico que sugere
    padrão onde não há, e ainda multiplicariam o tamanho do artefato por
    5.408 municípios vezes 10 agravos.

    A direção é sempre calculada e sempre declarada — inclusive como
    `indeterminada`, que é uma afirmação sobre o que este dado sustenta, e
    não sobre a epidemia.
    """
    serie = disease.get("serie_semanal") or {}
    disease["tendencia"] = tendencia(serie)
    if sum(serie.values()) < MINIMO_PARA_TENDENCIA:
        disease["serie_semanal"] = {}
        disease["tendencia"]["serie_omitida"] = bool(serie)
    else:
        # Em ordem cronológica. A acumulação segue a ordem dos registros no
        # arquivo, que não é a do tempo: a série saía `SE02, SE18, SE15,
        # SE16` e quem iterasse recebia a curva embaralhada. A curva nacional
        # já era ordenada; esta não era, e a diferença entre as duas é
        # exatamente o tipo de incoerência que ninguém confere.
        disease["serie_semanal"] = dict(sorted(serie.items()))


def finalize_municipality_rows(
    municipalities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for municipality in municipalities:
        diseases = [
            finalize_disease_summary(disease)
            for disease in municipality.pop("doencas_por_codigo").values()
        ]
        for disease in diseases:
            _resolver_tendencia(disease)
        diseases.sort(key=lambda item: item["risk_score"], reverse=True)

        municipality["doencas"] = diseases
        municipality["doencas_altas"] = [
            disease
            for disease in diseases
            if disease["nivel_risco"] in {"alto", "critico"}
        ]
        municipality["total_casos_provaveis"] = sum(
            disease["casos_provaveis"] for disease in diseases
        )
        municipality["total_casos_descartados"] = sum(
            disease["casos_descartados"] for disease in diseases
        )
        municipality["total_sinais_alarme"] = sum(
            disease["sinais_alarme"] for disease in diseases
        )
        municipality["total_casos_graves"] = sum(
            disease["casos_graves"] for disease in diseases
        )
        municipality["total_hospitalizacoes"] = sum(
            disease["hospitalizacoes"] for disease in diseases
        )
        municipality["total_obitos"] = sum(disease["obitos"] for disease in diseases)
        municipality["risk_score"] = round(
            sum(disease["risk_score"] for disease in diseases), 2
        )
        # Pior nível entre os agravos, cada um já calculado com o seu próprio
        # perfil. A versão anterior aplicava o perfil de arbovirose à soma de
        # até 10 agravos e 5 anos-fonte: 24,3% dos 5.339 municípios saíam
        # como "crítico" e a variável deixava de discriminar.
        municipality["nivel_risco"] = worst_level(
            disease["nivel_risco"] for disease in diseases
        )
        # População e taxa de incidência NÃO nascem aqui. O lookup do IBGE
        # nunca trouxe denominador, então este cálculo produzia None em 100%
        # dos casos. A autoridade única passa a ser
        # `aggregation/population_enrichment.py`, aplicada na entrada do
        # relatório em memória — o que faz o denominador valer também para o
        # cache em disco e o snapshot embarcado.
        rows.append(municipality)

    return sorted(rows, key=lambda item: item["risk_score"], reverse=True)


def build_epidemiology_report(
    records_by_disease: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    year: int,
    municipality_lookup: Mapping[str, Mapping[str, str]] | None = None,
    source_years: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    lookup = municipality_lookup or {}
    years_by_source = source_years or {}
    municipalities: dict[str, dict[str, Any]] = {}
    skipped_records = 0

    for disease_code, records in records_by_disease.items():
        source = DISEASE_SOURCES.get(disease_code)
        if source is None:
            continue

        for record in records:
            municipality_code = _extract_municipality_code(record)
            if not municipality_code:
                skipped_records += 1
                continue

            municipality = municipalities.setdefault(
                municipality_code,
                create_municipality_summary(
                    municipality_code, record, year, lookup.get(municipality_code)
                ),
            )
            disease = municipality["doencas_por_codigo"].setdefault(
                source.codigo,
                create_disease_summary(
                    source, year, years_by_source.get(source.codigo)
                ),
            )
            add_record_to_summaries(disease, municipality, source, record)

    # Antes da finalização, que poda as séries de baixo volume.
    curvas = agregar_curvas(municipalities.values())

    rows = finalize_municipality_rows(municipalities.values())
    alerts = build_high_alerts(rows)
    return {
        "metadata": {
            "periodo": {"ano": year},
            "fonte": "SINAN/OpenDataSUS via Portal de Dados Abertos do SUS",
            "municipios": len(rows),
            "curvas": curvas,
            "formula_risco": RISK_FORMULA,
            "formulas_por_doenca": {
                source.codigo: risk_profile_for_source(source).formula
                for source in DISEASE_SOURCES.values()
            },
            "registros_ignorados": skipped_records,
        },
        "municipios": rows,
        "alertas_altos": alerts,
    }


def build_high_alerts(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for row in rows:
        for disease in row["doencas_altas"]:
            alerts.append(
                {
                    "codigo_municipio": row["codigo_municipio"],
                    "municipio": row["municipio"],
                    "estado": row["estado"],
                    "codigo_doenca": disease["codigo"],
                    "doenca": disease["nome"],
                    "virus": disease["virus"],
                    "tipo": disease["tipo"],
                    "casos_provaveis": disease["casos_provaveis"],
                    "casos_graves": disease["casos_graves"],
                    "sinais_alarme": disease["sinais_alarme"],
                    "hospitalizacoes": disease["hospitalizacoes"],
                    "obitos": disease["obitos"],
                    "risk_score": disease["risk_score"],
                    "nivel_risco": disease["nivel_risco"],
                    "ultima_notificacao": disease["ultima_notificacao"],
                }
            )
    return sorted(alerts, key=lambda item: item["risk_score"], reverse=True)


# Small internal helpers (kept here for now as they are specific to report building)

def _extract_municipality_code(record: Mapping[str, Any]) -> str:
    for field in ("ID_MN_RESI", "ID_MUNICIP", "COD_MUN_LPI", "MUNICIPIO", "COMUNINF"):
        code = normalize_text(clean_value(record.get(field)))
        if code:
            return clean_value(record.get(field))
    return ""


# Código IBGE de UF para sigla. O SINAN entrega `SG_UF` e os dois primeiros
# dígitos do código municipal como número, não como sigla.
UF_CODE_TO_ABBR = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP",
    "17": "TO", "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB",
    "26": "PE", "27": "AL", "28": "SE", "29": "BA", "31": "MG", "32": "ES",
    "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS", "50": "MS",
    "51": "MT", "52": "GO", "53": "DF",
}

UF_ABBREVIATIONS = frozenset(UF_CODE_TO_ABBR.values())


def state_from_record(record: Mapping[str, Any]) -> str:
    """UF do registro, tolerando código numérico ou sigla.

    A versão anterior fazia `.upper()` em `SG_UF` e devolvia "35" como se
    fosse uma UF. A lógica correta existia em app.py e nunca esteve no
    caminho do relatório, então todo município fora do lookup do IBGE saía
    com um número no lugar da sigla.
    """
    code = _digits(_extract_municipality_code(record))
    if len(code) >= 2 and code[:2] in UF_CODE_TO_ABBR:
        return UF_CODE_TO_ABBR[code[:2]]

    raw = clean_value(record.get("SG_UF", "")).strip().upper()
    if raw in UF_ABBREVIATIONS:
        return raw
    return UF_CODE_TO_ABBR.get(_digits(raw), "")


def _digits(value: Any) -> str:
    return "".join(char for char in clean_value(value) if char.isdigit())


# Nome anterior mantido para os chamadores internos deste módulo.
_state_from_record = state_from_record
