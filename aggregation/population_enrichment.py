"""
Denominador populacional e taxa de incidência.

Autoridade única sobre população no projeto. `report_builder` tentava
computar `taxa_incidencia_100k` a partir de `lookup_item["populacao"]`, mas
o lookup nunca teve esse campo: medido no relatório real, 0 de 5.339
municípios tinham população e a chave nem aparecia no JSON servido.

Aplicar aqui — no mesmo ponto em que a recência é aplicada — faz o
denominador valer para as três origens do dado (carga nova, cache em disco e
snapshot embarcado), em vez de só depois de uma recarga completa.

A taxa nunca é publicada como número nu: vem com o denominador ao lado e uma
ressalva quando a população é pequena demais para a taxa ser estável.
"""

from typing import Any, Iterable, Mapping

from domain.rates import incidence_per_100k

# Abaixo disto a taxa por 100 mil oscila violentamente com um único caso:
# em um município de 800 habitantes, 1 caso já são 125 por 100 mil.
# O número continua sendo publicado, mas marcado — suprimir em silêncio é
# tão desonesto quanto publicar sem ressalva.
MIN_RELIABLE_POPULATION = 10_000


def incidence_block(cases: int, population: int | None) -> dict[str, Any]:
    """Taxa por 100 mil com o denominador e a ressalva sempre juntos."""
    rate = incidence_per_100k(cases or 0, population)
    if rate is None:
        return {
            "por_100k": None,
            "populacao": population or None,
            "confiavel": False,
            "ressalva": "sem população conhecida para este município",
        }
    if population is not None and population < MIN_RELIABLE_POPULATION:
        return {
            "por_100k": rate,
            "populacao": population,
            "confiavel": False,
            "ressalva": (
                f"população de {population:,} habitantes: a taxa oscila muito "
                "com poucos casos"
            ).replace(",", "."),
        }
    return {
        "por_100k": rate,
        "populacao": population,
        "confiavel": True,
        "ressalva": None,
    }


def enrich_municipality_with_population(
    municipality: Mapping[str, Any], populations: Mapping[str, int]
) -> dict[str, Any]:
    """Cópia do município com população e incidência. Não muta a entrada."""
    enriched = dict(municipality)
    code = str(municipality.get("codigo_municipio") or "")
    population = populations.get(code)
    enriched["populacao"] = population
    enriched["incidencia"] = incidence_block(
        municipality.get("total_casos_provaveis") or 0, population
    )
    return enriched


def population_coverage(
    municipalities: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Quanto do relatório tem denominador. Publicado, não presumido."""
    rows = list(municipalities)
    with_population = sum(1 for row in rows if row.get("populacao"))
    return {
        "municipios_total": len(rows),
        "municipios_com_populacao": with_population,
        "fonte": "IBGE — estimativas populacionais (SIDRA 6579, variável 9324)",
        "populacao_minima_confiavel": MIN_RELIABLE_POPULATION,
        "aviso": (
            "Municípios abaixo da população mínima têm a taxa marcada como não "
            "confiável. `risk_score` continua sendo contagem absoluta e não "
            "deve ser comparado entre municípios de portes diferentes."
        ),
    }


def enrich_with_population(
    report: Mapping[str, Any], populations: Mapping[str, int]
) -> dict[str, Any]:
    """Relatório com denominador populacional em cada município."""
    municipalities = [
        enrich_municipality_with_population(item, populations)
        for item in (report.get("municipios") or [])
    ]
    metadata = dict(report.get("metadata") or {})
    metadata["populacao"] = population_coverage(municipalities)

    enriched = dict(report)
    enriched["municipios"] = municipalities
    enriched["metadata"] = metadata
    return enriched
