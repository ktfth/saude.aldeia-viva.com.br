"""
Municipality Lookup - Improved IBGE data enrichment for the Aldeia Viva Saúde BIRO.

Improvements over the original version:
- File-based caching of IBGE response (avoids hitting the API on every report)
- Better error handling and logging
- Graceful degradation (returns partial data instead of empty dict on failure)
- Clear separation of concerns (part of the ingestion layer)

This is critical for the cockpit so that users see real municipality names
instead of "Código 123456".
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Will be resolved lazily
MUNICIPALITY_CACHE_FILE = None  # type: ignore


def _load_from_cache() -> dict[str, dict[str, str]]:
    from app import SINAN_CACHE_DIR

    cache_file = SINAN_CACHE_DIR / "ibge_municipalities.json"
    if not cache_file.exists():
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Municipality lookup loaded from cache (%d entries)", len(data))
        return data
    except Exception as error:
        logger.warning("Failed to read municipality cache: %s", error)
        return {}


def _save_to_cache(data: dict[str, dict[str, str]]) -> None:
    # Lazy import
    from app import SINAN_CACHE_DIR

    cache_file = SINAN_CACHE_DIR / "ibge_municipalities.json"
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Municipality lookup cached to disk (%d entries)", len(data))
    except Exception as error:
        logger.warning("Failed to write municipality cache: %s", error)


def load_municipality_lookup(force_refresh: bool = False) -> dict[str, dict[str, str]]:
    """
    Load enriched municipality data from IBGE.

    Strategy:
    1. Try disk cache first (fast & reliable)
    2. If cache miss or force_refresh=True → fetch from IBGE API
    3. On any failure, return whatever we have (even if partial/empty)
    """
    if not force_refresh:
        cached = _load_from_cache()
        if cached:
            return cached

    # Lazy imports to avoid circular dependency during Fase 0 extraction
    from app import (
        IBGE_MUNICIPALITIES_URL,
        REQUEST_TIMEOUT_SECONDS,
    )

    logger.info("Fetching fresh municipality data from IBGE...")
    try:
        with urllib.request.urlopen(
            IBGE_MUNICIPALITIES_URL, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw_data = response.read()
    except Exception as error:
        logger.warning("Failed to fetch municipalities from IBGE: %s", error)
        # Return whatever is in cache (even if empty)
        return _load_from_cache()

    # Handle possible gzip compression from the server
    if raw_data[:2] == b"\x1f\x8b":
        import gzip
        raw_data = gzip.decompress(raw_data)

    try:
        municipalities = json.loads(raw_data.decode("utf-8-sig"))
    except Exception as error:
        logger.error("Failed to parse IBGE municipality JSON: %s", error)
        return _load_from_cache()

    lookup: dict[str, dict[str, str]] = {}
    for municipality in municipalities:
        code7 = str(municipality.get("id", "")).strip()
        if not code7 or len(code7) < 6:
            continue

        code6 = code7[:6]
        nome = str(municipality.get("nome", "")).strip() or f"Código {code6}"

        # Try to get state abbreviation
        state = ""
        microrregiao = municipality.get("microrregiao") or {}
        mesorregiao = microrregiao.get("mesorregiao") or {}
        uf = mesorregiao.get("UF") or {}
        state = str(uf.get("sigla", "")).strip().upper()

        if not state:
            # Fallback using immediate region
            regiao_imediata = municipality.get("regiao-imediata") or {}
            regiao_intermediaria = regiao_imediata.get("regiao-intermediaria") or {}
            uf2 = regiao_intermediaria.get("UF") or {}
            state = str(uf2.get("sigla", "")).strip().upper()

        lookup[code6] = {
            "municipio": nome,
            "estado": state,
            "codigo_ibge": code7,
        }

    if lookup:
        _save_to_cache(lookup)
        logger.info("Successfully loaded %d municipalities from IBGE", len(lookup))
    else:
        logger.warning("IBGE returned no usable municipalities")

    return lookup
