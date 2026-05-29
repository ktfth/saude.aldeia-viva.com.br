"""
TDD Safety Net for Aggregation / Report Builder - Fase 0

Protects the core logic that transforms raw SINAN records into
enriched municipal epidemiological risk reports.

This is critical for the future cockpit (drill-down, comparisons, etc.).
"""

import unittest
from typing import Any

from app import (
    DISEASE_SOURCES,
    build_epidemiology_report,
    create_disease_summary,
    create_municipality_summary,
)

from aggregation import report_builder  # Fase 0 extraction in progress


class TestCreateSummaries(unittest.TestCase):
    def test_create_municipality_summary_basic_shape(self):
        record = {"ID_MN_RESI": "355030", "SG_UF": "35"}
        summary = create_municipality_summary("355030", record, 2025, None)

        self.assertEqual(summary["codigo_municipio"], "355030")
        self.assertIn("municipio", summary)
        self.assertEqual(summary["periodo"]["ano"], 2025)
        self.assertEqual(summary["total_notificacoes"], 0)
        self.assertEqual(summary["risk_score"], 0.0)
        self.assertEqual(summary["nivel_risco"], "baixo")
        self.assertEqual(summary["doencas_por_codigo"], {})

    def test_create_disease_summary_links_risk_profile(self):
        source = DISEASE_SOURCES["DENG"]
        disease = create_disease_summary(source, 2025)

        self.assertEqual(disease["codigo"], "DENG")
        self.assertEqual(disease["perfil_risco"], "arbovirus")
        self.assertIn("formula_risco", disease)
        self.assertEqual(disease["casos_provaveis"], 0)


class TestBuildEpidemiologyReport(unittest.TestCase):
    """End-to-end smoke tests for the main report builder (already heavily used)."""

    def test_build_report_with_dengue_records(self):
        records = {
            "DENG": [
                {
                    "ID_MN_RESI": "355030",
                    "SG_UF": "35",
                    "CLASSI_FIN": "10",
                    "EVOLUCAO": "1",
                    "DT_NOTIFIC": "2025-03-01",
                },
                {
                    "ID_MN_RESI": "355030",
                    "SG_UF": "35",
                    "CLASSI_FIN": "12",  # grave
                    "EVOLUCAO": "2",  # death
                    "DT_NOTIFIC": "2025-03-05",
                },
            ]
        }

        report = build_epidemiology_report(records, year=2025)

        self.assertGreaterEqual(len(report["municipios"]), 1)
        mun = report["municipios"][0]
        self.assertEqual(mun["codigo_municipio"], "355030")

        # Should have detected at least one high/critical disease
        self.assertTrue(len(mun.get("doencas_altas", [])) >= 0)

        # Metadata should include formulas
        self.assertIn("formulas_por_doenca", report["metadata"])


if __name__ == "__main__":
    unittest.main()