"""
Quem embarcar esta API no proprio produto nao pode ser quebrado em silencio.

O produto foi definido como API para embarcar em software de terceiros. Isso
muda o que conta como defeito: renomear um campo deixa de ser refactor e vira
quebra de contrato no software de outra pessoa, descoberta pelos usuarios
DELA.

E o `/openapi.json` nao protege isso. Medido: os corpos de resposta chegam ao
schema como `schema: {}` -- o FastAPI nao tem tipo declarado para eles. O que
o integrador le no codigo dele sao os nomes dos campos, e nada os vigiava.

Superficie medida em 2026-09-06: 1.232 caminhos de campo em oito endpoints,
754 depois de normalizar as chaves que sao dado (codigo de agravo, ano, UF,
codigo IBGE). Enumerar aquelas faria a rede quebrar a cada mudanca de base --
falso positivo treina qualquer um a ignorar a rede.

A assimetria e o ponto: ACRESCENTAR campo nao quebra ninguem, porque um
cliente que nao o conhece segue funcionando. REMOVER ou RENOMEAR quebra. Esta
rede so falha na segunda.

`contrato-v1.json` e regravado por `scripts/gravar_contrato.py`, que e um ato
deliberado -- nunca "para o teste passar".
"""

import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app
from domain.contract_shape import PUBLIC_ROUTES, breaking_changes, field_paths
from domain.disease_sources import DISEASE_SOURCES
from tests import PREMIUM_KEY, ensure_real_report

RAIZ = Path(__file__).resolve().parent.parent
CONTRATO = RAIZ / "contrato-v1.json"


class TestOContratoV1NaoEncolhe(unittest.TestCase):
    forma_atual: dict[str, list[str]] = {}
    gravado: dict[str, list[str]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        ensure_real_report()
        cls.gravado = json.loads(CONTRATO.read_text(encoding="utf-8"))["rotas"]
        codigos = set(DISEASE_SOURCES)
        with TestClient(app.app) as cliente:
            for rota in PUBLIC_ROUTES:
                app.rate_limiter.reset()
                resposta = cliente.get(rota, headers={"X-API-Key": PREMIUM_KEY})
                assert resposta.status_code < 400, (
                    f"{rota} respondeu {resposta.status_code}"
                )
                cls.forma_atual[rota] = sorted(
                    field_paths(resposta.json(), codigos)
                )

    def test_nenhum_campo_do_contrato_desapareceu(self) -> None:
        perdas = breaking_changes(self.gravado, self.forma_atual)
        self.assertEqual(
            perdas,
            {},
            "campo removido do contrato publico. Se foi de proposito, regrave "
            "com `python scripts/gravar_contrato.py` e registre a depreciacao; "
            "se nao foi, isto e a quebra que a rede existe para pegar",
        )

    def test_toda_rota_sob_contrato_esta_gravada(self) -> None:
        """Acrescentar rota a PUBLIC_ROUTES sem gravar deixaria um vao."""
        self.assertEqual(set(PUBLIC_ROUTES), set(self.gravado))

    def test_o_contrato_gravado_nao_esta_vazio(self) -> None:
        """Truncar o arquivo faria a rede aprovar qualquer coisa."""
        total = sum(len(campos) for campos in self.gravado.values())
        self.assertGreater(total, 500, f"contrato com apenas {total} campos")

    def test_campos_novos_nao_sao_quebra(self) -> None:
        """A assimetria, escrita como assercao para nao se perder."""
        antes = {"/x": ["a"]}
        depois = {"/x": ["a", "b"]}
        self.assertEqual(breaking_changes(antes, depois), {})
        self.assertEqual(
            breaking_changes(depois, antes), {"/x": ["b"]}
        )

    def test_endpoint_inteiro_sumindo_e_quebra(self) -> None:
        self.assertIn("/x", breaking_changes({"/x": ["a"]}, {}))


if __name__ == "__main__":
    unittest.main()
