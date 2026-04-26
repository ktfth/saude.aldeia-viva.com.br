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
from typing import Any, Iterable, Mapping

import datasus_dbc
from dbfread import DBF
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPEN_DATA_SUS_S3_BASE = "https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br"
DATASUS_SINAN_DBC_BASE = (
    "ftp://ftp.datasus.gov.br/dissemin/publicos/SINAN/DADOS/FINAIS"
)
IBGE_MUNICIPALITIES_URL = (
    "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
)
DEFAULT_YEAR = int(os.getenv("SINAN_YEAR", datetime.now(UTC).year))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("SINAN_REQUEST_TIMEOUT_SECONDS", "45"))
SINAN_CACHE_DIR = Path(os.getenv("SINAN_CACHE_DIR", ".cache/datasus"))
APP_ROOT = Path(__file__).resolve().parent
BUNDLED_REPORT_DIRS = (
    APP_ROOT / "api" / "data",
    APP_ROOT / "data",
)
BUNDLED_REPORT_DIR = BUNDLED_REPORT_DIRS[0]
REPORT_CACHE_VERSION = "risk-report-v1"
DEFAULT_DISEASE_CODES = (
    "DENG",
    "CHIK",
    "ZIKA",
    "YF",
    "LEPT",
    "MENI",
    "BOTU",
    "TOXC",
    "TOXG",
    "HANS",
)

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


@dataclass(frozen=True)
class RiskProfile:
    formula: str
    case_weight: float = 1.0
    warning_weight: float = 4.0
    severe_weight: float = 8.0
    death_weight: float = 20.0
    hospitalization_weight: float = 2.0
    moderate_threshold: float = 5.0
    high_threshold: float = 25.0
    critical_threshold: float = 100.0
    death_is_critical: bool = True
    severe_is_high: bool = True


RISK_PROFILES = {
    "arbovirus": RiskProfile(
        formula=(
            "casos_provaveis + 4*sinais_alarme + 8*casos_graves "
            "+ 20*obitos + 2*hospitalizacoes"
        )
    ),
    "yellow_fever": RiskProfile(
        formula="2*casos_provaveis + 30*obitos",
        case_weight=2.0,
        warning_weight=0.0,
        severe_weight=0.0,
        death_weight=30.0,
        hospitalization_weight=0.0,
        moderate_threshold=6.0,
        high_threshold=20.0,
        critical_threshold=60.0,
    ),
    "leptospirosis": RiskProfile(
        formula=(
            "casos_provaveis + 6*sinais_alarme + 12*casos_graves "
            "+ 25*obitos + 4*hospitalizacoes"
        ),
        warning_weight=6.0,
        severe_weight=12.0,
        death_weight=25.0,
        hospitalization_weight=4.0,
        moderate_threshold=5.0,
        high_threshold=20.0,
        critical_threshold=50.0,
    ),
    "meningitis": RiskProfile(
        formula="2*casos_provaveis + 12*casos_graves + 35*obitos",
        case_weight=2.0,
        warning_weight=0.0,
        severe_weight=12.0,
        death_weight=35.0,
        hospitalization_weight=0.0,
        moderate_threshold=6.0,
        high_threshold=22.0,
        critical_threshold=70.0,
    ),
    "animal_accident": RiskProfile(
        formula=(
            "casos_provaveis + 5*sinais_alarme + 10*casos_graves "
            "+ 20*obitos + 2*hospitalizacoes"
        ),
        warning_weight=5.0,
        severe_weight=10.0,
        death_weight=20.0,
        hospitalization_weight=2.0,
    ),
    "rare_severe": RiskProfile(
        formula="4*casos_provaveis + 20*casos_graves + 35*obitos + 8*hospitalizacoes",
        case_weight=4.0,
        warning_weight=0.0,
        severe_weight=20.0,
        death_weight=35.0,
        hospitalization_weight=8.0,
        moderate_threshold=4.0,
        high_threshold=20.0,
        critical_threshold=60.0,
    ),
    "chronic": RiskProfile(
        formula="casos_provaveis + 20*obitos",
        warning_weight=0.0,
        severe_weight=0.0,
        hospitalization_weight=0.0,
    ),
}

RISK_FORMULA = RISK_PROFILES["arbovirus"].formula


@dataclass(frozen=True)
class DiseaseSource:
    codigo: str
    nome: str
    virus: str
    tipo: str
    folder: str
    file_prefix: str
    first_year: int
    direct_csv_url: str = ""
    dbc_prefix: str = ""
    csv_encoding: str = "utf-8-sig"
    year_field: str = "NU_ANO"
    latest_year: int | None = None
    risk_profile: str = "arbovirus"
    severity_code_field: str = "CLASSI_FIN"
    warning_codes: frozenset[str] = frozenset()
    severe_codes: frozenset[str] = frozenset()
    warning_fields: frozenset[str] = frozenset()
    severe_fields: frozenset[str] = frozenset()
    hospitalization_fields: frozenset[str] = frozenset({"HOSPITALIZ"})


DISEASE_SOURCES: dict[str, DiseaseSource] = {
    "DENG": DiseaseSource(
        codigo="DENG",
        nome="Dengue",
        virus="DENV",
        tipo="Arbovirose urbana",
        folder="Dengue",
        file_prefix="DENG",
        first_year=2000,
        warning_codes=frozenset({"11"}),
        severe_codes=frozenset({"2", "3", "4", "12"}),
    ),
    "CHIK": DiseaseSource(
        codigo="CHIK",
        nome="Febre de Chikungunya",
        virus="CHIKV",
        tipo="Arbovirose urbana",
        folder="Chikungunya",
        file_prefix="CHIK",
        first_year=2015,
    ),
    "ZIKA": DiseaseSource(
        codigo="ZIKA",
        nome="Zika",
        virus="ZIKV",
        tipo="Arbovirose urbana",
        folder="Zikavirus",
        file_prefix="ZIKA",
        first_year=2016,
    ),
    "YF": DiseaseSource(
        codigo="YF",
        nome="Febre Amarela",
        virus="YFV",
        tipo="Arbovirose silvestre/urbana",
        folder="",
        file_prefix="",
        first_year=1994,
        direct_csv_url=(
            "https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/"
            "Febre+Amarela/fa_casoshumanos_1994-2025.csv"
        ),
        csv_encoding="latin-1",
        year_field="ANO_IS",
        latest_year=2025,
        risk_profile="yellow_fever",
        hospitalization_fields=frozenset(),
    ),
    "LEPT": DiseaseSource(
        codigo="LEPT",
        nome="Leptospirose",
        virus="Leptospira spp.",
        tipo="Zoonose bacteriana",
        folder="",
        file_prefix="",
        first_year=2000,
        dbc_prefix="LEPT",
        latest_year=2024,
        risk_profile="leptospirosis",
        warning_fields=frozenset({"CLI_ICTERI", "CLI_HEMORR"}),
        severe_fields=frozenset({"CLI_RENAL", "CLI_RESPIR", "CLI_CARDIA", "CLI_MENING"}),
        hospitalization_fields=frozenset({"ATE_HOSP"}),
    ),
    "MENI": DiseaseSource(
        codigo="MENI",
        nome="Meningite",
        virus="Múltiplos agentes",
        tipo="Doença infecciosa",
        folder="",
        file_prefix="",
        first_year=2007,
        dbc_prefix="MENI",
        latest_year=2022,
        risk_profile="meningitis",
        severe_fields=frozenset({"CLI_COMA", "CLI_PETEQU", "CLI_CONVUL"}),
        hospitalization_fields=frozenset(),
    ),
    "ANIM": DiseaseSource(
        codigo="ANIM",
        nome="Acidentes por Animais Peçonhentos",
        virus="Animais peçonhentos",
        tipo="Acidente/agravo de notificação",
        folder="",
        file_prefix="",
        first_year=2007,
        dbc_prefix="ANIM",
        latest_year=2022,
        risk_profile="animal_accident",
        severity_code_field="TRA_CLASSI",
        warning_codes=frozenset({"2"}),
        severe_codes=frozenset({"3"}),
        hospitalization_fields=frozenset(),
    ),
    "BOTU": DiseaseSource(
        codigo="BOTU",
        nome="Botulismo",
        virus="Clostridium botulinum",
        tipo="Doença bacteriana/toxina",
        folder="",
        file_prefix="",
        first_year=2007,
        dbc_prefix="BOTU",
        latest_year=2023,
        risk_profile="rare_severe",
        severe_fields=frozenset({"STRESPIRA", "STCARDIACA", "STCOMA"}),
        hospitalization_fields=frozenset({"STHOSPITAL"}),
    ),
    "TOXC": DiseaseSource(
        codigo="TOXC",
        nome="Toxoplasmose Congênita",
        virus="Toxoplasma gondii",
        tipo="Doença parasitária congênita",
        folder="",
        file_prefix="",
        first_year=2019,
        dbc_prefix="TOXC",
        latest_year=2023,
        risk_profile="chronic",
        hospitalization_fields=frozenset(),
    ),
    "TOXG": DiseaseSource(
        codigo="TOXG",
        nome="Toxoplasmose Gestacional",
        virus="Toxoplasma gondii",
        tipo="Doença parasitária gestacional",
        folder="",
        file_prefix="",
        first_year=2019,
        dbc_prefix="TOXG",
        latest_year=2023,
        risk_profile="chronic",
        hospitalization_fields=frozenset(),
    ),
    "HANS": DiseaseSource(
        codigo="HANS",
        nome="Hanseníase",
        virus="Mycobacterium leprae",
        tipo="Doença bacteriana crônica",
        folder="",
        file_prefix="",
        first_year=2001,
        dbc_prefix="HANS",
        latest_year=2023,
        risk_profile="chronic",
        hospitalization_fields=frozenset(),
    ),
}

DENGUE_CLASSIFICATION_LABELS = {
    "1": "Dengue clássico",
    "2": "Dengue com complicações",
    "3": "Febre hemorrágica do dengue",
    "4": "Síndrome do choque da dengue",
    "5": "Descartado",
    "8": "Inconclusivo",
    "10": "Dengue",
    "11": "Dengue com sinais de alarme",
    "12": "Dengue grave",
    "13": "Chikungunya",
}

CHIKUNGUNYA_CLASSIFICATION_LABELS = {
    "5": "Descartado",
    "8": "Inconclusivo",
    "13": "Chikungunya",
}

ZIKA_CLASSIFICATION_LABELS = {
    "0": "Sem classificação final",
    "1": "Zika",
    "2": "Zika",
    "5": "Descartado",
    "8": "Inconclusivo",
}

YELLOW_FEVER_CLASSIFICATION_LABELS = {
    "": "Febre amarela confirmada",
}

SAO_PAULO_DISTRICTS = (
    "Água Rasa",
    "Alto de Pinheiros",
    "Anhanguera",
    "Aricanduva",
    "Artur Alvim",
    "Arthur Alvim",
    "Barra Funda",
    "Bela Vista",
    "Belém",
    "Bom Retiro",
    "Brás",
    "Brasilândia",
    "Butantã",
    "Cachoeirinha",
    "Cambuci",
    "Campo Belo",
    "Campo Grande",
    "Campo Limpo",
    "Cangaíba",
    "Capão Redondo",
    "Carrão",
    "Casa Verde",
    "Cidade Ademar",
    "Cidade Dutra",
    "Cidade Líder",
    "Cidade Tiradentes",
    "Consolação",
    "Cursino",
    "Ermelino Matarazzo",
    "Freguesia do Ó",
    "Grajaú",
    "Guaianases",
    "Iguatemi",
    "Ipiranga",
    "Itaim Bibi",
    "Itaim Paulista",
    "Itaquera",
    "Jabaquara",
    "Jaçanã",
    "Jaguara",
    "Jaguaré",
    "Jaraguá",
    "Jardim Ângela",
    "Jardim Helena",
    "Jardim Paulista",
    "Jardim São Luís",
    "Jardim São Luiz",
    "José Bonifácio",
    "Lajeado",
    "Lapa",
    "Liberdade",
    "Limão",
    "Mandaqui",
    "Marsilac",
    "Moema",
    "Mooca",
    "Moóca",
    "Morumbi",
    "Parelheiros",
    "Pari",
    "Parque do Carmo",
    "Pedreira",
    "Penha",
    "Perdizes",
    "Perus",
    "Pinheiros",
    "Pirituba",
    "Ponte Rasa",
    "Raposo Tavares",
    "República",
    "Rio Pequeno",
    "Sacomã",
    "Santa Cecília",
    "Santana",
    "Santo Amaro",
    "São Domingos",
    "São Lucas",
    "São Mateus",
    "São Miguel",
    "São Rafael",
    "Sapopemba",
    "Saúde",
    "Sé",
    "Socorro",
    "Tatuapé",
    "Tremembé",
    "Tucuruvi",
    "Vila Andrade",
    "Vila Curuçá",
    "Vila Formosa",
    "Vila Guilherme",
    "Vila Jacuí",
    "Vila Leopoldina",
    "Vila Maria",
    "Vila Mariana",
    "Vila Matilde",
    "Vila Medeiros",
    "Vila Prudente",
    "Vila Sônia",
)


def build_sao_paulo_district_aliases() -> dict[str, dict[str, str]]:
    aliases: dict[str, dict[str, str]] = {}
    for district in SAO_PAULO_DISTRICTS:
        aliases[normalize_alias_key(district)] = {
            "tipo": "distrito",
            "localidade": district,
            "municipio_resolvido": "São Paulo",
            "codigo_municipio": "355030",
            "estado": "SP",
            "granularidade_disponivel": "municipio",
        }
    return aliases


def normalize_alias_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


LOCALITY_ALIASES = build_sao_paulo_district_aliases()

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


def build_source_url(source: DiseaseSource, year: int) -> str:
    suffix = str(year)[-2:]
    return (
        f"{OPEN_DATA_SUS_S3_BASE}/SINAN/{source.folder}/csv/"
        f"{source.file_prefix}BR{suffix}.csv.zip"
    )


def build_dbc_source_url(source: DiseaseSource, year: int) -> str:
    suffix = str(year)[-2:]
    return f"{DATASUS_SINAN_DBC_BASE}/{source.dbc_prefix}BR{suffix}.dbc"


def load_csv_records_from_zip(url: str) -> list[dict[str, str]]:
    payload = download_bytes(url)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        csv_member = next(
            member for member in archive.namelist() if member.lower().endswith(".csv")
        )
        with archive.open(csv_member) as raw_file:
            text_file = io.TextIOWrapper(raw_file, encoding="utf-8-sig", errors="replace")
            return list(csv.DictReader(text_file))


def load_csv_records_from_url(url: str, *, encoding: str) -> list[dict[str, str]]:
    payload = download_bytes(url)
    text = payload.decode(encoding, errors="replace")
    return list(csv.DictReader(text.splitlines(), delimiter=";"))


def load_dbc_records_from_url(url: str) -> list[dict[str, str]]:
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
            logger.warning("Não foi possível remover arquivo temporário DBF: %s", temp_path)


def download_bytes(url: str) -> bytes:
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
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
    return SINAN_CACHE_DIR / f"{digest}{suffix}"


def load_latest_available_records(
    source: DiseaseSource, target_year: int
) -> tuple[int, str, list[dict[str, str]]]:
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
            logger.info("%s indisponível em %s: HTTP %s", source.codigo, year, error.code)
    raise RuntimeError(f"Nenhum CSV disponível para {source.nome}.")


def normalize_dbf_record(row: Mapping[str, Any]) -> dict[str, str]:
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


def load_municipality_lookup() -> dict[str, dict[str, str]]:
    try:
        with urllib.request.urlopen(
            IBGE_MUNICIPALITIES_URL, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            municipalities = decode_json_payload(response.read())
    except Exception as error:
        logger.warning("Não foi possível carregar municípios do IBGE: %s", error)
        return {}

    lookup: dict[str, dict[str, str]] = {}
    for municipality in municipalities:
        code7 = clean_value(municipality.get("id"))
        code6 = code7[:6]
        state = find_ibge_state_abbr(municipality)
        lookup[code6] = {
            "municipio": clean_value(municipality.get("nome")) or f"Código {code6}",
            "estado": state,
            "codigo_ibge": code7,
        }
    return lookup


def find_ibge_state_abbr(municipality: Mapping[str, Any]) -> str:
    microregion = municipality.get("microrregiao") or {}
    mesoregion = microregion.get("mesorregiao") or {}
    state = mesoregion.get("UF") or {}
    if state.get("sigla"):
        return clean_value(state["sigla"]).upper()

    immediate_region = municipality.get("regiao-imediata") or {}
    intermediate_region = immediate_region.get("regiao-intermediaria") or {}
    state = intermediate_region.get("UF") or {}
    return clean_value(state.get("sigla")).upper()


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

    source_order = {source.codigo: index for index, source in enumerate(disease_sources)}
    sources.sort(key=lambda item: source_order.get(clean_value(item.get("codigo")), 999))

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
            code.strip().upper()
            for code in configured_codes.split(",")
            if code.strip()
        ]
    else:
        codes = list(DEFAULT_DISEASE_CODES)

    return [DISEASE_SOURCES[code] for code in codes if code in DISEASE_SOURCES]


def report_cache_path(year: int, disease_codes: Iterable[str]) -> Path:
    return report_cache_file_path(SINAN_CACHE_DIR, year, disease_codes)


def bundled_report_cache_path(year: int, disease_codes: Iterable[str]) -> Path:
    return report_cache_file_path(BUNDLED_REPORT_DIRS[0], year, disease_codes)


def bundled_report_cache_paths(year: int, disease_codes: Iterable[str]) -> list[Path]:
    return [
        report_cache_file_path(root_dir, year, disease_codes)
        for root_dir in BUNDLED_REPORT_DIRS
    ]


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


def report_has_content(report: Mapping[str, Any]) -> bool:
    metadata = report.get("metadata") or {}
    return clean_value(metadata.get("status")) == "ok" and bool(
        report.get("municipios")
    )


def load_bundled_report_cache(
    year: int, disease_codes: Iterable[str]
) -> dict[str, Any] | None:
    for path in bundled_report_cache_paths(year, disease_codes):
        if not path.exists():
            continue
        try:
            report = load_report_cache(path)
        except Exception as error:
            logger.warning("Cache embarcado inválido em %s: %s", path, error)
            continue
        if report_has_content(report):
            return report
    return None


def public_report_cache_path(path: Path) -> str:
    try:
        return path.relative_to(SINAN_CACHE_DIR).as_posix()
    except ValueError:
        return path.name


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

    db_clini = list(report.get("municipios") or [])
    db_alertas = list(report.get("alertas_altos") or [])
    db_metadata = metadata


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
    bundled_report = None

    if os.getenv("VERCEL") == "1" and not force_refresh:
        bundled_report = load_bundled_report_cache(year, enabled_codes)
        if bundled_report is not None:
            apply_report_state(
                bundled_report,
                cache_hit=True,
                cache_path=bundled_report_cache_path(year, enabled_codes),
                cache_source="bundled",
            )
            logger.info(
                "Relatório epidemiológico carregado do snapshot embarcado: %s",
                bundled_report_cache_path(year, enabled_codes),
            )
            return bundled_report

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

    report = fetch_epidemiology_report(year)
    if not report_has_content(report):
        bundled_report = load_bundled_report_cache(year, enabled_codes)
        if bundled_report is not None:
            apply_report_state(
                bundled_report,
                cache_hit=True,
                cache_path=bundled_report_cache_path(year, enabled_codes),
                cache_source="bundled",
            )
            logger.warning(
                "Carga real vazia; usando snapshot embarcado em %s",
                bundled_report_cache_path(year, enabled_codes),
            )
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
            municipality_code = extract_municipality_code(record)
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


def create_municipality_summary(
    municipality_code: str,
    record: Mapping[str, Any],
    year: int,
    lookup_item: Mapping[str, str] | None,
) -> dict[str, Any]:
    state = clean_value(lookup_item.get("estado") if lookup_item else "")
    if not state:
        state = state_from_record(record)
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


def create_disease_summary(source: DiseaseSource, year: int) -> dict[str, Any]:
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


def finalize_municipality_rows(
    municipalities: Iterable[dict[str, Any]]
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
        rows.append(municipality)

    return sorted(rows, key=lambda item: item["risk_score"], reverse=True)


def finalize_disease_summary(disease: dict[str, Any]) -> dict[str, Any]:
    profile = RISK_PROFILES.get(disease["perfil_risco"], RISK_PROFILES["arbovirus"])
    disease["risk_score"] = round(
        (profile.case_weight * disease["casos_provaveis"])
        + (profile.warning_weight * disease["sinais_alarme"])
        + (profile.severe_weight * disease["casos_graves"])
        + (profile.death_weight * disease["obitos"])
        + (profile.hospitalization_weight * disease["hospitalizacoes"]),
        2,
    )
    disease["nivel_risco"] = risk_level(
        disease["risk_score"],
        disease["casos_provaveis"],
        disease["casos_graves"],
        disease["obitos"],
        profile,
    )
    disease["classificacoes"] = dict(sorted(disease["classificacoes"].items()))
    return disease


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


def risk_level(
    score: float,
    probable_cases: int,
    severe_cases: int,
    deaths: int,
    profile: RiskProfile | None = None,
) -> str:
    active_profile = profile or RISK_PROFILES["arbovirus"]
    if (active_profile.death_is_critical and deaths > 0) or (
        score >= active_profile.critical_threshold
    ):
        return "critico"
    if (active_profile.severe_is_high and severe_cases > 0) or (
        score >= active_profile.high_threshold
    ):
        return "alto"
    if probable_cases >= 5 or score >= active_profile.moderate_threshold:
        return "moderado"
    return "baixo"


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


def classification_label(disease_code: str, code: str) -> str:
    if disease_code == "YF" and not code:
        return YELLOW_FEVER_CLASSIFICATION_LABELS[""]
    if not code:
        return "Sem classificação final"

    labels_by_disease = {
        "DENG": DENGUE_CLASSIFICATION_LABELS,
        "CHIK": CHIKUNGUNYA_CLASSIFICATION_LABELS,
        "ZIKA": ZIKA_CLASSIFICATION_LABELS,
        "YF": YELLOW_FEVER_CLASSIFICATION_LABELS,
    }
    labels = labels_by_disease.get(disease_code, {})
    return labels.get(code, f"Classificação {code}")


def update_latest_date(summary: dict[str, Any], field: str, candidate: str) -> None:
    if not candidate:
        return
    current = summary.get(field)
    if current is None or candidate > current:
        summary[field] = candidate


def any_flag(record: Mapping[str, Any], prefix: str) -> bool:
    return any(
        field.startswith(prefix) and is_truthy_code(value)
        for field, value in record.items()
    )


def has_any_positive_field(record: Mapping[str, Any], fields: Iterable[str]) -> bool:
    return any(is_truthy_code(record.get(field)) for field in fields)


def is_truthy_code(value: Any) -> bool:
    text = normalize_text(clean_value(value))
    return text in {"1", "sim", "s", "yes", "true"}


def is_hospitalized_record(source: DiseaseSource, record: Mapping[str, Any]) -> bool:
    return has_any_positive_field(record, source.hospitalization_fields)


def is_death_record(record: Mapping[str, Any]) -> bool:
    if clean_code(record.get("EVOLUCAO")) == "2":
        return True
    death_value = normalize_text(clean_value(record.get("OBITO")))
    return death_value in {"sim", "s", "yes"}


def first_present(record: Mapping[str, Any], fields: Iterable[str]) -> str:
    for field in fields:
        value = clean_value(record.get(field))
        if value:
            return value
    return ""


def parse_date_value(value: str) -> str:
    text = clean_value(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def risk_profile_for_source(source: DiseaseSource) -> RiskProfile:
    return RISK_PROFILES.get(source.risk_profile, RISK_PROFILES["arbovirus"])


def clean_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


def clean_code(value: Any) -> str:
    text = clean_value(value)
    if text.endswith(".0"):
        text = text[:-2]
    return text


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def filter_risk_index(
    rows: Iterable[Mapping[str, Any]],
    *,
    municipio: str | None = None,
    estado: str | None = None,
    somente_altos: bool = False,
    nivel_minimo: str | None = None,
) -> list[dict[str, Any]]:
    state_filter = clean_value(estado).upper()
    locality_alias = resolve_locality_alias(municipio, state_filter)
    municipality_filter = normalize_text(
        locality_alias["municipio_resolvido"] if locality_alias else clean_value(municipio)
    )
    min_level = clean_value(nivel_minimo).lower()
    filtered: list[dict[str, Any]] = []

    for row in rows:
        if state_filter and clean_value(row.get("estado")).upper() != state_filter:
            continue
        if municipality_filter and not municipality_matches(
            row, municipality_filter, locality_alias
        ):
            continue
        if somente_altos and not row.get("doencas_altas"):
            continue
        if min_level and not level_at_least(clean_value(row.get("nivel_risco")), min_level):
            continue
        filtered.append(with_locality_alias(row, locality_alias))
    return filtered


def filter_alerts(
    alerts: Iterable[Mapping[str, Any]],
    *,
    municipio: str | None = None,
    estado: str | None = None,
    doenca: str | None = None,
) -> list[dict[str, Any]]:
    state_filter = clean_value(estado).upper()
    locality_alias = resolve_locality_alias(municipio, state_filter)
    municipality_filter = normalize_text(
        locality_alias["municipio_resolvido"] if locality_alias else clean_value(municipio)
    )
    disease_filter = normalize_text(clean_value(doenca))
    filtered: list[dict[str, Any]] = []

    for alert in alerts:
        if state_filter and clean_value(alert.get("estado")).upper() != state_filter:
            continue
        if municipality_filter and not municipality_matches(
            alert, municipality_filter, locality_alias
        ):
            continue
        disease_name = normalize_text(clean_value(alert.get("doenca")))
        disease_code = normalize_text(clean_value(alert.get("codigo_doenca")))
        if disease_filter and disease_filter not in {disease_name, disease_code}:
            continue
        filtered.append(with_locality_alias(alert, locality_alias))
    return filtered


def resolve_locality_alias(
    municipio: str | None, state_filter: str
) -> dict[str, str] | None:
    query = clean_value(municipio)
    alias = LOCALITY_ALIASES.get(normalize_text(query))
    if not alias:
        return None
    if state_filter and alias["estado"] != state_filter:
        return None
    return {"consulta": query, **alias}


def municipality_matches(
    row: Mapping[str, Any],
    municipality_filter: str,
    locality_alias: Mapping[str, str] | None = None,
) -> bool:
    if locality_alias:
        return (
            clean_value(row.get("codigo_municipio"))
            == locality_alias["codigo_municipio"]
            and clean_value(row.get("estado")).upper() == locality_alias["estado"]
        )

    name = normalize_text(clean_value(row.get("municipio")))
    code = normalize_text(clean_value(row.get("codigo_municipio")))
    return municipality_filter in name or municipality_filter == code


def with_locality_alias(
    row: Mapping[str, Any], locality_alias: Mapping[str, str] | None
) -> dict[str, Any]:
    output = dict(row)
    if locality_alias:
        output["filtro_localidade"] = dict(locality_alias)
    return output


def level_at_least(level: str, minimum: str) -> bool:
    order = {"baixo": 0, "moderado": 1, "alto": 2, "critico": 3}
    return order.get(level, -1) >= order.get(minimum, -1)


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
        ("/sobre", "Dados e metodologia", "sobre"),
        ("/agentes", "Agentes", "agentes"),
        ("/docs", "API", "api"),
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
  <link rel="alternate" type="application/json" href="{canonical_url(request, '/agent.json')}" title="Manifesto para agentes">
  <link rel="alternate" type="text/plain" href="{canonical_url(request, '/llms.txt')}" title="Instruções para LLMs">
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
  <style>{BASE_CSS}</style>
</head>
<body>
  <header class="site-header">
    <a class="brand" href="/dashboard" aria-label="{escape_html(SITE_NAME)}">
      <span class="brand-mark" aria-hidden="true">AV</span>
      <span><strong>{escape_html(SITE_NAME)}</strong><small>{escape_html(SITE_TAGLINE)}</small></span>
    </a>
    <nav aria-label="Navegação principal">{nav}</nav>
  </header>
  {body}
  <footer class="site-footer">
    <span>Dados reais SINAN/OpenDataSUS, enriquecidos por município via IBGE.</span>
    <span><a href="/v1/metadata">Metadados</a> <a href="/agent.json">agent.json</a> <a href="/llms.txt">llms.txt</a></span>
  </footer>
  {extra_script}
</body>
</html>"""


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
  --amber: #b7791f;
  --red: #b42318;
  --blue: #2457a6;
  --shadow: 0 18px 45px rgba(23, 33, 28, .08);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: #f7faf8;
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.5;
}
a { color: inherit; }
.site-header {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  min-height: 76px;
  padding: 14px clamp(18px, 4vw, 56px);
  border-bottom: 1px solid rgba(20, 108, 67, .16);
  background: rgba(247, 250, 248, .94);
  backdrop-filter: blur(16px);
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  min-width: 250px;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border-radius: 8px;
  background: var(--green);
  color: #fff;
  font-weight: 800;
  letter-spacing: 0;
}
.brand strong { display: block; font-size: 1rem; letter-spacing: 0; }
.brand small { display: block; max-width: 360px; color: var(--muted); font-size: .78rem; }
nav { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
nav a {
  min-height: 36px;
  padding: 8px 11px;
  border-radius: 8px;
  color: var(--muted);
  font-size: .92rem;
  text-decoration: none;
}
nav a:hover, nav a.active { background: #e7f2ec; color: var(--green); }
.page { max-width: 1180px; margin: 0 auto; padding: 34px clamp(18px, 4vw, 34px) 56px; }
.hero {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(320px, .95fr);
  gap: 32px;
  align-items: center;
  padding: 28px 0 34px;
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
h1 { max-width: 780px; font-size: clamp(2.1rem, 5vw, 4.7rem); }
h2 { font-size: clamp(1.45rem, 3vw, 2.35rem); }
h3 { font-size: 1.04rem; }
.lead { max-width: 720px; color: var(--muted); font-size: clamp(1.02rem, 2vw, 1.22rem); }
.actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 22px; }
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 42px;
  padding: 10px 15px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  color: var(--ink);
  font-weight: 700;
  text-decoration: none;
  cursor: pointer;
}
.button.primary { border-color: var(--green); background: var(--green); color: #fff; }
.panel {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
}
.radar-panel { padding: 18px; }
.radar-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
.metric { min-height: 108px; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--soft); }
.metric span { color: var(--muted); font-size: .8rem; font-weight: 700; text-transform: uppercase; }
.metric strong { display: block; margin-top: 8px; font-size: clamp(1.55rem, 4vw, 2.25rem); line-height: 1; }
.toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 1.25fr) minmax(90px, .42fr) minmax(160px, .72fr) minmax(136px, .58fr) auto;
  gap: 10px;
  align-items: end;
  margin: 22px 0;
  padding: 14px;
}
label { display: grid; gap: 6px; color: var(--muted); font-size: .82rem; font-weight: 700; }
input, select {
  width: 100%;
  min-height: 42px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 9px 11px;
  background: #fff;
  color: var(--ink);
  font: inherit;
}
input:focus, select:focus, .button:focus { outline: 3px solid rgba(6, 122, 118, .2); outline-offset: 2px; }
.switch { display: flex; align-items: center; gap: 9px; min-height: 42px; padding-top: 22px; color: var(--ink); }
.switch input { width: 18px; min-height: 18px; }
.content-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(320px, .8fr); gap: 18px; align-items: start; }
.section-panel { padding: 18px; }
.section-head { display: flex; align-items: start; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
.section-head p { margin: 6px 0 0; color: var(--muted); }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; min-width: 720px; border-collapse: collapse; background: #fff; }
th, td { padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: .76rem; text-transform: uppercase; letter-spacing: .06em; background: #f7faf8; }
tr:last-child td { border-bottom: 0; }
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 25px;
  padding: 3px 8px;
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
.alert-list { display: grid; gap: 10px; }
.alert-item { padding: 13px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.alert-item header { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 9px; }
.alert-item p { margin: 0; color: var(--muted); }
.facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 26px 0; }
.fact { min-height: 138px; padding: 18px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.fact p { margin: 10px 0 0; color: var(--muted); }
.prose { max-width: 880px; }
.prose p, .prose li { color: var(--muted); }
.prose code, .code-block { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
.code-block {
  overflow-x: auto;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #132019;
  color: #eef8f1;
  font-size: .9rem;
}
.link-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }
.link-card { padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: #fff; text-decoration: none; }
.link-card span { display: block; color: var(--muted); margin-top: 6px; }
.status-line { min-height: 24px; color: var(--muted); font-size: .92rem; }
.site-footer {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 24px clamp(18px, 4vw, 56px);
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: .88rem;
}
.site-footer a { margin-left: 12px; color: var(--green); font-weight: 700; text-decoration: none; }
@media (max-width: 820px) {
  .site-header { position: static; align-items: flex-start; flex-direction: column; }
  nav { justify-content: flex-start; }
  .hero, .content-grid, .facts, .link-grid { grid-template-columns: 1fr; }
  .toolbar { grid-template-columns: 1fr; }
  .switch { padding-top: 0; }
  .radar-grid { grid-template-columns: 1fr; }
  .site-footer { flex-direction: column; }
  .site-footer a { margin: 0 12px 0 0; }
}
@media (prefers-reduced-motion: no-preference) {
  .button, nav a, .link-card, .alert-item { transition: transform .18s ease, border-color .18s ease, background .18s ease; }
  .button:hover, .link-card:hover, .alert-item:hover { transform: translateY(-1px); }
}
"""


def render_dashboard_page(request: Request) -> str:
    summary = dashboard_summary()
    alerts = db_alertas[:6]
    initial_json = json.dumps(
        {"summary": summary, "alerts": alerts, "municipios": db_clini[:20]},
        ensure_ascii=False,
    )
    body = f"""
<main class="page" id="risk-dashboard">
  <section class="hero" aria-labelledby="dashboard-title">
    <div>
      <p class="eyebrow">Monitoramento epidemiológico municipal</p>
      <h1 id="dashboard-title">Risco epidemiológico de múltiplos agravos em uma visão operacional.</h1>
      <p class="lead">Dados reais do SINAN/OpenDataSUS e DBCs do DATASUS, enriquecidos por município e organizados para leitura executiva, técnica e automatizada.</p>
      <div class="actions">
        <a class="button primary" href="#consulta">Consultar risco</a>
        <a class="button" href="/v1/high-alerts">Ver JSON de alertas</a>
      </div>
    </div>
    <aside class="panel radar-panel" aria-label="Resumo da carga atual">
      <h2>Radar atual</h2>
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
      <input id="municipio" name="municipio" value="perus" autocomplete="address-level2">
    </label>
    <label>UF
      <input id="estado" name="estado" value="SP" maxlength="2" autocomplete="address-level1">
    </label>
    <label>Nível mínimo
      <select id="nivel_minimo" name="nivel_minimo">
        <option value="">Todos</option>
        <option value="moderado">Moderado</option>
        <option value="alto">Alto</option>
        <option value="critico">Crítico</option>
      </select>
    </label>
    <label class="switch"><input id="somente_altos" name="somente_altos" type="checkbox"> Somente altos</label>
    <button class="button primary" type="submit">Atualizar</button>
  </form>

  <div class="content-grid">
    <section class="panel section-panel" aria-labelledby="municipios-title">
      <div class="section-head">
        <div>
          <h2 id="municipios-title">Resultado por município</h2>
          <p id="dashboard-status" class="status-line">Pronto para consulta.</p>
        </div>
        <a class="button" href="/sobre">Metodologia</a>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Município</th><th>Risco</th><th>Casos</th><th>Óbitos</th><th>Doenças altas</th></tr></thead>
          <tbody id="risk-rows">{render_dashboard_rows(db_clini[:8])}</tbody>
        </table>
      </div>
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
  <script id="initial-dashboard-data" type="application/json">{escape_html(initial_json)}</script>
</main>"""
    return render_web_page(
        request,
        path="/dashboard",
        title=f"Dashboard de risco epidemiológico | {SITE_NAME}",
        description=SITE_DESCRIPTION,
        body=body,
        json_ld=base_json_ld(request)
        + [dataset_json_ld(request), breadcrumb_json_ld(request, "Dashboard", "/dashboard")],
        active="dashboard",
        extra_script=f"<script>{DASHBOARD_JS}</script>",
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
<main class="page">
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
        + [dataset_json_ld(request), breadcrumb_json_ld(request, "Dados e metodologia", "/sobre")],
        active="sobre",
    )


def render_agents_page(request: Request) -> str:
    body = f"""
<main class="page">
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
  <section class="panel section-panel prose">
    <h2>Endpoints recomendados</h2>
    <div class="link-grid">
      <a class="link-card" href="/v1/high-alerts"><strong>/v1/high-alerts</strong><span>Alertas altos e críticos por município, doença e vírus.</span></a>
      <a class="link-card" href="/v1/risk-index"><strong>/v1/risk-index</strong><span>Índice enriquecido com filtros por município, UF e nível mínimo.</span></a>
      <a class="link-card" href="/v1/diseases"><strong>/v1/diseases</strong><span>Catálogo de doenças e agravos suportados pela API.</span></a>
      <a class="link-card" href="/v1/metadata"><strong>/v1/metadata</strong><span>Fontes, ano, status da carga e fórmula de risco.</span></a>
      <a class="link-card" href="/openapi.json"><strong>/openapi.json</strong><span>Contrato OpenAPI para geração de clientes e ferramentas.</span></a>
    </div>
    <h2>Exemplo</h2>
    <pre class="code-block">GET /v1/high-alerts?estado=SP&amp;limite=10
GET /v1/risk-index?municipio=perus&amp;estado=SP&amp;somente_altos=false</pre>
    <h2>Regras de interpretação</h2>
    <p>Use <code>nivel_risco</code> para priorização, <code>risk_score</code> para ordenação, <code>/v1/diseases</code> para descobrir agravos carregados e <code>filtro_localidade</code> para identificar quando a consulta original foi resolvida para outro município. Não inferir bairro ou distrito quando <code>granularidade_disponivel</code> for municipal.</p>
  </section>
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
        + [software_json_ld(request), breadcrumb_json_ld(request, "Agentes", "/agentes")],
        active="agentes",
    )


DASHBOARD_JS = r"""
const form = document.getElementById('consulta');
const rows = document.getElementById('risk-rows');
const alerts = document.getElementById('alert-list');
const statusLine = document.getElementById('dashboard-status');
const levelClass = (value) => String(value || 'baixo').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const fmt = new Intl.NumberFormat('pt-BR');

function badge(value) {
  const level = levelClass(value);
  return `<span class="badge ${level}">${value || 'baixo'}</span>`;
}

function diseaseNames(items) {
  if (!items || items.length === 0) return 'Sem alerta alto';
  return items.map((item) => `${item.nome || item.doenca} (${item.nivel_risco})`).join(', ');
}

function renderRows(items) {
  if (!Array.isArray(items) || items.length === 0) {
    rows.innerHTML = '<tr><td colspan="5">Nenhum município encontrado para o filtro.</td></tr>';
    return;
  }
  rows.innerHTML = items.map((item) => `
    <tr>
      <td><strong>${item.municipio}</strong><br><span class="status-line">${item.estado} · ${item.codigo_municipio}</span></td>
      <td>${badge(item.nivel_risco)}<br><span class="status-line">score ${fmt.format(item.risk_score || 0)}</span></td>
      <td>${fmt.format(item.total_casos_provaveis || 0)}</td>
      <td>${fmt.format(item.total_obitos || 0)}</td>
      <td>${diseaseNames(item.doencas_altas)}</td>
    </tr>
  `).join('');
}

function renderAlerts(items) {
  if (!Array.isArray(items) || items.length === 0) {
    alerts.innerHTML = '<p class="status-line">Nenhum alerta alto para o filtro.</p>';
    return;
  }
  alerts.innerHTML = items.map((item) => `
    <article class="alert-item">
      <header><strong>${item.doenca}</strong>${badge(item.nivel_risco)}</header>
      <p>${item.municipio}/${item.estado} · ${item.virus} · ${fmt.format(item.casos_provaveis || 0)} casos prováveis</p>
      <p>Graves ${fmt.format(item.casos_graves || 0)} · Óbitos ${fmt.format(item.obitos || 0)} · Score ${fmt.format(item.risk_score || 0)}</p>
    </article>
  `).join('');
}

async function loadDashboard(event) {
  if (event) event.preventDefault();
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const [key, value] of data.entries()) {
    if (value && key !== 'somente_altos') params.set(key, value);
  }
  params.set('somente_altos', document.getElementById('somente_altos').checked ? 'true' : 'false');
  params.set('limite', '25');

  statusLine.textContent = 'Atualizando...';
  try {
    const riskResponse = await fetch(`/v1/risk-index?${params.toString()}`);
    const alertParams = new URLSearchParams();
    if (params.get('municipio')) alertParams.set('municipio', params.get('municipio'));
    if (params.get('estado')) alertParams.set('estado', params.get('estado'));
    alertParams.set('limite', '10');
    const alertResponse = await fetch(`/v1/high-alerts?${alertParams.toString()}`);
    const riskPayload = await riskResponse.json();
    const alertPayload = await alertResponse.json();
    renderRows(Array.isArray(riskPayload) ? riskPayload : []);
    renderAlerts(alertPayload.alerts || []);
    statusLine.textContent = Array.isArray(riskPayload) ? `${riskPayload.length} município(s) retornado(s).` : riskPayload.message;
  } catch (error) {
    statusLine.textContent = 'Não foi possível atualizar os dados agora.';
  }
}

form.addEventListener('submit', loadDashboard);
"""


def dashboard_summary() -> dict[str, int]:
    return {
        "municipios_monitorados": len(db_clini),
        "alertas_altos": len(db_alertas),
        "casos_provaveis": sum_int(row.get("total_casos_provaveis") for row in db_clini),
        "obitos": sum_int(row.get("total_obitos") for row in db_clini),
    }


def render_dashboard_rows(rows: Iterable[Mapping[str, Any]]) -> str:
    rendered = []
    for row in rows:
        rendered.append(
            "<tr>"
            f"<td><strong>{escape_html(row.get('municipio'))}</strong><br>"
            f"<span class=\"status-line\">{escape_html(row.get('estado'))} · {escape_html(row.get('codigo_municipio'))}</span></td>"
            f"<td>{render_badge(row.get('nivel_risco'))}<br><span class=\"status-line\">score {format_number(row.get('risk_score'))}</span></td>"
            f"<td>{format_number(row.get('total_casos_provaveis'))}</td>"
            f"<td>{format_number(row.get('total_obitos'))}</td>"
            f"<td>{escape_html(', '.join(clean_value(item.get('nome')) for item in row.get('doencas_altas', [])) or 'Sem alerta alto')}</td>"
            "</tr>"
        )
    return "".join(rendered) or '<tr><td colspan="5">Dados ainda não carregados.</td></tr>'


def render_alert_items(alerts: Iterable[Mapping[str, Any]]) -> str:
    rendered = []
    for alert in alerts:
        rendered.append(
            '<article class="alert-item">'
            f"<header><strong>{escape_html(alert.get('doenca'))}</strong>{render_badge(alert.get('nivel_risco'))}</header>"
            f"<p>{escape_html(alert.get('municipio'))}/{escape_html(alert.get('estado'))} · "
            f"{escape_html(alert.get('virus'))} · {format_number(alert.get('casos_provaveis'))} casos prováveis</p>"
            f"<p>Graves {format_number(alert.get('casos_graves'))} · Óbitos {format_number(alert.get('obitos'))} · "
            f"Score {format_number(alert.get('risk_score'))}</p>"
            "</article>"
        )
    return "".join(rendered) or '<p class="status-line">Nenhum alerta alto carregado.</p>'


def render_metric(label: str, value: Any) -> str:
    return (
        '<div class="metric">'
        f"<span>{escape_html(label)}</span>"
        f"<strong>{format_number(value)}</strong>"
        "</div>"
    )


def render_badge(value: Any) -> str:
    level = normalize_text(clean_value(value) or "baixo")
    return f'<span class="badge {level}">{escape_html(value or "baixo")}</span>'


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
        "temporalCoverage": str(db_metadata.get("periodo", {}).get("ano", DEFAULT_YEAR)),
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
                "query": ["municipio", "estado", "somente_altos", "nivel_minimo", "limite"],
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
                "url": loaded.get("url") or source.direct_csv_url or catalog_source_url(source),
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
    if source.dbc_prefix:
        return build_dbc_source_url(source, source.latest_year or DEFAULT_YEAR)
    if source.folder and source.file_prefix:
        return build_source_url(source, min(DEFAULT_YEAR, source.latest_year or DEFAULT_YEAR))
    return ""


def llms_text(request: Request) -> str:
    base = public_base_url(request)
    return f"""# {SITE_NAME}

{SITE_DESCRIPTION}

Use /v1/high-alerts for high and critical epidemiological alerts by municipality, disease and virus.
Use /v1/risk-index for enriched municipal risk context and explanation fields.
Use /v1/diseases to discover supported diseases and source freshness.
Use /v1/metadata for data freshness, source URLs and the risk formula.

Base URL: {base}
OpenAPI: {base}/openapi.json
Agent manifest: {base}/agent.json
Human dashboard: {base}/dashboard
Methodology: {base}/sobre

Important interpretation rules:
- Public processed granularity is municipal.
- When filtro_localidade appears, the original query was resolved to an official municipality.
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
    municipio: str | None = Query(
        default=None,
        description="Filtra por nome parcial ou código de município DataSUS/IBGE sem dígito.",
    ),
    estado: str | None = Query(default=None, description="Filtra por UF, ex.: SP."),
    somente_altos: bool = Query(
        default=False, description="Retorna apenas municípios com doença em nível alto/crítico."
    ),
    nivel_minimo: str | None = Query(
        default=None, description="baixo, moderado, alto ou critico."
    ),
    limite: int = Query(default=100, ge=1, le=1000),
):
    rows = filter_risk_index(
        db_clini,
        municipio=municipio,
        estado=estado,
        somente_altos=somente_altos,
        nivel_minimo=nivel_minimo,
    )
    return rows[:limite] if rows else {"message": "Dados não disponíveis para o filtro."}


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
) -> dict[str, Any]:
    alerts = filter_alerts(
        db_alertas, municipio=municipio, estado=estado, doenca=doenca
    )
    return {
        "metadata": db_metadata,
        "total": len(alerts),
        "alerts": alerts[:limite],
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
    )
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
