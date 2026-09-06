"""
Uma página que ninguém encontra não converte ninguém.

Medido em 2026-09-06: buscar pelo domínio exato não devolve o serviço, e a
consulta temática que um comprador faria ("dados dengue por município,
incidência por 100 mil") devolve boletins em PDF de secretarias estaduais,
TabNet e painéis Power BI -- nenhuma API. A lacuna é real e desatendida; o
que faltava era ser encontrável.

E o `sitemap.xml` não oferecia `/planos` ao rastreador. É a página onde se
pede uma chave e onde está o preço: a única do site onde dinheiro acontece.

A regra aqui: o que está na navegação está no sitemap. Duas listas escritas
em lugares diferentes divergem em silêncio, que é como esta ficou de fora.
"""

import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report

RAIZ = Path(__file__).resolve().parent.parent


def _rotas_da_navegacao() -> set[str]:
    """Os destinos que `render_web_page` monta no `<nav>`."""
    fonte = (RAIZ / "app.py").read_text(encoding="utf-8")
    bloco = fonte[fonte.index("nav_items = ("):]
    # Fecha na tupla EXTERNA: cortar no primeiro ")" pararia dentro da
    # primeira entrada e a rede vigiaria uma rota só. O guarda de tamanho
    # abaixo existe por isso, e pegou exatamente este erro.
    fim = '\n    )'
    bloco = bloco[: bloco.index(fim)]
    return set(re.findall(r'\("(/[a-z.]*)"', bloco))


class TestOQueEstaNaNavegacaoEstaNoSitemap(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _locs(self) -> set[str]:
        xml = self.client.get("/sitemap.xml").text
        return {
            re.sub(r"https?://[^/]+", "", loc)
            for loc in re.findall(r"<loc>([^<]+)</loc>", xml)
        }

    def test_toda_pagina_da_navegacao_esta_no_sitemap(self) -> None:
        faltando = _rotas_da_navegacao() - self._locs()
        self.assertEqual(
            faltando,
            set(),
            f"página na navegação e ausente do sitemap: {sorted(faltando)}",
        )

    def test_a_pagina_de_planos_esta_anunciada(self) -> None:
        """O defeito concreto, para não ser desfeito em silêncio."""
        self.assertIn("/planos", self._locs())

    def test_o_sitemap_nao_anuncia_rota_inexistente(self) -> None:
        """Sitemap apontando para 404 queima confiança do rastreador."""
        quebradas = {}
        for rota in sorted(self._locs()):
            app.rate_limiter.reset()
            codigo = self.client.get(rota).status_code
            if codigo >= 400:
                quebradas[rota] = codigo
        self.assertEqual(quebradas, {})

    def test_o_robots_aponta_para_o_sitemap(self) -> None:
        robots = self.client.get("/robots.txt").text
        self.assertIn("sitemap.xml", robots.lower())

    def test_a_navegacao_nao_esta_vazia(self) -> None:
        """Se a extração parar de achar rotas, a rede vira decoração."""
        self.assertGreaterEqual(len(_rotas_da_navegacao()), 4)


if __name__ == "__main__":
    unittest.main()
