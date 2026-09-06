"""
Risk calculation domain for the Aldeia Viva Saúde BIRO.

This module contains the core epidemiological risk scoring logic.
It is intentionally pure and has no external dependencies besides the
standard library and the dataclasses used for configuration.

All functions are designed to be immutable and side-effect free.
"""

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .disease_sources import DiseaseSource  # type: ignore[attr-defined]


@dataclass(frozen=True)
class RiskProfile:
    """Immutable definition of how risk is calculated for a family of diseases."""

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


RISK_PROFILES: dict[str, RiskProfile] = {
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


# Do mais grave ao menos grave. Usado para consolidar o nível de um
# conjunto de agravos sem reaplicar fórmula alguma sobre a soma.
RISK_LEVEL_ORDER = ("critico", "alto", "moderado", "baixo")


def worst_level(levels) -> str:
    """Pior nível entre os informados.

    Um município não tem perfil de risco — cada agravo tem. Consolidar pelo
    pior nível preserva o perfil correto de cada agravo, em vez de aplicar o
    perfil de arbovirose a uma soma que mistura agravos e anos-fonte.
    """
    seen = {level for level in levels if level in RISK_LEVEL_ORDER}
    for level in RISK_LEVEL_ORDER:
        if level in seen:
            return level
    return "baixo"


def risk_profile_for_source(source: "DiseaseSource") -> RiskProfile:
    """Return the appropriate RiskProfile for a given disease source."""
    return RISK_PROFILES.get(source.risk_profile, RISK_PROFILES["arbovirus"])


def risk_level(
    score: float,
    probable_cases: int,
    severe_cases: int,
    deaths: int,
    profile: RiskProfile | None = None,
) -> str:
    """Determine the operational risk level based on score and signals."""
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


def finalize_disease_summary(disease: dict[str, Any]) -> dict[str, Any]:
    """
    Calculate risk_score and nivel_risco for a single disease summary dict.

    This function mutates the input dict (historical behavior preserved for now).
    Future versions should return a new dict (immutability goal).
    """
    profile = RISK_PROFILES.get(
        disease.get("perfil_risco", "arbovirus"), RISK_PROFILES["arbovirus"]
    )
    disease["risk_score"] = round(
        (profile.case_weight * disease.get("casos_provaveis", 0))
        + (profile.warning_weight * disease.get("sinais_alarme", 0))
        + (profile.severe_weight * disease.get("casos_graves", 0))
        + (profile.death_weight * disease.get("obitos", 0))
        + (profile.hospitalization_weight * disease.get("hospitalizacoes", 0)),
        2,
    )
    disease["nivel_risco"] = risk_level(
        disease["risk_score"],
        disease.get("casos_provaveis", 0),
        disease.get("casos_graves", 0),
        disease.get("obitos", 0),
        profile,
    )
    if "classificacoes" in disease:
        disease["classificacoes"] = dict(sorted(disease["classificacoes"].items()))
    return disease
