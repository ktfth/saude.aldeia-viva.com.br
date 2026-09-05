"""
Recência do sinal epidemiológico.

Um alerta de vigilância só é acionável se o sinal que o originou ainda estiver
vivo. O SINAN entrega a data da última notificação, mas o produto tratava
igualmente um surto de ontem e um surto de 2022: ambos apareciam como
"CRÍTICO" no ano-base corrente.

Este módulo transforma a data bruta em uma dimensão de primeira classe:
idade em dias, faixa de frescor e um veredito explícito sobre se o número
pode ser lido como situação atual.

Funções puras, sem efeito colateral. Nunca mutam a entrada.
"""

from datetime import date, datetime
from typing import Any, Iterable, Mapping

# Faixas de frescor operacional.
LIVE = "vivo"
COOLING = "esfriando"
DORMANT = "dormente"
FOSSIL = "fossil"
UNKNOWN = "desconhecido"

# Limites em dias. Escolhidos para a janela de decisão da vigilância municipal:
#   até 30d  -> transmissão provavelmente ativa, cabe ação imediata
#   31-90d   -> sinal recente, fora da janela de resposta imediata
#   91-365d  -> ainda no ano epidemiológico, sem movimento recente
#   >365d    -> pertence a outro ano; não descreve a situação atual
LIVE_MAX_DAYS = 30
COOLING_MAX_DAYS = 90
DORMANT_MAX_DAYS = 365

DEFAULT_DATE_FIELDS = ("ultima_notificacao", "ultimo_inicio_sintomas")

FRESHNESS_ORDER = (LIVE, COOLING, DORMANT, FOSSIL, UNKNOWN)

_FRESHNESS_DESCRIPTION = {
    LIVE: "Notificação nos últimos 30 dias. Transmissão provavelmente ativa.",
    COOLING: "Última notificação entre 31 e 90 dias. Sinal recente, sem movimento na janela de resposta imediata.",
    DORMANT: "Última notificação entre 91 e 365 dias. Dentro do ano, sem sinal recente.",
    FOSSIL: "Última notificação há mais de um ano. Não descreve a situação atual.",
    UNKNOWN: "Sem data de notificação utilizável. Idade do sinal desconhecida.",
}


def parse_signal_date(value: Any) -> date | None:
    """Converte um valor cru do SINAN em `date`, ou None quando inutilizável."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    head = text.split("T", 1)[0].split(" ", 1)[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def signal_age_days(value: Any, reference: date) -> int | None:
    """Idade do sinal em dias corridos. None quando a data é inutilizável.

    Datas no futuro (erro de digitação, comum no SINAN) são tratadas como 0
    em vez de produzir idade negativa.
    """
    parsed = parse_signal_date(value)
    if parsed is None:
        return None
    return max(0, (reference - parsed).days)


def signal_freshness(age_days: int | None) -> str:
    """Classifica a idade em uma faixa operacional de frescor."""
    if age_days is None:
        return UNKNOWN
    if age_days <= LIVE_MAX_DAYS:
        return LIVE
    if age_days <= COOLING_MAX_DAYS:
        return COOLING
    if age_days <= DORMANT_MAX_DAYS:
        return DORMANT
    return FOSSIL


def humanize_age(age_days: int | None) -> str:
    """Rótulo curto em português, legível em uma linha de tabela."""
    if age_days is None:
        return "sem data"
    if age_days == 0:
        return "hoje"
    if age_days == 1:
        return "ontem"
    if age_days < 30:
        return f"há {age_days} dias"
    if age_days < 365:
        months = age_days // 30
        return f"há {months} {'mês' if months == 1 else 'meses'}"
    years = age_days // 365
    return f"há {years} {'ano' if years == 1 else 'anos'}"


def describe_signal(value: Any, reference: date) -> dict[str, Any]:
    """Descrição completa e autoexplicativa da recência de um sinal.

    O campo `referencia` torna o resultado reproduzível: um agente que
    reprocessa a resposta amanhã sabe contra qual data a idade foi medida.
    """
    age = signal_age_days(value, reference)
    freshness = signal_freshness(age)
    parsed = parse_signal_date(value)
    return {
        "data": parsed.isoformat() if parsed else None,
        "idade_dias": age,
        "frescor": freshness,
        "rotulo": humanize_age(age),
        "descricao": _FRESHNESS_DESCRIPTION[freshness],
        "confiavel_como_atual": freshness in (LIVE, COOLING),
        "referencia": reference.isoformat(),
    }


def most_recent_value(
    record: Mapping[str, Any], date_fields: Iterable[str]
) -> Any | None:
    """Entre os campos de data candidatos, devolve o valor mais recente."""
    return freshest_date(record.get(field) for field in date_fields)


def freshest_date(values: Iterable[Any]) -> Any | None:
    """Entre vários valores de data, devolve o mais recente utilizável."""
    best_value: Any | None = None
    best_date: date | None = None
    for value in values:
        parsed = parse_signal_date(value)
        if parsed is None:
            continue
        if best_date is None or parsed > best_date:
            best_date, best_value = parsed, value
    return best_value


def with_recency(
    record: Mapping[str, Any],
    reference: date,
    *,
    date_fields: Iterable[str] = DEFAULT_DATE_FIELDS,
    key: str = "recencia",
) -> dict[str, Any]:
    """Cópia do registro acrescida do bloco de recência. Não muta a entrada."""
    enriched = dict(record)
    enriched[key] = describe_signal(most_recent_value(record, date_fields), reference)
    return enriched


def worst_freshness(values: Iterable[str]) -> str:
    """Frescor consolidado: o mais fresco entre os sinais disponíveis.

    Para um município, o que importa operacionalmente é o sinal mais vivo que
    ele tem — é ele que dispara a ação.
    """
    seen = {v for v in values if v in FRESHNESS_ORDER}
    for level in FRESHNESS_ORDER:
        if level in seen:
            return level
    return UNKNOWN
