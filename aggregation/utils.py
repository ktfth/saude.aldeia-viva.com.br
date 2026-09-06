"""
Shared pure utility functions for the aggregation layer.

These are small, side-effect free helpers used across
report_builder, filters, and potentially future modules
in the cockpit (drill-down, comparisons, exports, etc.).

Goal: eliminate duplication and keep modules small and focused.
"""

import unicodedata
from datetime import datetime
from typing import Any, Iterable, Mapping


def clean_value(value: Any) -> str:
    """Return stripped string or empty string if None."""
    return "" if value is None else str(value).strip()


def clean_code(value: Any) -> str:
    """Clean classification codes (remove trailing .0 from some SINAN exports)."""
    text = clean_value(value)
    if text.endswith(".0"):
        text = text[:-2]
    return text


def normalize_text(value: str) -> str:
    """Minúsculas sem acentos, para comparação tolerante.

    O import estava embutido na função e era resolvido duas vezes por
    chamada. Está no caminho quente da ingestão — `is_truthy_code` chama isto
    para cada campo candidato de cada um dos ~363 mil registros de uma carga.
    Medido: 1,6x mais rápido com o import no topo do módulo.
    """
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).lower()


def is_truthy_code(value: Any) -> bool:
    """Common SINAN truthy values for yes/positive flags."""
    text = normalize_text(clean_value(value))
    return text in {"1", "sim", "s", "yes", "true"}


# Formatos que o SINAN de fato entrega, entre CSV e DBF.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y%m%d",
    "%Y/%m/%d",
)


def parse_date_value(value: Any) -> str:
    """Data em `YYYY-MM-DD`, ou string vazia quando não for uma data.

    A versão anterior devolvia a STRING CRUA quando nenhum formato batia:
    `"2026-04-21 10:33:00"`, `"31/02/2026"` e `"9999-99-99"` saíam intactos.
    Como `update_latest_date` comparava texto, um valor assim vencia
    comparações e virava a última notificação do agravo — corrompendo a
    dimensão de recência de forma plausível, sem nada falhar.

    Falhar fechado é a única saída honesta: sem data utilizável, a camada de
    recência já sabe representar "desconhecido".
    """
    text = clean_value(value)
    if not text:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def first_present(record: Mapping[str, Any], fields: Iterable[str]) -> str:
    """Return the first non-empty value among the given fields."""
    for field in fields:
        value = clean_value(record.get(field))
        if value:
            return value
    return ""


def update_latest_date(summary: dict[str, Any], field: str, candidate: Any) -> None:
    """Guarda a data mais recente, comparando datas e não texto.

    A comparação anterior era `candidate > current` entre strings, ou seja,
    ordem lexicográfica: `"21/04/2020" > "2026-04-21"` é verdadeiro, e uma
    data de 2020 sobrescrevia uma de 2026. O sentinela `"9999-99-99"` vencia
    qualquer data real.

    O candidato é normalizado antes de entrar, de modo que o valor guardado é
    sempre ISO — ou não existe.
    """
    normalized = parse_date_value(candidate)
    if not normalized:
        return
    current = summary.get(field)
    if not current or normalized > parse_date_value(current):
        summary[field] = normalized


def any_flag(record: Mapping[str, Any], prefix: str) -> bool:
    """Check if any field starting with prefix has a truthy value."""
    return any(
        field.startswith(prefix) and is_truthy_code(value)
        for field, value in record.items()
    )


def has_any_positive_field(record: Mapping[str, Any], fields: Iterable[str]) -> bool:
    """Check if any of the specific fields has a truthy value."""
    return any(is_truthy_code(record.get(field)) for field in fields)
