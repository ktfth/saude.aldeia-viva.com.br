"""
Uma correção de estilo tem que chegar em quem já visitou o site.

`static_asset_version()` calcula uma impressão digital sobre TODOS os
arquivos CSS e a aplica em `main.css?v=...`. Só que quase todo o estilo mora
nos componentes, e o navegador os busca pela URL crua do `@import` — sem
parâmetro nenhum. `main.css?v=novo` era rebaixado; `components/*.css` vinham
do cache.

Medido ao vivo: depois de editar `detail-panel.css`, a página aplicava a
regra ANTERIOR à edição (`@media (max-width: 820px)`), lida do CSSOM do
navegador. Foi por isso que uma correção de layout mobile mediu como se não
existisse — e é a segunda vez nesta série que este defeito aparece. Da
primeira, a correção cobriu só o arquivo de entrada.

O modo de falha é o pior possível: o servidor está certo, o arquivo está
certo, o teste passa, e o usuário vê o layout velho.
"""

import os
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report

ROOT = Path(__file__).resolve().parent.parent
CSS_DIR = ROOT / "web" / "static" / "css"


class TestOsImportsCarregamAVersao(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_todo_import_servido_tem_versao(self) -> None:
        css = self.client.get("/static/css/main.css").text
        importados = re.findall(r'@import url\("([^"]+)"\)', css)
        self.assertTrue(importados, "main.css servido sem nenhum @import")
        sem_versao = [u for u in importados if "?v=" not in u]
        self.assertEqual(
            sem_versao, [], f"@import sem versão continua vindo do cache: {sem_versao}"
        )

    def test_a_versao_e_a_mesma_do_link_da_pagina(self) -> None:
        """Duas impressões digitais diferentes seria pior que nenhuma."""
        pagina = self.client.get("/dashboard").text
        do_link = re.search(r"main\.css\?v=([0-9a-f]+)", pagina).group(1)
        css = self.client.get("/static/css/main.css").text
        dos_imports = set(re.findall(r"\?v=([0-9a-f]+)", css))
        self.assertEqual(dos_imports, {do_link})

    def test_o_arquivo_em_disco_segue_modular(self) -> None:
        """A versão é aplicada ao servir; o fonte continua sem `?v=` fixo."""
        disco = (CSS_DIR / "main.css").read_text(encoding="utf-8")
        self.assertNotIn("?v=", disco)

    def test_todos_os_componentes_estao_importados(self) -> None:
        css = self.client.get("/static/css/main.css").text
        importados = {
            Path(u.split("?")[0]).name
            for u in re.findall(r'@import url\("([^"]+)"\)', css)
        }
        em_disco = {p.name for p in CSS_DIR.rglob("*.css")} - {"main.css"}
        self.assertEqual(
            em_disco - importados,
            set(),
            "arquivo CSS que existe mas nenhuma página carrega",
        )


class TestAImpressaoDigitalCobreOsComponentes(unittest.TestCase):
    def test_mexer_num_componente_muda_a_versao(self) -> None:
        """O defeito só é fechado se editar um componente invalidar a cache."""
        alvo = CSS_DIR / "components" / "detail-panel.css"
        original = alvo.stat()

        app._ASSET_VERSION = None
        antes = app.static_asset_version()
        try:
            os.utime(alvo, (original.st_atime, original.st_mtime + 60))
            app._ASSET_VERSION = None
            depois = app.static_asset_version()
        finally:
            os.utime(alvo, (original.st_atime, original.st_mtime))
            app._ASSET_VERSION = None

        self.assertNotEqual(antes, depois)

    def test_a_versao_e_estavel_dentro_do_processo(self) -> None:
        self.assertEqual(app.static_asset_version(), app.static_asset_version())


if __name__ == "__main__":
    unittest.main()
