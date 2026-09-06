"""
Uma carga parcial não pode substituir uma carga completa.

Observado ao vivo durante esta iteração, causado pela própria correção que
ligou a expiração da cache: uma recarga real disparou, as fontes CSV
(Dengue, Chikungunya, Zika) carregaram, as fontes DBC falharam por falta das
extensões nativas — e o resultado, com 4.462 municípios e 4 de 10 agravos,
sobrescreveu a cache que tinha 5.339 municípios e 10 agravos.

`report_has_content` só verificava `status == "ok"` e lista não vazia. Uma
carga que perdeu seis fontes passava nesse teste e degradava a base de forma
permanente, a cada tentativa de renovação num ambiente onde alguma fonte
esteja fora.

A regra: renovar nunca pode perder cobertura. Se a carga nova cobre menos
agravos que a cache existente, a cache permanece e a degradação é declarada.
"""

import unittest

from aggregation.cache_policy import (
    covered_sources,
    is_regression,
)


def _report(codigos, erros=()):
    return {
        "metadata": {
            "status": "ok",
            "fontes": [{"codigo": c, "nome": c, "registros": 10} for c in codigos],
            "erros": [{"fonte": e, "erro": "fonte fora"} for e in erros],
        },
        "municipios": [{"codigo_municipio": "355030"}],
    }


class TestCoveredSources(unittest.TestCase):
    def test_counts_sources_with_records(self) -> None:
        self.assertEqual(covered_sources(_report(["DENG", "CHIK"])), {"DENG", "CHIK"})

    def test_ignores_sources_without_records(self) -> None:
        report = _report(["DENG"])
        report["metadata"]["fontes"].append({"codigo": "MENI", "registros": 0})
        self.assertEqual(covered_sources(report), {"DENG"})

    def test_empty_report(self) -> None:
        self.assertEqual(covered_sources({}), set())
        self.assertEqual(covered_sources({"metadata": {}}), set())


class TestRegressionDetection(unittest.TestCase):
    def test_losing_a_source_is_a_regression(self) -> None:
        """O caso real: CSV carregou, DBC falhou."""
        anterior = _report(["DENG", "CHIK", "ZIKA", "LEPT", "MENI", "HANS"])
        nova = _report(["DENG", "CHIK", "ZIKA"], erros=["LEPT", "MENI", "HANS"])
        self.assertTrue(is_regression(nova, anterior))

    def test_same_coverage_is_not_a_regression(self) -> None:
        anterior = _report(["DENG", "CHIK"])
        nova = _report(["DENG", "CHIK"])
        self.assertFalse(is_regression(nova, anterior))

    def test_gaining_a_source_is_not_a_regression(self) -> None:
        anterior = _report(["DENG"])
        nova = _report(["DENG", "MENI"])
        self.assertFalse(is_regression(nova, anterior))

    def test_swapping_sources_counts_as_a_regression(self) -> None:
        """Cobertura igual em número, mas perdendo um agravo, é perda."""
        anterior = _report(["DENG", "MENI"])
        nova = _report(["DENG", "ZIKA"])
        self.assertTrue(is_regression(nova, anterior))

    def test_no_previous_report_is_never_a_regression(self) -> None:
        self.assertFalse(is_regression(_report(["DENG"]), None))
        self.assertFalse(is_regression(_report(["DENG"]), {}))

    def test_a_previous_report_without_sources_does_not_block(self) -> None:
        """Cache antiga sem metadados de fonte não pode travar a renovação."""
        self.assertFalse(is_regression(_report(["DENG"]), {"metadata": {}}))


class TestRefreshDoesNotDegradeTheBase(unittest.TestCase):
    """Comportamento de ponta a ponta."""

    def setUp(self) -> None:
        import os
        import app

        self.app = app
        self._previous = os.environ.get("SINAN_REPORT_CACHE_MAX_AGE_DAYS")
        os.environ["SINAN_REPORT_CACHE_MAX_AGE_DAYS"] = "7"

    def tearDown(self) -> None:
        import os

        if self._previous is None:
            os.environ.pop("SINAN_REPORT_CACHE_MAX_AGE_DAYS", None)
        else:
            os.environ["SINAN_REPORT_CACHE_MAX_AGE_DAYS"] = self._previous

    def _complete_cache(self, tmp):
        from pathlib import Path

        report = {
            "metadata": {
                "status": "ok",
                "carregado_em": "2020-01-01",
                "fontes": [
                    {"codigo": c, "nome": c, "registros": 10}
                    for c in ("DENG", "CHIK", "ZIKA", "LEPT", "MENI", "HANS")
                ],
                "erros": [],
            },
            "municipios": [
                {"codigo_municipio": str(500000 + i), "doencas": []} for i in range(50)
            ],
            "alertas_altos": [],
        }
        path = Path(tmp) / "cache.json"
        self.app.save_report_cache(report, path)
        return path

    def test_partial_refresh_keeps_the_complete_cache(self) -> None:
        import tempfile
        from unittest.mock import patch

        from tests import preserved_report_state

        partial = {
            "metadata": {
                "status": "ok",
                "carregado_em": "2026-09-06",
                "fontes": [
                    {"codigo": c, "nome": c, "registros": 10}
                    for c in ("DENG", "CHIK", "ZIKA")
                ],
                "erros": [{"fonte": "Meningite", "erro": "sem deps"}],
            },
            "municipios": [{"codigo_municipio": "355030", "doencas": []}],
            "alertas_altos": [],
        }

        with preserved_report_state(), tempfile.TemporaryDirectory() as tmp:
            path = self._complete_cache(tmp)
            with patch.object(
                self.app, "report_cache_path", return_value=path
            ), patch.object(
                self.app, "fetch_epidemiology_report", return_value=partial
            ):
                report = self.app.load_or_refresh_report(2026)

            # A cache completa, com 50 municipios, permanece.
            self.assertEqual(len(report["municipios"]), 50)
            carga = report["metadata"]["carga"]
            self.assertTrue(carga["cobertura_degradada"])
            # O aviso nomeia os agravos perdidos pelo codigo, que e estavel.
            for codigo in ("HANS", "LEPT", "MENI"):
                self.assertIn(codigo, carga["aviso_cobertura"])
            self.assertIn("base anterior foi mantida", carga["aviso_cobertura"])

    def test_complete_refresh_replaces_the_cache(self) -> None:
        import tempfile
        from unittest.mock import patch

        from tests import preserved_report_state

        complete = {
            "metadata": {
                "status": "ok",
                "carregado_em": "2026-09-06",
                "fontes": [
                    {"codigo": c, "nome": c, "registros": 10}
                    for c in ("DENG", "CHIK", "ZIKA", "LEPT", "MENI", "HANS")
                ],
                "erros": [],
            },
            "municipios": [{"codigo_municipio": "355030", "doencas": []}],
            "alertas_altos": [],
        }

        with preserved_report_state(), tempfile.TemporaryDirectory() as tmp:
            path = self._complete_cache(tmp)
            with patch.object(
                self.app, "report_cache_path", return_value=path
            ), patch.object(
                self.app, "fetch_epidemiology_report", return_value=complete
            ):
                report = self.app.load_or_refresh_report(2026)

            self.assertEqual(len(report["municipios"]), 1)
            self.assertFalse(report["metadata"]["carga"]["cobertura_degradada"])


if __name__ == "__main__":
    unittest.main()
