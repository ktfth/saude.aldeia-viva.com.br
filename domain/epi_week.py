"""Semana epidemiológica brasileira, e a direção que o número está tomando.

O produto respondia *quanto* e não conseguia responder *subindo ou descendo*.
Todo campo era total acumulado de um período. Decisão de alocação se toma
pela direção: 500 casos em queda e 500 em alta são decisões opostas, e
apareciam idênticos.

**Por que não a semana ISO.** A semana epidemiológica do Ministério da Saúde
começa no DOMINGO; a ISO começa na segunda. E a SE 1 é a que termina no
primeiro sábado de janeiro com pelo menos quatro dias no ano novo, regra
diferente da ISO (que ancora na primeira quinta-feira). Usar ISO num domínio
onde o profissional compara com o boletim da secretaria produziria números
deslocados em uma semana — do tipo que ninguém confere e todo mundo repassa.

Medido na fonte antes de escrever isto: `DT_NOTIFIC` e `DT_SIN_PRI` estão
preenchidas em **100%** dos registros de 2026, e cobrem 34 a 35 semanas com
8 a 10 dias de defasagem. A curva é construtível e é atual.
"""

import math
from datetime import date, timedelta
from typing import Iterable, Mapping

# Janelas comparadas para dizer a direção. Quatro semanas absorvem o ruído de
# uma semana isolada sem diluir uma virada real.
JANELA_SEMANAS = 4

# Abaixo disto a razão entre janelas é ruído: dois casos virando três é +50%,
# e não significa nada.
MINIMO_PARA_TENDENCIA = 10

# Variação relativa a partir da qual a direção deixa de ser "estável".
LIMIAR_DIRECAO = 0.20

# Semanas do fim da série que ainda estão se preenchendo, e por isso NÃO
# entram no cálculo da direção.
#
# Toda curva por início de sintomas termina baixa: as notificações daquelas
# semanas ainda não chegaram. Sem excluí-las, toda tendência tende a
# "descendo" por artefato — medido na primeira versão desta implementação,
# 1.631 municípios "descendo" contra 1.038 "subindo", e a última semana da
# curva nacional de dengue caía de 8.537 para 3.706 sem que nada tivesse
# acontecido na epidemia.
#
# O corte vem do atraso real, medido em 124.169 registros de Chikungunya de
# 2026 (notificação menos início de sintomas):
#
#     mediana 3 dias   p90 11 dias   p95 18 dias
#     84,0% das notificações chegam em 1 semana
#     93,4% em 2 semanas
#     96,0% em 3 semanas
#
# Duas semanas deixam 6,6% por chegar — resíduo que não inverte direção. Uma
# semana deixaria 16%, que inverte.
SEMANAS_PROVISORIAS = 2

# Probabilidade acima da qual a diferença entre as janelas é compatível com
# acaso. Duas condições são exigidas para afirmar direção, e elas pegam erros
# opostos:
#
#   magnitude sem significância  Botulismo com 4 casos contra 7 dava -43% e
#                                seria anunciado como "descendo"; é ruído.
#   significância sem magnitude  Dengue com 36.018 contra 37.887 é uma
#                                diferença inquestionável em 74 mil casos, e
#                                -5% não muda decisão nenhuma.
#
# Só é direção o que passa nas duas.
P_MAXIMO_ACASO = 0.05

SUBINDO = "subindo"
ESTAVEL = "estavel"
DESCENDO = "descendo"
INDETERMINADA = "indeterminada"


def _domingo_da_se1(ano: int) -> date:
    """Domingo em que começa a SE 1 do ano.

    A SE 1 termina no primeiro sábado de janeiro que tenha ao menos quatro
    dias do ano novo na sua semana.
    """
    primeiro = date(ano, 1, 1)
    # weekday(): segunda=0 ... sábado=5
    ate_sabado = (5 - primeiro.weekday()) % 7
    sabado = primeiro + timedelta(days=ate_sabado)
    if sabado.day < 4:
        sabado += timedelta(days=7)
    return sabado - timedelta(days=6)


def semana_epidemiologica(dia: date) -> tuple[int, int]:
    """(ano epidemiológico, número da semana) do dia informado.

    O ano epidemiológico pode diferir do calendário na virada: 31 de dezembro
    costuma cair na SE 1 do ano seguinte.
    """
    for ano in (dia.year + 1, dia.year, dia.year - 1):
        inicio = _domingo_da_se1(ano)
        if dia >= inicio:
            numero = (dia - inicio).days // 7 + 1
            # Uma SE 53 só existe quando o ano seguinte começa depois dela.
            if numero <= 52 or dia < _domingo_da_se1(ano + 1):
                return ano, numero
    raise ValueError(f"data fora de qualquer semana epidemiológica: {dia!r}")


def rotulo_semana(chave: tuple[int, int]) -> str:
    """`2026-SE34` — a forma como o boletim oficial escreve."""
    ano, numero = chave
    return f"{ano}-SE{numero:02d}"


def serie_por_semana(datas: Iterable[date]) -> dict[str, int]:
    """Contagem por semana epidemiológica, ordenada no tempo."""
    contagem: dict[tuple[int, int], int] = {}
    for dia in datas:
        if dia is None:
            continue
        contagem[semana_epidemiologica(dia)] = (
            contagem.get(semana_epidemiologica(dia), 0) + 1
        )
    return {rotulo_semana(k): contagem[k] for k in sorted(contagem)}


def _compativel_com_acaso(recente: int, anterior: int) -> bool:
    """A divisão entre as duas janelas cabe no acaso?

    Sob taxa constante, o total observado se reparte entre as duas janelas
    como uma binomial de probabilidade 1/2. A pergunta é se a repartição
    observada é extrema o bastante para descartar isso.

    Exato até mil eventos; acima, a aproximação normal com correção de
    continuidade, que nessa faixa já é indistinguível.
    """
    total = recente + anterior
    if total == 0:
        return True

    extremo = max(recente, anterior)
    if total <= 1000:
        cauda = sum(math.comb(total, k) for k in range(extremo, total + 1))
        p = 2 * cauda / (2**total)
    else:
        media = total / 2
        desvio = math.sqrt(total) / 2
        z = (extremo - 0.5 - media) / desvio
        p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    return min(p, 1.0) > P_MAXIMO_ACASO


def tendencia(
    serie: Mapping[str, int],
    janela: int = JANELA_SEMANAS,
    provisorias: int = SEMANAS_PROVISORIAS,
) -> dict[str, object]:
    """Direção do agravo comparando as duas últimas janelas fechadas.

    As `provisorias` semanas finais ficam de fora: elas ainda estão se
    preenchendo, e incluí-las faz toda curva parecer em queda.

    Devolve `indeterminada` — e não `estavel` — quando o volume é baixo demais
    para a razão significar alguma coisa. A diferença importa: "estável" é uma
    afirmação sobre a epidemia, "indeterminada" é uma afirmação sobre o que
    este dado sustenta.
    """
    todas = sorted(serie)
    rotulos = todas[: len(todas) - provisorias] if provisorias else todas
    recentes = rotulos[-janela:]
    anteriores = rotulos[-2 * janela : -janela]

    soma_recente = sum(serie[r] for r in recentes)
    soma_anterior = sum(serie[r] for r in anteriores)

    if not anteriores or soma_recente + soma_anterior < MINIMO_PARA_TENDENCIA:
        return {
            "direcao": INDETERMINADA,
            "casos_janela_recente": soma_recente,
            "casos_janela_anterior": soma_anterior,
            "variacao": None,
            "janela_semanas": janela,
            "semanas_provisorias_ignoradas": provisorias,
            "motivo": (
                f"menos de {MINIMO_PARA_TENDENCIA} casos nas duas janelas; "
                "a razão entre números pequenos não descreve tendência"
            ),
        }

    acaso = _compativel_com_acaso(soma_recente, soma_anterior)

    if soma_anterior == 0:
        variacao = None
        direcao = ESTAVEL if acaso else SUBINDO
    else:
        variacao = round(soma_recente / soma_anterior - 1, 4)
        importa = abs(variacao) > LIMIAR_DIRECAO
        if acaso or not importa:
            direcao = ESTAVEL
        else:
            direcao = SUBINDO if variacao > 0 else DESCENDO

    return {
        "direcao": direcao,
        "casos_janela_recente": soma_recente,
        "casos_janela_anterior": soma_anterior,
        "variacao": variacao,
        "janela_semanas": janela,
        "semanas_provisorias_ignoradas": provisorias,
        "compativel_com_acaso": acaso,
        "motivo": None,
    }
