"""
Quais sinais a fonte de cada agravo realmente traz.

`formula_risco` publica cinco termos para toda arbovirose:

    casos_provaveis + 4*sinais_alarme + 8*casos_graves + 20*obitos + 2*hospitalizacoes

Mas a fonte de cada agravo não traz os mesmos campos. Verificado nos arquivos
reais: `ZIKABR26.csv` tem 38 colunas e nenhuma `ALRM_` ou `GRAV_`, enquanto
`DENGBR26.csv` tem 121 colunas com 9 e 15 delas. E `disease_sources.py`
declara `warning_codes` e `severe_codes` vazios para Zika e Chikungunya.

Medido no relatório inteiro, contando em quantos municípios cada sinal chega a
ser maior que zero:

    Zika          0 de 579    em sinais_alarme, casos_graves, hospitalizacoes
    Chikungunya   0 de 1.920  em sinais_alarme, casos_graves
    Hanseníase    0 de 3.088  em sinais_alarme, casos_graves, hospitalizacoes

O resultado é que o score da Zika é, na prática, apenas a contagem de casos —
e sai com a mesma fórmula impressa ao lado do score da Dengue, que usa os
cinco termos. Um zero que significa "a fonte não traz" fica indistinguível de
um zero que significa "não houve".

Este módulo não conserta a fonte. Ele declara a lacuna, para que ninguém
compare dois scores construídos com pesos diferentes achando que são a mesma
medida.
"""

from typing import Any, Iterable, Mapping

# Os termos do score, além de `casos_provaveis`, que é sempre medido.
TRACKED_SIGNALS = ("sinais_alarme", "casos_graves", "hospitalizacoes", "obitos")

SIGNAL_LABELS = {
    "sinais_alarme": "sinais de alarme",
    "casos_graves": "casos graves",
    "hospitalizacoes": "hospitalizações",
    "obitos": "óbitos",
}

DISEASE_COLLECTIONS = ("doencas", "doencas_altas")


def signals_without_data(report: Mapping[str, Any]) -> dict[str, list[str]]:
    """Por código de agravo, os sinais que nunca são positivos na carga.

    Um sinal genuinamente raro poderia ser zero por acaso; nunca positivo em
    milhares de municípios é evidência de que a fonte não o traz. O nome do
    campo publicado — `sinais_sem_dados` — é deliberadamente sobre a carga, e
    não uma afirmação sobre a doença.
    """
    seen: dict[str, set[str]] = {}
    positive: dict[str, set[str]] = {}

    for municipality in report.get("municipios") or []:
        for disease in municipality.get("doencas") or []:
            code = str(disease.get("codigo") or "")
            if not code:
                continue
            seen.setdefault(code, set())
            bucket = positive.setdefault(code, set())
            for signal in TRACKED_SIGNALS:
                try:
                    if float(disease.get(signal) or 0) > 0:
                        bucket.add(signal)
                except (TypeError, ValueError):
                    continue

    return {
        code: sorted(set(TRACKED_SIGNALS) - positive.get(code, set()))
        for code in seen
    }


def describe_coverage(missing: Iterable[str]) -> str:
    """Frase legível com os sinais ausentes, ou string vazia."""
    labels = [SIGNAL_LABELS.get(signal, signal) for signal in missing]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " e " + labels[-1]


def with_signal_coverage(report: Mapping[str, Any]) -> dict[str, Any]:
    """Relatório com a lacuna de sinais declarada em cada agravo."""
    gaps = signals_without_data(report)

    municipalities = []
    for municipality in report.get("municipios") or []:
        enriched = dict(municipality)
        for collection in DISEASE_COLLECTIONS:
            enriched[collection] = [
                {
                    **disease,
                    "sinais_sem_dados": gaps.get(str(disease.get("codigo") or ""), []),
                }
                for disease in (municipality.get(collection) or [])
            ]
        municipalities.append(enriched)

    com_lacuna = sorted(code for code, missing in gaps.items() if missing)

    metadata = dict(report.get("metadata") or {})
    metadata["cobertura_de_sinais"] = {
        "por_agravo": {code: missing for code, missing in sorted(gaps.items())},
        "agravos_com_lacuna": com_lacuna,
        "sinais_avaliados": list(TRACKED_SIGNALS),
        "aviso": (
            "Estes sinais nunca aparecem positivos nesta carga, o que indica que "
            "a fonte daquele agravo não os traz. O termo correspondente da "
            "`formula_risco` fica sempre zero, então scores de agravos com "
            "lacunas diferentes NÃO são comparáveis entre si."
        ),
    }

    enriched_report = dict(report)
    enriched_report["municipios"] = municipalities
    enriched_report["metadata"] = metadata
    return enriched_report
