"""
Semana epidemiológica brasileira, e a direção que o número toma.

O produto respondia *quanto* e não *subindo ou descendo*. Todo campo era total
acumulado. Decisão de alocação se toma pela direção.

Por que não a semana ISO: a SE do Ministério da Saúde começa no DOMINGO e a
SE 1 é a que termina no primeiro sábado de janeiro com quatro ou mais dias no
ano novo. A ISO começa na segunda e ancora na primeira quinta. Usar ISO aqui
deslocaria os números em uma semana contra o boletim da secretaria — erro que
ninguém confere e todo mundo repassa.

Medido na fonte antes de construir: `DT_NOTIFIC` e `DT_SIN_PRI` preenchidas em
100% dos registros de 2026, cobrindo 34 a 35 semanas com 8 a 10 dias de
defasagem.
"""

import unittest
from datetime import date

from domain.epi_week import (
    DESCENDO,
    LIMIAR_DIRECAO,
    P_MAXIMO_ACASO,
    SEMANAS_PROVISORIAS,
    ESTAVEL,
    INDETERMINADA,
    MINIMO_PARA_TENDENCIA,
    SUBINDO,
    rotulo_semana,
    semana_epidemiologica,
    serie_por_semana,
    tendencia,
)


class TestAFronteiraDaSemana(unittest.TestCase):
    """As bordas são onde a convenção brasileira difere da ISO."""

    def test_a_se1_comeca_no_domingo(self) -> None:
        # 2026: primeiro sábado é 3/jan, com menos de 4 dias no ano novo,
        # então a SE 1 só fecha em 10/jan e abre no domingo 4.
        self.assertEqual(semana_epidemiologica(date(2026, 1, 4)), (2026, 1))
        self.assertEqual(semana_epidemiologica(date(2026, 1, 10)), (2026, 1))

    def test_o_primeiro_de_janeiro_pode_ser_do_ano_anterior(self) -> None:
        self.assertEqual(semana_epidemiologica(date(2026, 1, 1)), (2025, 53))
        self.assertEqual(semana_epidemiologica(date(2026, 1, 3)), (2025, 53))

    def test_dezembro_pode_abrir_o_ano_seguinte(self) -> None:
        """2025 abre em 29/dez/2024 — é assim que o boletim conta."""
        self.assertEqual(semana_epidemiologica(date(2024, 12, 29)), (2025, 1))

    def test_nao_e_a_semana_iso(self) -> None:
        """Datas em que as duas convenções divergem, por construção.

        A primeira versão deste teste exigia divergência em 2026-01-04
        também, e reprovou: naquele domingo a SE e a ISO coincidem
        legitimamente. Coincidir em UM dia não é sinal de nada; o que
        distingue as convenções são as bordas abaixo.
        """
        for dia, esperado_se in (
            (date(2026, 1, 1), (2025, 53)),   # ISO diria 2026-W01
            (date(2024, 12, 29), (2025, 1)),  # ISO diria 2024-W52
        ):
            with self.subTest(dia=dia):
                self.assertEqual(semana_epidemiologica(dia), esperado_se)
                self.assertNotEqual(
                    semana_epidemiologica(dia),
                    dia.isocalendar()[:2],
                    "virou semana ISO",
                )

    def test_o_rotulo_segue_a_forma_do_boletim(self) -> None:
        self.assertEqual(rotulo_semana((2026, 34)), "2026-SE34")
        self.assertEqual(rotulo_semana((2026, 1)), "2026-SE01")

    def test_semanas_consecutivas_avancam_de_um(self) -> None:
        base = date(2026, 1, 4)
        for n in range(1, 30):
            dia = date.fromordinal(base.toordinal() + 7 * (n - 1))
            self.assertEqual(semana_epidemiologica(dia), (2026, n))


class TestASerie(unittest.TestCase):
    def test_conta_por_semana_e_ordena_no_tempo(self) -> None:
        serie = serie_por_semana(
            [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 12)]
        )
        self.assertEqual(serie, {"2026-SE01": 2, "2026-SE02": 1})
        self.assertEqual(list(serie), sorted(serie))

    def test_ignora_datas_ausentes(self) -> None:
        self.assertEqual(serie_por_semana([date(2026, 1, 5), None]), {"2026-SE01": 1})

    def test_serie_vazia_nao_quebra(self) -> None:
        self.assertEqual(serie_por_semana([]), {})


class TestADirecao(unittest.TestCase):
    """A comparação entre janelas, isolada.

    Estes casos passam `provisorias=0` de propósito: aqui a variável sob
    teste é a razão entre as janelas. O descarte da cauda incompleta tem
    classe própria abaixo — misturar os dois faria cada teste medir duas
    coisas e não dizer qual quebrou.
    """

    def _serie(self, *contagens):
        return {f"2026-SE{n:02d}": c for n, c in enumerate(contagens, start=1)}

    def test_subindo(self) -> None:
        t = tendencia(self._serie(10, 10, 10, 10, 30, 30, 30, 30), provisorias=0)
        self.assertEqual(t["direcao"], SUBINDO)
        self.assertAlmostEqual(t["variacao"], 2.0)

    def test_descendo(self) -> None:
        t = tendencia(self._serie(30, 30, 30, 30, 10, 10, 10, 10), provisorias=0)
        self.assertEqual(t["direcao"], DESCENDO)

    def test_estavel_dentro_do_limiar(self) -> None:
        t = tendencia(self._serie(20, 20, 20, 20, 21, 21, 21, 21), provisorias=0)
        self.assertEqual(t["direcao"], ESTAVEL)

    def test_volume_baixo_e_indeterminado_e_nao_estavel(self) -> None:
        """A diferença importa: uma fala da epidemia, a outra do dado."""
        t = tendencia(self._serie(1, 0, 1, 0, 1, 1, 0, 1), provisorias=0)
        self.assertEqual(t["direcao"], INDETERMINADA)
        self.assertIsNone(t["variacao"])
        self.assertIn(str(MINIMO_PARA_TENDENCIA), t["motivo"])

    def test_dois_virando_tres_nao_e_alta_de_50_por_cento(self) -> None:
        """O ruído que o mínimo existe para barrar."""
        t = tendencia(self._serie(0, 0, 0, 2, 0, 0, 0, 3), provisorias=0)
        self.assertEqual(t["direcao"], INDETERMINADA)

    def test_sem_janela_anterior_e_indeterminado(self) -> None:
        t = tendencia(self._serie(50, 50), provisorias=0)
        self.assertEqual(t["direcao"], INDETERMINADA)

    def test_a_janela_anterior_zerada_com_volume_e_subida(self) -> None:
        t = tendencia(self._serie(0, 0, 0, 0, 40, 40, 40, 40), provisorias=0)
        self.assertEqual(t["direcao"], SUBINDO)
        self.assertIsNone(t["variacao"], "razão por zero não é número")

    def test_o_motivo_so_existe_quando_indeterminado(self) -> None:
        self.assertIsNone(tendencia(self._serie(*[20] * 8), provisorias=0)["motivo"])


class TestACaudaIncompletaNaoViraQueda(unittest.TestCase):
    """Toda curva por início de sintomas termina baixa, e não é queda.

    As notificações das últimas semanas ainda não chegaram. Medido em 124.169
    registros de Chikungunya de 2026: 84,0% das notificações chegam em uma
    semana, 93,4% em duas, 96,0% em três — mediana de 3 dias, p90 de 11.

    Sem excluir a cauda, o artefato enviesa TODA tendência para baixo. Medido
    na primeira versão desta implementação: 1.631 municípios "descendo" contra
    1.038 "subindo", e a curva nacional de dengue caindo de 8.537 para 3.706
    na última semana sem que nada tivesse acontecido.
    """

    def _serie(self, *contagens):
        return {f"2026-SE{n:02d}": c for n, c in enumerate(contagens, start=1)}

    def test_serie_estavel_com_cauda_incompleta_nao_e_queda(self) -> None:
        serie = self._serie(30, 30, 30, 30, 30, 30, 30, 30, 12, 4)
        self.assertEqual(tendencia(serie, provisorias=0)["direcao"], DESCENDO)
        self.assertEqual(tendencia(serie)["direcao"], ESTAVEL)

    def test_uma_queda_real_continua_sendo_queda(self) -> None:
        """Excluir a cauda não pode cegar o produto para a queda de verdade."""
        serie = self._serie(40, 40, 40, 40, 10, 10, 10, 10, 4, 1)
        self.assertEqual(tendencia(serie)["direcao"], DESCENDO)

    def test_uma_alta_real_continua_sendo_alta(self) -> None:
        serie = self._serie(10, 10, 10, 10, 40, 40, 40, 40, 15, 5)
        self.assertEqual(tendencia(serie)["direcao"], SUBINDO)

    def test_o_quanto_foi_ignorado_e_declarado(self) -> None:
        t = tendencia(self._serie(*[20] * 10))
        self.assertEqual(t["semanas_provisorias_ignoradas"], SEMANAS_PROVISORIAS)

    def test_o_corte_cobre_a_maior_parte_do_atraso(self) -> None:
        """Duas semanas deixam 6,6% por chegar; uma deixaria 16%."""
        self.assertGreaterEqual(SEMANAS_PROVISORIAS, 2)


class TestDirecaoExigeRuidoEMagnitude(unittest.TestCase):
    """Duas condições, porque os erros são opostos.

    Medido no relatório real, com o limiar de magnitude sozinho:

      Botulismo   4 casos contra 7  ->  -43%, anunciado como "descendo".
                  Onze casos no país inteiro; é ruído com cara de direção.
      Dengue      36.018 contra 37.887 -> -5%, diferença inquestionável em
                  74 mil casos, e que não muda decisão nenhuma.

    Um limiar de magnitude pega o segundo e deixa passar o primeiro. Um teste
    de significância pega o primeiro e deixa passar o segundo — em n grande,
    tudo é significativo. Só é direção o que passa nos dois.
    """

    def _janelas(self, anterior, recente):
        return {
            "2026-SE01": 0, "2026-SE02": 0, "2026-SE03": 0, "2026-SE04": anterior,
            "2026-SE05": 0, "2026-SE06": 0, "2026-SE07": 0, "2026-SE08": recente,
        }

    def test_magnitude_grande_sem_significancia_e_estavel(self) -> None:
        t = tendencia(self._janelas(7, 4), provisorias=0)
        self.assertEqual(t["direcao"], ESTAVEL)
        self.assertLess(t["variacao"], -LIMIAR_DIRECAO)
        self.assertTrue(t["compativel_com_acaso"])

    def test_significancia_sem_magnitude_e_estavel(self) -> None:
        t = tendencia(self._janelas(37887, 36018), provisorias=0)
        self.assertEqual(t["direcao"], ESTAVEL)
        self.assertFalse(t["compativel_com_acaso"], "74 mil casos: não é acaso")

    def test_com_as_duas_e_direcao(self) -> None:
        for anterior, recente, esperado in ((1309, 703, DESCENDO), (703, 1309, SUBINDO)):
            with self.subTest(anterior=anterior):
                t = tendencia(self._janelas(anterior, recente), provisorias=0)
                self.assertEqual(t["direcao"], esperado)
                self.assertFalse(t["compativel_com_acaso"])

    def test_o_limite_de_acaso_esta_declarado(self) -> None:
        self.assertGreater(P_MAXIMO_ACASO, 0)
        self.assertLess(P_MAXIMO_ACASO, 0.5)

    def test_metade_exata_e_sempre_acaso(self) -> None:
        t = tendencia(self._janelas(500, 500), provisorias=0)
        self.assertTrue(t["compativel_com_acaso"])
        self.assertEqual(t["direcao"], ESTAVEL)


class TestASerieChegaEmOrdem(unittest.TestCase):
    """A acumulação segue a ordem dos registros, que não é a do tempo."""

    def test_a_serie_do_municipio_sai_ordenada(self) -> None:
        from aggregation.report_builder import (
            add_record_to_summaries,
            finalize_municipality_rows,
        )
        from domain.disease_sources import DISEASE_SOURCES

        fonte = DISEASE_SOURCES["DENG"]
        municipio = {
            "codigo_municipio": "1", "municipio": "X", "estado": "GO",
            "total_notificacoes": 0, "doencas_por_codigo": {},
        }
        from aggregation.report_builder import create_disease_summary

        doenca = create_disease_summary(fonte, 2026, 2026)
        municipio["doencas_por_codigo"]["DENG"] = doenca
        # Fora de ordem de propósito, como vêm do arquivo.
        for dia in ("2026-05-04", "2026-01-05", "2026-08-10", "2026-03-02"):
            for _ in range(4):
                add_record_to_summaries(
                    doenca, municipio, fonte,
                    {"DT_SIN_PRI": dia, "CLASSI_FIN": "1"},
                )

        (linha,) = finalize_municipality_rows([municipio])
        serie = linha["doencas"][0]["serie_semanal"]
        self.assertEqual(list(serie), sorted(serie))
        self.assertGreater(len(serie), 1, "a série foi podada e o teste vira vazio")


if __name__ == "__main__":
    unittest.main()
