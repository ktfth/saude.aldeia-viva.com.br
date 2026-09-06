import csv
import gzip
import hashlib
import hmac
import html
import io
import threading
import json
import logging
import os
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# Domain imports (Fase 0 refactoring - foundation for cockpit)
from domain.disease_sources import (
    DEFAULT_DISEASE_CODES,
    DISEASE_SOURCES,
    DiseaseSource,
    classification_label,
)
from domain.risk import (
    RISK_FORMULA,
    RISK_PROFILES,
    RiskProfile,
    finalize_disease_summary,
    risk_level,
    risk_profile_for_source,
)

# Aggregation layer (Fase 0 - continuing extraction)
from aggregation.report_builder import (
    build_epidemiology_report,
    build_high_alerts,
    create_disease_summary,
    create_municipality_summary,
)
from aggregation.filters import (
    filter_risk_index,
    filter_alerts,
    resolve_locality_alias,
    municipality_matches,
    with_locality_alias,
    level_at_least,
    LOCALITY_ALIASES,
    get_supported_bairros,
)
from aggregation.utils import normalize_text

# Ingestion layer extraction in progress (Fase 0).
from ingestion.municipality_lookup import load_municipality_lookup
from aggregation.recency_enrichment import enrich_report
from aggregation.population_enrichment import enrich_with_population
from aggregation.ordering import ORDERINGS, sort_municipalities
from aggregation.cache_policy import DEFAULT_MAX_AGE_DAYS, cache_is_fresh
from ingestion.population_lookup import load_population_lookup
from presentation.signal import (
    render_data_status,
    render_incidence_cell,
    render_risk_cell,
    render_signal_strip,
    render_signal_tag,
    render_strip_legend,
    sort_diseases_for_strip,
)
from aggregation.report_builder import finalize_municipality_rows, is_death_record, is_hospitalized_record
from aggregation.utils import any_flag, clean_code, clean_value, first_present, has_any_positive_field, is_truthy_code, parse_date_value, update_latest_date

# The loader module exists. We avoid top-level import here to prevent
# circular dependencies during the gradual monolith breakup.

from fastapi import FastAPI, Query, Request, Header, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

try:
    from bundled_report_snapshot import load_embedded_report_snapshot
except ImportError:  # pragma: no cover - fallback for local-only runs before bundling
    load_embedded_report_snapshot = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPEN_DATA_SUS_S3_BASE = "https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br"
DATASUS_SINAN_DBC_BASE = "ftp://ftp.datasus.gov.br/dissemin/publicos/SINAN/DADOS/FINAIS"
IBGE_MUNICIPALITIES_URL = (
    "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
)
DEFAULT_YEAR = int(os.getenv("SINAN_YEAR", datetime.now(UTC).year))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("SINAN_REQUEST_TIMEOUT_SECONDS", "45"))
SINAN_CACHE_DIR = Path(os.getenv("SINAN_CACHE_DIR", ".cache/datasus"))
APP_ROOT = Path(__file__).resolve().parent
REPORT_CACHE_VERSION = "risk-report-v1"


def report_cache_max_age_days() -> int:
    """Dias até a cache agregada deixar de ser servida sem tentar renovação.

    Lido a cada chamada, e não no import, para que testes e operação possam
    ajustá-lo sem reiniciar o processo. `0` desliga a expiração — escape hatch
    para ambientes sem rede, onde tentar buscar só produz latência e log.
    """
    try:
        return int(
            os.getenv("SINAN_REPORT_CACHE_MAX_AGE_DAYS", str(DEFAULT_MAX_AGE_DAYS))
        )
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS

# =============================================================================
# Static assets (Fase 0 - extração de interface para permitir melhorias sustentáveis)
# =============================================================================
STATIC_DIR = APP_ROOT / "web" / "static"

USERS_DB_PATH = Path(os.getenv("USERS_DB_PATH", "data/users.json"))
USAGE_LOG_PATH = Path(os.getenv("USAGE_LOG_PATH", "data/usage.jsonl"))


class APIKeyManager:
    def __init__(self, path: Path):
        self.path = path
        self.keys = {}
        self.load_keys()

    def load_keys(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.keys = data.get("keys", {})
            except Exception as e:
                logger.error(f"Error loading API keys: {e}")

    def validate_key(self, api_key: str) -> Optional[dict]:
        """Valida a chave, aceitando entrada em texto puro ou em hash.

        Uma entrada de `users.json` cuja chave comece com `sha256:` guarda o
        digest em vez do segredo, o que permite migrar o arquivo sem quebrar
        quem já usa as chaves atuais. A comparação usa `compare_digest` para
        não vazar informação pelo tempo de resposta.
        """
        if not api_key:
            return None

        entry = self.keys.get(api_key)
        if entry is not None:
            return entry

        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        for stored, value in self.keys.items():
            if not stored.startswith("sha256:"):
                continue
            if hmac.compare_digest(stored[len("sha256:") :], digest):
                return value
        return None


def key_fingerprint(api_key: str) -> str:
    """Identificador estável de uma chave, sem conter a chave.

    O log de uso gravava o valor cru do cabeçalho X-API-Key em disco, num
    arquivo append-only, sem rotação. Qualquer envio de logs, backup ou
    compartilhamento do diretório de dados vazava a credencial. O objetivo do
    log é contar uso por cliente, e para isso um prefixo de digest basta.
    """
    if not api_key or api_key == TIER_ANONYMOUS:
        return TIER_ANONYMOUS
    return "key:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


class UsageTracker:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # RateLimiter já tinha lock; este não tinha, e escritas concorrentes
        # de processos com múltiplas threads podiam intercalar linhas.
        self._lock = threading.Lock()

    def log_usage(self, api_key: str, path: str, method: str, status_code: int):
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "api_key": key_fingerprint(api_key),
            "path": path,
            "method": method,
            "status_code": status_code,
        }
        line = json.dumps(entry) + "\n"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)


class RateLimiter:
    def __init__(self):
        self.requests = {}
        self.lock = threading.Lock()

    def is_allowed(self, api_key: str, limit: int) -> bool:
        now = datetime.now(UTC)
        minute = now.strftime("%Y-%m-%d %H:%M")
        key = f"{api_key}:{minute}"

        with self.lock:
            count = self.requests.get(key, 0)
            if count >= limit:
                return False
            self.requests[key] = count + 1

            # Clean up old entries (simple)
            if len(self.requests) > 1000:
                self.requests = {k: v for k, v in self.requests.items() if minute in k}

            return True


api_key_manager = APIKeyManager(USERS_DB_PATH)
usage_tracker = UsageTracker(USAGE_LOG_PATH)
rate_limiter = RateLimiter()

UF_CODE_TO_ABBR = {
    "11": "RO",
    "12": "AC",
    "13": "AM",
    "14": "RR",
    "15": "PA",
    "16": "AP",
    "17": "TO",
    "21": "MA",
    "22": "PI",
    "23": "CE",
    "24": "RN",
    "25": "PB",
    "26": "PE",
    "27": "AL",
    "28": "SE",
    "29": "BA",
    "31": "MG",
    "32": "ES",
    "33": "RJ",
    "35": "SP",
    "41": "PR",
    "42": "SC",
    "43": "RS",
    "50": "MS",
    "51": "MT",
    "52": "GO",
    "53": "DF",
}


# DiseaseSource + DISEASE_SOURCES moved to domain/disease_sources.py (Fase 0 - Opção A)


# DISEASE_SOURCES moved to domain/disease_sources.py (Fase 0 - Opção A)
    # (DISEASE_SOURCES + labels fully removed - now in domain/disease_sources.py)

# SAO_PAULO_DISTRICTS + LOCALITY_ALIASES moved to aggregation/filters.py (Fase 0 - Filtros)

db_clini: list[dict[str, Any]] = []
db_alertas: list[dict[str, Any]] = []
db_metadata: dict[str, Any] = {
    "status": "not_loaded",
    "periodo": {"ano": DEFAULT_YEAR},
    "fontes": [],
    "erros": [],
}
REPORT_BOOTSTRAP_LOCK = threading.Lock()
REQUESTS_REQUIRING_REPORT = {
    "/",
    "/dashboard",
    "/sobre",
    "/agentes",
    "/agent.json",
    "/llms.txt",
    "/robots.txt",
    "/sitemap.xml",
    "/health",
    "/v1/risk-index",
    "/v1/high-alerts",
    "/v1/metadata",
    "/v1/diseases",
}

SITE_NAME = "Aldeia Viva Saúde"
SITE_TAGLINE = "Inteligência epidemiológica com dados reais do SINAN/OpenDataSUS"
SITE_DESCRIPTION = (
    "Dashboard e API para acompanhar risco epidemiológico municipal, alertas altos, "
    "doenças, agravos e agentes como Dengue, Chikungunya, Zika, Febre Amarela, "
    "Leptospirose e Meningite no Brasil."
)
PUBLIC_PATHS = ("/dashboard", "/sobre", "/agentes", "/docs", "/openapi.json")


# build_source_url and build_dbc_source_url moved to ingestion/sinan_loader.py (Fase 0)


# load_csv_records_from_*, load_dbc_records_from_url, download_bytes,
# fetch_url_bytes and cache_path_for_url moved to ingestion/sinan_loader.py (Fase 0)


# A "Fase 0" moveu estas funcoes para ingestion/sinan_loader.py e deixou no
# lugar apenas este comentario — o import nunca foi acrescentado. Resultado:
# `fetch_epidemiology_report` levantava NameError em toda chamada, e o
# caminho de recarga de dados ficou morto. Nao aparecia porque a cache em
# disco nao expirava e os testes de /v1/refresh mockavam a carga inteira.
from ingestion.sinan_loader import load_latest_available_records


# load_municipality_lookup moved to ingestion/municipality_lookup.py (Fase 0 - improved version with caching)


def fetch_epidemiology_report(year: int = DEFAULT_YEAR) -> dict[str, Any]:
    records_by_disease: dict[str, list[dict[str, str]]] = {}
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    disease_sources = enabled_disease_sources()
    with ThreadPoolExecutor(max_workers=min(4, len(disease_sources) or 1)) as executor:
        futures = {
            executor.submit(load_latest_available_records, source, year): source
            for source in disease_sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                source_year, url, records = future.result()
                records_by_disease[source.codigo] = records
                sources.append(
                    {
                        "codigo": source.codigo,
                        "nome": source.nome,
                        "ano": source_year,
                        "url": url,
                        "registros": len(records),
                    }
                )
            except Exception as error:
                logger.error("Erro ao carregar %s: %s", source.nome, error)
                errors.append({"fonte": source.nome, "erro": str(error)})

    source_order = {
        source.codigo: index for index, source in enumerate(disease_sources)
    }
    sources.sort(
        key=lambda item: source_order.get(clean_value(item.get("codigo")), 999)
    )

    # O ano real de cada fonte ja era conhecido aqui e so ia para o metadata.
    # Agora chega ao builder, para que cada agravo carimbe o proprio ano.
    report = build_epidemiology_report(
        records_by_disease,
        year=year,
        municipality_lookup=load_municipality_lookup(),
        source_years={
            clean_value(item["codigo"]): item["ano"]
            for item in sources
            if item.get("ano") is not None
        },
    )
    report["metadata"]["fontes"] = sources
    report["metadata"]["erros"] = errors
    report["metadata"]["status"] = "ok" if records_by_disease else "empty"
    report["metadata"]["carregado_em"] = datetime.now(UTC).isoformat()
    return report


def enabled_disease_sources() -> list[DiseaseSource]:
    configured_codes = clean_value(os.getenv("SINAN_DISEASE_CODES"))
    if configured_codes:
        codes = [
            code.strip().upper() for code in configured_codes.split(",") if code.strip()
        ]
    else:
        codes = list(DEFAULT_DISEASE_CODES)

    return [DISEASE_SOURCES[code] for code in codes if code in DISEASE_SOURCES]


def report_cache_path(year: int, disease_codes: Iterable[str]) -> Path:
    return report_cache_file_path(SINAN_CACHE_DIR, year, disease_codes)


def report_cache_file_path(
    root_dir: Path, year: int, disease_codes: Iterable[str]
) -> Path:
    normalized_codes = sorted(
        {clean_value(code).upper() for code in disease_codes if clean_value(code)}
    )
    cache_key = json.dumps(
        {
            "version": REPORT_CACHE_VERSION,
            "year": int(year),
            "diseases": normalized_codes,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:16]
    return root_dir / "reports" / f"risk-report-{int(year)}-{digest}.json"


def save_report_cache(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def load_report_cache(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Cache de relatório inválido.")
    return loaded


def public_report_cache_path(path: Path) -> str:
    try:
        return path.relative_to(SINAN_CACHE_DIR).as_posix()
    except ValueError:
        return path.name


_POPULATION_CACHE: dict[str, int] | None = None


def cached_population_lookup() -> dict[str, int]:
    """População do IBGE, buscada uma vez por processo.

    Falha em silêncio de propósito: sem denominador, a interface publica a
    taxa como indisponível — o que é honesto — em vez de derrubar a carga.
    """
    global _POPULATION_CACHE
    if _POPULATION_CACHE is None:
        try:
            _POPULATION_CACHE = load_population_lookup()
        except Exception as error:  # pragma: no cover - defensivo
            logger.warning("População indisponível: %s", error)
            _POPULATION_CACHE = {}
    return _POPULATION_CACHE


def signal_reference_date() -> date:
    """Data contra a qual a idade dos sinais é medida.

    Normalmente é hoje. `SIGNAL_REFERENCE_DATE=AAAA-MM-DD` fixa a referência,
    o que torna a resposta reproduzível para quem precisa reprocessar um
    resultado antigo — inclusive agentes automatizados.
    """
    override = os.getenv("SIGNAL_REFERENCE_DATE")
    if override:
        try:
            return date.fromisoformat(override.strip())
        except ValueError:
            logger.warning(
                "SIGNAL_REFERENCE_DATE inválida (%s); usando a data de hoje.", override
            )
    return datetime.now(UTC).date()


def apply_report_state(
    report: Mapping[str, Any],
    *,
    cache_hit: bool,
    cache_path: Path | None = None,
    cache_source: str | None = None,
) -> dict[str, Any]:
    global db_alertas, db_clini, db_metadata

    metadata = dict(report.get("metadata") or {})
    cache_metadata = dict(metadata.get("cache") or {})
    cache_metadata.update(
        {
            "hit": cache_hit,
            "source": cache_source or ("disk" if cache_hit else "refresh"),
            "path": public_report_cache_path(cache_path) if cache_path else None,
            "aplicado_em": datetime.now(UTC).isoformat(),
        }
    )
    metadata["cache"] = {
        key: value for key, value in cache_metadata.items() if value is not None
    }

    # Recência e denominador populacional são aplicados aqui, no ponto onde
    # as três origens de dado (carga nova, cache em disco, snapshot embarcado)
    # convergem. Assim nenhum consumidor recebe um alerta sem saber a idade
    # dele, nem um número absoluto sem saber sobre quantos habitantes.
    enriched = enrich_report({**report, "metadata": metadata}, signal_reference_date())
    enriched = enrich_with_population(enriched, cached_population_lookup())

    db_clini = list(enriched.get("municipios") or [])
    db_alertas = list(enriched.get("alertas_altos") or [])
    db_metadata = dict(enriched.get("metadata") or metadata)
    # Devolve o que de fato foi aplicado. Antes esta funcao nao retornava
    # nada e `load_or_refresh_report` devolvia o relatorio CRU, entao quem
    # usasse o valor de retorno via uma forma diferente da que a API serve.
    return enriched


def report_has_content(report: Mapping[str, Any]) -> bool:
    metadata = report.get("metadata") or {}
    return clean_value(metadata.get("status")) == "ok" and bool(
        report.get("municipios")
    )


def report_cache_disabled() -> bool:
    return (
        os.getenv("SINAN_DISABLE_REPORT_CACHE") == "1"
        or os.getenv("SINAN_DISABLE_CACHE") == "1"
    )


def load_or_refresh_report(
    year: int = DEFAULT_YEAR, *, force_refresh: bool = False
) -> dict[str, Any]:
    enabled_codes = [source.codigo for source in enabled_disease_sources()]
    cache_path = report_cache_path(year, enabled_codes)

    # Cache vencida nao e servida sem antes tentar buscar dado novo. Guardada
    # em `stale_report` para voltar a ser usada se a busca falhar: dado velho
    # e rotulado (metadata.carga) e melhor que painel vazio.
    stale_report: dict[str, Any] | None = None

    if not force_refresh and not report_cache_disabled() and cache_path.exists():
        try:
            report = load_report_cache(cache_path)
            if not report_has_content(report):
                raise ValueError("Relatório em cache sem conteúdo útil.")
            if cache_is_fresh(
                report,
                signal_reference_date(),
                max_age_days=report_cache_max_age_days(),
            ):
                applied = apply_report_state(
                    report, cache_hit=True, cache_path=cache_path
                )
                logger.info(
                    "Relatório epidemiológico carregado do cache: %s", cache_path
                )
                return applied
            stale_report = report
            logger.info(
                "Cache agregado vencido em %s; tentando recarregar as fontes.",
                cache_path,
            )
        except Exception as error:
            logger.warning("Cache agregado inválido em %s: %s", cache_path, error)

    bundled_report = None
    # O snapshot embarcado so entra quando nao ha cache utilizavel. Com uma
    # cache vencida em maos ele seria um retrocesso: o snapshot e mais antigo
    # que qualquer cache que o proprio servico tenha gravado.
    if (
        not force_refresh
        and stale_report is None
        and load_embedded_report_snapshot is not None
    ):
        try:
            bundled_report = load_embedded_report_snapshot()
        except Exception as error:
            logger.warning("Snapshot embarcado inválido: %s", error)
            bundled_report = None
        if bundled_report is not None and report_has_content(bundled_report):
            applied = apply_report_state(
                bundled_report,
                cache_hit=True,
                cache_path=None,
                cache_source="bundled",
            )
            logger.info("Relatório epidemiológico carregado do snapshot embarcado.")
            return applied

    try:
        report = fetch_epidemiology_report(year)
    except Exception as error:
        logger.warning("Falha ao recarregar as fontes: %s", error)
        report = {"metadata": {"status": "error"}, "municipios": [], "alertas_altos": []}

    if not report_has_content(report):
        # Antes de recorrer ao snapshot embarcado, a cache vencida deste ano
        # e' o dado mais proximo da realidade que temos.
        if stale_report is not None:
            applied = apply_report_state(
                stale_report,
                cache_hit=True,
                cache_path=cache_path,
                cache_source="disk-stale",
            )
            logger.warning(
                "Recarga sem conteúdo; mantendo a cache vencida de %s.", cache_path
            )
            return applied

        if load_embedded_report_snapshot is not None:
            try:
                bundled_report = load_embedded_report_snapshot()
            except Exception as error:
                logger.warning("Snapshot embarcado inválido: %s", error)
                bundled_report = None
        if bundled_report is not None and report_has_content(bundled_report):
            applied = apply_report_state(
                bundled_report,
                cache_hit=True,
                cache_path=None,
                cache_source="bundled",
            )
            logger.warning("Carga real vazia; usando snapshot embarcado.")
            if not report_cache_disabled():
                try:
                    save_report_cache(report=bundled_report, path=cache_path)
                except Exception as error:
                    logger.warning(
                        "Não foi possível salvar cache agregado embarcado em %s: %s",
                        cache_path,
                        error,
                    )
            return applied

    if not report_cache_disabled():
        try:
            save_report_cache(report, cache_path)
            logger.info("Relatório epidemiológico salvo no cache: %s", cache_path)
        except Exception as error:
            logger.warning(
                "Não foi possível salvar cache agregado em %s: %s",
                cache_path,
                error,
            )

    return apply_report_state(
        report,
        cache_hit=False,
        cache_path=None if report_cache_disabled() else cache_path,
        cache_source="refresh",
    )


def report_state_ready() -> bool:
    return db_metadata.get("status") == "ok" and bool(db_clini)


def ensure_report_state_loaded_sync() -> None:
    if report_state_ready():
        return
    with REPORT_BOOTSTRAP_LOCK:
        if report_state_ready():
            return
        load_or_refresh_report(
            DEFAULT_YEAR, force_refresh=os.getenv("SINAN_FORCE_REFRESH") == "1"
        )


async def ensure_report_state_loaded() -> None:
    if report_state_ready():
        return
    await run_in_threadpool(ensure_report_state_loaded_sync)
def extract_municipality_code(record: Mapping[str, Any]) -> str:
    for field in ("ID_MN_RESI", "ID_MUNICIP", "COD_MUN_LPI", "MUNICIPIO", "COMUNINF"):
        code = normalize_municipality_code(record.get(field))
        if code:
            return code
    return ""


def normalize_municipality_code(value: Any) -> str:
    digits = "".join(char for char in clean_value(value) if char.isdigit())
    if not digits or set(digits) == {"0"}:
        return ""
    return digits[:6]


def state_from_record(record: Mapping[str, Any]) -> str:
    municipality_code = extract_municipality_code(record)
    if len(municipality_code) >= 2:
        state = UF_CODE_TO_ABBR.get(municipality_code[:2])
        if state:
            return state

    state_code = clean_code(record.get("SG_UF"))
    if state_code in UF_CODE_TO_ABBR:
        return UF_CODE_TO_ABBR[state_code]
    return ""


def decode_json_payload(payload: bytes) -> Any:
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    return json.loads(payload.decode("utf-8-sig"))


# classification_label is now imported from domain.disease_sources (Fase 0)


















# risk_profile_for_source moved to domain/risk.py (Fase 0)





# filter_risk_index, filter_alerts, resolve_locality_alias, etc.
# moved to aggregation/filters.py (Fase 0 - Filtros)


def public_base_url(request: Request) -> str:
    configured = clean_value(os.getenv("PUBLIC_BASE_URL"))
    if configured:
        return configured.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


def canonical_url(request: Request, path: str) -> str:
    return f"{public_base_url(request)}{path}"


def render_web_page(
    request: Request,
    *,
    path: str,
    title: str,
    description: str,
    body: str,
    json_ld: list[dict[str, Any]],
    active: str,
    extra_head: str = "",
    extra_script: str = "",
) -> str:
    canonical = canonical_url(request, path)
    schema = "\n".join(
        f'<script type="application/ld+json">{json.dumps(item, ensure_ascii=False)}</script>'
        for item in json_ld
    )
    nav_items = (
        ("/dashboard", "Dashboard", "dashboard"),
        ("/sobre", "Metodologia", "sobre"),
        ("/planos", "Planos e API", "planos"),
        ("/agentes", "Agentes", "agentes"),
        ("/docs", "Documentação", "api"),
    )
    nav = "\n".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>'
        for href, label, key in nav_items
    )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(title)}</title>
  <meta name="description" content="{escape_html(description)}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <meta name="geo.region" content="BR">
  <meta name="geo.placename" content="Brasil">
  <link rel="canonical" href="{canonical}">
  <link rel="alternate" type="application/json" href="{canonical_url(request, "/agent.json")}" title="Manifesto para agentes">
  <link rel="alternate" type="text/plain" href="{canonical_url(request, "/llms.txt")}" title="Instruções para LLMs">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{escape_html(SITE_NAME)}">
  <meta property="og:title" content="{escape_html(title)}">
  <meta property="og:description" content="{escape_html(description)}">
  <meta property="og:url" content="{canonical}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{escape_html(title)}">
  <meta name="twitter:description" content="{escape_html(description)}">
  {schema}
  {extra_head}
  <link rel="stylesheet" href="/static/css/main.css">
</head>
<body>
  <a class="skip-link" href="#conteudo-principal">Pular para o conteúdo</a>
  <header class="site-header">
    <a class="brand" href="/dashboard" aria-label="{escape_html(SITE_NAME)}">
      <span class="brand-mark" aria-hidden="true">AV</span>
      <span><strong>{escape_html(SITE_NAME)}</strong><small>{escape_html(SITE_TAGLINE)}</small></span>
    </a>
    <nav aria-label="Navegação principal">{nav}</nav>
  </header>
  {body}
  <footer class="site-footer">
    <div>
      <strong>{escape_html(SITE_NAME)}</strong>
      <span>Dados reais SINAN/OpenDataSUS, enriquecidos por município via IBGE.</span>
    </div>
    <div class="footer-links"><a href="/v1/metadata">Metadados</a> <a href="/agent.json">agent.json</a> <a href="/llms.txt">llms.txt</a></div>
  </footer>
  {extra_script}
</body>
</html>"""


# =============================================================================
# LEGACY INLINE ASSETS (Fase 0 - being phased out)
# These huge strings are kept only as fallback during the transition.
# Real source of truth is now in web/static/css/main.css and web/static/js/dashboard.js
# =============================================================================


def render_dashboard_page(request: Request) -> str:
    """Painel operacional em quatro blocos.

    Cortados nesta reconstrução, por não mudarem nenhuma decisão:
      - hero de landing page (h1 gigante, lead, callout, tres botoes)
      - painel "Radar atual" com quatro métricas nacionais
      - atalhos rápidos, que duplicavam o seletor de nível mínimo
      - legenda de risco, redundante com os próprios badges
      - coluna lateral de alertas, mesmo dado da tabela em outro corte
      - banner que anunciava ao usuário um detalhe de implementação
      - gráfico Chart.js de dois pontos (~200 KB de CDN)
      - payload JSON embutido que script nenhum lia

    Entrou no lugar um único fato, que era o mais importante e o único
    invisível: a idade do dado.
    """
    year = signal_reference_date().year
    period_year = clean_value(db_metadata.get("periodo", {}).get("ano")) or str(
        DEFAULT_YEAR
    )
    body = f"""
<main class="page" id="conteudo-principal">
  <div id="risk-dashboard">
    <header class="dash-head">
      <h1 id="dashboard-title">Risco epidemiológico municipal</h1>
      <p class="lead">Notificações reais do SINAN/OpenDataSUS por município e agravo, com a idade de cada fonte declarada.</p>
    </header>

    {render_data_status(db_metadata)}

    <form class="panel toolbar" id="consulta" data-endpoint="/v1/risk-index">
      <label>Município, distrito ou código IBGE
        <input id="municipio" name="municipio" value="" placeholder="Ex.: Perus, Goiânia ou 355030" autocomplete="address-level2">
      </label>
      <label>UF
        <input id="estado" name="estado" value="" placeholder="SP" maxlength="2" autocomplete="address-level1" autocapitalize="characters">
      </label>
      <label>Nível mínimo
        <select id="nivel_minimo" name="nivel_minimo">
          <option value="">Todos os níveis</option>
          <option value="moderado">Moderado ou acima</option>
          <option value="alto">Alto ou acima</option>
          <option value="critico">Apenas crítico</option>
        </select>
      </label>
      <label>Ordenar por
        <select id="ordenar" name="ordenar">
          <option value="taxa">Incidência por 100 mil</option>
          <option value="score">Score de risco</option>
          <option value="casos">Casos absolutos</option>
          <option value="obitos">Óbitos</option>
        </select>
      </label>
      <div class="toolbar-actions">
        <button class="button ghost" type="reset">Limpar</button>
        <button class="button primary" type="submit">Consultar</button>
      </div>
    </form>

    <section class="panel section-panel" aria-labelledby="municipios-title">
      <div class="section-head">
        <div>
          <h2 id="municipios-title">Municípios</h2>
          <p id="dashboard-status" class="status-line" role="status" aria-live="polite">Ordenado por incidência por 100 mil habitantes. Selecione uma linha para a visão completa por agravo.</p>
        </div>
        <a class="button" href="/sobre">Metodologia</a>
      </div>
      <div class="table-wrap">
        <table aria-describedby="dashboard-status">
          <thead><tr><th>Município</th><th>Risco</th><th class="num">Incidência</th><th>Agravos por idade da fonte</th></tr></thead>
          <tbody id="risk-rows">{render_dashboard_rows(sort_municipalities(db_clini, "taxa")[:8])}</tbody>
        </table>
      </div>
      {render_strip_legend()}
    </section>

    <div id="municipio-detail-panel" class="panel section-panel" hidden>
      <div class="detail-header">
        <div class="detail-title-group">
          <h2 id="detail-municipio-title" class="detail-title"></h2>
          <p id="detail-municipio-subtitle" class="detail-subtitle"></p>
        </div>
        <button type="button" class="button ghost detail-close-btn" id="close-detail">Fechar</button>
      </div>

      <div class="detail-grid">
        <div class="detail-card detail-card--metrics">
          <h3 class="detail-section-title">Resumo</h3>
          <div class="detail-metrics" id="detail-summary"></div>
        </div>

        <div class="detail-card detail-card--wide">
          <div class="detail-section-header">
            <h3 class="detail-section-title">Agravos</h3>
            <span class="detail-hint">Cada linha declara a idade da própria fonte</span>
          </div>
          <div class="detail-table-wrapper">
            <table class="detail-table" id="detail-diseases-table">
              <thead>
                <tr>
                  <th>Agravo</th>
                  <th>Fonte</th>
                  <th>Sinal</th>
                  <th class="num">Casos</th>
                  <th class="num">Graves</th>
                  <th class="num">Óbitos</th>
                  <th>Nível</th>
                </tr>
              </thead>
              <tbody id="detail-diseases-body"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</main>"""
    return render_web_page(
        request,
        path="/dashboard",
        title=f"Dashboard de risco epidemiológico | {SITE_NAME}",
        description=SITE_DESCRIPTION,
        body=body,
        json_ld=base_json_ld(request)
        + [
            dataset_json_ld(request),
            breadcrumb_json_ld(request, "Dashboard", "/dashboard"),
        ],
        active="dashboard",
        extra_script='<script src="/static/js/dashboard.js" defer></script>',
    )


def render_plans_page(request: Request) -> str:
    body = f"""
<main class="page" id="conteudo-principal">
  <section class="hero">
    <div class="prose">
      <p class="eyebrow">Planos e API Professional</p>
      <h1>Apoie o projeto e obtenha acesso ilimitado.</h1>
      <p class="lead">O Aldeia Viva Saúde é um projeto de código aberto que depende de assinaturas para manter a infraestrutura e o processamento de dados.</p>
    </div>
  </section>
  <div class="text-layout">
    <section class="panel section-panel prose">
      <h2>Modelos de Assinatura</h2>
      <div class="link-grid">
        <div class="link-card">
          <strong>Gratuito</strong>
          <span>Acesso público ao dashboard e API com limites estritos (5 registros por busca, 10 req/min).</span>
          <p>R$ 0/mês</p>
        </div>
        <div class="link-card" style="border: 2px solid var(--teal);">
          <strong>Profissional</strong>
          <span>Acesso completo à API, limites ampliados (1000 registros, 100 req/min) e suporte a integração.</span>
          <p>R$ 149/mês</p>
        </div>
        <div class="link-card">
          <strong>Enterprise</strong>
          <span>Relatórios customizados, exportação de dados brutos e acesso prioritário a novos agravos.</span>
          <p>Sob consulta</p>
        </div>
      </div>
      <h2>Por que assinar?</h2>
      <ul>
        <li><strong>Sem limites:</strong> Obtenha todos os municípios em uma única chamada.</li>
        <li><strong>Dados Premium:</strong> Acesso ao endpoint <code>/v1/professional-report</code> com metadados estendidos.</li>
        <li><strong>Sustentabilidade:</strong> Ajude a manter o serviço de inteligência epidemiológica ativo e gratuito para agentes comunitários.</li>
      </ul>
      <div class="actions">
        <a class="button primary" href="mailto:contato@aldeia-viva.com.br?subject=Assinatura%20Professional">Solicitar Chave API</a>
      </div>
    </section>
    <aside class="text-aside" aria-label="Informações Adicionais">
      <article class="note-card"><strong>Chaves API</strong><p>Para obter uma chave, envie um e-mail com sua necessidade. Ativamos chaves gratuitas para pesquisadores e ONGs.</p></article>
      <article class="note-card"><strong>Faturamento</strong><p>Pagamento via PIX ou Boleto para empresas brasileiras.</p></article>
    </aside>
  </div>
</main>"""
    return render_web_page(
        request,
        path="/planos",
        title=f"Planos e API | {SITE_NAME}",
        description="Assine o Aldeia Viva Saúde para obter acesso profissional à API epidemiológica.",
        body=body,
        json_ld=base_json_ld(request)
        + [breadcrumb_json_ld(request, "Planos", "/planos")],
        active="planos",
    )


def render_explanation_page(request: Request) -> str:
    source_rows = "".join(
        f"<li><strong>{escape_html(clean_value(item.get('nome')))}</strong>: "
        f"{format_number(item.get('registros', 0))} registros, ano {escape_html(clean_value(item.get('ano')))}.</li>"
        for item in db_metadata.get("fontes", [])
    )
    formula_rows = "".join(
        f"<li><strong>{escape_html(source.nome)}</strong>: "
        f"<code>{escape_html(risk_profile_for_source(source).formula)}</code></li>"
        for source in DISEASE_SOURCES.values()
    )
    body = f"""
<main class="page" id="conteudo-principal">
  <section class="hero">
    <div class="prose">
      <p class="eyebrow">Dados e metodologia</p>
      <h1>Como o índice transforma notificações em decisão.</h1>
      <p class="lead">A API consolida dados do SINAN/OpenDataSUS por município, doença e vírus, preservando a granularidade municipal disponível nos CSVs públicos.</p>
    </div>
  </section>
  <section class="facts" aria-label="Pontos metodológicos">
    <article class="fact"><h3>Fonte</h3><p>SINAN/OpenDataSUS e arquivos DBC do DATASUS para notificações; IBGE para nomes e UFs dos municípios.</p></article>
    <article class="fact"><h3>Granularidade municipal</h3><p>Distritos e bairros, como Perus, são resolvidos para o município oficial quando houver alias conhecido.</p></article>
    <article class="fact"><h3>Limite de uso</h3><p>Dados não substituem vigilância epidemiológica oficial, investigação local ou validação clínica.</p></article>
  </section>
  <div class="text-layout">
    <section class="panel section-panel prose">
      <h2>Fórmula do score</h2>
      <p>O score prioriza volume, gravidade, sinais de alarme, hospitalizações e óbitos. Na fase atual, cada doença ou agravo possui um perfil de risco próprio.</p>
      <pre class="code-block">{escape_html(RISK_FORMULA)}</pre>
      <ul>{formula_rows}</ul>
      <h2>Fontes carregadas</h2>
      <ul>{source_rows or "<li>Nenhuma fonte carregada nesta instância.</li>"}</ul>
      <h2>Interpretação</h2>
      <p>O nível <strong>crítico</strong> aparece quando há óbitos ou score muito elevado. O nível <strong>alto</strong> aparece quando há gravidade ou concentração relevante de casos. A saída lista doenças e vírus para facilitar leitura por gestores, sistemas e agentes.</p>
    </section>
    <aside class="text-aside" aria-label="Resumo metodológico">
      <article class="note-card"><strong>Uso recomendado</strong><p>Priorize a investigação local dos municípios com risco alto ou crítico e valide sinais graves nas fontes oficiais.</p></article>
      <article class="note-card"><strong>Leitura do score</strong><p>O score organiza prioridade operacional; ele não substitui vigilância epidemiológica, diagnóstico ou boletins oficiais.</p></article>
      <article class="note-card"><strong>Granularidade</strong><p>Quando o dado de bairro não existe na fonte, a API informa o município oficial associado à consulta.</p></article>
    </aside>
  </div>
</main>"""
    return render_web_page(
        request,
        path="/sobre",
        title=f"Dados, fontes e metodologia | {SITE_NAME}",
        description=(
            "Entenda as fontes SINAN/OpenDataSUS, a fórmula de risco, a granularidade "
            "municipal e as limitações da API epidemiológica."
        ),
        body=body,
        json_ld=base_json_ld(request)
        + [
            dataset_json_ld(request),
            breadcrumb_json_ld(request, "Dados e metodologia", "/sobre"),
        ],
        active="sobre",
    )


def render_agents_page(request: Request) -> str:
    body = f"""
<main class="page" id="conteudo-principal">
  <section class="hero">
    <div class="prose">
      <p class="eyebrow">Consumo por agentes e integrações</p>
      <h1>Conteúdo estruturado para sistemas, LLMs e automações.</h1>
      <p class="lead">Esta página descreve os endpoints estáveis, a semântica dos campos e os limites de uso para agentes consumirem o conteúdo com baixa ambiguidade.</p>
      <div class="actions">
        <a class="button primary" href="/agent.json">Abrir agent.json</a>
        <a class="button" href="/llms.txt">Abrir llms.txt</a>
      </div>
    </div>
  </section>
  <div class="text-layout">
    <section class="panel section-panel prose">
      <h2>Endpoints recomendados</h2>
      <div class="link-grid">
        <a class="link-card" href="/v1/high-alerts"><strong>/v1/high-alerts</strong><span>Alertas altos e críticos por município, doença e vírus.</span></a>
        <a class="link-card" href="/v1/risk-index"><strong>/v1/risk-index</strong><span>Índice enriquecido com filtros por município, UF e nível mínimo.</span></a>
        <a class="link-card" href="/v1/diseases"><strong>/v1/diseases</strong><span>Catálogo de doenças e agravos suportados pela API.</span></a>
        <a class="link-card" href="/v1/bairros"><strong>/v1/bairros</strong><span>Lista agrupada de bairros/distritos (SP, RJ, MG, PE) para busca por nome (resolve para município).</span></a>
        <a class="link-card" href="/v1/metadata"><strong>/v1/metadata</strong><span>Fontes, ano, status da carga e fórmula de risco.</span></a>
        <a class="link-card" href="/openapi.json"><strong>/openapi.json</strong><span>Contrato OpenAPI para geração de clientes e ferramentas.</span></a>
      </div>
      <h2>Exemplo</h2>
      <pre class="code-block">GET /v1/high-alerts?estado=SP&amp;limite=10
GET /v1/risk-index?municipio=perus&amp;estado=SP&amp;somente_altos=false</pre>
      <h2>Regras de interpretação</h2>
      <p>Use <code>nivel_risco</code> para priorização, <code>risk_score</code> para ordenação, <code>/v1/diseases</code> para descobrir agravos carregados e <code>filtro_localidade</code> para identificar quando a consulta original foi feita por um <strong>bairro ou distrito</strong> (suportado em SP, RJ, MG e PE). O sistema resolve o nome para o município correspondente — a granularidade dos dados continua municipal.</p>
    </section>
    <aside class="text-aside" aria-label="Orientações para integrações">
      <article class="note-card"><strong>Comece por alertas</strong><p>Use <code>/v1/high-alerts</code> para triagem e <code>/v1/risk-index</code> para telas de exploração com filtros.</p></article>
      <article class="note-card"><strong>Baixa ambiguidade</strong><p>Prefira códigos de município e UF quando disponíveis para evitar homônimos ou aliases locais.</p></article>
      <article class="note-card"><strong>Automação segura</strong><p>Registre parâmetros consultados e não extrapole bairro/distrito quando a granularidade retornada for municipal.</p></article>
      <article class="note-card"><strong>Busca por bairro</strong><p>Use nomes de bairros/distritos de São Paulo, Rio de Janeiro, Belo Horizonte ou Recife (ex: Perus, Copacabana, Savassi, Boa Viagem). O sistema resolve automaticamente para o município e inclui <code>filtro_localidade</code>. Veja a lista em <code>/v1/bairros</code>.</p></article>
    </aside>
  </div>

  <div class="text-layout">
    <section class="panel section-panel prose">
      <h2>Bairros e distritos suportados (multi-cidade)</h2>
      <p>
        O sistema resolve nomes de bairros e distritos para o município oficial (granularidade sempre municipal).
        Atualmente suportamos <strong>São Paulo (distritos)</strong>, <strong>Rio de Janeiro</strong>, <strong>Belo Horizonte</strong> e <strong>Recife</strong>.
        Ao usar um destes nomes no parâmetro <code>municipio</code>, a resposta inclui <code>filtro_localidade</code> com a origem da consulta.
      </p>

      <p><strong>Exemplos de buscas que funcionam:</strong></p>
      <div style="display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0;">
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">perus</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">grajaú</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">copacabana</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">ipanema</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">savassi</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">pampulha</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">boa viagem</code>
        <code style="background:#f1f5f9; padding:2px 8px; border-radius:4px;">madalena</code>
      </div>

      <p>
        <strong>Lista completa e estruturada:</strong> <a href="/v1/bairros">GET /v1/bairros</a>
        (retorna todas as cidades com seus bairros em formato agrupado).
      </p>

      <p style="font-size: 0.9rem; color: #64748b;">
        Dica para agentes: Prefira buscar pelo nome do bairro quando estiver em campo. O sistema entrega os dados do município com o contexto da origem (bairro → município).
      </p>
    </section>
  </div>
</main>"""
    return render_web_page(
        request,
        path="/agentes",
        title=f"Guia para agentes e integrações | {SITE_NAME}",
        description=(
            "Contrato de consumo para agentes, LLMs e integrações que precisam consultar "
            "alertas epidemiológicos e índice de risco."
        ),
        body=body,
        json_ld=base_json_ld(request)
        + [
            software_json_ld(request),
            breadcrumb_json_ld(request, "Agentes", "/agentes"),
        ],
        active="agentes",
    )


# Legacy inline JS (Fase 0) - real implementation moved to web/static/js/dashboard.js


def dashboard_summary() -> dict[str, int]:
    return {
        "municipios_monitorados": len(db_clini),
        "alertas_altos": len(db_alertas),
        "casos_provaveis": sum_int(
            row.get("total_casos_provaveis") for row in db_clini
        ),
        "obitos": sum_int(row.get("total_obitos") for row in db_clini),
    }


def render_dashboard_rows(rows: Iterable[Mapping[str, Any]]) -> str:
    """Quatro colunas. A coluna com nomes de doenças virou tira de agravos:
    mesma informação em menos pixels, mais a idade da fonte que faltava."""
    year = signal_reference_date().year
    rendered = []
    for row in rows:
        nome = escape_html(row.get("municipio"))
        estado = escape_html(row.get("estado"))
        codigo = escape_html(row.get("codigo_municipio"))

        filtro = row.get("filtro_localidade") or {}
        if filtro.get("tipo") in {"distrito", "bairro"}:
            origem = escape_html(filtro.get("localidade", ""))
            sub = f"{origem} &rarr; {estado} &middot; {codigo}"
        else:
            sub = f"{estado} &middot; {codigo}"

        altas = [clean_value(item.get("nome")) for item in row.get("doencas_altas", [])]
        if altas:
            resumo = ", ".join(escape_html(x) for x in altas[:2])
            if len(altas) > 2:
                resumo += f" +{len(altas) - 2}"
        else:
            resumo = "sem agravo em nível alto"

        strip = render_signal_strip(
            sort_diseases_for_strip(row.get("doencas") or []), year
        )

        rendered.append(
            '<tr class="municipality-row" tabindex="0" role="button" '
            f'data-codigo="{codigo}" aria-label="Abrir visão completa de {nome}">'
            f'<td data-label="Município"><strong>{nome}</strong>'
            f'<span class="cell-sub">{sub}</span>'
            f'<span class="cell-sub">{resumo}</span></td>'
            f'<td data-label="Risco">{render_risk_cell(row, render_badge)}</td>'
            f'<td data-label="Incidência" class="num">{render_incidence_cell(row)}</td>'
            f'<td data-label="Agravos">{strip}</td>'
            "</tr>"
        )
    return (
        "".join(rendered)
        or '<tr><td class="empty-cell" colspan="4"><strong>Nenhum município encontrado.</strong>'
           'Remova a UF, amplie o nível mínimo ou busque pelo código IBGE.</td></tr>'
    )


def render_alert_items(alerts: Iterable[Mapping[str, Any]]) -> str:
    rendered = []
    for alert in alerts:
        rendered.append(
            f'<article class="alert-item {normalize_text(clean_value(alert.get("nivel_risco")) or "baixo")}">'
            f"<header><strong>{escape_html(alert.get('municipio'))}/{escape_html(alert.get('estado'))}</strong>{render_badge(alert.get('nivel_risco'))}</header>"
            f"<p><strong>{escape_html(alert.get('doenca'))}</strong> · "
            f"{escape_html(alert.get('virus'))} · {format_number(alert.get('casos_provaveis'))} casos prováveis</p>"
            f"<p>Graves {format_number(alert.get('casos_graves'))} · Óbitos {format_number(alert.get('obitos'))} · "
            f"Score {format_number(alert.get('risk_score'))}</p>"
            "</article>"
        )
    return (
        "".join(rendered)
        or '<p class="status-line">Nenhum alerta alto carregado. Tente ampliar o filtro ou consultar todos os níveis.</p>'
    )


def render_metric(label: str, value: Any) -> str:
    return (
        '<div class="metric">'
        f"<span>{escape_html(label)}</span>"
        f"<strong>{format_number(value)}</strong>"
        "</div>"
    )


def render_badge(value: Any) -> str:
    raw_label = clean_value(value) or "baixo"
    level = normalize_text(raw_label)
    label = {
        "critico": "Crítico",
        "alto": "Alto",
        "moderado": "Moderado",
        "baixo": "Baixo",
    }.get(level, raw_label)
    marker = {"critico": "●", "alto": "▲", "moderado": "◆", "baixo": "●"}.get(
        level, "●"
    )
    return f'<span class="badge {level}">{marker} {escape_html(label)}</span>'


def base_json_ld(request: Request) -> list[dict[str, Any]]:
    base = public_base_url(request)
    return [
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "@id": f"{base}/#organization",
            "name": SITE_NAME,
            "url": base,
            "areaServed": "BR",
        },
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "@id": f"{base}/#website",
            "name": SITE_NAME,
            "url": base,
            "description": SITE_DESCRIPTION,
            "inLanguage": "pt-BR",
            "publisher": {"@id": f"{base}/#organization"},
        },
    ]


def dataset_json_ld(request: Request) -> dict[str, Any]:
    base = public_base_url(request)
    sources = [
        clean_value(item.get("url"))
        for item in db_metadata.get("fontes", [])
        if clean_value(item.get("url"))
    ]
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "@id": f"{base}/#dataset-epidemiologia",
        "name": "Índice de risco epidemiológico municipal",
        "description": SITE_DESCRIPTION,
        "url": canonical_url(request, "/dashboard"),
        "license": "https://dados.gov.br/",
        "isBasedOn": sources,
        "spatialCoverage": {"@type": "Place", "name": "Brasil"},
        "temporalCoverage": str(
            db_metadata.get("periodo", {}).get("ano", DEFAULT_YEAR)
        ),
        "creator": {"@id": f"{base}/#organization"},
        "variableMeasured": [
            "casos_provaveis",
            "casos_graves",
            "sinais_alarme",
            "hospitalizacoes",
            "obitos",
            "risk_score",
            "nivel_risco",
        ],
    }


def software_json_ld(request: Request) -> dict[str, Any]:
    base = public_base_url(request)
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "@id": f"{base}/#api",
        "name": "Epidemiology Intelligence API",
        "applicationCategory": "HealthApplication",
        "operatingSystem": "Web",
        "url": canonical_url(request, "/agentes"),
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "BRL"},
    }


def breadcrumb_json_ld(request: Request, name: str, path: str) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": SITE_NAME,
                "item": canonical_url(request, "/dashboard"),
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": name,
                "item": canonical_url(request, path),
            },
        ],
    }


def agent_freshness() -> dict[str, Any]:
    """Bloco de frescor legível por máquina.

    Antes expunha apenas `status`, `loaded_at`, `period` e `cache` — nada que
    permitisse a um agente decidir se podia afirmar que um número descreve o
    presente. Agora carrega os três relógios do sistema.
    """
    recency = db_metadata.get("recencia") or {}
    load = db_metadata.get("carga") or {}
    return {
        "status": db_metadata.get("status"),
        "loaded_at": db_metadata.get("carregado_em"),
        "load_age_days": load.get("idade_dias"),
        "is_current": load.get("atualizada", False),
        "warning": load.get("aviso"),
        "period": db_metadata.get("periodo"),
        "data_horizon": recency.get("horizonte_dado"),
        "total_sources": recency.get("agravos_total"),
        "current_year_sources": recency.get("agravos_com_fonte_do_ano_corrente"),
        "sources_by_year": recency.get("fontes_por_ano"),
        "sources": recency.get("fontes"),
        "freshness_definition": recency.get("definicao"),
        "cache": db_metadata.get("cache"),
    }


def agent_manifest(request: Request) -> dict[str, Any]:
    base = public_base_url(request)
    return {
        "name": "Epidemiology Intelligence API",
        "description": SITE_DESCRIPTION,
        "language": "pt-BR",
        "base_url": base,
        "freshness": agent_freshness(),
        "limits": {
            "anonymous": {
                "max_results": TIER_MAX_LIMIT["anonymous"],
                "rate_limit_per_minute": 10,
            },
            "free": {"max_results": TIER_MAX_LIMIT["free"]},
            "note": (
                "Sem cabeçalho X-API-Key, `limite` é rebaixado silenciosamente ao teto "
                "do tier anônimo. Leia os cabeçalhos de resposta para saber se houve corte."
            ),
            "response_headers": {
                "X-Total-Results": "total de registros que satisfazem o filtro",
                "X-Returned-Results": "quantos vieram nesta resposta",
                "X-Limit-Applied": "teto efetivamente aplicado",
            },
        },
        "interpretation": [
            "Cada agravo traz `fonte.ano`: o ano do arquivo SINAN de onde ele veio. "
            "O relatório é uma colcha de anos — não assuma que `periodo.ano` descreve o agravo.",
            "Cada agravo traz `recencia.frescor`, medido contra o horizonte da PRÓPRIA fonte. "
            "`vivo` significa que o município notificou até o fim daquele arquivo, "
            "não que o fato seja de hoje.",
            "Antes de afirmar que algo é a situação atual, verifique `fonte.do_ano_corrente` "
            "e `metadata.carga.atualizada`. Um agravo `vivo` com `fonte.ano` de 2022 é dado de 2022.",
            "`metadata.carga.idade_dias` diz há quantos dias o serviço não busca dados novos. "
            "É falha operacional do serviço, não fato epidemiológico sobre os municípios.",
            "`risk_score` é soma ponderada de contagens absolutas, sem denominador populacional. "
            "Ordenar por ele aproxima uma ordenação por população; não o leia como incidência.",
            "`nivel_risco` consolida os agravos de TODOS os anos-fonte: é gravidade histórica. "
            "`nivel_risco_fonte_atual` olha só os agravos cujo arquivo é do ano corrente e é o "
            "que responde 'exige ação agora?'. No dado atual: 24,3% contra 8,8% de 'crítico'.",
            "`historico_mais_grave` indica que o município já esteve em nível pior por conta de "
            "agravos de fontes antigas. Use para contexto, nunca para priorizar ação de hoje.",
            "`incidencia.por_100k` é a única medida comparável entre municípios de portes "
            "diferentes. Use `ordenar=taxa` para priorizar por ela; `ordenar=score` (padrão) "
            "correlaciona 0,82 com a população e responde 'onde há mais casos', não 'onde é pior'.",
            "`incidencia.confiavel` é falso quando a população é pequena demais para a taxa ser "
            "estável. Nesse caso publique o número com a ressalva, nunca sozinho.",
        ],
        "recommended_use": [
            "Use /v1/high-alerts para priorizar municípios com doenças em nível alto ou crítico.",
            "Use /v1/risk-index para explicar o contexto completo por município.",
            "Use doencas[].formula_risco e /v1/diseases para interpretar o score de cada agravo.",
            "Use /v1/metadata para citar fontes, ano carregado e fórmula de risco.",
        ],
        "limitations": [
            "A granularidade pública processada é municipal.",
            "Consultas por distrito ou bairro podem ser resolvidas para o município correspondente quando houver alias conhecido.",
            "Alguns DBCs grandes, como ANIM, são suportados mas podem exigir habilitação explícita por SINAN_DISEASE_CODES.",
            "O relatório reúne arquivos-fonte de anos diferentes por agravo; "
            "consulte fonte.ano em cada um antes de datar uma afirmação.",
            "Não há população municipal na base, logo não há taxa por 100 mil habitantes.",
            "Os dados não substituem vigilância epidemiológica oficial ou investigação local.",
        ],
        "endpoints": {
            "/v1/high-alerts": {
                "method": "GET",
                "description": "Alertas altos e críticos por município, doença e vírus.",
                "query": ["municipio", "estado", "doenca", "limite"],
            },
            "/v1/risk-index": {
                "method": "GET",
                "description": "Índice enriquecido por município com doenças, vírus, score e classificação.",
                "query": [
                    "ano",
                    "municipio",
                    "estado",
                    "somente_altos",
                    "nivel_minimo",
                    "ordenar",
                    "limite",
                ],
                "ordenacoes": list(ORDERINGS),
            },
            "/v1/metadata": {
                "method": "GET",
                "description": "Status da carga, fontes e fórmula do score.",
                "query": [],
            },
            "/v1/diseases": {
                "method": "GET",
                "description": "Catálogo de doenças/agravos atualmente suportados pela API.",
                "query": [],
            },
            "/v1/bairros": {
                "method": "GET",
                "description": "Lista de bairros/distritos suportados em SP, RJ, MG e PE (resolvidos automaticamente para o município). Suporta filtros ?uf= e ?municipio=.",
                "query": ["uf", "municipio"],
            },
            "/openapi.json": {
                "method": "GET",
                "description": "Contrato OpenAPI da API.",
                "query": [],
            },
        },
        "example_requests": [
            f"{base}/v1/high-alerts?estado=SP&limite=10",
            f"{base}/v1/risk-index?municipio=perus&estado=SP&somente_altos=false",
        ],
    }


def supported_diseases_catalog() -> list[dict[str, Any]]:
    sources_by_code = {
        clean_value(item.get("codigo")): item for item in db_metadata.get("fontes", [])
    }
    catalog: list[dict[str, Any]] = []
    for source in DISEASE_SOURCES.values():
        loaded = sources_by_code.get(source.codigo, {})
        catalog.append(
            {
                "codigo": source.codigo,
                "nome": source.nome,
                "virus": source.virus,
                "tipo": source.tipo,
                "primeiro_ano_suportado": source.first_year,
                "habilitado_por_padrao": source.codigo in DEFAULT_DISEASE_CODES,
                "carregado": bool(loaded),
                "perfil_risco": source.risk_profile,
                "formula_risco": risk_profile_for_source(source).formula,
                "ano_carregado": loaded.get("ano"),
                "registros_carregados": loaded.get("registros"),
                "url": loaded.get("url")
                or source.direct_csv_url
                or catalog_source_url(source),
                "fonte": (
                    "CSV direto OpenDataSUS"
                    if source.direct_csv_url
                    else "DBC DATASUS/SINAN"
                    if source.dbc_prefix
                    else "CSV anual zipado SINAN/OpenDataSUS"
                ),
            }
        )
    return catalog


def catalog_source_url(source: DiseaseSource) -> str:
    from ingestion.sinan_loader import build_dbc_source_url, build_source_url

    if source.dbc_prefix:
        return build_dbc_source_url(source, source.latest_year or DEFAULT_YEAR)
    if source.folder and source.file_prefix:
        return build_source_url(
            source, min(DEFAULT_YEAR, source.latest_year or DEFAULT_YEAR)
        )
    return ""


def llms_text(request: Request) -> str:
    """Instruções para LLMs.

    Reescrito em pt-BR (o resto do produto sempre foi) e corrigido: a versão
    anterior afirmava que bairro só era resolvido em São Paulo, quando o
    código suporta SP, RJ, MG e PE — e não dizia uma palavra sobre a idade
    das fontes nem sobre o teto silencioso de resultados.
    """
    base = public_base_url(request)
    load = db_metadata.get("carga") or {}
    recency = db_metadata.get("recencia") or {}
    age = load.get("idade_dias")
    idade = f"{age} dia(s)" if age is not None else "idade desconhecida"
    current = recency.get("agravos_com_fonte_do_ano_corrente")
    total = recency.get("agravos_total")
    teto = TIER_MAX_LIMIT["anonymous"]
    return f"""# {SITE_NAME}

{SITE_DESCRIPTION}

Endpoints:
- /v1/high-alerts — alertas altos e críticos por município, doença e vírus.
- /v1/risk-index — índice enriquecido por município. Aceita `ano` para carregar outro ano.
- /v1/diseases — catálogo de agravos suportados.
- /v1/bairros — bairros e distritos resolvidos para município em SP, RJ, MG e PE. Filtre com ?uf=RJ.
- /v1/metadata — estado da carga, fontes, fórmula do score e os blocos temporais.

Base URL: {base}
OpenAPI: {base}/openapi.json
Manifesto para agentes: {base}/agent.json
Painel humano: {base}/dashboard
Metodologia: {base}/sobre

Estado atual desta instância:
- Carga com {idade}.
- {current if current is not None else "?"} de {total if total is not None else "?"} agravos com arquivo-fonte do ano corrente.

Regras de interpretação (leia antes de afirmar qualquer coisa):
- A granularidade pública processada é municipal. Não infira contagem por bairro ou distrito.
- `filtro_localidade` indica que a consulta usou nome de bairro ou distrito; o dado
  devolvido continua sendo municipal.
- Cada agravo traz `fonte.ano`: o ano do arquivo SINAN de onde ele veio. O relatório reúne
  anos diferentes por agravo, e `periodo.ano` apenas repete o ano solicitado — não o tome
  como o ano do dado.
- Cada agravo traz `recencia.frescor`, medido contra o horizonte da própria fonte.
  `vivo` quer dizer que o município notificou até o fim daquele arquivo, não que o fato
  seja de hoje. Um agravo `vivo` com `fonte.ano` igual a 2022 continua sendo dado de 2022.
- `metadata.carga.idade_dias` mede há quanto tempo o serviço não busca dados novos.
  É falha operacional do serviço, não fato epidemiológico sobre os municípios.
- `risk_score` é soma ponderada de contagens absolutas, sem denominador populacional:
  {RISK_FORMULA}
  Ordenar por ele aproxima uma ordenação por população; não o leia como incidência.
  Não há população na base, logo não há taxa por 100 mil habitantes.
- Cada agravo tem `formula_risco` própria; não assuma uma fórmula única para todos.
- Sem cabeçalho X-API-Key, o parâmetro `limite` é rebaixado ao teto anônimo ({teto}
  resultados). Leia X-Total-Results, X-Returned-Results e X-Limit-Applied para saber
  se a resposta foi cortada.
- Os dados não substituem vigilância epidemiológica oficial nem investigação local.
"""


def sitemap_xml(request: Request) -> str:
    now = datetime.now(UTC).date().isoformat()
    urls = "\n".join(
        f"  <url><loc>{canonical_url(request, path)}</loc><lastmod>{now}</lastmod></url>"
        for path in PUBLIC_PATHS
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>\n'


def sum_int(values: Iterable[Any]) -> int:
    total = 0
    for value in values:
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def format_number(value: Any) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def escape_html(value: Any) -> str:
    return html.escape(clean_value(value), quote=True)


TIER_ANONYMOUS = "anonymous"


TIER_MAX_LIMIT = {"anonymous": 5, "free": 20}


def tier_limit(user: Mapping[str, Any], requested: int) -> int:
    """Teto de resultados por tier. Antes estava duplicado em cada endpoint."""
    ceiling = TIER_MAX_LIMIT.get(str(user.get("tier")))
    return min(requested, ceiling) if ceiling else requested


def declare_result_counts(
    response: Response | None, *, total: int, returned: int, limit: int
) -> None:
    """Publica o corte em headers.

    Sem isto, um cliente que pede 25 e recebe 5 não tem como distinguir
    "só existem 5" de "você foi truncado" — e o painel afirmava a primeira
    leitura enquanto a segunda era a verdadeira.
    """
    if response is None:
        return
    response.headers["X-Total-Results"] = str(total)
    response.headers["X-Returned-Results"] = str(returned)
    response.headers["X-Limit-Applied"] = str(limit)


async def get_api_user(x_api_key: str | None = Header(None)):
    tier_info = {"tier": TIER_ANONYMOUS, "rate_limit": 10}
    request_key = TIER_ANONYMOUS

    if x_api_key:
        user = api_key_manager.validate_key(x_api_key)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API Key",
            )
        tier_info = user
        request_key = x_api_key

    if not rate_limiter.is_allowed(request_key, tier_info["rate_limit"]):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )

    return tier_info


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Iniciando ingestão de dados reais do SINAN/OpenDataSUS...")
    load_or_refresh_report(
        DEFAULT_YEAR, force_refresh=os.getenv("SINAN_FORCE_REFRESH") == "1"
    )
    yield


app = FastAPI(
    title="Epidemiology Intelligence API",
    description=(
        "API com dados reais do SINAN/OpenDataSUS para índice de risco "
        "epidemiológico por município, doença/vírus e alertas altos. "
        "Documentação interativa em **/docs**; esquema OpenAPI em **/openapi.json**."
    ),
    version="2.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    tags_metadata=[
        {
            "name": "Risco",
            "description": (
                "Métricas, score e alertas processados a partir de dados reais "
                "de notificação do SINAN/OpenDataSUS."
            ),
        },
        {
            "name": "Sistema",
            "description": "Utilitários e verificação de saúde do serviço.",
        },
    ],
)


# Serve extracted static assets (CSS + future JS islands) - Fase 0 UI extraction
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
else:
    logger.warning("STATIC_DIR %s does not exist - static assets will not be served", STATIC_DIR)


@app.middleware("http")
async def track_usage_middleware(request: Request, call_next):
    api_key = request.headers.get("X-API-Key", TIER_ANONYMOUS)
    response = await call_next(request)

    # Log usage only for API endpoints
    if request.url.path.startswith("/v1/"):
        usage_tracker.log_usage(
            api_key, request.url.path, request.method, response.status_code
        )

    return response


@app.middleware("http")
async def bootstrap_report_state(request: Request, call_next):
    if request.url.path in REQUESTS_REQUIRING_REPORT:
        await ensure_report_state_loaded()
    return await call_next(request)


@app.get("/", tags=["Sistema"], include_in_schema=False, response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    return HTMLResponse(render_dashboard_page(request))


@app.get(
    "/dashboard",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(render_dashboard_page(request))


@app.get(
    "/sobre",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def sobre(request: Request) -> HTMLResponse:
    return HTMLResponse(render_explanation_page(request))


@app.get(
    "/agentes",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def agentes(request: Request) -> HTMLResponse:
    return HTMLResponse(render_agents_page(request))


@app.get(
    "/planos",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def planos(request: Request) -> HTMLResponse:
    return HTMLResponse(render_plans_page(request))


@app.get("/agent.json", tags=["Sistema"], include_in_schema=False)
async def get_agent_manifest(request: Request) -> JSONResponse:
    return JSONResponse(agent_manifest(request))


@app.get(
    "/llms.txt",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=PlainTextResponse,
)
async def get_llms_txt(request: Request) -> PlainTextResponse:
    return PlainTextResponse(llms_text(request))


@app.get(
    "/robots.txt",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=PlainTextResponse,
)
async def get_robots_txt(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nSitemap: {canonical_url(request, '/sitemap.xml')}\n"
    )


@app.get(
    "/sitemap.xml",
    tags=["Sistema"],
    include_in_schema=False,
    response_class=Response,
)
async def get_sitemap_xml(request: Request) -> Response:
    return Response(content=sitemap_xml(request), media_type="application/xml")


@app.get("/health", tags=["Sistema"], summary="Health check")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "data_status": db_metadata.get("status"),
        "carga": db_metadata.get("carga"),
        "loaded_at": db_metadata.get("carregado_em"),
        "cache": db_metadata.get("cache"),
    }


@app.get(
    "/v1/risk-index",
    tags=["Risco"],
    summary="Índice de risco enriquecido",
    response_description="Municípios com totais, score, nível de risco e doenças/vírus.",
)
async def get_risk_index(
    ano: int | None = Query(
        default=None,
        description="Ano específico para carregar os dados (permite comparação). Se omitido, usa o ano global configurado.",
    ),
    municipio: str | None = Query(
        default=None,
        description="Filtra por nome parcial ou código de município DataSUS/IBGE sem dígito.",
    ),
    estado: str | None = Query(default=None, description="Filtra por UF, ex.: SP."),
    somente_altos: bool = Query(
        default=False,
        description="Retorna apenas municípios com doença em nível alto/crítico.",
    ),
    nivel_minimo: str | None = Query(
        default=None, description="baixo, moderado, alto ou critico."
    ),
    ordenar: str = Query(
        default="score",
        pattern="^(score|taxa|casos|obitos)$",
        description=(
            "Critério de ordenação. `score` soma contagens absolutas e "
            "correlaciona 0,82 com a população; `taxa` usa incidência por 100 "
            "mil habitantes e é o que compara municípios de portes diferentes."
        ),
    ),
    limite: int = Query(default=100, ge=1, le=1000),
    response: Response = None,  # type: ignore[assignment]
    user: dict = Depends(get_api_user),
):
    limite = tier_limit(user, limite)

    effective_year = ano if ano is not None else DEFAULT_YEAR

    if effective_year != DEFAULT_YEAR:
        # Carga de outro ano sob demanda. Vai para a threadpool: a versão
        # anterior fazia download síncrono dentro de um `async def`, travando
        # o event loop a cada clique numa linha do painel.
        year_report = await run_in_threadpool(fetch_epidemiology_report, effective_year)
        year_rows = year_report.get("municipios", [])
    else:
        year_rows = db_clini

    rows = filter_risk_index(
        year_rows,
        municipio=municipio,
        estado=estado,
        somente_altos=somente_altos,
        nivel_minimo=nivel_minimo,
    )
    rows = sort_municipalities(rows, ordenar)
    page = rows[:limite] if rows else []
    declare_result_counts(response, total=len(rows), returned=len(page), limit=limite)
    if not rows:
        return {"message": "Dados não disponíveis para o filtro."}
    return page


@app.get(
    "/v1/high-alerts",
    tags=["Risco"],
    summary="Doenças e vírus em nível alto ou crítico",
    response_description="Lista consolidada de alertas altos por município e doença/vírus.",
)
async def get_high_alerts(
    municipio: str | None = Query(default=None),
    estado: str | None = Query(default=None),
    doenca: str | None = Query(
        default=None, description="Filtra por nome ou código: DENG, CHIK ou ZIKA."
    ),
    limite: int = Query(default=100, ge=1, le=1000),
    response: Response = None,  # type: ignore[assignment]
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    limite = tier_limit(user, limite)

    alerts = filter_alerts(
        db_alertas, municipio=municipio, estado=estado, doenca=doenca
    )
    page = alerts[:limite]
    declare_result_counts(response, total=len(alerts), returned=len(page), limit=limite)
    return {
        "metadata": db_metadata,
        "total": len(alerts),
        "retornados": len(page),
        "limite_aplicado": limite,
        "alerts": page,
    }


@app.get(
    "/v1/professional-report",
    tags=["Risco"],
    summary="Relatório profissional detalhado (Premium)",
    response_description="Dados detalhados para análise profissional.",
)
async def get_professional_report(
    municipio: str | None = Query(default=None),
    estado: str | None = Query(default=None),
    user: dict = Depends(get_api_user),
):
    if user["tier"] not in {"premium", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este endpoint requer uma assinatura Premium.",
        )

    rows = filter_risk_index(db_clini, municipio=municipio, estado=estado)

    # Add advanced analytics for Professional tier
    summary = {
        "total_municipios": len(rows),
        "total_casos_provaveis": sum(r["total_casos_provaveis"] for r in rows),
        "total_obitos": sum(r["total_obitos"] for r in rows),
        "media_risk_score": round(sum(r["risk_score"] for r in rows) / len(rows), 2)
        if rows
        else 0,
        "distribuicao_risco": {
            "critico": len([r for r in rows if r["nivel_risco"] == "critico"]),
            "alto": len([r for r in rows if r["nivel_risco"] == "alto"]),
            "moderado": len([r for r in rows if r["nivel_risco"] == "moderado"]),
            "baixo": len([r for r in rows if r["nivel_risco"] == "baixo"]),
        },
    }

    return {
        "metadata": {
            **db_metadata,
            "report_type": "professional",
            "generated_for": user.get("owner"),
            "analytics_version": "1.0.0",
        },
        "summary": summary,
        "data": rows,
    }


@app.post(
    "/v1/refresh",
    tags=["Sistema"],
    summary="Reprocessa os dados reais e atualiza o cache agregado",
)
async def refresh_report(
    force_refresh: bool = Query(
        default=True,
        description="Quando true, ignora o cache agregado e reprocessa as fontes reais.",
    ),
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    # Único endpoint que reprocessa a carga inteira. Estava aberto: sem chave,
    # sem rate limit, acionável por qualquer um.
    if user["tier"] not in {"premium", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Recarregar a base exige uma chave de API com permissão de escrita.",
        )
    await run_in_threadpool(
        load_or_refresh_report, DEFAULT_YEAR, force_refresh=force_refresh
    )
    return {
        "metadata": db_metadata,
        "municipios": len(db_clini),
        "alertas_altos": len(db_alertas),
    }


@app.get(
    "/v1/export/pdf",
    tags=["Risco"],
    summary="Gera relatório PDF (Premium)",
    response_description="Arquivo PDF com análise epidemiológica.",
)
async def export_pdf(
    municipio: str | None = Query(default=None),
    estado: str | None = Query(default=None),
    user: dict = Depends(get_api_user),
):
    # Este endpoint respondia 200 com "Relatório PDF gerado com sucesso" e uma
    # `download_url` apontando para /reports/custom/..., rota que não existe no
    # app: um 404 garantido, vendido como sucesso. Para um agente autônomo isso
    # é pior que um erro — ele registra a operação como concluída e segue.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Geração de PDF não implementada. Use /v1/professional-report para "
            "obter os mesmos dados em JSON e renderizar do seu lado."
        ),
    )


@app.get(
    "/v1/metadata",
    tags=["Sistema"],
    summary="Metadados da carga de dados",
)
async def get_metadata() -> dict[str, Any]:
    return db_metadata


@app.get(
    "/v1/diseases",
    tags=["Risco"],
    summary="Doenças e agravos suportados",
)
async def get_diseases() -> dict[str, Any]:
    diseases = supported_diseases_catalog()
    return {
        "total": len(diseases),
        "doencas": diseases,
    }


@app.get(
    "/v1/bairros",
    tags=["Risco"],
    summary="Bairros e distritos suportados para resolução (multi-cidade)",
    response_description="Lista agrupada de bairros/distritos de SP, RJ, MG e PE. Use ?uf=RJ ou ?municipio=recife para filtrar.",
)
async def get_bairros(
    uf: str | None = Query(default=None, description="Filtrar por UF (ex: RJ, MG)"),
    municipio: str | None = Query(default=None, description="Filtrar por nome do município (ex: recife, 'rio de janeiro')"),
) -> dict[str, Any]:
    return get_supported_bairros(uf=uf, municipio=municipio)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
