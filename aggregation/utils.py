"""
Shared pure utility functions for the aggregation layer.

These are small, side-effect free helpers used across
report_builder, filters, and potentially future modules
in the cockpit (drill-down, comparisons, exports, etc.).

Goal: eliminate duplication and keep modules small and focused.
"""

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
    """Lowercase + remove accents/diacritics for fuzzy matching."""
    normalized = __import__("unicodedata").normalize("NFKD", value)
    return "".join(
        char for char in normalized if not __import__("unicodedata").combining(char)
    ).lower()


def is_truthy_code(value: Any) -> bool:
    """Common SINAN truthy values for yes/positive flags."""
    text = normalize_text(clean_value(value))
    return text in {"1", "sim", "s", "yes", "true"}


def parse_date_value(value: str) -> str:
    """Try to parse common Brazilian/ISO date formats into YYYY-MM-DD string."""
    text = clean_value(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def first_present(record: Mapping[str, Any], fields: Iterable[str]) -> str:
    """Return the first non-empty value among the given fields."""
    for field in fields:
        value = clean_value(record.get(field))
        if value:
            return value
    return ""


def update_latest_date(summary: dict[str, Any], field: str, candidate: str) -> None:
    """Update a date field in a summary dict only if the candidate is newer."""
    if not candidate:
        return
    current = summary.get(field)
    if not current or candidate > current:
        summary[field] = candidate


def any_flag(record: Mapping[str, Any], prefix: str) -> bool:
    """Check if any field starting with prefix has a truthy value."""
    return any(
        field.startswith(prefix) and is_truthy_code(value)
        for field, value in record.items()
    )


def has_any_positive_field(record: Mapping[str, Any], fields: Iterable[str]) -> bool:
    """Check if any of the specific fields has a truthy value."""
    return any(is_truthy_code(record.get(field)) for field in fields)
