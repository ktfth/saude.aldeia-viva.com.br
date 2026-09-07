"""
O cache de download nao pode servir arquivo velho em silencio.

Ele nao tinha prazo: `if cache_path.exists(): return ...`, para sempre. Os
arquivos preliminares de arbovirose crescem durante o ano, entao o cache
servia em setembro o download de abril.

ISSO ENGANOU TRES MEDICOES NESTA SERIE, e a pior quase virou decisao de
negocio: uma recarga comparou os totais, achou zero diferenca, e a conclusao
a tirar dali era "o pipeline de atualizacao nao vale a pena". Com o cache
desligado, a mesma recarga trouxe +195.160 casos de dengue e +229 obitos.
So desconfiei porque o numero de Zika bateu EXATAMENTE com o de abril.

Um cache que mente sobre a idade do que serve e pior que nenhum cache: ele
faz uma medicao parecer uma medicao da fonte.
"""

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion import sinan_loader

RAIZ = Path(__file__).resolve().parent.parent


class TestOCacheDeDownloadTemPrazo(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="av-cache-"))
        self.url = "https://exemplo.invalido/arquivo.zip"
        patcher = patch("app.SINAN_CACHE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _grava(self, conteudo: bytes, idade_dias: float) -> Path:
        caminho = sinan_loader.cache_path_for_url(self.url)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(conteudo)
        quando = time.time() - idade_dias * 86400
        os.utime(caminho, (quando, quando))
        return caminho

    def test_copia_recente_e_reaproveitada(self) -> None:
        self._grava(b"do cache", idade_dias=0.1)
        with patch.object(sinan_loader, "fetch_url_bytes") as buscou:
            self.assertEqual(sinan_loader.download_bytes(self.url), b"do cache")
        buscou.assert_not_called()

    def test_copia_velha_e_buscada_de_novo(self) -> None:
        """O defeito: em setembro ele servia o download de abril."""
        self._grava(b"de abril", idade_dias=140)
        with patch.object(
            sinan_loader, "fetch_url_bytes", return_value=b"de hoje"
        ) as buscou:
            self.assertEqual(sinan_loader.download_bytes(self.url), b"de hoje")
        buscou.assert_called_once()

    def test_a_copia_nova_substitui_a_velha(self) -> None:
        caminho = self._grava(b"de abril", idade_dias=140)
        with patch.object(sinan_loader, "fetch_url_bytes", return_value=b"de hoje"):
            sinan_loader.download_bytes(self.url)
        self.assertEqual(caminho.read_bytes(), b"de hoje")

    def test_usar_o_cache_e_anunciado(self) -> None:
        """Servir em silêncio é o que fazia a medição parecer da fonte."""
        self._grava(b"do cache", idade_dias=0.1)
        with self.assertLogs("ingestion.sinan_loader", level="INFO") as capturado:
            with patch.object(sinan_loader, "fetch_url_bytes"):
                sinan_loader.download_bytes(self.url)
        self.assertTrue(
            any("cache" in linha.lower() for linha in capturado.output),
            "usar o cache tem que aparecer no log",
        )

    def test_desligar_o_cache_ignora_a_copia(self) -> None:
        self._grava(b"do cache", idade_dias=0.1)
        with patch.dict(os.environ, {"SINAN_DISABLE_CACHE": "1"}):
            with patch.object(
                sinan_loader, "fetch_url_bytes", return_value=b"de hoje"
            ):
                self.assertEqual(sinan_loader.download_bytes(self.url), b"de hoje")

    def test_limite_zero_desliga_a_expiracao(self) -> None:
        """Escape hatch para ambiente sem rede, como no cache do relatório."""
        self._grava(b"de abril", idade_dias=140)
        with patch.dict(os.environ, {"SINAN_SOURCE_CACHE_MAX_AGE_DAYS": "0"}):
            with patch.object(sinan_loader, "fetch_url_bytes") as buscou:
                self.assertEqual(sinan_loader.download_bytes(self.url), b"de abril")
            buscou.assert_not_called()

    def test_o_prazo_e_lido_a_cada_chamada(self) -> None:
        with patch.dict(os.environ, {"SINAN_SOURCE_CACHE_MAX_AGE_DAYS": "9"}):
            self.assertEqual(sinan_loader.source_cache_max_age_days(), 9)
        self.assertEqual(
            sinan_loader.source_cache_max_age_days(),
            sinan_loader.DEFAULT_SOURCE_CACHE_MAX_AGE_DAYS,
        )

    def test_valor_invalido_cai_no_padrao(self) -> None:
        with patch.dict(os.environ, {"SINAN_SOURCE_CACHE_MAX_AGE_DAYS": "nao-numero"}):
            self.assertEqual(
                sinan_loader.source_cache_max_age_days(),
                sinan_loader.DEFAULT_SOURCE_CACHE_MAX_AGE_DAYS,
            )


if __name__ == "__main__":
    unittest.main()
