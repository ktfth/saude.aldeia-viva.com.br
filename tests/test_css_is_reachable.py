"""
CSS que nenhuma página alcança.

`test_css_coverage.py` cobre um sentido: classe usada no HTML tem que existir
em algum CSS. O sentido contrário nunca teve rede, e acumulou 16 regras que
não estilizam nada — restos de um painel com "radar", "métricas" e "facts"
que deixou de existir.

Isso não é só arrumação. Duas dessas regras eram armadilhas para tela
estreita, esperando alguém renderizar a classe:

    .risk-legend   repeat(4, minmax(190px, 1fr))   ->  760px de mínimo rígido
    .content-grid  minmax(360px, .65fr)            ->  não cabe em 390px

E `test_responsive_layout.py` teve que ser escrito com uma exceção para
`.content-grid` até ela ser removida — exceção por causa de uma classe que
ninguém renderiza é como uma rede começa a apodrecer.

Duas funções de render em `app.py` (`render_alert_items`, `render_metric`)
também não eram chamadas por ninguém: Python morto produzindo CSS morto.

As classes de estado — nível de risco e frescor do sinal — são aplicadas por
interpolação (`class="badge {nivel}"`) e não aparecem em nenhuma varredura de
texto. A isenção delas é DERIVADA das constantes de domínio, e não uma lista
escrita à mão: uma lista à mão envelhece calada, e o objetivo aqui é
exatamente o oposto.
"""

import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app
from domain.recency import FRESHNESS_ORDER
from domain.risk import RISK_LEVEL_ORDER
from tests import ensure_real_report

ROOT = Path(__file__).resolve().parent.parent
CSS_DIR = ROOT / "web" / "static" / "css"
JS = ROOT / "web" / "static" / "js" / "dashboard.js"

HTML_ROUTES = ("/dashboard", "/sobre", "/planos", "/agentes")

# Classes aplicadas por interpolação, cujo conjunto é declarado no domínio.
DINAMICAS = set(RISK_LEVEL_ORDER) | set(FRESHNESS_ORDER)

# O que não é classe de estado nem aparece em marcação, com o motivo.
ISENTAS = {
    "active": "aplicada na navegação por comparação de rota",
    "short": "modificador de .skeleton-line, aplicado ao montar o esqueleto",
    "is-stale": "aplicada quando a carga passa do prazo",
    "is-featured": "aplicada ao plano em destaque",
}


def _classes_renderizadas() -> set[str]:
    ensure_real_report()
    app.rate_limiter.reset()
    encontradas: set[str] = set()
    with TestClient(app.app) as client:
        for rota in HTML_ROUTES:
            resposta = client.get(rota)
            assert resposta.status_code == 200, f"{rota} devolveu {resposta.status_code}"
            for m in re.finditer(r'class="([^"]+)"', resposta.text):
                encontradas.update(c for c in m.group(1).split() if "$" not in c)

    js = JS.read_text(encoding="utf-8")
    for m in re.finditer(r'class="([^"{}$]+)"', js):
        encontradas.update(m.group(1).split())
    for m in re.finditer(r"classList\.(?:add|toggle|remove)\(([^)]*)\)", js):
        encontradas.update(re.findall(r"['\"]([a-zA-Z0-9_-]+)['\"]", m.group(1)))
    return encontradas


def _classes_definidas() -> dict[str, set[str]]:
    definidas: dict[str, set[str]] = {}
    for arquivo in sorted(CSS_DIR.rglob("*.css")):
        texto = re.sub(r"/\*.*?\*/", "", arquivo.read_text(encoding="utf-8"), flags=re.S)
        for seletor in re.findall(r"([^{}]+)\{", texto):
            if seletor.strip().startswith("@"):
                continue
            for classe in re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", seletor):
                definidas.setdefault(classe, set()).add(arquivo.name)
    return definidas


class TestTodaClasseDeCssEAlcancavel(unittest.TestCase):
    def test_nenhuma_regra_estiliza_o_nada(self) -> None:
        renderizadas = _classes_renderizadas() | DINAMICAS | set(ISENTAS)
        orfas = {
            classe: sorted(arquivos)
            for classe, arquivos in _classes_definidas().items()
            if classe not in renderizadas
        }
        self.assertEqual(
            orfas,
            {},
            "classe definida em CSS que nenhuma página renderiza — remova a "
            f"regra ou declare o motivo em ISENTAS: {orfas}",
        )

    def test_as_isencoes_ainda_fazem_sentido(self) -> None:
        """Isenção para classe que já não existe no CSS é lixo acumulando."""
        definidas = set(_classes_definidas())
        sobrando = sorted(c for c in ISENTAS if c not in definidas)
        self.assertEqual(sobrando, [], f"isenção sem regra correspondente: {sobrando}")

    def test_os_estados_vem_do_dominio(self) -> None:
        """Se a isenção virar lista à mão, ela para de acompanhar o domínio."""
        self.assertIn("critico", DINAMICAS)
        self.assertIn("vivo", DINAMICAS)
        self.assertIn("fossil", DINAMICAS)


if __name__ == "__main__":
    unittest.main()
