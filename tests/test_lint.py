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
