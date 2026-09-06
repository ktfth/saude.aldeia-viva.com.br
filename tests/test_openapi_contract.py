"""
O schema tem que dizer o que o serviço faz.

Este projeto trata agentes como usuários de primeira classe, e para um agente
`/openapi.json` não é documentação — é o contrato. Um humano que lê "Gera
relatório PDF" e recebe 501 entende; um agente planeja um download, registra
a etapa como possível e quebra adiante.

Medido antes desta rede: nove endpoints, um mentindo.

    GET /v1/export/pdf   devolve 501   declarava 200, 422

O runtime já era honesto — o endpoint levanta 501 apontando para
`/v1/professional-report` desde a iteração que removeu o "Relatório PDF
gerado com sucesso" com uma `download_url` para uma rota inexistente. E
`docs/api-referencia.md` também dizia a verdade: "**Não implementado.**
Responde 501".

Só o schema seguia prometendo `200: "Arquivo PDF com análise
epidemiológica"`. Três autoridades sobre o mesmo fato, e a que os agentes
leem era a errada.

A rede é a própria medição: exercita cada operação publicada e exige que o
status devolvido esteja entre os declarados.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from tests import PREMIUM_KEY, ensure_real_report

# Operações que não podem ser exercitadas sem argumentos construídos à mão.
# Cada entrada precisa de motivo — a lista existe para não virar tapete.
NAO_EXERCITAVEIS: dict[str, str] = {}


class TestOSchemaDizOQueOServicoFaz(unittest.TestCase):
    """Cada operação é exercitada UMA vez, e as asserções leem o resultado.

    A primeira versão varria todos os endpoints em cada teste — 17s sozinha,
    num suite de 15s. `/v1/risk-index` e `/v1/professional-report` percorrem
    5.339 municípios; varrer duas vezes não acrescenta informação.
    """

    respostas: dict[str, int] = {}
    spec: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        with TestClient(app.app) as client:
            cls.spec = client.get("/openapi.json").json()
            # A recarga real é exercitada em test_cache_freshness; aqui o que
            # importa é a forma da resposta, não o trabalho pesado.
            with patch.object(app, "load_or_refresh_report", return_value=None):
                for caminho, metodo, _ in cls._operacoes():
                    chave = f"{metodo} {caminho}"
                    if chave in NAO_EXERCITAVEIS:
                        continue
                    app.rate_limiter.reset()
                    cls.respostas[chave] = client.request(
                        metodo, caminho, headers={"X-API-Key": PREMIUM_KEY}
                    ).status_code

    @classmethod
    def _operacoes(cls):
        for caminho, metodos in sorted(cls.spec["paths"].items()):
            for metodo, operacao in metodos.items():
                yield caminho, metodo.upper(), operacao

    def test_ha_operacoes_a_conferir(self) -> None:
        """Guarda contra a extração silenciosamente parar de achar rotas."""
        self.assertGreaterEqual(len(self.respostas), 8)

    def test_todo_endpoint_devolve_um_status_declarado(self) -> None:
        for caminho, metodo, operacao in self._operacoes():
            chave = f"{metodo} {caminho}"
            if chave not in self.respostas:
                continue
            with self.subTest(operacao=chave):
                declarados = sorted(operacao.get("responses", {}))
                self.assertIn(
                    str(self.respostas[chave]),
                    declarados,
                    f"{chave} devolve {self.respostas[chave]} e declara "
                    f"{declarados}",
                )

    def test_nenhuma_operacao_promete_sucesso_que_nao_entrega(self) -> None:
        """O caso do PDF, escrito como regra."""
        for caminho, metodo, operacao in self._operacoes():
            chave = f"{metodo} {caminho}"
            if self.respostas.get(chave, 0) < 400:
                continue
            with self.subTest(operacao=chave):
                self.assertNotIn(
                    "200",
                    operacao.get("responses", {}),
                    f"{chave} nunca responde 200 mas declara um",
                )

    def test_toda_isencao_tem_motivo(self) -> None:
        for chave, motivo in NAO_EXERCITAVEIS.items():
            self.assertTrue(motivo, f"isenção de {chave} sem motivo escrito")

    def test_o_resumo_do_pdf_nao_promete_pdf(self) -> None:
        """O defeito concreto, para a correção não ser desfeita sem falhar."""
        operacao = self.spec["paths"]["/v1/export/pdf"]["get"]
        self.assertIn("501", operacao["responses"])
        self.assertNotIn("200", operacao["responses"])
        self.assertIn("professional-report", operacao["summary"])


if __name__ == "__main__":
    unittest.main()
