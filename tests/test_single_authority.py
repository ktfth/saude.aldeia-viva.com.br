"""
Um fato, um lugar. Quatro lugares diziam qual nível de risco é mais grave.

`domain/risk.py` declara `RISK_LEVEL_ORDER`. Além dele, três tabelas escritas
à mão repetiam o conjunto — e cada uma falhava de um jeito diferente se um
nível fosse acrescentado ao domínio e esquecido ali:

    aggregation/filters.py   `.get(nivel, -1)` -> o nível some de TODO filtro
    app.py  distribuicao     a chave some, e as contagens param de somar
    app.py  rótulo/marcador  o usuário vê o código cru no lugar do rótulo

Nenhuma levantaria erro. É a mesma família de todo defeito desta série: a
divergência é possível, silenciosa, e só aparece quando alguém repara.

As duas primeiras passaram a derivar de `RISK_LEVEL_ORDER`. A terceira não
pode — rótulo e marcador são decisão de interface, não de domínio — então
aqui a cobertura é exigida em vez de derivada.
"""

import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app
from aggregation.filters import SEVERIDADE_DO_NIVEL, level_at_least
from domain.risk import RISK_LEVEL_ORDER
from tests import PREMIUM_KEY, ensure_real_report

RAIZ = Path(__file__).resolve().parent.parent

# Módulos que podem enumerar os níveis, e por quê.
PODEM_ENUMERAR = {
    "risk.py": "declara RISK_LEVEL_ORDER; é a autoridade",
    "app.py": "rótulo e marcador de interface, cobertura exigida abaixo",
}


class TestASeveridadeVemDoDominio(unittest.TestCase):
    def test_a_tabela_de_severidade_e_derivada(self) -> None:
        self.assertEqual(set(SEVERIDADE_DO_NIVEL), set(RISK_LEVEL_ORDER))

    def test_a_ordem_declarada_e_respeitada(self) -> None:
        """Do mais grave ao menos grave, como o domínio diz."""
        severidades = [SEVERIDADE_DO_NIVEL[n] for n in RISK_LEVEL_ORDER]
        self.assertEqual(severidades, sorted(severidades, reverse=True))

    def test_o_filtro_concorda_com_a_ordem(self) -> None:
        mais_grave, menos_grave = RISK_LEVEL_ORDER[0], RISK_LEVEL_ORDER[-1]
        self.assertTrue(level_at_least(mais_grave, menos_grave))
        self.assertFalse(level_at_least(menos_grave, mais_grave))
        for nivel in RISK_LEVEL_ORDER:
            self.assertTrue(level_at_least(nivel, nivel), nivel)

    def test_nivel_desconhecido_nao_passa_por_qualquer_minimo(self) -> None:
        """`-1` de default: era assim que um nível novo sumia de todo filtro."""
        self.assertFalse(level_at_least("inexistente", RISK_LEVEL_ORDER[-1]))


class TestAInterfaceCobreTodosOsNiveis(unittest.TestCase):
    def test_todo_nivel_tem_rotulo(self) -> None:
        faltando = set(RISK_LEVEL_ORDER) - set(app.ROTULO_DO_NIVEL)
        self.assertEqual(
            faltando, set(), f"nível sem rótulo chega ao usuário como código: {faltando}"
        )

    def test_todo_nivel_tem_marcador(self) -> None:
        faltando = set(RISK_LEVEL_ORDER) - set(app.MARCADOR_DO_NIVEL)
        self.assertEqual(faltando, set())

    def test_nao_ha_rotulo_para_nivel_que_nao_existe(self) -> None:
        """Rótulo sobrando é sinal de nível removido do domínio e esquecido."""
        sobrando = set(app.ROTULO_DO_NIVEL) - set(RISK_LEVEL_ORDER)
        self.assertEqual(sobrando, set())


class TestADistribuicaoCobreTodosOsMunicipios(unittest.TestCase):
    """A soma é a asserção: uma chave que suma deixa de fechar com o total.

    A distribuição era montada com quatro comparações escritas à mão. Um
    nível acrescentado ao domínio e esquecido ali sumiria do resultado sem
    erro algum — e o cliente receberia contagens que não somam o total,
    sem nada indicando o que faltou.
    """

    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_as_contagens_somam_o_total(self) -> None:
        resumo = self.client.get(
            "/v1/professional-report?estado=SP", headers={"X-API-Key": PREMIUM_KEY}
        ).json()["summary"]
        self.assertEqual(
            sum(resumo["distribuicao_risco"].values()),
            resumo["total_municipios"],
        )

    def test_a_distribuicao_traz_todos_os_niveis_do_dominio(self) -> None:
        resumo = self.client.get(
            "/v1/professional-report?estado=SP", headers={"X-API-Key": PREMIUM_KEY}
        ).json()["summary"]
        self.assertEqual(
            set(resumo["distribuicao_risco"]), set(RISK_LEVEL_ORDER)
        )


class TestNinguemMaisEnumeraOsNiveis(unittest.TestCase):
    """A rede geral: impede a quarta tabela de nascer."""

    def test_nenhum_modulo_novo_escreve_a_propria_lista(self) -> None:
        # Procura literais de dicionário ou tupla que citem TRÊS ou mais
        # níveis, em qualquer ordem.
        #
        # A primeira versão desta busca era um regex com os nomes em
        # sequência fixa, e não pegou a mutação de verificação: a tabela
        # reintroduzida listava do menos grave ao mais grave, ordem oposta à
        # do padrão. Uma rede que só pega uma das ordenações não é rede.
        literal = re.compile(r"[{(][^{}()]{0,400}[})]", re.S)
        reincidentes = {}
        for caminho in sorted(RAIZ.rglob("*.py")):
            if any(
                parte in caminho.parts
                for parte in (".venv", "venv", "tests", "scripts", "__pycache__")
            ):
                continue
            if caminho.name in PODEM_ENUMERAR:
                continue
            texto = caminho.read_text(encoding="utf-8", errors="replace")
            for trecho in literal.findall(texto):
                citados = [n for n in RISK_LEVEL_ORDER if f'"{n}"' in trecho]
                if len(citados) >= 3:
                    reincidentes[str(caminho.relative_to(RAIZ))] = sorted(citados)
                    break
        self.assertEqual(
            reincidentes,
            {},
            "segunda tabela de níveis fora do domínio — derive de "
            f"RISK_LEVEL_ORDER: {reincidentes}",
        )

    def test_toda_isencao_tem_motivo(self) -> None:
        for arquivo, motivo in PODEM_ENUMERAR.items():
            self.assertTrue(motivo, f"isenção de {arquivo} sem motivo escrito")


if __name__ == "__main__":
    unittest.main()
