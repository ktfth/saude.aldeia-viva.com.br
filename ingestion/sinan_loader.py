"""
SINAN Data Loader - Ingestion layer for the Aldeia Viva Saúde BIRO.

Handles fetching and parsing raw data from OpenDataSUS / DATASUS
(DBC files via FTP and CSV via S3/CKAN).

This module is intentionally isolated so we can:
- Add better caching and retry strategies
- Support multi-year historical loads for the cockpit
- Make the system more resilient when official sources are unstable

All heavy I/O and external dependencies should live here.
"""

import logging
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Iterable, Mapping


from aggregation.utils import clean_code, clean_value

# Note: Some constants are accessed lazily inside functions.
# The load_*_records functions are defined later in this same file.


def build_source_url(source, year: int) -> str:
    from app import OPEN_DATA_SUS_S3_BASE

    suffix = str(year)[-2:]
    return (
        f"{OPEN_DATA_SUS_S3_BASE}/SINAN/{source.folder}/csv/"
        f"{source.file_prefix}BR{suffix}.csv.zip"
    )


def build_dbc_source_url(source, year: int) -> str:
    from app import DATASUS_SINAN_DBC_BASE

    suffix = str(year)[-2:]
    return f"{DATASUS_SINAN_DBC_BASE}/{source.dbc_prefix}BR{suffix}.dbc"

logger = logging.getLogger(__name__)


def normalize_dbf_record(row: Mapping[str, Any]) -> dict[str, str]:
    """Normalize a DBF row into clean strings (handles None and date objects)."""
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if value is None:
            normalized[key] = ""
        elif isinstance(value, date):
            normalized[key] = value.isoformat()
        else:
            normalized[key] = clean_value(value)
    return normalized


def filter_records_by_latest_available_year(
    records: Iterable[Mapping[str, Any]], *, target_year: int, year_field: str
) -> tuple[int, list[dict[str, Any]]]:
    """Find the most recent year <= target_year that has data and filter to it."""
    rows = [dict(record) for record in records]
    available_years = sorted(
        {
            int(clean_code(row.get(year_field)))
            for row in rows
            if clean_code(row.get(year_field)).isdigit()
        }
    )
    candidate_years = [year for year in available_years if year <= target_year]
    if not candidate_years:
        raise RuntimeError(f"Nenhum registro disponível até {target_year}.")

    latest_year = candidate_years[-1]
    return latest_year, [
        row for row in rows if clean_code(row.get(year_field)) == str(latest_year)
    ]


def load_latest_available_records(
    source, target_year: int
) -> tuple[int, str, list[dict[str, str]]]:
    """
    Try to load the most recent available data for a disease source.

    Returns (year, url, records).
    Raises RuntimeError if nothing usable is found.
    """
    if source.direct_csv_url:
        records = load_csv_records_from_url(
            source.direct_csv_url, encoding=source.csv_encoding
        )
        year, filtered_records = filter_records_by_latest_available_year(
            records, target_year=target_year, year_field=source.year_field
        )
        return year, source.direct_csv_url, filtered_records

    if source.dbc_prefix:
        start_year = min(target_year, source.latest_year or target_year)
        for year in range(start_year, source.first_year - 1, -1):
            url = build_dbc_source_url(source, year)
            try:
                return year, url, load_dbc_records_from_url(url)
            except (urllib.error.URLError, urllib.error.HTTPError) as error:
                logger.info("%s indisponível em %s: %s", source.codigo, year, error)
        raise RuntimeError(f"Nenhum DBC disponível para {source.nome}.")

    for year in range(target_year, source.first_year - 1, -1):
        url = build_source_url(source, year)
        try:
            return year, url, load_csv_records_from_zip(url)
        except urllib.error.HTTPError as error:
            if error.code not in {403, 404}:
                raise
            logger.info(
                "%s indisponível em %s: HTTP %s", source.codigo, year, error.code
            )
    raise RuntimeError(f"Nenhum CSV disponível para {source.nome}.")


# =============================================================================
# Low-level download + parsing functions (moved from app.py during Fase 0)
# =============================================================================

import csv
import hashlib
import io
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping


# Constants are accessed lazily inside functions to avoid circular import
# problems during the gradual Fase 0 extraction from the monolith.


def download_bytes(url: str) -> bytes:
    from app import REQUEST_TIMEOUT_SECONDS, SINAN_CACHE_DIR

    if os.getenv("SINAN_DISABLE_CACHE") == "1":
        return fetch_url_bytes(url)

    cache_path = cache_path_for_url(url)
    if cache_path.exists():
        return cache_path.read_bytes()

    payload = fetch_url_bytes(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return payload


def fetch_url_bytes(url: str) -> bytes:
    from app import REQUEST_TIMEOUT_SECONDS

    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def cache_path_for_url(url: str) -> Path:
    from app import SINAN_CACHE_DIR

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
    return SINAN_CACHE_DIR / f"{digest}{suffix}"


def load_csv_records_from_zip(url: str) -> list[dict[str, str]]:
    payload = download_bytes(url)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        csv_member = next(
            member for member in archive.namelist() if member.lower().endswith(".csv")
        )
        with archive.open(csv_member) as raw_file:
            text_file = io.TextIOWrapper(
                raw_file, encoding="utf-8-sig", errors="replace"
            )
            return list(csv.DictReader(text_file))


def load_csv_records_from_url(url: str, *, encoding: str) -> list[dict[str, str]]:
    payload = download_bytes(url)
    text = payload.decode(encoding, errors="replace")
    return list(csv.DictReader(text.splitlines(), delimiter=";"))



def _load_dbc_toolchain():
    """Importa as dependencias nativas de DBC apenas quando de fato usadas.

    `datasus_dbc` e `dbfread` sao extensoes nativas que nem sempre tem wheel
    para a versao de Python em uso. Manter o import no topo do modulo derrubava
    a importacao do app inteiro, mesmo quando a carga usava CSV ou o snapshot
    embarcado. O erro agora acontece no ponto de uso, com mensagem acionavel.
    """
    try:
        import datasus_dbc
        from dbfread import DBF
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError(
            "Leitura de DBC exige os pacotes nativos 'datasus-dbc' e 'dbfread'. "
            "Instale-os ou use as fontes CSV/snapshot."
        ) from exc
    return datasus_dbc, DBF


def load_dbc_records_from_url(url: str) -> list[dict[str, str]]:
    datasus_dbc, DBF = _load_dbc_toolchain()
    dbc_payload = download_bytes(url)
    dbf_payload = datasus_dbc.decompress_bytes(dbc_payload)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dbf") as temp_file:
        temp_file.write(dbf_payload)
        temp_path = temp_file.name

    try:
        table = DBF(temp_path, encoding="latin-1", ignore_missing_memofile=True)
        return [normalize_dbf_record(row) for row in table]
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.warning(
                "Não foi possível remover arquivo temporário DBF: %s", temp_path
            )
