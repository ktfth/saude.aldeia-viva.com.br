"""
Report Builder - Aggregation layer for the Aldeia Viva Saúde BIRO.

Responsible for transforming raw SINAN records (by disease) into
enriched per-municipality epidemiological risk reports.

This module is the heart of the data transformation that powers
the future cockpit (drill-down, year comparisons, maps, etc.).

All functions here should remain as pure as possible.
"""

from datetime import datetime
from typing import Any, Iterable, Mapping

from domain.disease_sources import DISEASE_SOURCES, classification_label
from domain.rates import incidence_per_100k
from domain.risk import (
    RISK_FORMULA,
    finalize_disease_summary,
    risk_level,
    risk_profile_for_source,
)

from .utils import (
    any_flag,
    clean_code,
    clean_value,
    first_present,
    has_any_positive_field,
    is_truthy_code,
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

    # populacao is optional — read it from the lookup item when present.
    # The value may be an int (preferred) or absent; default to None for
    # graceful degradation in rate calculations downstream.
    raw_populacao = lookup_item.get("populacao") if lookup_item else None
    populacao: int | None = int(raw_populacao) if raw_populacao is not None else None

    return {
        "codigo_municipio": municipality_code,
        "municipio": clean_value(lookup_item.get("municipio") if lookup_item else "")
        or f"Código {municipality_code}",
        "estado": state,
        "populacao": populacao,
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


def create_disease_summary(source, year: int) -> dict[str, Any]:
    return {
        "codigo": source.codigo,
        "nome": source.nome,
        "virus": source.virus,
        "tipo": source.tipo,
        "perfil_risco": source.risk_profile,
        "formula_risco": risk_profile_for_source(source).formula,
        "periodo": {"ano": year},
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


def finalize_municipality_rows(
    municipalities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for municipality in municipalities:
        diseases = [
            finalize_disease_summary(disease)
            for disease in municipality.pop("doencas_por_codigo").values()
        ]
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
        municipality["nivel_risco"] = risk_level(
            municipality["risk_score"],
            municipality["total_casos_provaveis"],
            municipality["total_casos_graves"],
            municipality["total_obitos"],
        )
        municipality["taxa_incidencia_100k"] = incidence_per_100k(
            municipality["total_casos_provaveis"],
            municipality.get("populacao"),
        )
        rows.append(municipality)

    return sorted(rows, key=lambda item: item["risk_score"], reverse=True)


def build_epidemiology_report(
    records_by_disease: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    year: int,
    municipality_lookup: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    lookup = municipality_lookup or {}
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
                source.codigo, create_disease_summary(source, year)
            )
            add_record_to_summaries(disease, municipality, source, record)

    rows = finalize_municipality_rows(municipalities.values())
    alerts = build_high_alerts(rows)
    return {
        "metadata": {
            "periodo": {"ano": year},
            "fonte": "SINAN/OpenDataSUS via Portal de Dados Abertos do SUS",
            "municipios": len(rows),
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


def _state_from_record(record: Mapping[str, Any]) -> str:
    # Simplified version - the real UF mapping logic lives in app.py for now
    return clean_value(record.get("SG_UF", "")).upper()
