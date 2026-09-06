from collections import Counter
import gzip
import hashlib
import hmac
import html
import threading
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# Domain imports (Fase 0 refactoring - foundation for cockpit)
from domain.disease_sources import (
    DEFAULT_DISEASE_CODES,
    DISEASE_SOURCES,
    DiseaseSource,
)
from domain.risk import (
    RISK_FORMULA,
    RISK_LEVEL_ORDER,
    risk_profile_for_source,
)

# Aggregation layer (Fase 0 - continuing extraction)
from aggregation.report_builder import (
    build_epidemiology_report,
)
from aggregation.filters import (
    filter_risk_index,
    filter_alerts,
    CITY_NEIGHBORHOODS,
    ambiguous_locality_names,
    get_supported_bairros,
)
from aggregation.utils import normalize_text

# Ingestion layer extraction in progress (Fase 0).
from ingestion.municipality_lookup import load_municipality_lookup
from aggregation.recency_enrichment import enrich_report
from aggregation.population_enrichment import (
    MIN_RELIABLE_POPULATION,
    enrich_with_population,
)
from aggregation.case_composition import with_case_composition
from aggregation.signal_coverage import (
    describe_coverage as describe_signal_gap,
    with_signal_coverage,
)
from aggregation.ordering import ORDERINGS, sort_municipalities
from aggregation.cache_policy import (
    DEFAULT_MAX_AGE_DAYS,
    cache_is_fresh,
    is_regression,
    missing_sources,
)
from domain.tiers import (
    MAX_RESULTS_PER_CALL,
    TIERS,
    format_rate_limit,
    tier_by_code,
    tier_max_results,
)
from ingestion.population_lookup import load_population_lookup
from presentation.signal import (
    render_data_status,
    render_incidence_cell,
    render_risk_cell,
    render_signal_strip,
    render_strip_legend,
    sort_diseases_for_strip,
)
from aggregation.utils import clean_code, clean_value

# The loader module exists. We avoid top-level import here to prevent
# circular dependencies during the gradual monolith breakup.

from fastapi import FastAPI, Query, Request, Header, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

try:
    from bundled_report_snapshot import load_embedded_report_snapshot
except ImportError:  # pragma: no cover - ausência é anômala; avisada abaixo
    load_embedded_report_snapshot = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if load_embedded_report_snapshot is None:
    # O comentário anterior descrevia esta guarda como "fallback for
    # local-only runs before bundling" — uma fase que acabou. Hoje
    # `bundled_report_snapshot.py` é a ÚNICA origem de dado num deploy:
    # `.vercelignore` exclui o resto, e os JSON versionados foram removidos
    # por não serem lidos por código nenhum.
    #
    # Sem este aviso, um artefato sem o módulo subiria com a base vazia e o
    # log mudo — o mesmo modo de falha do arquivo de chaves ausente, que
    # custou 8 testes e uma API inteira em 401 antes de passar a avisar.
    #
    # O aviso não pode ficar dentro do `except`: `logger` só existe depois.
    logger.warning(
        "bundled_report_snapshot indisponível; sem cache utilizável o "
        "serviço dependerá de rede para ter qualquer dado."
    )

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

DEFAULT_USERS_DB_PATH = "data/users.json"


def users_db_path() -> Path:
    """Arquivo de chaves de API, lido a cada uso e não no import.

    Mesma razão de `report_cache_max_age_days`: congelar a configuração no
    import faz o processo depender da ordem em que os módulos são carregados.
    Aqui isso tinha consequência concreta — `unittest discover` importa os
    módulos de teste como top-level, então `tests/__init__.py` só executa
    quando alguém faz `from tests import ...`, o que em vários arquivos
    acontece DEPOIS de `import app`. A base de chaves da suíte era instalada
    tarde demais e ficava inerte: a suíte usava o arquivo real da máquina e
    passava por isso, não pelo fixture.
    """
    return Path(os.getenv("USERS_DB_PATH", DEFAULT_USERS_DB_PATH))
USAGE_LOG_PATH = Path(os.getenv("USAGE_LOG_PATH", "data/usage.jsonl"))


class APIKeyManager:
    def __init__(self, path: Path | None = None):
        self._fixed_path = path
        self._in_memory = False
        self._loaded_from: tuple[str, str | None] | None = None
        self.keys: dict[str, dict] = {}
        self.load_keys()

    @classmethod
    def from_keys(cls, keys: Mapping[str, dict]) -> "APIKeyManager":
        """Gerenciador com chaves em memória, sem origem em disco.

        Existe para os testes de validação, que precisam de um conjunto
        controlado de chaves. Antes eles usavam `__new__` e atribuíam
        `keys` direto, o que os acoplava aos atributos privados da classe.
        """
        manager = cls.__new__(cls)
        manager._fixed_path = None
        manager._in_memory = True
        manager._loaded_from = None
        manager.keys = dict(keys)
        return manager

    @property
    def path(self) -> Path:
        return self._fixed_path or users_db_path()

    def _reload_if_source_changed(self) -> None:
        """Recarrega quando o ambiente passa a apontar para outra origem."""
        if self._in_memory:
            return
        origem = (str(self.path), os.getenv("USERS_DB_JSON"))
        if origem != self._loaded_from:
            self.load_keys()

    def load_keys(self):
        """Carrega as chaves de `USERS_DB_JSON` ou do arquivo, e declara a falta.

        O arquivo de chaves não está no repositório — e não deve estar, porque
        guarda credenciais. O efeito colateral disso era silencioso: um clone
        limpo, ou um deploy disparado pelo git, subia com ZERO chaves válidas
        e não dizia nada. Todo endpoint autenticado devolvia 401 e o log ficava
        mudo. Medido num clone real: 8 testes falhando, todos por esta causa.

        `USERS_DB_JSON` permite ao ambiente carregar as chaves como segredo, em
        vez de depender de um arquivo que o repositório não pode transportar.
        """
        self._loaded_from = (str(self.path), os.getenv("USERS_DB_JSON"))
        self.keys = {}

        inline = os.getenv("USERS_DB_JSON")
        if inline:
            try:
                self.keys = json.loads(inline).get("keys", {})
            except (json.JSONDecodeError, AttributeError) as error:
                logger.error("USERS_DB_JSON presente mas inválido: %s", error)
        elif self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.keys = data.get("keys", {})
            except Exception as e:
                logger.error(f"Error loading API keys: {e}")
        else:
            logger.warning(
                "Arquivo de chaves ausente em %s e USERS_DB_JSON não definido.",
                self.path,
            )

        if not self.keys:
            logger.warning(
                "Nenhuma chave de API carregada: apenas o tier anônimo "
                "responderá (5 registros por chamada, 10 req/min). Defina "
                "USERS_DB_JSON ou USERS_DB_PATH para habilitar as chaves."
            )

    def validate_key(self, api_key: str) -> Optional[dict]:
        """Valida a chave, aceitando entrada em texto puro ou em hash.

        Uma entrada de `users.json` cuja chave comece com `sha256:` guarda o
        digest em vez do segredo, o que permite migrar o arquivo sem quebrar
        quem já usa as chaves atuais. A comparação usa `compare_digest` para
        não vazar informação pelo tempo de resposta.
        """
        if not api_key:
            return None

        self._reload_if_source_changed()

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


# Prefixo estável para o dreno de logs filtrar. Mudá-lo quebra qualquer
# consulta de uso já montada em cima dele.
USAGE_LOG_PREFIX = "USO"

usage_logger = logging.getLogger("uso")


class UsageTracker:
    """Contagem de uso por cliente, em dois canais.

    O canal em arquivo era o único, e em produção ele não existe: na Vercel o
    disco é somente leitura fora de `/tmp`, e `vercel.json` não define
    `USAGE_LOG_PATH`. A escrita falhava, o `try/except` engolia, e o servico
    seguia respondendo — SEM REGISTRAR UMA ÚNICA REQUISIÇÃO desde que subiu.
    Apontar para `/tmp` também não resolveria: container serverless é efêmero,
    então log em arquivo é arquiteturalmente incapaz de medir demanda ali.

    Isto importa além de analytics. Com o produto sendo uma API para embarcar
    em software de terceiros, contar chamadas É o mecanismo de cobrança: não
    se fatura por uso o que não se consegue contar.

    O canal primário passa a ser a saída padrão, que a plataforma captura e
    drena. O arquivo continua para desenvolvimento local, e desiste sozinho
    quando o disco não aceita — sem repetir o aviso a cada requisição, que em
    produção afogaria o próprio dreno.
    """

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._file_available = True
        self._warned_once = False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._file_available = False
            logger.warning(
                "Log de uso em arquivo indisponível (%s): %s. A contagem "
                "segue pela saída padrão.",
                log_path,
                error,
            )
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
        linha = json.dumps(entry, ensure_ascii=False)

        # Canal primário: sobrevive a disco somente leitura e a container
        # efêmero. É por aqui que a demanda passa a ser observável.
        usage_logger.info("%s %s", USAGE_LOG_PREFIX, linha)

        if not self._file_available:
            return
        try:
            with self._lock:
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    print(linha, file=handle)
        except OSError as error:
            # Contar requisições é telemetria; falhar ao contar não pode custar
            # a resposta. A exceção derrubava TODA rota /v1/* com 500 enquanto
            # /dashboard respondia normalmente.
            self._file_available = False
            if not self._warned_once:
                self._warned_once = True
                logger.warning(
                    "Log de uso em arquivo desativado (%s). A contagem segue "
                    "pela saída padrão.",
                    error,
                )


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

            # Descarta janelas antigas quando o dicionário cresce demais.
            if len(self.requests) > 1000:
                self.requests = {k: v for k, v in self.requests.items() if minute in k}

            return True

    def reset(self) -> None:
        """Zera as janelas de contagem.

        O limite é global ao processo e conta por minuto de relógio, então uma
        rajada de requisições — uma suíte de testes, por exemplo — esbarra nele
        legitimamente. Sem uma forma explícita de zerar, o teste seguinte falha
        por 429 em vez de pelo que estava verificando.
        """
        with self.lock:
            self.requests = {}


api_key_manager = APIKeyManager()
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
# Rotas anunciadas no sitemap.xml. Toda página que aparece na navegação
# precisa estar aqui: `/planos` ficou de fora desde sempre — justamente a
# página onde alguém pede uma chave e onde está o preço. Medido em
# 2026-09-06, a busca pelo domínio não devolve nada, e a página de conversão
# nem era oferecida ao rastreador.
PUBLIC_PATHS = (
    "/dashboard",
    "/sobre",
    "/planos",
    "/agentes",
    "/docs",
    "/openapi.json",
)


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
    degraded_sources: list[str] | None = None,
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
    # Declara quais termos da formula a fonte de cada agravo nao alimenta.
    # Sem isto, um zero que significa "a fonte nao traz" fica indistinguivel
    # de um zero que significa "nao houve".
    enriched = with_signal_coverage(enriched)
    # Declara quanto de `casos_provaveis` ainda e notificacao nao investigada.
    # E o numerador da incidencia: fracoes pendentes muito diferentes produzem
    # incidencias que nao estao na mesma escala.
    enriched = with_case_composition(enriched)

    db_clini = list(enriched.get("municipios") or [])
    db_alertas = list(enriched.get("alertas_altos") or [])
    db_metadata = dict(enriched.get("metadata") or metadata)
    carga = dict(db_metadata.get("carga") or {})
    carga["cobertura_degradada"] = bool(degraded_sources)
    carga["aviso_cobertura"] = (
        "A última tentativa de recarga não trouxe: "
        + ", ".join(degraded_sources)
        + ". A base anterior foi mantida."
        if degraded_sources
        else None
    )
    db_metadata["carga"] = carga
    enriched = {**enriched, "metadata": db_metadata}
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

    # Renovar nao pode perder cobertura. Uma carga em que parte das fontes
    # falhou (rede fora, dependencia nativa ausente) passava em
    # `report_has_content` e substituia uma base mais completa.
    if (
        stale_report is not None
        and report_has_content(report)
        and is_regression(report, stale_report)
    ):
        perdidos = missing_sources(report, stale_report)
        applied = apply_report_state(
            stale_report,
            cache_hit=True,
            cache_path=cache_path,
            cache_source="disk-stale",
            degraded_sources=perdidos,
        )
        logger.warning(
            "Recarga perderia cobertura (%s); mantendo a cache anterior.",
            ", ".join(perdidos),
        )
        return applied

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


_ASSET_VERSION: str | None = None


def static_asset_version() -> str:
    """Impressão digital dos arquivos CSS, para invalidar cache de navegador.

    StaticFiles envia ETag e Last-Modified, mas um navegador que já tem a
    folha em cache pode continuar usando a versão antiga depois de um deploy.
    Observado ao vivo: o CSS novo estava no servidor e a página renderizava
    com o antigo. Sem isto, uma correção de estilo simplesmente não chega ao
    usuário — e nada falha.

    Calculada uma vez por processo, sobre mtime e tamanho de cada arquivo.
    """
    global _ASSET_VERSION
    if _ASSET_VERSION is not None:
        return _ASSET_VERSION

    digest = hashlib.sha256()
    try:
        for path in sorted((STATIC_DIR / "css").rglob("*.css")):
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    except OSError as error:  # pragma: no cover - defensivo
        logger.warning("Não foi possível versionar os assets: %s", error)
        digest.update(b"sem-versao")

    _ASSET_VERSION = digest.hexdigest()[:8]
    return _ASSET_VERSION


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
  <link rel="stylesheet" href="/static/css/main.css?v={static_asset_version()}">
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
    # `year` e `period_year` eram calculados aqui e não usados por ninguém:
    # alimentavam o gráfico Chart.js e o payload JSON embutido que a docstring
    # acima diz terem sido removidos. Sobrava uma chamada a
    # `signal_reference_date()` desperdiçada a cada render de página.
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
        extra_script=(
            '<script src="/static/js/dashboard.js'
            f'?v={static_asset_version()}" defer></script>'
        ),
    )


def render_tier_cards() -> str:
    """Cartões de plano gerados da declaração — nunca escritos à mão.

    Os números aqui divergiam do código: a página anunciava 100 req/min para
    o Profissional, que tem 10.000, e chamava de "Gratuito" tanto o acesso
    sem chave quanto a chave gratuita, que têm limites diferentes.
    """
    cards = []
    for tier in TIERS:
        destaque = ' class="link-card is-featured"' if tier.codigo == "premium" else ' class="link-card"'
        if tier.max_results is None:
            registros = (
                f"até {format_rate_limit(MAX_RESULTS_PER_CALL)} registros por chamada, "
                "base completa por paginação"
            )
        else:
            registros = f"{tier.max_results} registros por chamada"
        cards.append(
            f"<div{destaque}>"
            f"<strong>{escape_html(tier.nome)}</strong>"
            f"<span>{escape_html(tier.resumo)}</span>"
            f'<span class="tier-limits">{escape_html(registros)}'
            f" &middot; {format_rate_limit(tier.rate_limit)} req/min</span>"
            f"<p>{escape_html(tier.preco)}</p>"
            "</div>"
        )
    return "".join(cards)


def render_plans_page(request: Request) -> str:
    """Página de planos.

    Todo número vem de `domain/tiers.py`. A versão anterior trazia os limites
    escritos à mão no HTML e prometia "sem limites: todos os municípios em uma
    única chamada" — impossível em qualquer tier, já que a chamada é limitada
    a mil registros e a base tem mais de cinco mil municípios. A promessa
    agora é a que o
    código cumpre: base completa por paginação.
    """
    body = f"""
<main class="page" id="conteudo-principal">
  <header class="dash-head">
    <h1>Planos e chaves de API</h1>
    <p class="lead">Projeto de código aberto. As assinaturas custeiam a infraestrutura e o processamento das fontes do SINAN.</p>
  </header>

  <section class="panel section-panel" aria-labelledby="planos-title">
    <div class="section-head">
      <div>
        <h2 id="planos-title">Níveis de acesso</h2>
        <p class="status-line">Os limites abaixo são os que o serviço aplica de fato — a página lê da mesma declaração que o código usa.</p>
      </div>
    </div>
    <div class="link-grid">{render_tier_cards()}</div>
  </section>

  <section class="panel section-panel prose" aria-labelledby="incluso-title">
    <h2 id="incluso-title">O que a chave Profissional dá acesso</h2>
    <ul>
      <li><strong>Base completa:</strong> até {format_rate_limit(MAX_RESULTS_PER_CALL)} registros por chamada; use <code>pagina</code> para percorrer os {format_rate_limit(len(db_clini)) if db_clini else "todos os"} municípios.</li>
      <li><strong>Relatório profissional:</strong> <code>/v1/professional-report</code>, com metadados estendidos.</li>
      <li><strong>Recarga sob demanda:</strong> <code>POST /v1/refresh</code> reprocessa as fontes.</li>
      <li><strong>Limite de requisições:</strong> {format_rate_limit(tier_by_code("premium").rate_limit)} por minuto.</li>
    </ul>
    <p class="status-line">Todo endpoint devolve <code>X-Total-Results</code>, <code>X-Returned-Results</code>, <code>X-Limit-Applied</code>, <code>X-Page</code> e <code>X-Has-More</code>, para que o cliente saiba quando a resposta foi cortada.</p>
    <div class="actions">
      <a class="button primary" href="mailto:contato@aldeia-viva.com.br?subject=Chave%20de%20API">Solicitar chave</a>
      <a class="button" href="/agentes">Contrato para agentes</a>
      <a class="button" href="/docs">Documentação da API</a>
    </div>
    <p class="status-line">Chaves gratuitas são ativadas para pesquisadores, ONGs e equipes de vigilância municipal. Pagamento por PIX ou boleto.</p>
  </section>
</main>"""
    return render_web_page(
        request,
        path="/planos",
        title=f"Planos e API | {SITE_NAME}",
        description="Níveis de acesso à API epidemiológica do Aldeia Viva Saúde.",
        body=body,
        json_ld=base_json_ld(request)
        + [breadcrumb_json_ld(request, "Planos", "/planos")],
        active="planos",
    )


def render_pending_summary() -> str:
    """Frase com a fração pendente por agravo, do maior para o menor."""
    composicao = db_metadata.get("composicao_dos_casos") or {}
    por_agravo = composicao.get("por_agravo") or {}
    if not por_agravo:
        return "sem dados de classificação nesta carga."

    itens = sorted(
        por_agravo.items(), key=lambda kv: -kv[1].get("proporcao_pendente", 0)
    )
    partes = []
    for codigo, dados in itens[:4]:
        fonte = DISEASE_SOURCES.get(codigo)
        nome = fonte.nome if fonte else codigo
        partes.append(
            f"<strong>{escape_html(nome)}</strong> "
            f"{dados.get('proporcao_pendente', 0) * 100:.0f}%"
        )
    geral = composicao.get("proporcao_pendente_geral", 0) * 100
    return ", ".join(partes) + f" — no conjunto, {geral:.0f}% dos casos prováveis."


def render_signal_gaps() -> str:
    """Lista, por agravo, os termos da fórmula que a fonte não alimenta."""
    cobertura = db_metadata.get("cobertura_de_sinais") or {}
    por_agravo = cobertura.get("por_agravo") or {}
    linhas = []
    for codigo, faltando in sorted(por_agravo.items()):
        if not faltando:
            continue
        fonte = DISEASE_SOURCES.get(codigo)
        nome = fonte.nome if fonte else codigo
        linhas.append(
            f"<strong>{escape_html(nome)}</strong>: sem "
            f"{escape_html(describe_signal_gap(faltando))}"
        )
    if not linhas:
        return "Nesta carga, todos os agravos trazem todos os sinais."
    return "Nesta carga: " + "; ".join(linhas) + "."


def render_explanation_page(request: Request) -> str:
    """Página de metodologia.

    É para onde o painel manda quem quer saber como o número foi feito, e por
    isso é a página onde uma afirmação errada custa mais caro — ela é citada.

    A versão anterior descrevia o método que existia antes das correções:
    dizia que o nível crítico vinha de "óbitos ou score muito elevado"
    (limiar removido por saturar 24,3% dos municípios) e mandava priorizar
    pelo score, que correlaciona 0,822 com a população. Não mencionava o
    denominador populacional nem a dimensão temporal.
    """
    source_rows = "".join(
        f"<li><strong>{escape_html(clean_value(item.get('nome')))}</strong>: "
        f"{format_number(item.get('registros', 0))} registros, "
        f"arquivo de <strong>{escape_html(clean_value(item.get('ano')))}</strong>.</li>"
        for item in db_metadata.get("fontes", [])
    )
    formula_rows = "".join(
        f"<li><strong>{escape_html(source.nome)}</strong>: "
        f"<code>{escape_html(risk_profile_for_source(source).formula)}</code></li>"
        for source in DISEASE_SOURCES.values()
    )
    recencia = db_metadata.get("recencia") or {}
    populacao = db_metadata.get("populacao") or {}
    # O literal repetia `MIN_RELIABLE_POPULATION`: mudar a constante deixaria
    # esta página mostrando o número antigo quando o metadata não trouxesse
    # a chave.
    minimo = populacao.get("populacao_minima_confiavel") or MIN_RELIABLE_POPULATION
    atuais = recencia.get("agravos_com_fonte_do_ano_corrente")
    total_agravos = recencia.get("agravos_total")

    body = f"""
<main class="page" id="conteudo-principal">
  <header class="dash-head">
    <h1>Como o índice transforma notificações em decisão</h1>
    <p class="lead">Consolidação do SINAN/OpenDataSUS por município e agravo, com o denominador populacional do IBGE e a idade de cada fonte declarada.</p>
  </header>

  {render_data_status(db_metadata)}

  <div class="text-layout">
    <section class="panel section-panel prose">
      <h2>O que comparar entre municípios</h2>
      <p>A incidência <strong>por 100 mil habitantes</strong> (<code>incidencia.por_100k</code>) é a única medida comparável entre municípios de portes diferentes. <code>risk_score</code> é soma ponderada de contagens absolutas e, medido nesta base, tem <strong>correlação de 0,82 com a população</strong>: ordenar por ele responde "onde há mais casos", não "onde é pior".</p>
      <p>A taxa nunca é publicada sozinha. Abaixo de {format_number(minimo)} habitantes um único caso move a incidência o bastante para desestabilizá-la, então esses municípios vêm com a taxa marcada e ficam abaixo dos confiáveis na ordenação — publicados, nunca suprimidos em silêncio. População estimada pelo IBGE (SIDRA 6579).</p>

      <h2>Como o nível de risco é calculado</h2>
      <p>Cada agravo tem o seu próprio perfil de risco e o seu próprio nível. O nível do município é o <strong>pior nível entre os agravos</strong> dele — não há fórmula aplicada sobre a soma, porque somar dez agravos e cinco anos-fonte num só número deixava 24,3% dos municípios em "crítico" e a variável parava de discriminar.</p>
      <p><code>nivel_risco</code> considera todos os anos-fonte: é gravidade histórica. <code>nivel_risco_fonte_atual</code> olha só os agravos cujo arquivo é do ano corrente, e é ele que responde "exige ação agora?". Quando o histórico é mais grave, o painel diz.</p>
      <ul>{formula_rows}</ul>

      <h2>Nem toda fórmula usa todos os termos</h2>
      <p>A fonte de cada agravo traz campos diferentes. O arquivo da Zika tem 38 colunas e nenhuma de sinal de alarme ou gravidade; o da Dengue tem 121, com 24 delas. Quando a fonte não traz o campo, o termo correspondente da fórmula fica <strong>sempre zero</strong> — e o score da Zika acaba sendo, na prática, apenas a contagem de casos.</p>
      <p>{render_signal_gaps()}</p>
      <p><code>doencas[].sinais_sem_dados</code> declara isso em cada agravo. <strong>Scores de agravos com lacunas diferentes não são comparáveis entre si</strong>, ainda que a fórmula impressa ao lado seja a mesma. E um zero nesses campos pode significar "não houve" ou "a fonte não traz": consulte a lista antes de afirmar ausência.</p>

      <h2>Quanto de "caso provável" ainda está em aberto</h2>
      <p><code>casos_provaveis</code> é notificações menos descartados — a definição padrão do SINAN. Mas parte desses casos ainda não foi investigada, e a fração varia muito: {render_pending_summary()}</p>
      <p>Isso é normal em dado recente, porque o encerramento é assíncrono. O que importa é a consequência: <code>casos_provaveis</code> é o <strong>numerador da incidência</strong>, então agravos com frações pendentes muito diferentes produzem incidências que não estão na mesma escala. <code>doencas[].composicao</code> declara isso em cada agravo.</p>
      <p>E <code>casos_descartados</code> igual a zero, hoje o caso de oito dos dez agravos, costuma significar "nada encerrado ainda" — não "nada descartado".</p>

      <h2>Três relógios, que não podem ser confundidos</h2>
      <ul>
        <li><strong>Carga</strong> — há quanto tempo o serviço buscou dados. É falha operacional do serviço, não fato sobre os municípios.</li>
        <li><strong>Fonte</strong> — de que ano é o arquivo daquele agravo. O relatório reúne <strong>anos diferentes</strong> por agravo: hoje, {atuais if atuais is not None else "alguns"} de {total_agravos if total_agravos is not None else "dez"} têm arquivo do ano corrente.</li>
        <li><strong>Recência</strong> — até quando o município notificou, medido <em>dentro</em> da fonte daquele agravo. É o único dos três que é fato epidemiológico sobre o município.</li>
      </ul>
      <p>Um agravo com sinal recente cuja fonte é de 2022 continua sendo dado de 2022. Confira <code>fonte.ano</code> antes de datar qualquer afirmação.</p>

      <h2>Fontes carregadas nesta instância</h2>
      <ul>{source_rows or "<li>Nenhuma fonte carregada nesta instância.</li>"}</ul>

      <h2>Granularidade</h2>
      <p>A granularidade processada é sempre municipal. Nomes de bairro e distrito de São Paulo, Rio de Janeiro, Belo Horizonte e Recife são resolvidos para o município oficial, e a resposta traz <code>filtro_localidade</code> com a origem da consulta. Alguns nomes existem em <strong>mais de uma cidade</strong> suportada: informe a UF para escolher, ou leia o bloco de ambiguidade que a resposta devolve.</p>
      <p>Não infira contagem por bairro a partir de dado municipal.</p>
    </section>

    <aside class="text-aside" aria-label="Limites de uso">
      <article class="note-card"><strong>O que estes dados não são</strong><p>Não substituem vigilância epidemiológica oficial, investigação local, diagnóstico nem boletins oficiais.</p></article>
      <article class="note-card"><strong>Subnotificação</strong><p>O SINAN registra o que foi notificado. Incidência baixa pode significar poucos casos ou pouca notificação — o número não distingue os dois.</p></article>
      <article class="note-card"><strong>Contrato completo</strong><p>Campos, limites e regras de interpretação legíveis por máquina em <a href="/agent.json">agent.json</a> e <a href="/llms.txt">llms.txt</a>.</p></article>
    </aside>
  </div>
</main>"""
    return render_web_page(
        request,
        path="/sobre",
        title=f"Dados, fontes e metodologia | {SITE_NAME}",
        description=(
            "Fontes SINAN/OpenDataSUS, incidência por 100 mil habitantes, cálculo do "
            "nível de risco, idade das fontes e limitações da API epidemiológica."
        ),
        body=body,
        json_ld=base_json_ld(request)
        + [
            dataset_json_ld(request),
            breadcrumb_json_ld(request, "Dados e metodologia", "/sobre"),
        ],
        active="sobre",
    )


def render_locality_examples() -> str:
    """Dois exemplos por cidade, tirados do registro.

    A lista anterior era escrita a mao no HTML. Os oito nomes funcionavam,
    mas nada garantia que continuassem funcionando: bastava alguem editar
    CITY_NEIGHBORHOODS. Gerar do registro elimina a possibilidade de deriva.
    """
    exemplos: list[str] = []
    ambiguos = set(ambiguous_locality_names())
    for city in CITY_NEIGHBORHOODS.values():
        disponiveis = [
            bairro
            for bairro in sorted(city["bairros"])
            if normalize_text(bairro) not in ambiguos
        ]
        for bairro in disponiveis[:2]:
            exemplos.append(f"<code>{escape_html(bairro.lower())}</code>")
    return "".join(exemplos)


def render_ambiguous_localities() -> str:
    """Declara os nomes que existem em mais de uma cidade suportada."""
    ambiguos = ambiguous_locality_names()
    if not ambiguos:
        return ""
    itens = ", ".join(f"<code>{escape_html(nome)}</code>" for nome in ambiguos)
    return (
        f'<p class="status-line"><strong>Nomes ambíguos:</strong> {itens} existem '
        "em mais de uma cidade suportada. Informe <code>estado</code> para "
        "escolher; sem ele a resposta traz <code>filtro_localidade.ambiguidade</code> "
        "com as alternativas.</p>"
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
      <p>Use <code>nivel_risco_fonte_atual</code> para priorizar ação de hoje e <code>incidencia.por_100k</code> para comparar municípios: é a única medida que não depende do tamanho da cidade. <code>risk_score</code> é soma de contagens absolutas e correlaciona 0,82 com a população — ordene por ele apenas quando a pergunta for "onde há mais casos", nunca "onde é pior".</p>
      <p>Cada agravo declara <code>fonte.ano</code>, o ano do arquivo SINAN de onde veio, e <code>recencia.frescor</code>, medido dentro dessa fonte. O relatório reúne anos diferentes por agravo: verifique <code>fonte.do_ano_corrente</code> antes de datar qualquer afirmação. <code>/v1/diseases</code> lista os agravos carregados.</p>
      <p><code>filtro_localidade</code> aparece quando a consulta foi feita por <strong>bairro ou distrito</strong>. O sistema resolve o nome para o município — a granularidade dos dados continua municipal.</p>
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
      <div class="token-list">{render_locality_examples()}</div>
      {render_ambiguous_localities()}

      <p>
        <strong>Lista completa e estruturada:</strong> <a href="/v1/bairros">GET /v1/bairros</a>
        (retorna todas as cidades com seus bairros em formato agrupado).
      </p>

      <p class="status-line">Prefira o nome do bairro quando estiver em campo: o sistema entrega os dados do município com o contexto da origem.</p>
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


# Como cada nível se apresenta ao leitor. Rótulo e marcador são decisão de
# interface, não de domínio, e por isso moram aqui — mas o CONJUNTO de níveis
# é do domínio, e `tests/test_single_authority.py` exige que estas tabelas
# cubram `RISK_LEVEL_ORDER` inteiro. Sem isso, um nível novo chegaria ao
# usuário com o código cru no lugar do rótulo, sem nada falhar.
ROTULO_DO_NIVEL = {
    "critico": "Crítico",
    "alto": "Alto",
    "moderado": "Moderado",
    "baixo": "Baixo",
}

MARCADOR_DO_NIVEL = {
    "critico": "●",
    "alto": "▲",
    "moderado": "◆",
    "baixo": "●",
}


def render_badge(value: Any) -> str:
    raw_label = clean_value(value) or "baixo"
    level = normalize_text(raw_label)
    label = ROTULO_DO_NIVEL.get(level, raw_label)
    marker = MARCADOR_DO_NIVEL.get(level, "●")
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
        "signal_coverage": db_metadata.get("cobertura_de_sinais"),
        "case_composition": db_metadata.get("composicao_dos_casos"),
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
            "tiers": {
                tier.codigo: {
                    "name": tier.nome,
                    "max_results_per_call": tier.max_results or MAX_RESULTS_PER_CALL,
                    "rate_limit_per_minute": tier.rate_limit,
                    "price": tier.preco,
                }
                for tier in TIERS
            },
            "max_results_per_call": MAX_RESULTS_PER_CALL,
            "pagination": (
                "Use `pagina` junto com `limite`. O cabeçalho X-Has-More diz se "
                "existe página seguinte; X-Total-Results dá o total do filtro."
            ),
            "note": (
                "Sem cabeçalho X-API-Key, `limite` é rebaixado silenciosamente ao teto "
                "do tier anônimo. Leia os cabeçalhos de resposta para saber se houve corte."
            ),
            "response_headers": {
                "X-Total-Results": "total de registros que satisfazem o filtro",
                "X-Returned-Results": "quantos vieram nesta resposta",
                "X-Limit-Applied": "teto efetivamente aplicado",
                "X-Page": "página devolvida",
                "X-Has-More": "true quando existe página seguinte",
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
            "`doencas[].sinais_sem_dados` lista os termos da `formula_risco` que a fonte daquele "
            "agravo não alimenta — eles ficam sempre zero. A Zika, por exemplo, publica a fórmula "
            "de cinco termos e tem quatro sem dados: o score dela é a contagem de casos. "
            "Scores de agravos com lacunas diferentes NÃO são comparáveis entre si.",
            "Um zero em `sinais_alarme`, `casos_graves`, `hospitalizacoes` ou `obitos` pode "
            "significar 'não houve' ou 'a fonte não traz'. Consulte `sinais_sem_dados` antes de "
            "afirmar ausência.",
            "`doencas[].composicao.proporcao_pendente` diz quanto de `casos_provaveis` ainda não "
            "tem classificação final. É normal em dado recente, mas varia de 0% a 100% entre "
            "agravos — e como `casos_provaveis` é o numerador da incidência, agravos com frações "
            "pendentes muito diferentes produzem incidências fora da mesma escala.",
            "`casos_descartados` igual a zero costuma significar 'nada encerrado ainda', não "
            "'nada descartado'. Hoje é zero em oito dos dez agravos.",
            "`nivel_minimo` filtra por `nivel_risco_fonte_atual`, o mesmo campo que o painel "
            "exibe. Para recortar pela gravidade histórica, filtre `nivel_risco` do seu lado.",
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
                "query": ["municipio", "estado", "doenca", "pagina", "limite"],
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
                    "pagina",
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
    anonimo = tier_by_code(TIER_ANONYMOUS)
    teto = anonimo.max_results if anonimo else 5
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


def format_number(value: Any) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def escape_html(value: Any) -> str:
    return html.escape(clean_value(value), quote=True)


TIER_ANONYMOUS = "anonymous"


def paginate(rows: list[Any], *, page: int, limit: int) -> list[Any]:
    """Fatia uma página. Página além do fim devolve vazio, não erro.

    Sem paginação, a base completa era inalcançável: a chamada é limitada a
    1.000 registros e a base tem mais de cinco mil municípios. A página
    /planos prometia
    "todos os municípios em uma única chamada", o que nenhum tier conseguia
    cumprir.
    """
    start = (page - 1) * limit
    return rows[start : start + limit]


def tier_limit(user: Mapping[str, Any], requested: int) -> int:
    """Teto de resultados por chamada, lido da declaração de tiers.

    Antes o teto estava escrito em dois lugares (aqui e, com outros números,
    no HTML de /planos). A autoridade agora é `domain/tiers.py`.
    """
    ceiling = tier_max_results(user.get("tier"))
    return min(requested, ceiling) if ceiling else requested


def declare_result_counts(
    response: Response | None,
    *,
    total: int,
    returned: int,
    limit: int,
    page: int = 1,
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
    response.headers["X-Page"] = str(page)
    response.headers["X-Has-More"] = "true" if page * limit < total else "false"


async def get_api_user(x_api_key: str | None = Header(None)):
    anonymous = tier_by_code(TIER_ANONYMOUS)
    tier_info = {
        "tier": TIER_ANONYMOUS,
        "rate_limit": anonymous.rate_limit if anonymous else 10,
    }
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
    force = os.getenv("SINAN_FORCE_REFRESH") == "1"
    # Reentrar no ciclo de vida no mesmo processo nao pode reparsear os 17 MB
    # da cache: o estado ja esta em memoria. Custava 1,64s por entrada, e a
    # suite sobe o app vinte vezes.
    if not force and report_state_ready():
        logger.info("Estado do relatório já carregado; ingestão dispensada.")
        yield
        return

    logger.info("Iniciando ingestão de dados reais do SINAN/OpenDataSUS...")
    load_or_refresh_report(DEFAULT_YEAR, force_refresh=force)
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

    @app.get("/static/css/main.css", include_in_schema=False)
    async def serve_main_css() -> Response:
        """`main.css` com a versão propagada para cada `@import`.

        A impressão digital em `?v=` já cobria TODOS os arquivos CSS, mas só
        era aplicada ao `main.css`. Os componentes — onde vive quase todo o
        estilo — são buscados pelo navegador com a URL crua do `@import`, sem
        parâmetro nenhum. Resultado: `main.css?v=novo` era rebaixado, e o
        navegador seguia usando o `detail-panel.css` que já tinha em cache.

        Medido: depois de editar `detail-panel.css`, a página aplicava
        `@media (max-width: 820px)` — a regra anterior à edição. Uma correção
        de estilo simplesmente não chegava a quem já tinha visitado o site, e
        nada falhava.

        Esta rota é registrada ANTES do mount de `/static` para ter
        precedência sobre ele; o resto dos arquivos continua sendo servido
        pelo `StaticFiles`.
        """
        version = static_asset_version()
        try:
            css = (STATIC_DIR / "css" / "main.css").read_text(encoding="utf-8")
        except OSError as error:  # pragma: no cover - defensivo
            logger.error("Falha ao ler main.css: %s", error)
            raise HTTPException(status_code=404, detail="main.css indisponível")

        versionado = re.sub(
            r'@import url\("([^"?]+)"\)',
            lambda m: f'@import url("{m.group(1)}?v={version}")',
            css,
        )
        return Response(
            content=versionado,
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
else:
    logger.warning("STATIC_DIR %s does not exist - static assets will not be served", STATIC_DIR)


@app.middleware("http")
async def track_usage_middleware(request: Request, call_next):
    api_key = request.headers.get("X-API-Key", TIER_ANONYMOUS)
    response = await call_next(request)

    # Só as rotas de API são contadas. Isto é telemetria: qualquer falha aqui
    # é registrada e engolida, porque não pode custar a resposta ao cliente.
    # Em produção na Vercel a escrita em disco falhava e derrubava TODA rota
    # /v1/* com 500, enquanto /dashboard respondia normalmente.
    if request.url.path.startswith("/v1/"):
        try:
            usage_tracker.log_usage(
                api_key, request.url.path, request.method, response.status_code
            )
        except Exception as error:  # noqa: BLE001 - telemetria nunca derruba
            logger.warning("Falha ao registrar uso: %s", error)

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
        default=None,
        description=(
            "baixo, moderado, alto ou critico. Filtra por "
            "`nivel_risco_fonte_atual` — o mesmo campo que o painel exibe —, "
            "caindo em `nivel_risco` quando aquele não existe."
        ),
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
    pagina: int = Query(
        default=1,
        ge=1,
        description=(
            "Página de resultados, usada junto com `limite`. O cabeçalho "
            "X-Has-More indica se existe página seguinte."
        ),
    ),
    limite: int = Query(default=100, ge=1, le=MAX_RESULTS_PER_CALL),
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
    page = paginate(rows, page=pagina, limit=limite)
    declare_result_counts(
        response,
        total=len(rows),
        returned=len(page),
        limit=limite,
        page=pagina,
    )
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
        default=None,
        description=(
            "Código exato (DENG, CHIK, ZIKA...) ou parte do nome, sem acento: "
            "`chikungunya` e `amarela` funcionam."
        ),
    ),
    pagina: int = Query(default=1, ge=1),
    limite: int = Query(default=100, ge=1, le=MAX_RESULTS_PER_CALL),
    response: Response = None,  # type: ignore[assignment]
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    limite = tier_limit(user, limite)

    alerts = filter_alerts(
        db_alertas, municipio=municipio, estado=estado, doenca=doenca
    )
    page = paginate(alerts, page=pagina, limit=limite)
    declare_result_counts(
        response,
        total=len(alerts),
        returned=len(page),
        limit=limite,
        page=pagina,
    )
    return {
        "metadata": db_metadata,
        "total": len(alerts),
        "retornados": len(page),
        "pagina": pagina,
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

    contagem_por_nivel = Counter(row["nivel_risco"] for row in rows)

    # Add advanced analytics for Professional tier
    summary = {
        "total_municipios": len(rows),
        "total_casos_provaveis": sum(r["total_casos_provaveis"] for r in rows),
        "total_obitos": sum(r["total_obitos"] for r in rows),
        "media_risk_score": round(sum(r["risk_score"] for r in rows) / len(rows), 2)
        if rows
        else 0,
        # Percorre os níveis que o domínio declara, e não uma lista escrita
        # aqui: um nível acrescentado ao domínio e esquecido nesta chave
        # sumiria da distribuição em silêncio, e as contagens deixariam de
        # somar o total. De quebra, uma passada em vez de quatro sobre os
        # 5.408 municípios.
        "distribuicao_risco": {
            nivel: contagem_por_nivel.get(nivel, 0) for nivel in RISK_LEVEL_ORDER
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
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Não implementado — use /v1/professional-report",
    response_description=(
        "501. A geração de PDF não existe neste serviço; "
        "/v1/professional-report devolve os mesmos dados em JSON."
    ),
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
