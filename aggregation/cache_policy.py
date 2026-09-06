"""
Política de validade da cache agregada.

`load_or_refresh_report` decidia com `if cache_path.exists()`: a cache era
eterna. Como a chave é `(versão, ano, agravos)`, o mesmo arquivo era servido
enquanto o ano não virasse. Medido em produção: relatório carregado em
2026-04-26 ainda sendo servido 132 dias depois, com a barra de estado
avisando o usuário e nada no sistema jamais buscando dado novo.

Duas regras, e a segunda não pode ser sacrificada pela primeira:

  1. Cache vencida não é servida sem antes tentar buscar dado novo.
  2. Se a busca falhar, a cache vencida volta a ser usada. Dado velho e
     rotulado é melhor que painel vazio — e o rótulo já existe em
     `metadata.carga`.
"""

from datetime import date
from typing import Any, Mapping

from domain.recency import parse_signal_date, signal_age_days

# Sete dias: o mesmo limiar que `LOAD_FRESH_MAX_DAYS` usa para dizer ao
# usuário que a carga deixou de descrever o presente. Se o painel considera
# a carga velha, o sistema deve estar tentando renová-la.
DEFAULT_MAX_AGE_DAYS = 7


def cache_age_days(report: Mapping[str, Any], today: date) -> int | None:
    """Idade da carga registrada no relatório, em dias. None se ilegível."""
    metadata = report.get("metadata") or {}
    loaded_at = parse_signal_date(metadata.get("carregado_em"))
    if loaded_at is None:
        return None
    return signal_age_days(loaded_at, today)


def cache_is_fresh(
    report: Mapping[str, Any],
    today: date,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> bool:
    """A cache ainda descreve o presente?

    `max_age_days=0` desliga a expiração — escape hatch para ambientes sem
    rede, onde tentar buscar só produziria latência e log de erro.

    Idade desconhecida conta como vencida: sem carimbo não há como afirmar
    frescor, e tentar buscar é a decisão segura.
    """
    if max_age_days <= 0:
        return True
    age = cache_age_days(report, today)
    if age is None:
        return False
    return age <= max_age_days


def covered_sources(report: Mapping[str, Any] | None) -> set[str]:
    """Códigos de agravo que a carga trouxe com pelo menos um registro."""
    metadata = (report or {}).get("metadata") or {}
    return {
        str(source.get("codigo"))
        for source in metadata.get("fontes") or []
        if source.get("codigo") and (source.get("registros") or 0) > 0
    }


def is_regression(
    candidate: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> bool:
    """A carga nova perde cobertura em relação à anterior?

    Observado ao vivo: uma recarga em que as fontes CSV carregaram e as DBC
    falharam substituiu uma base de 5.339 municípios e 10 agravos por outra
    de 4.462 e 4 agravos. `report_has_content` aprovava, porque só olhava
    `status` e lista não vazia.

    Renovar não pode perder cobertura. Sem carga anterior, ou sem metadados
    de fonte nela, nada é bloqueado — não se pode travar a primeira carga.
    """
    known = covered_sources(previous)
    if not known:
        return False
    return not known.issubset(covered_sources(candidate))


def missing_sources(
    candidate: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> list[str]:
    """Agravos que a carga anterior tinha e a nova não trouxe."""
    return sorted(covered_sources(previous) - covered_sources(candidate))
