"""
Filters and locality resolution for the Aldeia Viva Saúde BIRO.

Contains all logic for filtering risk index and alerts results,
plus multi-city neighborhood / district alias resolution (critical UX
for field agents who search by bairro names in many capitals).

Resolution is always to the full municipality (granularidade = municipio).
New cities can be added declaratively in CITY_NEIGHBORHOODS.

This module supports the future cockpit features (advanced filtering,
geographic drill-down, saved views, etc.).
"""

from typing import Any, Iterable, Mapping

from domain.disease_sources import DISEASE_SOURCES
from domain.risk import RISK_FORMULA  # used in some metadata paths

from .utils import clean_value, normalize_text

# =============================================================================
# Multi-city neighborhood / district aliases (core UX for agents)
# =============================================================================
#
# Declarative registry. Adding support for a new city is a one-line addition
# of a well-curated tuple of common search names (bairros/distritos that
# field agents actually type).
#
# All resolution is to the municipality level (we never claim bairro granularity).
# The flat LOCALITY_ALIASES is built once at import for O(1) lookup speed.

SAO_PAULO_DISTRICTS = (
    # Zona Norte
    "Anhanguera", "Brasilândia", "Cachoeirinha", "Casa Verde", "Freguesia do Ó",
    "Jaçanã", "Jaraguá", "Limão", "Mandaqui", "Perus", "Pirituba",
    "Santana", "São Domingos", "Tremembé", "Tucuruvi", "Vila Guilherme",
    "Vila Maria", "Vila Medeiros",

    # Zona Leste
    "Aricanduva", "Artur Alvim", "Cangaíba", "Cidade Líder", "Cidade Tiradentes",
    "Ermelino Matarazzo", "Guaianases", "Itaim Paulista", "Itaquera",
    "Jardim Helena", "José Bonifácio", "Lajeado", "Penha", "Ponte Rasa",
    "Sapopemba", "São Mateus", "São Miguel", "São Rafael", "Vila Curuçá",
    "Vila Formosa", "Vila Jacuí", "Vila Matilde",

    # Zona Sul
    "Campo Belo", "Campo Grande", "Campo Limpo", "Capão Redondo",
    "Cidade Ademar", "Cidade Dutra", "Grajaú", "Ipiranga", "Jabaquara",
    "Jardim Ângela", "Jardim São Luís", "Jardim São Luiz", "Marsilac",
    "Moema", "Morumbi", "Parelheiros", "Pedreira", "Sacomã",
    "Santo Amaro", "Socorro", "Vila Andrade", "Vila Mariana",
    "Vila Prudente", "Vila Sônia",

    # Zona Oeste
    "Alto de Pinheiros", "Barra Funda", "Butantã", "Jaguaré",
    "Lapa", "Perdizes", "Pinheiros", "Raposo Tavares", "Rio Pequeno",
    "Vila Leopoldina", "Vila Madalena", "Vila Sônia",

    # Centro
    "Bela Vista", "Belém", "Bom Retiro", "Brás", "Cambuci", "Consolação",
    "Liberdade", "Pari", "República", "Santa Cecília", "Sé", "Saúde",
    "Tatuapé", "Vila Buarque",
)

# High-value, commonly-searched neighborhoods for other capitals.
# These are the names agents in the field are most likely to type.
RIO_DE_JANEIRO_BAIRROS = (
    "Copacabana", "Ipanema", "Leblon", "Botafogo", "Flamengo", "Lagoa",
    "Gávea", "Tijuca", "Vila Isabel", "Méier", "Madureira", "Bangu",
    "Campo Grande", "Jacarepaguá", "Barra da Tijuca", "Penha", "Olaria",
)

BELO_HORIZONTE_BAIRROS = (
    "Centro", "Savassi", "Lourdes", "Sion", "Funcionários", "Belvedere",
    "Cidade Nova", "Pampulha", "Venda Nova", "Buritis", "Castelo",
    "Gutierrez", "Mangabeiras", "Ouro Preto", "Santo Antônio", "Nova Lima",
)

RECIFE_BAIRROS = (
    "Boa Viagem", "Casa Forte", "Espinheiro", "Graças", "Ilha do Retiro",
    "Madalena", "Parnamirim", "Rosarinho", "Torre", "Várzea", "Candeias",
    "Jaboatão", "Pina", "Setúbal",
)


CITY_NEIGHBORHOODS: dict[str, dict[str, Any]] = {
    "SP:São Paulo": {
        "uf": "SP",
        "municipio": "São Paulo",
        "codigo_municipio": "355030",
        "tipo": "distrito",
        "bairros": SAO_PAULO_DISTRICTS,
    },
    "RJ:Rio de Janeiro": {
        "uf": "RJ",
        "municipio": "Rio de Janeiro",
        "codigo_municipio": "330455",
        "tipo": "bairro",
        "bairros": RIO_DE_JANEIRO_BAIRROS,
    },
    "MG:Belo Horizonte": {
        "uf": "MG",
        "municipio": "Belo Horizonte",
        "codigo_municipio": "310620",
        "tipo": "bairro",
        "bairros": BELO_HORIZONTE_BAIRROS,
    },
    "PE:Recife": {
        "uf": "PE",
        "municipio": "Recife",
        "codigo_municipio": "261160",
        "tipo": "bairro",
        "bairros": RECIFE_BAIRROS,
    },
}


def normalize_alias_key(value: str) -> str:
    """Normalize a search string for alias lookup (accent-insensitive, lower)."""
    return normalize_text(value)


def build_locality_aliases() -> dict[str, tuple[dict[str, str], ...]]:
    """Tabela de nome normalizado para TODOS os municípios candidatos.

    A versão anterior guardava um único candidato por nome
    (`aliases[key] = {...}`), então um bairro homônimo entre cidades era
    sobrescrito pela última cidade iterada. Medido: `penha` e `campo grande`
    existem em São Paulo e no Rio, e o Rio vencia — de modo que
    `municipio=penha&estado=SP` não devolvia nada, embora `/v1/bairros`
    anunciasse `penha` na lista de São Paulo.

    Os candidatos vêm ordenados por UF para que a mesma consulta devolva
    sempre o mesmo município: ordem de iteração de dicionário não pode
    decidir isso.
    """
    aliases: dict[str, list[dict[str, str]]] = {}
    for _city_key, city in CITY_NEIGHBORHOODS.items():
        for bairro in city["bairros"]:
            key = normalize_alias_key(bairro)
            aliases.setdefault(key, []).append(
                {
                    "tipo": city["tipo"],
                    "localidade": bairro,
                    "municipio_resolvido": city["municipio"],
                    "codigo_municipio": city["codigo_municipio"],
                    "estado": city["uf"],
                    "granularidade_disponivel": "municipio",
                }
            )
    deduplicated: dict[str, tuple[dict[str, str], ...]] = {}
    for key, candidates in aliases.items():
        by_city: dict[str, dict[str, str]] = {}
        for item in candidates:
            # Uma grafia repetida dentro da mesma cidade é erro de cadastro,
            # não ambiguidade: "Vila Sônia" aparece duas vezes na lista de
            # São Paulo e inflava a contagem publicada em /v1/bairros.
            by_city.setdefault(item["codigo_municipio"], item)
        deduplicated[key] = tuple(
            sorted(by_city.values(), key=lambda item: item["estado"])
        )
    return deduplicated


def ambiguous_locality_names() -> list[str]:
    """Nomes de bairro que existem em mais de uma cidade suportada."""
    return sorted(
        key for key, candidates in LOCALITY_ALIASES.items() if len(candidates) > 1
    )


# Flat lookup table — rebuilt automatically when CITY_NEIGHBORHOODS changes.
# Backward compatible name and shape.
LOCALITY_ALIASES = build_locality_aliases()


def _unique_neighborhoods(bairros) -> list[str]:
    """Lista ordenada sem grafias repetidas.

    O registro de São Paulo trazia "Vila Sônia" duas vezes, o que fazia
    /v1/bairros publicar 137 bairros quando existem 136 entradas reais.
    """
    seen: dict[str, str] = {}
    for bairro in bairros:
        seen.setdefault(normalize_alias_key(bairro), bairro)
    return sorted(seen.values())


def get_supported_bairros(
    uf: str | None = None,
    municipio: str | None = None,
) -> dict[str, Any]:
    """
    Return supported neighborhood lists.

    - No arguments (default): returns grouped structure with all supported cities.
    - With uf or municipio: returns the specific city entry (legacy shape for that city).

    Always additive and backward-compatible for single-city consumers.
    """
    if uf or municipio:
        # Filtered single-city response (supports old call sites + new filters)
        target_uf = clean_value(uf).upper() if uf else None
        target_mun = normalize_alias_key(municipio) if municipio else None

        for city_key, city in CITY_NEIGHBORHOODS.items():
            if target_uf and city["uf"] != target_uf:
                continue
            if target_mun and normalize_alias_key(city["municipio"]) != target_mun:
                continue
            bairros_sorted = _unique_neighborhoods(city["bairros"])
            return {
                "tipo": city["tipo"],
                "uf": city["uf"],
                "estado": city["uf"],  # legacy key kept for compatibility
                "municipio": city["municipio"],
                "codigo_municipio": city["codigo_municipio"],
                "bairros": bairros_sorted,
                "total": len(bairros_sorted),
                "descricao": (
                    f"Lista de {city['tipo']}s de {city['municipio']} que o sistema "
                    "consegue resolver automaticamente para o município correspondente."
                ),
                "como_usar": (
                    "Use o nome do bairro/distrito no parâmetro 'municipio' da API "
                    "(ex: /v1/risk-index?municipio=ipanema). O sistema resolve para o município "
                    "e inclui 'filtro_localidade' na resposta."
                ),
            }

    # Default: rich grouped view (recommended for agents and /agentes page)
    # `total_bairros` conta entradas por cidade e por isso soma nomes
    # homônimos duas vezes; `total_nomes_distintos` conta nomes únicos.
    # Publicar os dois evita que um consumidor conclua que existem 137 nomes
    # buscáveis quando são 134, três deles precisando de UF.
    cidades: list[dict[str, Any]] = []
    total_bairros = 0
    for city_key, city in CITY_NEIGHBORHOODS.items():
        bairros_sorted = _unique_neighborhoods(city["bairros"])
        total_bairros += len(bairros_sorted)
        cidades.append({
            "uf": city["uf"],
            "municipio": city["municipio"],
            "codigo_municipio": city["codigo_municipio"],
            "tipo": city["tipo"],
            "bairros": bairros_sorted,
            "total": len(bairros_sorted),
        })

    ambiguos = ambiguous_locality_names()

    return {
        "total_nomes_distintos": len(LOCALITY_ALIASES),
        "nomes_ambiguos": ambiguos,
        "aviso_ambiguidade": (
            "Estes nomes existem em mais de uma cidade suportada. Informe o "
            "parâmetro `estado` para escolher; sem ele a resposta traz o bloco "
            "`filtro_localidade.ambiguidade` com as alternativas."
        )
        if ambiguos
        else None,
        "versao": 2,
        "total_cidades": len(cidades),
        "total_bairros": total_bairros,
        "cidades": cidades,
        "descricao": (
            "Bairros e distritos suportados para resolução automática de busca. "
            "Ao informar um destes nomes no parâmetro 'municipio', o sistema resolve "
            "para o município oficial (granularidade sempre municipal)."
        ),
        "como_usar": (
            "GET /v1/risk-index?municipio=perus  → resolve para São Paulo\n"
            "GET /v1/risk-index?municipio=copacabana → resolve para Rio de Janeiro\n"
            "GET /v1/risk-index?municipio=savassi → resolve para Belo Horizonte"
        ),
        "cidades_suportadas": [c["municipio"] for c in cidades],
    }


# =============================================================================
# Core filtering functions
# =============================================================================

def resolve_locality_alias(
    municipio: str | None, state_filter: str
) -> dict[str, str] | None:
    query = clean_value(municipio)
    if not query:
        return None

    normalized = normalize_text(query)

    # Variações comuns usadas por agentes, na ordem de tentativa.
    variations = [
        normalized,
        normalized.replace("bairro ", ""),
        normalized.replace("bairro de ", ""),
        normalized.replace("distrito ", ""),
        normalized.replace("distrito de ", ""),
        normalized.replace("sp ", ""),
        normalized.replace("sao paulo ", ""),
    ]

    for variant in variations:
        candidates = LOCALITY_ALIASES.get(variant)
        if not candidates:
            continue

        if state_filter:
            # A UF desambigua. Sem isto, um bairro homônimo resolvia para a
            # cidade errada e a consulta com a UF certa vinha vazia.
            matching = [item for item in candidates if item["estado"] == state_filter]
            if not matching:
                continue
            return {"consulta": query, **matching[0]}

        chosen = dict(candidates[0])
        if len(candidates) > 1:
            # Sem UF a escolha é arbitrária; declarar é melhor que esconder.
            chosen["ambiguidade"] = [
                {
                    "municipio": item["municipio_resolvido"],
                    "estado": item["estado"],
                    "codigo_municipio": item["codigo_municipio"],
                }
                for item in candidates
            ]
            outras = ", ".join(
                f"{item['municipio_resolvido']}/{item['estado']}"
                for item in candidates[1:]
            )
            chosen["aviso"] = (
                f"O nome {query!r} também existe em {outras}. "
                "Informe o parâmetro `estado` para escolher."
            )
        return {"consulta": query, **chosen}

    return None


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
        locality_alias["municipio_resolvido"]
        if locality_alias
        else clean_value(municipio)
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
        if min_level and not level_at_least(
            clean_value(row.get("nivel_risco")), min_level
        ):
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
        locality_alias["municipio_resolvido"]
        if locality_alias
        else clean_value(municipio)
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
