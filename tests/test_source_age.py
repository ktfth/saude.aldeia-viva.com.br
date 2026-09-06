"""
Terceiro relógio: a idade da FONTE por agravo.

Medido no relatório real em 2026-09-05: o relatório rotulado "2026" é uma
colcha de anos. `metadata.fontes[].ano` mostra Dengue/Chikungunya/Zika=2026,
Febre Amarela=2025, Leptospirose=2024, Botulismo/Toxoplasmose/Hanseníase=2023
e Meningite=2022 — mas `report_builder.py:100` carimba `periodo.ano = 2026`
em todos, e o `risk_score` municipal soma cinco anos-fonte diferentes.

Consequência medida: classificar recência contra um horizonte global fazia
100% da Meningite (fonte 2022) parecer "fóssil" e 100% da Dengue (fonte 2026)
parecer "viva" — 1.633 contra 1.238 alertas. Isso não descreve município
nenhum: descreve qual arquivo o sistema baixou.

A correção: cada agravo é medido contra o horizonte da SUA PRÓPRIA fonte, e a
idade da fonte é publicada como fato separado.
"""

import unittest
from datetime import date

from aggregation.recency_enrichment import (
    describe_source,
    disease_horizons,
    enrich_report,
)

TODAY = date(2026, 9, 5)


def _report():
    """Réplica reduzida do defeito real: Dengue de 2026, Meningite de 2022."""
    return {
        "municipios": [
            {
                "codigo_municipio": "355030",
                "municipio": "São Paulo",
                "estado": "SP",
                "doencas": [
                    {
                        "codigo": "DENG",
                        "nome": "Dengue",
                        "ultima_notificacao": "2026-04-21",
                    },
                    {
                        "codigo": "MENI",
                        "nome": "Meningite",
                        "ultima_notificacao": "2022-12-30",
                    },
                ],
                "doencas_altas": [],
            },
            {
                "codigo_municipio": "330455",
                "municipio": "Rio de Janeiro",
                "estado": "RJ",
                "doencas": [
                    {
                        "codigo": "DENG",
                        "nome": "Dengue",
                        "ultima_notificacao": "2025-11-02",
                    },
                    {
                        "codigo": "MENI",
                        "nome": "Meningite",
                        "ultima_notificacao": "2022-12-31",
                    },
                ],
                "doencas_altas": [],
            },
        ],
        "alertas_altos": [
            {"codigo_doenca": "MENI", "doenca": "Meningite", "ultima_notificacao": "2022-12-30"},
            {"codigo_doenca": "DENG", "doenca": "Dengue", "ultima_notificacao": "2026-04-21"},
        ],
        "metadata": {"carregado_em": "2026-04-26T00:00:00+00:00"},
    }


class TestDiseaseHorizons(unittest.TestCase):
    def test_one_horizon_per_disease_code(self) -> None:
        got = disease_horizons(_report())
        self.assertEqual(got["DENG"], date(2026, 4, 21))
        self.assertEqual(got["MENI"], date(2022, 12, 31))

    def test_empty_report(self) -> None:
        self.assertEqual(disease_horizons({}), {})


class TestDescribeSource(unittest.TestCase):
    def test_recent_source(self) -> None:
        got = describe_source(date(2026, 4, 21), TODAY)
        self.assertEqual(got["ano"], 2026)
        self.assertEqual(got["idade_dias"], 137)
        self.assertTrue(got["do_ano_corrente"])

    def test_old_source_is_flagged(self) -> None:
        got = describe_source(date(2022, 12, 31), TODAY)
        self.assertEqual(got["ano"], 2022)
        self.assertFalse(got["do_ano_corrente"])
        self.assertEqual(got["rotulo"], "há 3 anos")

    def test_missing_source(self) -> None:
        got = describe_source(None, TODAY)
        self.assertIsNone(got["ano"])
        self.assertFalse(got["do_ano_corrente"])


class TestPerDiseaseRecency(unittest.TestCase):
    """O coração da correção."""

    def test_meningite_is_live_within_its_own_2022_source(self) -> None:
        """SP notificou meningite até o fim do arquivo de 2022. Isso é 'vivo'
        dentro da fonte — jamais 'o município parou de notificar'."""
        got = enrich_report(_report(), TODAY)
        sp = got["municipios"][0]
        meni = next(d for d in sp["doencas"] if d["codigo"] == "MENI")
        self.assertEqual(meni["recencia"]["frescor"], "vivo")

    def test_source_age_is_published_alongside(self) -> None:
        got = enrich_report(_report(), TODAY)
        meni = next(d for d in got["municipios"][0]["doencas"] if d["codigo"] == "MENI")
        self.assertEqual(meni["fonte"]["ano"], 2022)
        self.assertFalse(meni["fonte"]["do_ano_corrente"])

    def test_a_municipality_lagging_inside_a_current_source_is_visible(self) -> None:
        """RJ notificou dengue pela última vez 170 dias antes do fim da fonte
        de 2026. Esse é o único caso em que 'parou de notificar' é verdade."""
        got = enrich_report(_report(), TODAY)
        rj = got["municipios"][1]
        deng = next(d for d in rj["doencas"] if d["codigo"] == "DENG")
        self.assertEqual(deng["recencia"]["frescor"], "dormente")
        self.assertTrue(deng["fonte"]["do_ano_corrente"])

    def test_alerts_use_their_own_disease_horizon(self) -> None:
        got = enrich_report(_report(), TODAY)
        meni_alert = got["alertas_altos"][0]
        self.assertEqual(meni_alert["recencia"]["frescor"], "vivo")
        self.assertEqual(meni_alert["fonte"]["ano"], 2022)

    def test_metadata_reports_source_year_spread(self) -> None:
        rec = enrich_report(_report(), TODAY)["metadata"]["recencia"]
        self.assertEqual(rec["fontes_por_ano"], {"2022": 1, "2026": 1})
        self.assertEqual(rec["agravos_com_fonte_do_ano_corrente"], 1)
        self.assertEqual(rec["agravos_total"], 2)

    def test_municipality_temporal_coverage(self) -> None:
        sp = enrich_report(_report(), TODAY)["municipios"][0]
        self.assertEqual(sp["agravos_com_fonte_atual"], 1)
        self.assertEqual(sp["agravos_total"], 2)


if __name__ == "__main__":
    unittest.main()


class TestCurrentSourceRiskLevel(unittest.TestCase):
    """O nível que responde "devo agir hoje?".

    `nivel_risco` consolida os agravos de todos os anos-fonte: mede gravidade
    histórica e, medido no dado real, deixa 23,5% dos 5.339 municípios em
    "crítico". `nivel_risco_fonte_atual` olha só os agravos cujo arquivo é do
    ano corrente e cai para 8,8% — porque separa gravidade de cobertura em
    vez de misturar as duas num indicador só.

    Os dois convivem de propósito. Um município crítico apenas por um arquivo
    de 2022 continua sendo crítico na história e não é ação para hoje.
    """

    TODAY = date(2026, 9, 5)

    def _report(self):
        return {
            "municipios": [
                {
                    "codigo_municipio": "355030",
                    "municipio": "São Paulo",
                    "estado": "SP",
                    "nivel_risco": "critico",
                    "doencas": [
                        {
                            "codigo": "MENI",
                            "nome": "Meningite",
                            "nivel_risco": "critico",
                            "ultima_notificacao": "2022-12-30",
                        },
                        {
                            "codigo": "DENG",
                            "nome": "Dengue",
                            "nivel_risco": "moderado",
                            "ultima_notificacao": "2026-04-21",
                        },
                    ],
                    "doencas_altas": [],
                }
            ],
            "alertas_altos": [],
            "metadata": {},
        }

    def test_current_source_level_ignores_old_source_diseases(self) -> None:
        got = enrich_report(self._report(), self.TODAY)["municipios"][0]
        self.assertEqual(got["nivel_risco"], "critico")
        self.assertEqual(got["nivel_risco_fonte_atual"], "moderado")

    def test_declares_when_history_is_worse_than_the_present(self) -> None:
        """A UI precisa saber quando o histórico é pior, para não esconder."""
        got = enrich_report(self._report(), self.TODAY)["municipios"][0]
        self.assertTrue(got["historico_mais_grave"])

    def test_no_flag_when_both_agree(self) -> None:
        report = self._report()
        report["municipios"][0]["doencas"][0]["ultima_notificacao"] = "2026-04-20"
        got = enrich_report(report, self.TODAY)["municipios"][0]
        self.assertEqual(got["nivel_risco_fonte_atual"], "critico")
        self.assertFalse(got["historico_mais_grave"])

    def test_municipality_without_current_sources_is_baixo(self) -> None:
        report = self._report()
        report["municipios"][0]["doencas"] = [
            {
                "codigo": "MENI",
                "nome": "Meningite",
                "nivel_risco": "critico",
                "ultima_notificacao": "2022-12-30",
            }
        ]
        got = enrich_report(report, self.TODAY)["municipios"][0]
        self.assertEqual(got["nivel_risco_fonte_atual"], "baixo")
        self.assertEqual(got["agravos_com_fonte_atual"], 0)

    def test_metadata_reports_both_distributions(self) -> None:
        rec = enrich_report(self._report(), self.TODAY)["metadata"]["recencia"]
        self.assertEqual(rec["niveis_fonte_atual"]["moderado"], 1)
        self.assertEqual(rec["niveis_fonte_atual"]["critico"], 0)
        self.assertEqual(rec["municipios_com_historico_mais_grave"], 1)
