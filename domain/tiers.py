"""
Tiers de acesso — autoridade única.

A página `/planos` trazia os limites escritos à mão no HTML e divergia do
código: anunciava "100 req/min" para o Profissional, que tem `rate_limit`
10.000; chamava de "Gratuito" tanto o acesso sem chave (5 registros, 10
req/min) quanto a chave gratuita (20 registros, 100 req/min), que são ofertas
diferentes; e prometia "sem limites: todos os municípios em uma única
chamada", impossível em qualquer tier, já que a chamada é limitada a mil
registros e existem 5.339 municípios.

Números escritos à mão no HTML não têm como divergir do código se não
existirem. Esta declaração passa a alimentar a página, o manifesto para
agentes e o enforcement.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    """Um nível de acesso, com tudo que a página e o manifesto precisam."""

    codigo: str
    nome: str
    preco: str
    # None significa "sem teto próprio": a chamada ainda é limitada por
    # MAX_RESULTS_PER_CALL, e a base completa se obtém paginando.
    max_results: int | None
    rate_limit: int
    resumo: str


# Teto absoluto por chamada, imposto pelo próprio parâmetro `limite`.
# Nenhum tier ultrapassa isto; a base completa se obtém com `pagina`.
MAX_RESULTS_PER_CALL = 1000


TIERS: tuple[Tier, ...] = (
    Tier(
        codigo="anonymous",
        nome="Sem chave",
        preco="R$ 0/mês",
        max_results=5,
        rate_limit=10,
        resumo=(
            "Acesso público ao painel e à API, para consulta pontual e "
            "avaliação do contrato."
        ),
    ),
    Tier(
        codigo="free",
        nome="Chave gratuita",
        preco="R$ 0/mês",
        max_results=20,
        rate_limit=100,
        resumo=(
            "Para pesquisadores, ONGs e vigilância municipal. Solicite por "
            "e-mail descrevendo o uso."
        ),
    ),
    Tier(
        codigo="premium",
        nome="Profissional",
        preco="R$ 149/mês",
        max_results=None,
        rate_limit=10_000,
        resumo=(
            "Integração de sistemas: base completa por paginação e acesso ao "
            "relatório profissional."
        ),
    ),
)

# `admin` existe nos gates de autorização mas não é uma oferta comercial.
UNLISTED_TIERS = ("admin",)


def tier_by_code(codigo: str) -> Tier | None:
    """Tier pelo código, ou None quando não é uma oferta declarada."""
    for tier in TIERS:
        if tier.codigo == codigo:
            return tier
    return None


def tier_max_results(codigo: str | None) -> int | None:
    """Teto de resultados por chamada do tier. None quando não há teto próprio.

    `admin` e qualquer código desconhecido caem em None: o enforcement então
    aplica apenas o limite absoluto por chamada.
    """
    tier = tier_by_code(str(codigo or ""))
    return tier.max_results if tier else None


def format_rate_limit(value: int) -> str:
    """Formata o limite para leitura humana, no padrão pt-BR."""
    return f"{value:,}".replace(",", ".")
