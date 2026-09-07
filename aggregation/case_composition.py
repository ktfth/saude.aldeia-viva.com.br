"""
Quanto de "caso provável" ainda é notificação não investigada.

`casos_provaveis` é notificações menos descartados — a definição padrão do
SINAN, e correta. O defeito nunca foi a contagem: é que a composição do
número não era declarada.

Medido no relatório real, a fração sem classificação final:

    Hanseníase   100,0%      Dengue        45,4%
    Chikungunya   41,2%      Zika          25,5%
    Toxo. cong.   10,6%      Leptospirose   3,7%
    Meningite      2,8%      Febre Amarela  0,0%

    total: 41,6% de 389.531 casos prováveis

E `casos_descartados` é zero em oito dos dez agravos: não porque nada foi
descartado, mas porque nada foi encerrado.

Isso importa porque `casos_provaveis` é o numerador de `incidencia.por_100k`,
promovida a "única medida comparável entre municípios". Duas incidências
construídas sobre frações pendentes de 3% e de 100% não estão na mesma
escala.

Uma fração pendente alta é normal em dado recente — o encerramento é
assíncrono. Por isso o campo se chama `proporcao_pendente` e não algo como
"qualidade": é uma descrição da composição, não um julgamento da vigilância.
"""

from typing import Any, Mapping

# Rótulos de `classificacoes` que representam caso ainda não resolvido.
UNRESOLVED_LABELS = ("Sem classificação final", "Inconclusivo")

# Acima disto a comparação de incidência entre AGRAVOS deixa de ser direta.
COMPARABLE_MAX_PENDING = 0.20

# Acima disto a taxa de um MUNICÍPIO é dominada por notificação não
# investigada. Pergunta diferente da de cima, e por isso limiar próprio:
# aquele compara agravo com agravo, este descreve do que a taxa é feita.
#
# A linha em metade tem significado explicável: mais da metade do numerador
# ainda não foi encerrado. Medido na distribuição real — mediana 31%, p75
# 57%, p90 82% — o limiar de 20% sinalizaria 61% dos municípios, e ressalva
# que aparece na maioria das linhas não ajuda ninguém a escolher. Em 50%,
# sinaliza 29%.
MAIORIA_PENDENTE = 0.50

DISEASE_COLLECTIONS = ("doencas", "doencas_altas")


def composition_of(disease: Mapping[str, Any]) -> dict[str, Any]:
    """Composição dos casos prováveis de um agravo."""
    cases = int(disease.get("casos_provaveis") or 0)
    classifications = disease.get("classificacoes") or {}
    pending = sum(
        int(classifications.get(label) or 0) for label in UNRESOLVED_LABELS
    )
    proportion = round(pending / cases, 4) if cases else 0.0
    comparable = proportion <= COMPARABLE_MAX_PENDING

    ressalva = None
    if cases and not comparable:
        ressalva = (
            f"{proportion * 100:.0f}% dos casos prováveis ainda não têm "
            "classificação final; a incidência deste agravo não é diretamente "
            "comparável à de agravos com encerramento mais avançado."
        )

    return {
        "casos_provaveis": cases,
        "sem_classificacao_final": pending,
        "proporcao_pendente": proportion,
        "comparavel": comparable,
        "ressalva": ressalva,
    }


def _anotar_incidencia(municipality: dict[str, Any]) -> None:
    """Quanto do numerador da taxa ainda é notificação não investigada.

    A incidência divide `casos_provaveis` pela população, e `casos_provaveis`
    inclui notificação sem classificação final — que é a definição correta do
    SINAN. O que faltava era a taxa dizer isso.

    Medido no relatório real: **Montividiu do Norte/GO mostra 6.080 casos por
    100 mil habitantes, e 100% deles sem classificação final**. Não é um
    número errado; é um número que significa "226 notificações, nenhuma
    investigada até o fim", e quem lê 6.080 por 100 mil não conclui isso.

    Fração pendente alta é normal em dado recente — o encerramento é
    assíncrono. Por isso a anotação descreve a composição em vez de julgar a
    vigilância. Roda aqui porque este módulo é o dono do fato; a incidência já
    existe quando ele executa.
    """
    incidencia = municipality.get("incidencia")
    if not isinstance(incidencia, Mapping):
        return

    casos = pendentes = 0
    for doenca in municipality.get("doencas") or []:
        classificacoes = doenca.get("classificacoes") or {}
        casos += int(doenca.get("casos_provaveis") or 0)
        pendentes += sum(
            int(classificacoes.get(rotulo) or 0) for rotulo in UNRESOLVED_LABELS
        )

    bloco = dict(incidencia)
    fracao = round(pendentes / casos, 4) if casos else None
    bloco["fracao_pendente"] = fracao
    bloco["ressalva_composicao"] = (
        None
        if fracao is None or fracao <= MAIORIA_PENDENTE
        else (
            f"{fracao * 100:.0f}% dos casos que compõem esta taxa ainda não "
            "têm classificação final: ela descreve principalmente notificação "
            "não investigada, e não caso encerrado"
        )
    )
    municipality["incidencia"] = bloco


def with_case_composition(report: Mapping[str, Any]) -> dict[str, Any]:
    """Relatório com a composição dos casos declarada em cada agravo."""
    por_agravo: dict[str, dict[str, int]] = {}

    municipalities = []
    for municipality in report.get("municipios") or []:
        enriched = dict(municipality)
        _anotar_incidencia(enriched)
        for collection in DISEASE_COLLECTIONS:
            items = []
            for disease in municipality.get(collection) or []:
                composicao = composition_of(disease)
                items.append({**disease, "composicao": composicao})
                if collection == "doencas":
                    code = str(disease.get("codigo") or "")
                    if code:
                        acc = por_agravo.setdefault(code, {"casos": 0, "pendentes": 0})
                        acc["casos"] += composicao["casos_provaveis"]
                        acc["pendentes"] += composicao["sem_classificacao_final"]
            enriched[collection] = items
        municipalities.append(enriched)

    resumo: dict[str, dict[str, Any]] = {}
    for code, acc in sorted(por_agravo.items()):
        proportion = round(acc["pendentes"] / acc["casos"], 4) if acc["casos"] else 0.0
        resumo[code] = {
            "casos_provaveis": acc["casos"],
            "sem_classificacao_final": acc["pendentes"],
            "proporcao_pendente": proportion,
            "comparavel": proportion <= COMPARABLE_MAX_PENDING,
        }

    total_casos = sum(a["casos"] for a in por_agravo.values())
    total_pendentes = sum(a["pendentes"] for a in por_agravo.values())

    metadata = dict(report.get("metadata") or {})
    metadata["composicao_dos_casos"] = {
        "por_agravo": resumo,
        "agravos_pouco_comparaveis": sorted(
            code for code, item in resumo.items() if not item["comparavel"]
        ),
        "proporcao_pendente_geral": (
            round(total_pendentes / total_casos, 4) if total_casos else 0.0
        ),
        "limite_comparavel": COMPARABLE_MAX_PENDING,
        "aviso": (
            "`casos_provaveis` inclui notificações ainda sem classificação "
            "final, o que é a definição correta e normal em dado recente. Como "
            "esse número é o numerador da incidência, agravos com frações "
            "pendentes muito diferentes produzem incidências que não estão na "
            "mesma escala. `casos_descartados` igual a zero costuma significar "
            "'nada encerrado ainda', não 'nada descartado'."
        ),
    }

    enriched_report = dict(report)
    enriched_report["municipios"] = municipalities
    enriched_report["metadata"] = metadata
    return enriched_report
