"""
Renderização server-side da dimensão temporal do dado.

Substitui o gráfico de linha de dois pontos que custava ~200 KB de Chart.js
carregados de CDN. A tira de agravos é SVG inline: chega junto com o HTML,
funciona sem JavaScript, não faz request algum e cabe em 360px.

Um canal visual por variável, deliberadamente:
  - cor do segmento  -> idade da FONTE daquele agravo
  - badge de risco   -> gravidade (fica onde já estava)
  - rótulo de sinal  -> recência dentro da fonte

Misturar gravidade e temporalidade no mesmo canal foi exatamente o defeito
que originou este módulo.
"""

from html import escape
from typing import Any, Iterable, Mapping, Sequence

# Cor por faixa de idade da fonte. Fora da escala de risco de propósito:
# "quão atual é o dado" não é "quão grave é a situação".
SOURCE_COLORS = {
    "atual": "#146c43",
    "recente": "#9a5b00",
    "antiga": "#8a6d3b",
    "ausente": "#b8c4bd",
}

SOURCE_LABELS = {
    "atual": "fonte do ano corrente",
    "recente": "fonte do ano anterior",
    "antiga": "fonte de 2 anos ou mais",
    "ausente": "sem fonte",
}

STRIP_HEIGHT = 22
STRIP_GAP = 2
STRIP_WIDTH = 168


def source_bucket(source: Mapping[str, Any] | None, current_year: int) -> str:
    """Classifica a idade da fonte em uma das quatro faixas de exibição."""
    if not source or source.get("ano") is None:
        return "ausente"
    if source.get("do_ano_corrente"):
        return "atual"
    return "recente" if current_year - int(source["ano"]) <= 1 else "antiga"


def render_signal_strip(
    diseases: Sequence[Mapping[str, Any]], current_year: int
) -> str:
    """Tira de agravos: um segmento por agravo, cor pela idade da fonte.

    Responde de relance a pergunta que a tabela não respondia: quanto do que
    estou vendo sobre este município é dado atual?
    """
    if not diseases:
        return '<span class="strip-caption">Sem agravos registrados</span>'

    count = len(diseases)
    total_gap = STRIP_GAP * (count - 1)
    segment = max(3.0, (STRIP_WIDTH - total_gap) / count)

    rects = []
    current = 0
    for index, disease in enumerate(diseases):
        source = disease.get("fonte") or {}
        bucket = source_bucket(source, current_year)
        if bucket == "atual":
            current += 1
        year = source.get("ano")
        title = (
            f"{disease.get('nome') or disease.get('codigo') or 'Agravo'} — "
            f"fonte {year if year is not None else 'ausente'}"
            f" ({SOURCE_LABELS[bucket]})"
        )
        rects.append(
            f'<rect x="{index * (segment + STRIP_GAP):.1f}" y="0" '
            f'width="{segment:.1f}" height="{STRIP_HEIGHT}" rx="2" '
            f'fill="{SOURCE_COLORS[bucket]}">'
            f"<title>{escape(title)}</title></rect>"
        )

    label = f"{current} de {count} agravos com fonte do ano corrente"
    return (
        f'<svg class="signal-strip" viewBox="0 0 {STRIP_WIDTH} {STRIP_HEIGHT}" '
        f'preserveAspectRatio="none" role="img" aria-label="{escape(label)}">'
        f'{"".join(rects)}</svg>'
        f'<span class="strip-caption">{current} de {count} atuais</span>'
    )


def render_strip_legend() -> str:
    """Legenda da tira. Uma linha, quatro itens, sem painel próprio."""
    items = "".join(
        f'<span><i style="background:{SOURCE_COLORS[key]}"></i>{escape(label)}</span>'
        for key, label in (
            ("atual", "fonte deste ano"),
            ("recente", "ano anterior"),
            ("antiga", "2 anos ou mais"),
            ("ausente", "sem fonte"),
        )
    )
    return f'<div class="strip-legend" aria-label="Legenda da tira de agravos">{items}</div>'


def render_signal_tag(recency: Mapping[str, Any] | None) -> str:
    """Rótulo do sinal: até quando este município notificou, dentro da fonte."""
    block = recency or {}
    level = str(block.get("frescor") or "desconhecido")
    label = str(block.get("rotulo") or "sem data")
    return f'<span class="signal-tag {escape(level)}">{escape(label)}</span>'


def render_source_tag(source: Mapping[str, Any] | None) -> str:
    """Ano da fonte, sempre ao lado do número — nunca em rodapé."""
    block = source or {}
    year = block.get("ano")
    if year is None:
        return '<span class="source-tag is-old">sem fonte</span>'
    old = "" if block.get("do_ano_corrente") else " is-old"
    title = f"Arquivo-fonte deste agravo: {year} ({block.get('rotulo', '')})".strip()
    return (
        f'<span class="source-tag{old}" title="{escape(title)}">'
        f"fonte {escape(str(year))}</span>"
    )


def render_data_status(metadata: Mapping[str, Any]) -> str:
    """Barra de estado do dado: a informação mais importante do painel.

    Antes disto, o dashboard exibia uma carga de abril como se fosse a
    situação de hoje, sem dizer uma palavra sobre a própria idade.
    """
    load = metadata.get("carga") or {}
    recency = metadata.get("recencia") or {}

    stale = not load.get("atualizada", False)
    parts: list[str] = []

    loaded_at = load.get("carregado_em")
    age = load.get("idade_dias")
    if loaded_at and age is not None:
        parts.append(
            f"Carga de <b>{escape(str(loaded_at))}</b> · há <b>{age}</b> dia(s)"
        )
    else:
        parts.append("Carga sem data registrada")

    total = recency.get("agravos_total")
    current = recency.get("agravos_com_fonte_do_ano_corrente")
    if total:
        parts.append(
            f"Fontes do ano corrente: <b>{current} de {total}</b> agravos"
        )

    if stale and load.get("aviso"):
        parts.append(f'<span class="status-warn">{escape(str(load["aviso"]))}</span>')

    joined = '<span class="status-sep" aria-hidden="true">|</span>'.join(
        f"<span>{part}</span>" for part in parts
    )
    classes = "data-status is-stale" if stale else "data-status"
    return (
        f'<div class="{classes}" role="status">{joined}</div>'
    )


def sort_diseases_for_strip(
    diseases: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Ordena os agravos da tira: fonte mais atual primeiro, depois por nome.

    Mantém a leitura da esquerda para a direita coerente entre municípios —
    sem isso, a mesma cor aparece em posições diferentes e a tira deixa de ser
    comparável entre linhas.
    """
    return sorted(
        (dict(item) for item in diseases),
        key=lambda item: (
            -((item.get("fonte") or {}).get("ano") or 0),
            str(item.get("nome") or ""),
        ),
    )


def render_risk_cell(row: Mapping[str, Any], badge_renderer) -> str:
    """Célula de risco: o nível acionável, com o histórico dito quando pior.

    O badge mostra `nivel_risco_fonte_atual` — o pior nível entre os agravos
    cujo arquivo-fonte é do ano corrente. É ele que responde "devo agir hoje?".

    Medido no dado real: usar o nível consolidado de todos os anos-fonte
    deixava 1.296 dos 5.339 municípios em "crítico"; restringir à fonte atual
    leva a 468. Os 828 que saem do topo estavam lá por Meningite de 2022 ou
    Leptospirose de 2024 — história, não ação de hoje.

    Nada é escondido: em 54,5% dos municípios o histórico é mais grave, e
    nesses casos a célula diz qual era o nível e por quê.
    """
    level = row.get("nivel_risco_fonte_atual") or row.get("nivel_risco")
    parts = [badge_renderer(level)]

    if row.get("historico_mais_grave"):
        historico = escape(str(row.get("nivel_risco") or ""))
        parts.append(
            f'<span class="cell-sub risk-history" '
            f'title="Nível considerando também agravos de fontes de anos anteriores">'
            f"histórico: {historico}</span>"
        )

    parts.append(f'<span class="cell-sub">{render_signal_tag(row.get("recencia"))}</span>')
    return "".join(parts)
