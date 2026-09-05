import csv
import gzip
import hashlib
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

# =============================================================================
# Static assets (Fase 0 - extração de interface para permitir melhorias sustentáveis)
# =============================================================================
STATIC_DIR = APP_ROOT / "web" / "static"
CSS_PATH = STATIC_DIR / "css" / "main.css"
JS_DASHBOARD_PATH = STATIC_DIR / "js" / "dashboard.js"

_CSS_CACHE: str | None = None

def load_main_css() -> str:
    """Load extracted main.css with simple cache + safe fallback to inline constant."""
    global _CSS_CACHE
    if _CSS_CACHE is not None:
        return _CSS_CACHE
    try:
        if CSS_PATH.exists():
            _CSS_CACHE = CSS_PATH.read_text(encoding="utf-8")
            return _CSS_CACHE
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to load external CSS at %s, using fallback: %s", CSS_PATH, exc)
    # Temporary fallback while we finish extraction (will be removed after full cutover)
    _CSS_CACHE = BASE_CSS  # type: ignore[name-defined]
    return _CSS_CACHE



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
        return self.keys.get(api_key)


class UsageTracker:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_usage(self, api_key: str, path: str, method: str, status_code: int):
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "api_key": api_key,
            "path": path,
            "method": method,
            "status_code": status_code,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


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


# load_latest_available_records, normalize_dbf_record and filter_records_by_latest_available_year
# moved to ingestion/sinan_loader.py (Fase 0 - Ingestão)


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

    report = build_epidemiology_report(
        records_by_disease,
        year=year,
        municipality_lookup=load_municipality_lookup(),
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
) -> None:
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

    # A recência do sinal é aplicada aqui, no ponto onde as três origens de
    # dado (carga nova, cache em disco, snapshot embarcado) convergem. Assim
    # nenhum consumidor recebe um alerta sem saber a idade dele.
    enriched = enrich_report({**report, "metadata": metadata}, signal_reference_date())

    db_clini = list(enriched.get("municipios") or [])
    db_alertas = list(enriched.get("alertas_altos") or [])
    db_metadata = dict(enriched.get("metadata") or metadata)


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

    if not force_refresh and not report_cache_disabled() and cache_path.exists():
        try:
            report = load_report_cache(cache_path)
            if not report_has_content(report):
                raise ValueError("Relatório em cache sem conteúdo útil.")
            apply_report_state(report, cache_hit=True, cache_path=cache_path)
            logger.info("Relatório epidemiológico carregado do cache: %s", cache_path)
            return report
        except Exception as error:
            logger.warning("Cache agregado inválido em %s: %s", cache_path, error)

    bundled_report = None
    if not force_refresh and load_embedded_report_snapshot is not None:
        try:
            bundled_report = load_embedded_report_snapshot()
        except Exception as error:
            logger.warning("Snapshot embarcado inválido: %s", error)
            bundled_report = None
        if bundled_report is not None and report_has_content(bundled_report):
            apply_report_state(
                bundled_report,
                cache_hit=True,
                cache_path=None,
                cache_source="bundled",
            )
            logger.info("Relatório epidemiológico carregado do snapshot embarcado.")
            return bundled_report

    report = fetch_epidemiology_report(year)
    if not report_has_content(report):
        if load_embedded_report_snapshot is not None:
            try:
                bundled_report = load_embedded_report_snapshot()
            except Exception as error:
                logger.warning("Snapshot embarcado inválido: %s", error)
                bundled_report = None
        if bundled_report is not None and report_has_content(bundled_report):
            apply_report_state(
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
            return bundled_report

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

    apply_report_state(
        report,
        cache_hit=False,
        cache_path=None if report_cache_disabled() else cache_path,
        cache_source="refresh",
    )
    return report


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


def fetch_and_process_data(
    estado: str = "BR", ano: int = DEFAULT_YEAR, mes: int | None = None
) -> list[dict[str, Any]]:
    report = fetch_epidemiology_report(ano)
    state_filter = None if estado.upper() == "BR" else estado
    return filter_risk_index(report["municipios"], estado=state_filter)


# build_epidemiology_report moved to aggregation/report_builder.py (Fase 0)
# The version below was the original and has been replaced by the import above.


def add_record_to_summaries(
    disease: dict[str, Any],
    municipality: dict[str, Any],
    source: DiseaseSource,
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








# finalize_disease_summary moved to domain/risk.py (Fase 0)
# Keeping the call site working via the import at the top of the file.



# risk_level moved to domain/risk.py (Fase 0)

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
BASE_CSS = """
:root {
  color-scheme: light;
  --ink: #17211c;
  --muted: #5d6b63;
  --line: #d9e4dd;
  --panel: #ffffff;
  --soft: #f4f8f5;
  --green: #146c43;
  --teal: #067a76;
  --amber: #9a5b00;
  --red: #b42318;
  --blue: #2457a6;
  --shadow: 0 18px 45px rgba(23, 33, 28, .08);
  --radius: 16px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background:
    radial-gradient(circle at top left, rgba(20, 108, 67, .08), transparent 32rem),
    linear-gradient(180deg, #f7faf8 0%, #fbfdfb 42%, #f7faf8 100%);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.5;
}
a { color: inherit; }
.skip-link {
  position: fixed;
  left: 16px;
  top: 12px;
  z-index: 100;
  transform: translateY(-160%);
  padding: 10px 14px;
  border-radius: 999px;
  background: var(--ink);
  color: #fff;
  font-weight: 800;
  text-decoration: none;
  transition: transform .18s ease;
}
.skip-link:focus { transform: translateY(0); }
.site-header {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: clamp(20px, 4vw, 48px);
  min-height: 80px;
  padding: 16px clamp(22px, 5vw, 72px);
  border-bottom: 1px solid rgba(20, 108, 67, .16);
  background: rgba(247, 250, 248, .94);
  backdrop-filter: blur(16px);
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  min-width: min(310px, 42vw);
  flex-shrink: 0;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: linear-gradient(135deg, var(--green), var(--teal));
  color: #fff;
  font-weight: 900;
  letter-spacing: -.03em;
  box-shadow: 0 10px 24px rgba(20, 108, 67, .22);
}
.brand strong { display: block; font-size: 1rem; letter-spacing: 0; }
.brand small { display: block; max-width: 360px; color: var(--muted); font-size: .78rem; }
nav { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
nav a {
  min-height: 40px;
  padding: 9px 13px;
  border-radius: 999px;
  color: var(--muted);
  font-size: .92rem;
  font-weight: 750;
  text-decoration: none;
}
nav a:hover { background: #eef7f2; color: var(--green); }
nav a.active { background: var(--green); color: #fff; box-shadow: 0 8px 20px rgba(20, 108, 67, .18); }
.page { max-width: 1360px; margin: 0 auto; padding: clamp(36px, 5vw, 64px) clamp(22px, 5vw, 56px) 72px; }
.hero {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(360px, .85fr);
  gap: clamp(36px, 6vw, 72px);
  align-items: center;
  padding: 36px 0 48px;
}
.eyebrow {
  margin: 0 0 12px;
  color: var(--green);
  font-size: .82rem;
  font-weight: 800;
  letter-spacing: .08em;
  text-transform: uppercase;
}
h1, h2, h3 { margin: 0; line-height: 1.08; letter-spacing: 0; }
h1 { max-width: 780px; font-size: clamp(2.1rem, 4.2vw, 4rem); letter-spacing: -.04em; }
h2 { font-size: clamp(1.45rem, 3vw, 2.35rem); letter-spacing: -.03em; }
h3 { font-size: 1.04rem; }
.lead { max-width: 760px; color: var(--muted); font-size: clamp(1.05rem, 2vw, 1.24rem); }
.summary-callout {
  max-width: 720px;
  margin: 20px 0 0;
  padding: 16px 18px;
  border: 1px solid rgba(20, 108, 67, .16);
  border-left: 5px solid var(--green);
  border-radius: 0 14px 14px 0;
  background: linear-gradient(90deg, #e8f5ee, rgba(255, 255, 255, .78));
  color: #123d28;
  font-weight: 800;
}
.actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 22px; }
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 10px 15px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
  color: var(--ink);
  font-weight: 800;
  text-decoration: none;
  cursor: pointer;
}
.button.primary { border-color: var(--green); background: var(--green); color: #fff; box-shadow: 0 10px 22px rgba(20, 108, 67, .14); }
.button.ghost { background: transparent; }
.button:hover { border-color: rgba(20, 108, 67, .42); }
.button.primary:hover { background: #0f5d38; }
.button:active { transform: translateY(0); }
.button:disabled { cursor: wait; opacity: .72; box-shadow: none; }
.panel {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  box-shadow: var(--shadow);
}
.radar-panel { padding: clamp(22px, 3vw, 30px); }
.radar-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.radar-head .eyebrow { margin-bottom: 8px; }
.data-freshness { padding: 6px 9px; border-radius: 999px; background: var(--soft); color: var(--muted); font-size: .78rem; font-weight: 800; white-space: nowrap; }
.radar-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
.metric { min-height: 118px; padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: linear-gradient(180deg, #ffffff, var(--soft)); }
.metric span { color: var(--muted); font-size: .78rem; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
.metric strong { display: block; margin-top: 10px; font-size: clamp(1.65rem, 4vw, 2.45rem); line-height: 1; letter-spacing: -.04em; }
.toolbar {
  display: grid;
  grid-template-columns: minmax(280px, 1.3fr) minmax(96px, .35fr) minmax(180px, .55fr) minmax(240px, .75fr) minmax(220px, auto);
  gap: 16px;
  align-items: end;
  margin: 28px 0 18px;
  padding: clamp(18px, 2.4vw, 24px);
}
.toolbar-actions { display: flex; gap: 12px; align-items: end; justify-content: flex-end; flex-wrap: nowrap; }
label { display: grid; gap: 6px; color: var(--muted); font-size: .82rem; font-weight: 700; }
input, select {
  width: 100%;
  min-height: 44px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 9px 11px;
  background: #fff;
  color: var(--ink);
  font: inherit;
}
input:hover, select:hover { border-color: rgba(20, 108, 67, .38); }
input:focus, select:focus, .button:focus { outline: 3px solid rgba(6, 122, 118, .2); outline-offset: 2px; }
:focus-visible { outline: 3px solid rgba(6, 122, 118, .28); outline-offset: 3px; }
.switch { display: flex; align-items: center; gap: 9px; min-height: 42px; padding-top: 22px; color: var(--ink); }
.switch input { width: 18px; min-height: 18px; }
.quick-filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 0 0 18px; }
.quick-filters span { color: var(--muted); font-size: .82rem; font-weight: 800; text-transform: uppercase; letter-spacing: .06em; }
.quick-filters .button[aria-pressed="true"] { border-color: var(--green); background: #e7f2ec; color: var(--green); }
.risk-legend { display: grid; grid-template-columns: repeat(4, minmax(190px, 1fr)); gap: 12px; margin-bottom: 26px; }
.risk-legend span { display: flex; align-items: center; gap: 10px; min-height: 72px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: rgba(255, 255, 255, .82); color: var(--muted); font-size: .9rem; line-height: 1.45; }
.risk-legend .badge { flex: 0 0 auto; }
.content-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, .65fr); gap: 24px; align-items: start; }
.section-panel { padding: clamp(22px, 2.8vw, 30px); }
.section-panel h2 { font-size: clamp(1.75rem, 2.4vw, 2.45rem); }
.section-head { display: flex; align-items: start; justify-content: space-between; gap: 20px; margin-bottom: 22px; }
.section-head p { margin: 6px 0 0; color: var(--muted); }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }
table { width: 100%; min-width: 780px; border-collapse: collapse; background: #fff; }
th, td { padding: 16px 18px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
td { line-height: 1.45; }
td:first-child { min-width: 170px; }
td:nth-child(2) { min-width: 120px; }
td:nth-child(5) { max-width: 520px; }
th { position: sticky; top: 0; z-index: 1; color: var(--muted); font-size: .76rem; text-transform: uppercase; letter-spacing: .06em; background: #f7faf8; }
tbody tr { transition: background .18s ease; }
tbody tr:hover { background: #f4f8f5; }
tr:last-child td { border-bottom: 0; }
.empty-cell { padding: 22px; color: var(--muted); }
.empty-cell strong { display: block; color: var(--ink); margin-bottom: 4px; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 27px;
  padding: 4px 9px;
  border-radius: 999px;
  background: #edf2f7;
  color: var(--muted);
  font-size: .78rem;
  font-weight: 800;
}
.badge.critico { background: #fee4e2; color: var(--red); }
.badge.alto { background: #fff3d6; color: var(--amber); }
.badge.moderado { background: #e4f4ff; color: var(--blue); }
.badge.baixo { background: #e8f5ee; color: var(--green); }
.alert-list { display: grid; gap: 12px; }
.alert-item { padding: 16px 18px; border: 1px solid var(--line); border-left: 5px solid var(--green); border-radius: 12px; background: #fff; }
.alert-item.critico { border-left-color: var(--red); }
.alert-item.alto { border-left-color: var(--amber); }
.alert-item.moderado { border-left-color: var(--blue); }
.alert-item header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.alert-item header strong { line-height: 1.25; }
.alert-item p { margin: 0; color: var(--muted); line-height: 1.5; }
.facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; margin: 34px 0; }
.fact { min-height: 148px; padding: 24px; border: 1px solid var(--line); border-radius: 14px; background: #fff; }
.fact p { margin: 10px 0 0; color: var(--muted); }
.text-layout { display: grid; grid-template-columns: minmax(0, .9fr) minmax(320px, .38fr); gap: 24px; align-items: start; }
.text-aside { display: grid; gap: 14px; }
.note-card { padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: linear-gradient(180deg, #ffffff, var(--soft)); }
.note-card strong { display: block; margin-bottom: 6px; }
.note-card p { margin: 0; color: var(--muted); }

/* ====== Visão Completa por Município - Design mais premium ====== */
#municipio-detail-panel {
  border-radius: 20px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.06);
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
  margin-bottom: 24px;
  padding-bottom: 18px;
  border-bottom: 1px solid #f1f5f9;
}

.detail-title-group {
  min-width: 0;
}

.detail-title {
  font-size: 1.65rem;
  font-weight: 700;
  line-height: 1.1;
  color: #0f172a;
  margin: 0 0 6px 0;
  letter-spacing: -0.025em;
}

.detail-subtitle {
  font-size: 0.95rem;
  color: #64748b;
  margin: 0;
  line-height: 1.3;
}

.detail-close-btn {
  flex-shrink: 0;
  font-size: 0.9rem;
  padding: 8px 18px;
  border-radius: 9999px;
  min-height: 44px; /* better touch target */
  min-width: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 18px;
}

.detail-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 16px;
  padding: 22px 24px;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.04);
  transition: box-shadow 0.2s cubic-bezier(0.4, 0, 0.2, 1), 
              transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

.detail-card:hover {
  box-shadow: 0 4px 14px rgba(15, 23, 42, 0.07);
  transform: translateY(-1px);
}

.detail-card--wide {
  grid-column: 1 / -1;
}

.detail-section-title {
  font-size: 1.02rem;
  font-weight: 600;
  color: #0f172a;
  margin: 0 0 14px 0;
  letter-spacing: -0.01em;
}

.detail-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.detail-hint {
  font-size: 0.75rem;
  color: #94a3b8;
  font-weight: 500;
}

.detail-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 10px;
}

.detail-metrics .stat-item {
  background: #f8fafc;
  border: 1px solid #e0e7ff;
  border-radius: 12px;
  padding: 13px 15px;
  transition: all 0.15s ease;
}

.detail-metrics .stat-item:hover {
  border-color: #c7d2fe;
  background: #f1f5f9;
}

.stat-label {
  font-size: 0.72rem;
  font-weight: 600;
  color: #64748b;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  display: block;
  margin-bottom: 3px;
}

.detail-table-wrapper {
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  overflow: hidden;
  background: #fff;
}

.detail-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 0.9rem;
}

.detail-table th {
  background: #f1f5f9;
  color: #475569;
  font-weight: 600;
  font-size: 0.73rem;
  text-transform: uppercase;
  letter-spacing: 0.7px;
  padding: 11px 14px;
  text-align: left;
  border-bottom: 1px solid #e2e8f0;
}

.detail-table th.num {
  text-align: right;
}

.detail-table td {
  padding: 11px 14px;
  border-bottom: 1px solid #f1f5f9;
  vertical-align: middle;
  color: #334155;
}

.detail-table tr:last-child td {
  border-bottom: none;
}

.detail-table tr:hover td {
  background: #f8fafc;
}

.detail-table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  font-weight: 500;
}

.detail-placeholder {
  padding: 18px 0 4px;
  color: #64748b;
  font-size: 0.9rem;
  line-height: 1.55;
}

.detail-placeholder p {
  margin: 0;
}

/* ====== Responsividade da Visão Completa ====== */
@media (max-width: 768px) {
  .detail-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 20px;
    padding-bottom: 14px;
  }

  .detail-close-btn {
    align-self: flex-end;
    padding: 10px 20px;
    font-size: 0.95rem;
  }

  .detail-grid {
    grid-template-columns: 1fr;
    gap: 16px;
  }

  .detail-card {
    padding: 18px 20px;
    border-radius: 14px;
  }

  .detail-metrics {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }

  .detail-table {
    font-size: 0.82rem;
  }

  .detail-table th,
  .detail-table td {
    padding: 8px 10px;
  }
}

@media (max-width: 480px) {
  .detail-title {
    font-size: 1.35rem;
  }

  .detail-subtitle {
    font-size: 0.85rem;
  }

  .detail-metrics {
    grid-template-columns: 1fr;
  }

  .detail-table-wrapper {
    margin: 0 -4px; /* allow table to breathe */
  }

  .detail-table {
    font-size: 0.78rem;
  }

  .detail-table th,
  .detail-table td {
    padding: 6px 8px;
  }

  .detail-close-btn {
    padding: 8px 16px;
    font-size: 0.9rem;
  }
}

/* Horizontal scroll for the detail table on small screens */
@media (max-width: 640px) {
  .detail-table-wrapper {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    border-radius: 12px;
  }

  .detail-table {
    min-width: 620px; /* forces horizontal scroll when needed */
  }
}
.prose { max-width: 960px; }
.prose h2 { margin-top: 34px; }
.prose h2:first-child { margin-top: 0; }
.prose p, .prose li { color: var(--muted); }
.prose li { margin: 8px 0; }
.prose ul { padding-left: 1.2rem; }
.prose code, .code-block { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
.code-block {
  overflow-x: auto;
  padding: 18px;
  border: 1px solid rgba(19, 32, 25, .2);
  border-radius: 14px;
  background: #132019;
  color: #eef8f1;
  font-size: .9rem;
  line-height: 1.7;
}
.link-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 22px; }
.link-card { padding: 20px; border: 1px solid var(--line); border-radius: 14px; background: #fff; text-decoration: none; }
.link-card strong { display: block; color: var(--green); font-size: 1.02rem; }
.link-card span { display: block; color: var(--muted); margin-top: 6px; }
.status-line { min-height: 24px; color: var(--muted); font-size: .92rem; }
.table-hint { margin: 14px 0 0; color: var(--muted); font-size: .86rem; }
.skeleton-line {
  display: block;
  width: 100%;
  max-width: 560px;
  height: 14px;
  border-radius: 999px;
  background: linear-gradient(90deg, #edf4ef 0%, #f8fbf9 45%, #edf4ef 90%);
  background-size: 220% 100%;
}
.skeleton-line.short { max-width: 260px; }
.site-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 30px clamp(22px, 5vw, 72px);
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: .88rem;
}
.site-footer div:first-child { display: grid; gap: 4px; }
.site-footer strong { color: var(--ink); }
.footer-links { display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }
.site-footer a { padding: 7px 10px; border-radius: 999px; color: var(--green); font-weight: 800; text-decoration: none; }
.site-footer a:hover { background: #e7f2ec; }
@media (max-width: 1180px) {
  .site-header { align-items: flex-start; flex-direction: column; }
  nav { justify-content: flex-start; }
  .hero { grid-template-columns: 1fr; }
  .toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .toolbar-actions { justify-content: flex-start; }
  .content-grid { grid-template-columns: 1fr; }
}
@media (max-width: 820px) {
  .site-header { position: static; }
  nav { justify-content: flex-start; overflow-x: auto; width: 100%; flex-wrap: nowrap; padding-bottom: 4px; }
  nav a { white-space: nowrap; }
  .page { padding: 26px 16px 56px; }
  .hero, .content-grid, .facts, .link-grid, .risk-legend, .text-layout { grid-template-columns: 1fr; }
  .quick-filters .button { flex: 1 1 140px; }
  .toolbar { grid-template-columns: 1fr; }
  .toolbar-actions { justify-content: stretch; flex-wrap: wrap; }
  .toolbar-actions .button { flex: 1 1 180px; }
  .switch { padding-top: 0; }
  .radar-grid { grid-template-columns: 1fr; }
  table, thead, tbody, tr, td { display: block; min-width: 0; width: 100%; }
  thead { display: none; }
  th { position: static; }
  tr { padding: 12px; border-bottom: 1px solid var(--line); }
  td { display: grid; grid-template-columns: 112px 1fr; gap: 10px; max-width: none; min-width: 0; padding: 8px 0; border-bottom: 0; }
  td::before { content: attr(data-label); color: var(--muted); font-size: .76rem; font-weight: 800; text-transform: uppercase; letter-spacing: .05em; }
  .empty-cell { display: block; }
  .empty-cell::before { content: none; }
  .site-footer { align-items: flex-start; flex-direction: column; }
  .footer-links { justify-content: flex-start; }
}
@media (prefers-reduced-motion: no-preference) {
  .button, nav a, .link-card, .alert-item, .metric, .fact { transition: transform .18s ease, border-color .18s ease, background .18s ease, box-shadow .18s ease; }
  .button:hover, .link-card:hover, .alert-item:hover, .metric:hover, .fact:hover { transform: translateY(-1px); }
  .link-card:hover, .fact:hover { border-color: rgba(20, 108, 67, .32); box-shadow: 0 14px 34px rgba(23, 33, 28, .07); }
  [aria-busy="true"] .skeleton-line { animation: shimmer 1.15s ease-in-out infinite; }
}
@keyframes shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
"""


def render_dashboard_page(request: Request) -> str:
    summary = dashboard_summary()
    alerts = db_alertas[:6]
    initial_json = json.dumps(
        {"summary": summary, "alerts": alerts, "municipios": db_clini[:20]},
        ensure_ascii=False,
    )
    alert_count = summary["alertas_altos"]
    summary_sentence = (
        f"{format_number(alert_count)} alerta(s) alto(s) ou crítico(s) carregado(s) nas fontes atuais."
        if alert_count
        else "Nenhum alerta alto ou crítico carregado nas fontes atuais."
    )
    period_year = clean_value(db_metadata.get("periodo", {}).get("ano")) or str(
        DEFAULT_YEAR
    )
    body = f"""
<main class="page" id="conteudo-principal">
  <div id="risk-dashboard">
  <section class="hero" aria-labelledby="dashboard-title">
    <div>
      <p class="eyebrow">Monitoramento epidemiológico municipal</p>
      <h1 id="dashboard-title">Risco epidemiológico de múltiplos agravos em uma visão operacional.</h1>
      <p class="lead">Dados reais do SINAN/OpenDataSUS e DBCs do DATASUS, enriquecidos por município e organizados para leitura executiva, técnica e automatizada.</p>
      <p class="summary-callout">{escape_html(summary_sentence)}</p>
      <div class="actions">
        <a class="button primary" href="#consulta">Consultar risco</a>
        <a class="button" href="/v1/high-alerts">Ver JSON de alertas</a>
        <a class="button" href="/sobre">Entender metodologia</a>
      </div>
    </div>
    <aside class="panel radar-panel" aria-label="Resumo da carga atual">
      <div class="radar-head">
        <div>
          <p class="eyebrow">Radar atual</p>
          <h2>Prioridade operacional</h2>
        </div>
        <span class="data-freshness">Ano-base {escape_html(period_year)}</span>
      </div>
      <div class="radar-grid">
        {render_metric("Municípios", summary["municipios_monitorados"])}
        {render_metric("Alertas altos", summary["alertas_altos"])}
        {render_metric("Casos prováveis", summary["casos_provaveis"])}
        {render_metric("Óbitos", summary["obitos"])}
      </div>
    </aside>
  </section>

  <form class="panel toolbar" id="consulta" data-endpoint="/v1/risk-index">
    <label>Município, distrito ou código
      <input id="municipio" name="municipio" value="perus" placeholder="Ex: Perus, São Paulo ou 355030" autocomplete="address-level2">
    </label>
    <label>UF
      <input id="estado" name="estado" value="SP" placeholder="SP" maxlength="2" autocomplete="address-level1" autocapitalize="characters">
    </label>
    <label>Nível mínimo
      <select id="nivel_minimo" name="nivel_minimo">
        <option value="">Todos</option>
        <option value="moderado">Moderado+</option>
        <option value="alto">Alto+</option>
        <option value="critico">Crítico</option>
      </select>
    </label>
    <label class="switch"><input id="somente_altos" name="somente_altos" type="checkbox"> Mostrar apenas alto ou crítico</label>
    <div class="toolbar-actions">
      <button class="button ghost" type="reset">Limpar filtros</button>
      <button class="button primary" type="submit">Atualizar</button>
    </div>
  </form>

  <div class="quick-filters" aria-label="Atalhos de consulta">
    <span>Atalhos</span>
    <button class="button ghost" type="button" data-quick-level="" aria-pressed="true">Todos</button>
    <button class="button ghost" type="button" data-quick-level="moderado" aria-pressed="false">Moderado+</button>
    <button class="button ghost" type="button" data-quick-level="alto" aria-pressed="false">Alto+</button>
    <button class="button ghost" type="button" data-quick-level="critico" aria-pressed="false">Crítico</button>
  </div>

  <div class="risk-legend" aria-label="Legenda dos níveis de risco">
    <span>{render_badge("baixo")} baixa concentração ou ausência de sinais graves</span>
    <span>{render_badge("moderado")} volume ou sinais relevantes</span>
    <span>{render_badge("alto")} gravidade, concentração ou hospitalizações</span>
    <span>{render_badge("critico")} óbitos ou score muito elevado</span>
  </div>

  <div class="content-grid">
    <section class="panel section-panel" aria-labelledby="municipios-title">
      <div class="section-head">
        <div>
          <h2 id="municipios-title">Resultado por município</h2>
          <p id="dashboard-status" class="status-line" role="status" aria-live="polite">Pronto para consulta. Clique em uma linha para ver a visão completa.</p>
        </div>
        <a class="button" href="/sobre">Metodologia</a>
      </div>
      <div class="table-wrap">
        <table aria-describedby="dashboard-status">
          <thead><tr><th>Município</th><th>Risco</th><th>Casos</th><th>Óbitos</th><th>Doenças altas</th></tr></thead>
          <tbody id="risk-rows">{render_dashboard_rows(db_clini[:8])}</tbody>
        </table>
      </div>
      <p class="table-hint">Dica: clique em qualquer linha para abrir a visão completa com todos os indicadores por doença.</p>
    </section>

    <aside class="panel section-panel" aria-labelledby="alertas-title">
      <div class="section-head">
        <div>
          <h2 id="alertas-title">Alertas altos</h2>
          <p>Destaques por doença, vírus e município.</p>
        </div>
      </div>
      <div class="alert-list" id="alert-list">{render_alert_items(alerts)}</div>
    </aside>
  </div>

  <!-- Visão Completa por Município - Design mais refinado -->
  <div id="municipio-detail-panel" class="panel section-panel" style="display: none; margin-top: 28px;">
    <div style="margin-bottom: 12px; padding: 10px 14px; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 8px; font-size: 0.82rem; color: #166534;">
      <strong>Comparação automática ativada:</strong> Ao abrir esta visão, os dados do ano anterior são carregados automaticamente para permitir comparação ano a ano.
    </div>

    <div class="detail-header">
      <div class="detail-title-group">
        <h2 id="detail-municipio-title" class="detail-title"></h2>
        <p id="detail-municipio-subtitle" class="detail-subtitle"></p>
      </div>
      <button type="button" class="button ghost detail-close-btn" id="close-detail">
        <span>Fechar</span>
      </button>
    </div>

    <div class="detail-grid">
      <!-- Resumo com métricas mais elegantes -->
      <div class="detail-card detail-card--metrics">
        <h3 class="detail-section-title">Resumo do Município</h3>
        <div class="detail-metrics" id="detail-summary"></div>
      </div>

      <!-- Tabela de todos os agravos - visual premium -->
      <div class="detail-card detail-card--wide">
        <div class="detail-section-header">
          <h3 class="detail-section-title">Todos os Agravos</h3>
          <span class="detail-hint">Dados consolidados do ano</span>
        </div>
        <div class="detail-table-wrapper">
          <table class="detail-table" id="detail-diseases-table">
            <thead>
              <tr>
                <th>Agravo</th>
                <th class="num">Casos</th>
                <th class="num">Alarme</th>
                <th class="num">Graves</th>
                <th class="num">Hosp.</th>
                <th class="num">Óbitos</th>
                <th class="num">Score</th>
                <th>Nível</th>
              </tr>
            </thead>
            <tbody id="detail-diseases-body"></tbody>
          </table>
        </div>
      </div>

      <!-- Comparação Ano Anterior (agora funcional) -->
      <div class="detail-card">
        <h3 class="detail-section-title">Comparação com Ano Anterior</h3>
        <div id="detail-comparison">
          <!-- Conteúdo preenchido dinamicamente via JS -->
          <p class="detail-placeholder">Carregando comparação com o ano anterior...</p>
        </div>
      </div>

      <!-- Evolução Temporal - Primeiro React island (Chart.js leve) -->
      <div class="detail-card">
        <h3 class="detail-section-title">Evolução Temporal</h3>
        <div id="evolution-chart-container">
          <canvas id="evolution-chart" height="120"></canvas>
        </div>
        <p id="evolution-chart-hint" style="margin-top: 8px; font-size: 0.78rem; color: #64748b;">
          Dados do ano atual + anterior carregados automaticamente.
        </p>
      </div>
    </div>
  </div>
  <script id="initial-dashboard-data" type="application/json">{escape_html(initial_json)}</script>
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
        # Load extracted dashboard JS (Fase 0) - the giant inline string is now in web/static/js/dashboard.js
        extra_script='<script src="/static/js/dashboard.js"></script>',
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
DASHBOARD_JS = r"""
const form = document.getElementById('consulta');
const rows = document.getElementById('risk-rows');
const alerts = document.getElementById('alert-list');
const statusLine = document.getElementById('dashboard-status');
const submitButton = form.querySelector('button[type="submit"]');
const ufInput = document.getElementById('estado');
const levelClass = (value) => String(value || 'baixo').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const fmt = new Intl.NumberFormat('pt-BR');

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function badge(value) {
  const level = levelClass(value);
  const label = { critico: 'Crítico', alto: 'Alto', moderado: 'Moderado', baixo: 'Baixo' }[level] || value || 'Baixo';
  const marker = { critico: '●', alto: '▲', moderado: '◆', baixo: '●' }[level] || '●';
  return `<span class="badge ${level}">${marker} ${escapeHtml(label)}</span>`;
}

function diseaseNames(items) {
  if (!items || items.length === 0) return 'Sem alerta alto';
  return items.map((item) => `${escapeHtml(item.nome || item.doenca)} (${escapeHtml(item.nivel_risco || 'alto')})`).join(', ');
}

function renderRows(items) {
  if (!Array.isArray(items) || items.length === 0) {
    rows.innerHTML = '<tr><td class="empty-cell" colspan="5"><strong>Nenhum município encontrado.</strong>Tente remover a UF, consultar todos os níveis ou buscar pelo código municipal.</td></tr>';
    return;
  }
  rows.innerHTML = items.map((item) => {
    let municipioHtml = `<strong>${escapeHtml(item.municipio)}</strong><br><span class="status-line">${escapeHtml(item.estado)} · ${escapeHtml(item.codigo_municipio)}</span>`;

    const filtro = item.filtro_localidade;
    if (filtro && filtro.tipo === "distrito") {
      municipioHtml = `<strong>${escapeHtml(item.municipio)}</strong><br><span class="status-line">Busca por: ${escapeHtml(filtro.localidade)} → ${escapeHtml(item.estado)} · ${escapeHtml(item.codigo_municipio)}</span>`;
    }

    return `
      <tr class="municipality-row" data-codigo="${escapeHtml(item.codigo_municipio)}" style="cursor: pointer;">
        <td data-label="Município">${municipioHtml}</td>
        <td data-label="Risco">${badge(item.nivel_risco)}<br><span class="status-line">score ${fmt.format(item.risk_score || 0)}</span></td>
        <td data-label="Casos">${fmt.format(item.total_casos_provaveis || 0)}</td>
        <td data-label="Óbitos">${fmt.format(item.total_obitos || 0)}</td>
        <td data-label="Doenças altas">${diseaseNames(item.doencas_altas)}</td>
      </tr>
    `;
  }).join('');

  // Make rows clickable for complete visibility per municipality
  document.querySelectorAll('.municipality-row').forEach(row => {
    row.addEventListener('click', () => {
      const codigo = row.dataset.codigo;
      const fullItem = items.find(i => i.codigo_municipio === codigo);
      if (fullItem) showMunicipioDetail(fullItem);
    });
  });
}

function renderAlerts(items) {
  if (!Array.isArray(items) || items.length === 0) {
    alerts.innerHTML = '<p class="status-line">Nenhum alerta alto encontrado para este filtro. Tente ampliar a consulta ou remover filtros.</p>';
    return;
  }
  alerts.innerHTML = items.map((item) => {
    const level = levelClass(item.nivel_risco);
    return `
      <article class="alert-item ${level}">
        <header><strong>${escapeHtml(item.municipio)}/${escapeHtml(item.estado)}</strong>${badge(item.nivel_risco)}</header>
        <p><strong>${escapeHtml(item.doenca)}</strong> · ${escapeHtml(item.virus)} · ${fmt.format(item.casos_provaveis || 0)} casos prováveis</p>
        <p>Graves ${fmt.format(item.casos_graves || 0)} · Óbitos ${fmt.format(item.obitos || 0)} · Score ${fmt.format(item.risk_score || 0)}</p>
      </article>
    `;
  }).join('');
}

function setLoading(isLoading) {
  submitButton.disabled = isLoading;
  submitButton.textContent = isLoading ? 'Consultando...' : 'Atualizar';
  form.setAttribute('aria-busy', isLoading ? 'true' : 'false');
  rows.closest('.table-wrap')?.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}

function updateQuickFilterState(level) {
  document.querySelectorAll('[data-quick-level]').forEach((button) => {
    button.setAttribute('aria-pressed', (button.dataset.quickLevel || '') === (level || '') ? 'true' : 'false');
  });
}

// Mostrar visão completa por município + carregar ano anterior automaticamente (decisão de alto valor)
async function showMunicipioDetail(item) {
  const panel = document.getElementById('municipio-detail-panel');
  if (!panel || !item) return;

  const currentYear = item.periodo?.ano || new Date().getFullYear();
  const previousYear = currentYear - 1;

  document.getElementById('detail-municipio-title').textContent = `${item.municipio} / ${item.estado}`;
  document.getElementById('detail-municipio-subtitle').textContent = `Código ${item.codigo_municipio} • Ano atual: ${currentYear} (com comparação automática para ${previousYear})`;

  // Resumo visual mais polido
  const summaryHtml = `
    <div class="stat-item">
      <span class="stat-label">Casos Prováveis</span>
      <strong style="font-size:1.35rem; display:block; margin-top:2px;">${fmt.format(item.total_casos_provaveis || 0)}</strong>
    </div>
    <div class="stat-item">
      <span class="stat-label">Óbitos Totais</span>
      <strong style="font-size:1.35rem; display:block; margin-top:2px; color:#b91c1c;">${fmt.format(item.total_obitos || 0)}</strong>
    </div>
    <div class="stat-item">
      <span class="stat-label">Hospitalizações</span>
      <strong style="font-size:1.35rem; display:block; margin-top:2px;">${fmt.format(item.total_hospitalizacoes || 0)}</strong>
    </div>
    <div class="stat-item">
      <span class="stat-label">Nível de Risco</span>
      <div style="margin-top:4px;">${badge(item.nivel_risco)}</div>
    </div>
  `;
  document.getElementById('detail-summary').innerHTML = summaryHtml;

  // Tabela de todas as doenças
  const tbody = document.getElementById('detail-diseases-body');
  const doencas = item.doencas || [];
  tbody.innerHTML = doencas.map(d => `
    <tr>
      <td>
        <div style="font-weight:600; color:#111827;">${escapeHtml(d.nome)}</div>
        <div style="font-size:0.78rem; color:#6b7280; margin-top:1px;">${escapeHtml(d.virus || '')}</div>
      </td>
      <td class="num">${fmt.format(d.casos_provaveis || 0)}</td>
      <td class="num">${fmt.format(d.sinais_alarme || 0)}</td>
      <td class="num">${fmt.format(d.casos_graves || 0)}</td>
      <td class="num">${fmt.format(d.hospitalizacoes || 0)}</td>
      <td class="num" style="font-weight:600;">${fmt.format(d.obitos || 0)}</td>
      <td class="num" style="font-weight:600;">${fmt.format(d.risk_score || 0)}</td>
      <td>${badge(d.nivel_risco)}</td>
    </tr>
  `).join('');

  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // === Decisão não recomendada de alto valor: carregar ano anterior automaticamente ===
  document.getElementById('detail-comparison').innerHTML = `
    <div style="padding: 14px; background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0; font-size: 0.9rem; color: #64748b;">
      Carregando automaticamente os dados de <strong>${previousYear}</strong> para comparação ano a ano...
    </div>
  `;

  try {
    const prevParams = new URLSearchParams({
      municipio: item.codigo_municipio,
      ano: previousYear,
      limite: '30'
    });

    const prevRes = await fetch(`/v1/risk-index?${prevParams.toString()}`);
    if (prevRes.ok) {
      const prevData = await prevRes.json();
      const prevItem = Array.isArray(prevData) ? prevData.find(m => m.codigo_municipio === item.codigo_municipio) : null;

      if (prevItem) {
        renderSimpleYearComparison(item, prevItem, currentYear, previousYear);
      } else {
        document.getElementById('detail-comparison').innerHTML = 
          `<p class="detail-placeholder">Não foram encontrados dados para ${previousYear} neste município.</p>`;
      }
    }
  } catch (e) {
    document.getElementById('detail-comparison').innerHTML = 
      `<p class="detail-placeholder">Não foi possível carregar os dados de ${previousYear}.</p>`;
  }
}

function renderSimpleYearComparison(current, previous, currentYear, previousYear) {
  const container = document.getElementById('detail-comparison');
  if (!container) return;

  const currScore = current.risk_score || 0;
  const prevScore = previous.risk_score || 0;
  const scoreDiff = currScore - prevScore;
  const scorePct = prevScore > 0 ? ((scoreDiff / prevScore) * 100) : 0;

  const currObitos = current.total_obitos || 0;
  const prevObitos = previous.total_obitos || 0;
  const obitosDiff = currObitos - prevObitos;

  const currCasos = current.total_casos_provaveis || 0;
  const prevCasos = previous.total_casos_provaveis || 0;
  const casosDiff = currCasos - prevCasos;
  const casosPct = prevCasos > 0 ? ((casosDiff / prevCasos) * 100) : 0;

  const getColor = (diff) => diff > 0 ? '#b91c1c' : (diff < 0 ? '#15803d' : '#64748b');
  const getArrow = (diff) => diff > 0 ? '▲' : (diff < 0 ? '▼' : '→');
  const getVerb = (diff) => diff > 0 ? 'piorou' : (diff < 0 ? 'melhorou' : 'manteve-se estável');

  const scoreColor = getColor(scoreDiff);
  const obitosColor = getColor(obitosDiff);
  const casosColor = getColor(casosDiff);

  container.innerHTML = `
    <div style="margin-bottom: 12px; font-size: 0.9rem; color: #475569;">
      O risco <strong style="color: ${scoreColor};">${getVerb(scoreDiff)}</strong> 
      ${scoreDiff !== 0 ? `em <strong>${Math.abs(scorePct).toFixed(1)}%</strong>` : ''} 
      em relação a ${previousYear}.
    </div>

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
      <!-- Ano Anterior -->
      <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px;">
        <div style="font-size: 0.7rem; color: #64748b; margin-bottom: 4px;">${previousYear}</div>
        <div style="font-size: 1.1rem; font-weight: 700; color: #334155;">Score: ${fmt.format(prevScore)}</div>
        <div style="font-size: 0.85rem; color: #64748b; margin-top: 4px;">
          ${fmt.format(prevCasos)} casos • ${fmt.format(prevObitos)} óbitos
        </div>
      </div>

      <!-- Ano Atual -->
      <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px;">
        <div style="font-size: 0.7rem; color: #64748b; margin-bottom: 4px;">${currentYear}</div>
        <div style="font-size: 1.1rem; font-weight: 700; color: ${scoreColor};">
          Score: ${fmt.format(currScore)} 
          <span style="font-size: 0.9rem;">${getArrow(scoreDiff)}</span>
        </div>
        <div style="font-size: 0.85rem; color: #64748b; margin-top: 4px;">
          ${fmt.format(currCasos)} casos 
          <span style="color: ${casosColor};">(${getArrow(casosDiff)} ${Math.abs(casosPct).toFixed(0)}%)</span> 
          • ${fmt.format(currObitos)} óbitos 
          <span style="color: ${obitosColor};">(${getArrow(obitosDiff)})</span>
        </div>
      </div>
    </div>

    <div style="margin-top: 10px; font-size: 0.8rem; color: #64748b;">
      Diferença no score: <strong style="color: ${scoreColor};">${scoreDiff > 0 ? '+' : ''}${scoreDiff.toFixed(1)}</strong>
    </div>
  `;
}

// Fechar painel de detalhe
function closeDetailPanel() {
  const panel = document.getElementById('municipio-detail-panel');
  if (panel) panel.style.display = 'none';
}

document.addEventListener('click', function(e) {
  if (e.target.id === 'close-detail') {
    closeDetailPanel();
  }
});

// Fechar com ESC (melhor UX)
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const panel = document.getElementById('municipio-detail-panel');
    if (panel && panel.style.display !== 'none') {
      closeDetailPanel();
    }
  }
});

// Fechar ao clicar fora do painel (melhor UX)
document.addEventListener('click', function(e) {
  const panel = document.getElementById('municipio-detail-panel');
  if (!panel || panel.style.display === 'none') return;

  // Fecha se clicar fora do painel e não for em uma linha da tabela
  if (!panel.contains(e.target) && !e.target.closest('.municipality-row')) {
    closeDetailPanel();
  }
});

function renderSkeleton() {
  rows.innerHTML = Array.from({ length: 4 }, () => `
    <tr aria-hidden="true">
      <td class="empty-cell" colspan="5"><span class="skeleton-line"></span><span class="skeleton-line short" style="margin-top: 10px;"></span></td>
    </tr>
  `).join('');
  alerts.innerHTML = '<article class="alert-item" aria-hidden="true"><span class="skeleton-line"></span><span class="skeleton-line short" style="margin-top: 10px;"></span></article>';
}

async function loadDashboard(event) {
  if (event) event.preventDefault();
  ufInput.value = ufInput.value.toUpperCase().trim();
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const [key, value] of data.entries()) {
    if (value && key !== 'somente_altos') params.set(key, String(value).trim());
  }
  params.set('somente_altos', document.getElementById('somente_altos').checked ? 'true' : 'false');
  params.set('limite', '25');

  statusLine.textContent = 'Atualizando dados...';
  setLoading(true);
  renderSkeleton();
  try {
    const riskResponse = await fetch(`/v1/risk-index?${params.toString()}`);
    const alertParams = new URLSearchParams();
    if (params.get('municipio')) alertParams.set('municipio', params.get('municipio'));
    if (params.get('estado')) alertParams.set('estado', params.get('estado'));
    alertParams.set('limite', '10');
    const alertResponse = await fetch(`/v1/high-alerts?${alertParams.toString()}`);
    if (!riskResponse.ok || !alertResponse.ok) throw new Error('Falha na consulta');
    const riskPayload = await riskResponse.json();
    const alertPayload = await alertResponse.json();
    renderRows(Array.isArray(riskPayload) ? riskPayload : []);
    renderAlerts(alertPayload.alerts || []);
    updateQuickFilterState(params.get('nivel_minimo') || '');
    statusLine.textContent = Array.isArray(riskPayload) ? `${riskPayload.length} município(s) retornado(s).` : (riskPayload.message || 'Consulta concluída.');
  } catch (error) {
    renderRows([]);
    renderAlerts([]);
    statusLine.textContent = 'Não foi possível atualizar os dados agora. Tente novamente em alguns instantes.';
  } finally {
    setLoading(false);
  }
}

ufInput.addEventListener('input', () => { ufInput.value = ufInput.value.toUpperCase(); });
form.addEventListener('reset', () => {
  window.setTimeout(() => {
    document.getElementById('municipio').value = '';
    ufInput.value = '';
    document.getElementById('nivel_minimo').value = '';
    document.getElementById('somente_altos').checked = false;
    loadDashboard();
  });
});
document.querySelectorAll('[data-quick-level]').forEach((button) => {
  button.addEventListener('click', () => {
    document.getElementById('nivel_minimo').value = button.dataset.quickLevel || '';
    document.getElementById('somente_altos').checked = ['alto', 'critico'].includes(button.dataset.quickLevel || '');
    updateQuickFilterState(button.dataset.quickLevel || '');
    loadDashboard();
  });
});
form.addEventListener('submit', loadDashboard);
"""


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
    rendered = []
    for row in rows:
        municipio_nome = escape_html(row.get("municipio"))
        estado = escape_html(row.get("estado"))
        codigo = escape_html(row.get("codigo_municipio"))

        # Suporte a bairro: mostra origem da consulta quando disponível
        filtro = row.get("filtro_localidade")
        if filtro and filtro.get("tipo") == "distrito":
            localidade_original = escape_html(filtro.get("localidade", ""))
            municipio_display = f'<strong>{municipio_nome}</strong><br><span class="status-line">Busca por: {localidade_original} → {estado} · {codigo}</span>'
        else:
            municipio_display = f'<strong>{municipio_nome}</strong><br><span class="status-line">{estado} · {codigo}</span>'

        rendered.append(
            "<tr>"
            f'<td data-label="Município">{municipio_display}</td>'
            f'<td data-label="Risco">{render_badge(row.get("nivel_risco"))}<br><span class="status-line">score {format_number(row.get("risk_score"))}</span></td>'
            f'<td data-label="Casos">{format_number(row.get("total_casos_provaveis"))}</td>'
            f'<td data-label="Óbitos">{format_number(row.get("total_obitos"))}</td>'
            f'<td data-label="Doenças altas">{escape_html(", ".join(clean_value(item.get("nome")) for item in row.get("doencas_altas", [])) or "Sem alerta alto")}</td>'
            "</tr>"
        )
    return (
        "".join(rendered)
        or '<tr><td class="empty-cell" colspan="5"><strong>Dados ainda não carregados.</strong>Recarregue a consulta ou verifique as fontes disponíveis.</td></tr>'
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


def agent_manifest(request: Request) -> dict[str, Any]:
    base = public_base_url(request)
    return {
        "name": "Epidemiology Intelligence API",
        "description": SITE_DESCRIPTION,
        "language": "pt-BR",
        "base_url": base,
        "freshness": {
            "status": db_metadata.get("status"),
            "loaded_at": db_metadata.get("carregado_em"),
            "period": db_metadata.get("periodo"),
            "cache": db_metadata.get("cache"),
        },
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
                    "municipio",
                    "estado",
                    "somente_altos",
                    "nivel_minimo",
                    "limite",
                ],
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
    base = public_base_url(request)
    return f"""# {SITE_NAME}

{SITE_DESCRIPTION}

Use /v1/high-alerts for high and critical epidemiological alerts by municipality, disease and virus.
Use /v1/risk-index for enriched municipal risk context and explanation fields.
Use /v1/diseases to discover supported diseases and source freshness.
Use /v1/bairros to see supported neighborhoods in SP, RJ, MG and PE (resolved to municipality). Add ?uf=RJ to filter.
Use /v1/metadata for data freshness, source URLs and the risk formula.

Base URL: {base}
OpenAPI: {base}/openapi.json
Agent manifest: {base}/agent.json
Human dashboard: {base}/dashboard
Methodology: {base}/sobre

Important interpretation rules:
- Public processed granularity is municipal.
- When `filtro_localidade` appears, the original query was made using a neighborhood (bairro) or district name. Currently, this resolution is mainly supported for São Paulo city districts. The data returned is always at the municipal level.
- Do not infer district-level or neighborhood-level case counts from municipal data.
- Risk formula: {RISK_FORMULA}
- Each disease entry includes formula_risco; do not assume one universal formula for all diseases.
- Data does not replace official epidemiological surveillance or local investigation.
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
    limite: int = Query(default=100, ge=1, le=1000),
    user: dict = Depends(get_api_user),
):
    # Enforce limits for anonymous/free users
    if user["tier"] == "anonymous" and limite > 5:
        limite = 5
    elif user["tier"] == "free" and limite > 20:
        limite = 20

    # Support loading specific year for comparison (non-recommended high-value path)
    effective_year = ano if ano is not None else DEFAULT_YEAR

    if effective_year != DEFAULT_YEAR:
        # Load specific year on demand (for detail view comparison)
        year_report = fetch_epidemiology_report(effective_year)
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
    return (
        rows[:limite] if rows else {"message": "Dados não disponíveis para o filtro."}
    )


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
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    # Enforce limits for anonymous/free users
    if user["tier"] == "anonymous" and limite > 5:
        limite = 5
    elif user["tier"] == "free" and limite > 20:
        limite = 20

    alerts = filter_alerts(
        db_alertas, municipio=municipio, estado=estado, doenca=doenca
    )
    return {
        "metadata": db_metadata,
        "total": len(alerts),
        "alerts": alerts[:limite],
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
) -> dict[str, Any]:
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
    if user["tier"] not in {"premium", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este endpoint requer uma assinatura Premium.",
        )

    # In a real scenario, we would use a library like ReportLab or WeasyPrint
    return {
        "message": "Relatório PDF gerado com sucesso.",
        "download_url": f"/reports/custom/report-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.pdf",
        "note": "A geração de PDF real exige dependências adicionais de sistema.",
    }


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
