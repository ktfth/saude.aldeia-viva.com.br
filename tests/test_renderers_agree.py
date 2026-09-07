"""
A mesma célula é desenhada duas vezes, em linguagens diferentes.

O painel renderiza no servidor (`presentation/signal.py`) na primeira carga, e
no cliente (`web/static/js/dashboard.js`) quando alguém filtra. São duas
implementações da MESMA célula, e nada as mantinha de acordo.

Medido: ao acrescentar a fração de fonte atual e a direção do agravo no
servidor, o cliente continuou sem elas. O efeito seria invisível na primeira
tela e apareceria só depois — o usuário filtra e a informação some, sem erro
algum. É o mesmo defeito das linhas clicáveis que só respondiam antes de
filtrar, corrigido antes nesta série.

A rede compara as classes que cada lado emite. Não prova que o HTML é
idêntico — provaria só reescrevendo um dos dois —, mas pega a divergência que
de fato acontece: alguém mexe num renderizador e esquece do outro.
"""

import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SERVIDOR = RAIZ / "presentation" / "signal.py"
CLIENTE = RAIZ / "web" / "static" / "js" / "dashboard.js"

# Prefixos das classes que descrevem conteúdo da célula, e que por isso os
# dois lados precisam emitir.
PREFIXOS = ("cell-", "trend-")

# Classes que existem de um lado só, com o motivo.
SO_DE_UM_LADO = {
    "cell-muted": "removida do CSS; nenhum dos dois emite mais",
}


def _classes(caminho: Path) -> set[str]:
    texto = caminho.read_text(encoding="utf-8")
    achadas = set()
    for bloco in re.findall(r'class="([^"]*)"', texto):
        for nome in re.split(r"[\s${}`]+", bloco):
            if nome.startswith(PREFIXOS):
                achadas.add(nome)
    # Classes montadas por variável, como `trend-up` nas tabelas de símbolo.
    for nome in re.findall(r"['\"](trend-[a-z]+)['\"]", texto):
        achadas.add(nome)
    return achadas - set(SO_DE_UM_LADO)


class TestOsDoisRenderizadoresConcordam(unittest.TestCase):
    def test_o_cliente_emite_tudo_que_o_servidor_emite(self) -> None:
        faltando = _classes(SERVIDOR) - _classes(CLIENTE)
        self.assertEqual(
            faltando,
            set(),
            "o servidor desenha e o cliente não: a informação some ao filtrar "
            f"-- {sorted(faltando)}",
        )

    def test_o_servidor_emite_tudo_que_o_cliente_emite(self) -> None:
        faltando = _classes(CLIENTE) - _classes(SERVIDOR)
        self.assertEqual(
            faltando,
            set(),
            "o cliente desenha e o servidor não: a primeira tela fica sem a "
            f"informação -- {sorted(faltando)}",
        )

    def test_a_extracao_ainda_enxerga_as_classes(self) -> None:
        """Se a busca parar de achar, a rede vira decoração."""
        self.assertGreaterEqual(len(_classes(SERVIDOR)), 5)
        self.assertGreaterEqual(len(_classes(CLIENTE)), 5)

    def test_toda_excecao_tem_motivo(self) -> None:
        for nome, motivo in SO_DE_UM_LADO.items():
            self.assertTrue(motivo, f"exceção de {nome} sem motivo escrito")


if __name__ == "__main__":
    unittest.main()
