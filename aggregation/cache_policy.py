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
