"""
Enriquecimento de recência aplicado ao relatório inteiro.

Este é o ponto onde a recência deixa de ser função pura e passa a ser
contrato: todo município, todo agravo e todo alerta servido pela API carrega
a idade do seu sinal.
"""

import unittest
from datetime import date

from aggregation.recency_enrichment import (
    enrich_alert,
    enrich_municipality,
    enrich_report,
    freshness_distribution,
)


REF = date(2026, 9, 5)

# Horizonte por agravo usado pelos testes desta suíte. Ver test_source_age.py
# para a razão de o frescor ser medido contra a fonte de cada agravo.
HORIZONS = {"MENI": date(2022, 12, 30), "DENG": date(2026, 8, 30)}


def enrich_one(municipality, reference=REF, horizons=None):
    return enrich_municipality(municipality, horizons or HORIZONS, reference, reference)


def enrich_one_alert(alert, reference=REF, horizons=None):
    return enrich_alert(alert, horizons or HORIZONS, reference, reference)


def _municipality(**overrides):
    base = {
        "codigo_municipio": "355030",
        "municipio": "São Paulo",
        "estado": "SP",
        "total_casos_provaveis": 10225,
        "doencas": [
            {"codigo": "MENI", "nome": "Meningite", "ultima_notificacao": "2022-12-30"},
            {"codigo": "DENG", "nome": "Dengue", "ultima_notificacao": "2026-08-30"},
        ],
        "doencas_altas": [
            {"codigo": "MENI", "nome": "Meningite", "ultima_notificacao": "2022-12-30"},
        ],
    }
    base.update(overrides)
    return base


class TestEnrichMunicipality(unittest.TestCase):
    def test_every_disease_gets_recency(self) -> None:
        got = enrich_one(_municipality())
        for disease in got["doencas"]:
            self.assertIn("recencia", disease)

    def test_doencas_altas_also_enriched(self) -> None:
        got = enrich_one(_municipality())
        self.assertEqual(got["doencas_altas"][0]["recencia"]["frescor"], "vivo")
        self.assertEqual(got["doencas_altas"][0]["fonte"]["ano"], 2022)

    def test_municipality_recency_uses_freshest_disease(self) -> None:
        """O município herda o sinal mais vivo que possui — é ele que aciona."""
        got = enrich_one(_municipality())
        self.assertEqual(got["recencia"]["frescor"], "vivo")
        self.assertEqual(got["recencia"]["data"], "2026-08-30")

    def test_municipality_without_diseases_is_unknown(self) -> None:
        got = enrich_one(_municipality(doencas=[], doencas_altas=[]))
        self.assertEqual(got["recencia"]["frescor"], "desconhecido")

    def test_does_not_mutate_input(self) -> None:
        original = _municipality()
        enrich_one(original)
        self.assertNotIn("recencia", original)
        self.assertNotIn("recencia", original["doencas"][0])

    def test_counts_live_diseases_and_current_sources(self) -> None:
        """Ambos os agravos notificaram até o fim da SUA fonte: 2 sinais vivos.
        Mas só um deles tem fonte do ano corrente — é essa contagem que diz
        quanto do painel do município descreve o presente."""
        got = enrich_one(_municipality())
        self.assertEqual(got["agravos_com_sinal_vivo"], 2)
        self.assertEqual(got["agravos_com_fonte_atual"], 1)
        self.assertEqual(got["agravos_total"], 2)


class TestEnrichAlert(unittest.TestCase):
    def test_alert_gets_recency_against_its_own_source(self) -> None:
        """Meningite notificada até o fim da fonte de 2022: viva NA FONTE.
        A idade da fonte é publicada à parte, em `fonte`."""
        got = enrich_one_alert(
            {"codigo_doenca": "MENI", "doenca": "Meningite",
             "ultima_notificacao": "2022-12-30"}
        )
        self.assertEqual(got["recencia"]["frescor"], "vivo")
        self.assertEqual(got["fonte"]["ano"], 2022)
        self.assertFalse(got["fonte"]["do_ano_corrente"])

    def test_does_not_mutate_input(self) -> None:
        original = {"codigo_doenca": "DENG", "doenca": "Dengue",
                    "ultima_notificacao": "2026-09-01"}
        enrich_one_alert(original)
        self.assertNotIn("recencia", original)


class TestFreshnessDistribution(unittest.TestCase):
    def test_counts_each_band(self) -> None:
        alerts = [
            {"recencia": {"frescor": "vivo"}},
            {"recencia": {"frescor": "vivo"}},
            {"recencia": {"frescor": "fossil"}},
        ]
        got = freshness_distribution(alerts)
        self.assertEqual(got["vivo"], 2)
        self.assertEqual(got["fossil"], 1)
        self.assertEqual(got["esfriando"], 0)

    def test_empty_input(self) -> None:
        got = freshness_distribution([])
        self.assertEqual(sum(got.values()), 0)


class TestEnrichReport(unittest.TestCase):
    def test_enriches_both_collections_and_records_reference(self) -> None:
        report = {
            "municipios": [_municipality()],
            "alertas_altos": [
                {"doenca": "Meningite", "ultima_notificacao": "2022-12-30"}
            ],
            "metadata": {"status": "ok"},
        }
        got = enrich_report(report, REF)
        self.assertEqual(got["municipios"][0]["recencia"]["frescor"], "vivo")
        self.assertEqual(got["alertas_altos"][0]["recencia"]["frescor"], "fossil")
        rec = got["metadata"]["recencia"]
        # A referência do sinal é o horizonte do dado, não a data de hoje.
        self.assertEqual(rec["referencia"], "2026-08-30")
        self.assertEqual(rec["horizonte_dado"], "2026-08-30")
        self.assertEqual(rec["alertas_total"], 1)

    def test_tolerates_missing_collections(self) -> None:
        got = enrich_report({"metadata": {}}, REF)
        self.assertEqual(got["municipios"], [])
        self.assertEqual(got["alertas_altos"], [])

    def test_does_not_mutate_input_report(self) -> None:
        report = {"municipios": [_municipality()], "alertas_altos": [], "metadata": {}}
        enrich_report(report, REF)
        self.assertNotIn("recencia", report["municipios"][0])
        self.assertNotIn("recencia", report["metadata"])


if __name__ == "__main__":
    unittest.main()


class TestTwoClocks(unittest.TestCase):
    """A idade da CARGA e a idade do SINAL são falhas diferentes.

    Medido no dado real em 2026-09-05: a última notificação de todo o dataset
    era 2026-04-22 e `carregado_em` era 2026-04-26. Nenhum município tinha
    "esfriado" — o pipeline é que não rodava havia 132 dias. Medir o sinal
    contra hoje transformava uma falha operacional do serviço em um falso
    diagnóstico epidemiológico sobre 5.339 municípios.
    """

    TODAY = date(2026, 9, 5)

    def _report(self):
        return {
            "municipios": [_municipality()],
            "alertas_altos": [
                {"codigo_doenca": "DENG", "doenca": "Dengue",
                 "ultima_notificacao": "2026-08-30"},
                {"codigo_doenca": "MENI", "doenca": "Meningite",
                 "ultima_notificacao": "2022-12-30"},
            ],
            "metadata": {"status": "ok", "carregado_em": "2026-08-31T00:00:00+00:00"},
        }

    def test_horizon_is_detected_from_the_data(self) -> None:
        got = enrich_report(self._report(), self.TODAY)
        self.assertEqual(got["metadata"]["recencia"]["horizonte_dado"], "2026-08-30")

    def test_signal_age_is_measured_against_the_horizon_not_today(self) -> None:
        """Um agravo que notificou até o fim da fonte está vivo NA FONTE."""
        got = enrich_report(self._report(), self.TODAY)
        dengue = got["alertas_altos"][0]
        self.assertEqual(dengue["recencia"]["idade_dias"], 0)
        self.assertEqual(dengue["recencia"]["frescor"], "vivo")

    def test_each_alert_is_measured_against_its_own_source(self) -> None:
        got = enrich_report(self._report(), self.TODAY)
        meningite = got["alertas_altos"][1]
        self.assertEqual(meningite["recencia"]["frescor"], "vivo")
        self.assertEqual(meningite["fonte"]["ano"], 2022)

    def test_load_age_is_reported_separately(self) -> None:
        carga = enrich_report(self._report(), self.TODAY)["metadata"]["carga"]
        self.assertEqual(carga["idade_dias"], 5)
        self.assertEqual(carga["defasagem_dias"], 6)
        self.assertEqual(carga["referencia"], "2026-09-05")
        # 5 dias ainda descreve o presente para dado do SINAN.
        self.assertTrue(carga["atualizada"])
        self.assertIsNone(carga["aviso"])

    def test_stale_load_is_flagged_with_a_warning(self) -> None:
        """O caso real medido em produção: carga de abril lida em setembro."""
        report = self._report()
        report["metadata"]["carregado_em"] = "2026-04-26T02:06:07+00:00"
        carga = enrich_report(report, self.TODAY)["metadata"]["carga"]
        self.assertEqual(carga["idade_dias"], 132)
        self.assertFalse(carga["atualizada"])
        self.assertIn("não é atualizada", carga["aviso"])

    def test_fresh_load_is_flagged_as_up_to_date(self) -> None:
        report = self._report()
        report["metadata"]["carregado_em"] = "2026-09-05T00:00:00+00:00"
        carga = enrich_report(report, self.TODAY)["metadata"]["carga"]
        self.assertEqual(carga["idade_dias"], 0)
        self.assertTrue(carga["atualizada"])

    def test_explicit_horizon_wins_over_detection(self) -> None:
        got = enrich_report(self._report(), self.TODAY, horizon=date(2026, 9, 5))
        # `horizonte_dado` continua sendo o que o dado contém de fato;
        # o override muda apenas a referência efetiva de medição.
        self.assertEqual(got["metadata"]["recencia"]["horizonte_dado"], "2026-08-30")
        self.assertEqual(got["metadata"]["recencia"]["referencia"], "2026-09-05")

    def test_empty_report_has_no_horizon_but_still_reports_load(self) -> None:
        got = enrich_report({"metadata": {}}, self.TODAY)
        self.assertIsNone(got["metadata"]["recencia"]["horizonte_dado"])
        self.assertIsNone(got["metadata"]["carga"]["idade_dias"])
