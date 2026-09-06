"""
Disease source catalog for the Aldeia Viva Saúde BIRO.

Contains the immutable definition of all supported SINAN agravos (diseases),
their metadata, data source configuration, and human-readable classification labels.

This module is pure data + small pure functions.
"""

from dataclasses import dataclass
from typing import Any

from .risk import RiskProfile  # only for type hints in methods if needed


@dataclass(frozen=True)
class DiseaseSource:
    """Configuration for one notifiable disease/agravo from SINAN."""

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


# =============================================================================
# Official catalog of supported diseases (source of truth)
# =============================================================================

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
        # "12" e a classificacao atual de dengue grave; "2", "3" e "4" sao da
        # classificacao antiga e nao aparecem em DENGBR26.csv (verificado em
        # 60 mil linhas). Ficam porque o parametro `ano` permite carregar
        # arquivos anteriores, onde ocorrem — ver
        # test_legacy_dengue_severe_classifications_count_as_grave.
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
        severe_fields=frozenset(
            {"CLI_RENAL", "CLI_RESPIR", "CLI_CARDIA", "CLI_MENING"}
        ),
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


# =============================================================================
# Human-readable classification labels (from SINAN CLASSI_FIN etc.)
# =============================================================================

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


def classification_label(disease_code: str, code: str) -> str:
    """Return human readable label for a SINAN final classification code."""
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
