"""
Código que ninguém alcança, e a origem de dado que pode faltar em silêncio.

Duas varreduras que este arquivo transforma em rede.

**Função pública sem nenhuma referência.** `render_alert_items` e
`render_metric` já haviam sido removidas nesta série; a varredura por AST
achou mais duas — `dashboard_summary`, resto do mesmo painel de métricas que
deixou de existir, e `worst_freshness`.

A segunda tinha um defeito dentro dela: o nome diz "pior", e ela devolve o
MAIS FRESCO. Espelha `worst_level` de `domain/risk.py`, que devolve o mais
grave — mesmo padrão de nome, semântica oposta. Era inofensiva só porque
ninguém a chamava; quem a revivesse por analogia receberia o contrário do que
esperava, sem erro.

**A origem de dado que pode sumir calada.** `bundled_report_snapshot.py` é a
ÚNICA origem num deploy: o `.vercelignore` exclui o resto, e os JSON
versionados foram removidos por não serem lidos. A guarda de import
devolvia `None` sem dizer nada, sob um comentário que a descrevia como
"fallback for local-only runs before bundling" — fase que acabou. É o mesmo
modo de falha do arquivo de chaves ausente, que custou 8 testes e uma API
inteira em 401 antes de passar a avisar.

A varredura por capturas de exceção que engolem em silêncio, feita junto,
NÃO achou defeito: as nove silenciosas pegam tipos estreitos (`ValueError`,
`TypeError`) para cair num padrão, que é o uso correto, e as dez largas todas
logam. A hipótese caiu, e está registrada aqui para não ser refeita do zero.
"""

import ast
import unittest
from pathlib import Path

import app

RAIZ = Path(__file__).resolve().parent.parent
IGNORAR = (".venv", "venv", "__pycache__", "test-results", ".git")

# Funções públicas sem referência que devem permanecer, e por quê.
MANTIDAS: dict[str, str] = {}


def _modulos():
    for caminho in sorted(RAIZ.rglob("*.py")):
        if not any(parte in caminho.parts for parte in IGNORAR):
            yield caminho


def _definidas_e_referenciadas():
    definidas: dict[str, str] = {}
    referenciadas: set[str] = set()

    for caminho in _modulos():
        try:
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensivo
            continue
        relativo = str(caminho.relative_to(RAIZ))
        auxiliar = relativo.startswith(("tests", "scripts"))

        for no in ast.walk(arvore):
            if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Handler de rota é chamado pelo framework, não por nome.
                rota = any(
                    (isinstance(d, ast.Call)
                     and getattr(getattr(d.func, "value", None), "id", "") == "app")
                    or (isinstance(d, ast.Attribute)
                        and getattr(d.value, "id", "") == "app")
                    for d in no.decorator_list
                )
                if not auxiliar and not rota and not no.name.startswith("_"):
                    definidas[no.name] = relativo
            elif isinstance(no, ast.Name):
                referenciadas.add(no.id)
            elif isinstance(no, ast.Attribute):
                referenciadas.add(no.attr)

    return definidas, referenciadas


class TestNenhumaFuncaoPublicaEstaOrfa(unittest.TestCase):
    def test_toda_funcao_publica_e_referenciada(self) -> None:
        definidas, referenciadas = _definidas_e_referenciadas()
        orfas = {
            nome: arquivo
            for nome, arquivo in definidas.items()
            if nome not in referenciadas and nome not in MANTIDAS
        }
        self.assertEqual(
            orfas,
            {},
            "função pública que ninguém chama — remova, ou declare o motivo "
            f"em MANTIDAS: {orfas}",
        )

    def test_a_varredura_ainda_enxerga_o_codigo(self) -> None:
        """Se a extração parar de achar funções, a rede vira decoração."""
        definidas, referenciadas = _definidas_e_referenciadas()
        self.assertGreater(len(definidas), 100)
        self.assertGreater(len(referenciadas), 300)

    def test_toda_excecao_tem_motivo(self) -> None:
        for nome, motivo in MANTIDAS.items():
            self.assertTrue(motivo, f"exceção de {nome} sem motivo escrito")


class TestAOrigemDeDadoNaoSomeCalada(unittest.TestCase):
    def test_o_snapshot_embarcado_esta_disponivel(self) -> None:
        """A guarda de import devolvia None sem dizer nada."""
        self.assertIsNotNone(
            app.load_embedded_report_snapshot,
            "bundled_report_snapshot não importou; num deploy isto significa "
            "base vazia, e antes desta rede o log ficava mudo",
        )

    def test_o_snapshot_traz_a_base_inteira(self) -> None:
        """Um snapshot degenerado se declara `ok` — já houve um versionado."""
        relatorio = app.load_embedded_report_snapshot()
        self.assertGreater(len(relatorio.get("municipios") or []), 5000)
        self.assertEqual((relatorio.get("metadata") or {}).get("status"), "ok")

    def test_nao_ha_outra_origem_versionada_competindo(self) -> None:
        """34 MB de JSON que ninguém lia, um deles degenerado, foram removidos."""
        for diretorio in ("data/reports", "api/data"):
            self.assertFalse(
                (RAIZ / diretorio).exists(),
                f"{diretorio} voltou; a origem de dado é o módulo embarcado",
            )


if __name__ == "__main__":
    unittest.main()
