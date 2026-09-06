"""
As regras `F` do pyflakes, exigidas limpas.

Uma varredura manual por codigo inalcancavel achou tres funcoes orfas e, ao
remover uma delas, uma quarta que so ficou orfa por consequencia. Depois
disso o `ruff` encontrou 53 imports sem uso, duas variaveis calculadas e
nunca lidas, e um `from datetime import date` repetido no meio de um arquivo
— residuo da extracao da "Fase 0", que deixou um SEGUNDO bloco de import no
meio do modulo.

Nenhum desses quebrava nada. Todos enganavam quem lesse o codigo sobre do que
ele depende e o que ele calcula. Escrever a varredura a mao levou duas
tentativas erradas nesta sessao; a ferramenta acerta de primeira e nao
esquece.

Escopo deliberadamente estreito: so as regras `F`. Elas descrevem fatos sobre
o codigo — este nome nao e usado, esta variavel nao e lida — e nao preferencia
de estilo. Uma rede de estilo geraria ruido e seria desligada.
"""

import subprocess
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
EXCLUIR = ".venv,venv,test-results,__pycache__"


class TestOEntrypointDaPlataformaExpoeOApp(unittest.TestCase):
    """`api/index.py` tem que expor `app`, e o linter quase o apagou.

    O arquivo existe para a Vercel encontrar o objeto `app` no modulo que o
    `vercel.json` aponta. Para o `ruff`, `from app import app` e um import
    sem uso — e o `--fix` o REMOVEU numa limpeza automatica. A funcao teria
    subido sem app nenhum e o site inteiro cairia no deploy seguinte.

    O commit ja estava empurrado quando percebi. Producao nao chegou a ser
    afetada porque o deploy vem depois, mas o repositorio ficou quebrado por
    alguns minutos, e nada acusava: o arquivo continuava valido, so vazio de
    proposito.

    A linha voltou com `noqa`, e esta rede afirma o que o `noqa` sozinho nao
    afirma — que o objeto de fato existe e e a aplicacao.
    """

    def test_o_modulo_expoe_um_app(self) -> None:
        import importlib.util

        caminho = RAIZ / "api" / "index.py"
        self.assertTrue(caminho.exists(), "entrypoint da Vercel sumiu")

        spec = importlib.util.spec_from_file_location("entrypoint_vercel", caminho)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)

        self.assertTrue(
            hasattr(modulo, "app"),
            "api/index.py nao expoe `app`; a funcao na Vercel sobe sem "
            "aplicacao e todas as rotas caem",
        )

    def test_o_app_exposto_e_a_aplicacao(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "entrypoint_vercel", RAIZ / "api" / "index.py"
        )
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)

        import app as modulo_principal

        self.assertIs(modulo.app, modulo_principal.app)

    def test_o_vercel_json_aponta_para_esse_arquivo(self) -> None:
        import json

        config = json.loads((RAIZ / "vercel.json").read_text(encoding="utf-8"))
        destinos = {rota.get("dest") for rota in config.get("routes", [])}
        self.assertIn("/api/index.py", destinos)


class TestSemDefeitoDeAlcance(unittest.TestCase):
    def _ruff(self, regras: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, "-m", "ruff", "check",
                "--select", regras, "--exclude", EXCLUIR, "--no-cache", ".",
            ],
            cwd=RAIZ, capture_output=True, text=True,
        )

    def test_o_linter_esta_instalado(self) -> None:
        """Declarado em requirements-dev.txt; ausente, a rede seria decorativa."""
        resultado = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True, text=True,
        )
        self.assertEqual(
            resultado.returncode, 0,
            "ruff ausente — instale com `pip install -r requirements-dev.txt`",
        )

    def test_nenhum_import_sem_uso(self) -> None:
        resultado = self._ruff("F401")
        self.assertEqual(
            resultado.returncode, 0,
            f"import sem uso engana sobre as dependencias do modulo:"
            f"{chr(10)}{resultado.stdout}",
        )

    def test_nenhuma_variavel_calculada_e_ignorada(self) -> None:
        """Costuma ser calculo que perdeu o consumidor, como o do grafico removido."""
        resultado = self._ruff("F841")
        self.assertEqual(resultado.returncode, 0, resultado.stdout)

    def test_nenhum_nome_redefinido_sem_uso(self) -> None:
        """Um `import` repetido no meio do arquivo escondia dois blocos."""
        resultado = self._ruff("F811")
        self.assertEqual(resultado.returncode, 0, resultado.stdout)

    def test_a_familia_F_inteira_esta_limpa(self) -> None:
        resultado = self._ruff("F")
        self.assertEqual(resultado.returncode, 0, resultado.stdout)


if __name__ == "__main__":
    unittest.main()
