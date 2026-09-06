import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


class AppImportTest(unittest.TestCase):
    def test_app_module_imports_without_hanging(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import app; print('ok')"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_build_epidemiology_report_enriches_municipality_output(self) -> None:
        records_by_disease = {
            "DENG": [
                {
                    "ID_MN_RESI": "355030",
                    "SG_UF": "35",
                    "CLASSI_FIN": "10",
                    "DT_NOTIFIC": "2026-01-05",
                    "EVOLUCAO": "1",
                    "HOSPITALIZ": "2",
                },
                {
                    "ID_MN_RESI": "355030",
                    "SG_UF": "35",
                    "CLASSI_FIN": "12",
                    "DT_NOTIFIC": "2026-01-10",
                    "EVOLUCAO": "2",
                    "HOSPITALIZ": "1",
                },
            ],
            "CHIK": [
                {
                    "ID_MN_RESI": "355030",
                    "SG_UF": "35",
                    "CLASSI_FIN": "5",
                    "DT_NOTIFIC": "2026-01-08",
                    "EVOLUCAO": "1",
                    "HOSPITALIZ": "2",
                }
            ],
            "ZIKA": [
                {
                    "ID_MN_RESI": "355030",
                    "SG_UF": "35",
                    "CLASSI_FIN": "2",
                    "DT_NOTIFIC": "2026-01-12",
                    "EVOLUCAO": "1",
                }
            ],
        }

        report = app.build_epidemiology_report(
            records_by_disease,
            year=2026,
            municipality_lookup={
                "355030": {"municipio": "São Paulo", "estado": "SP"}
            },
        )

        rows = {item["codigo_municipio"]: item for item in report["municipios"]}
        sao_paulo = rows["355030"]
        dengue = {item["codigo"]: item for item in sao_paulo["doencas"]}["DENG"]

        self.assertEqual(sao_paulo["municipio"], "São Paulo")
        self.assertEqual(sao_paulo["estado"], "SP")
        self.assertEqual(sao_paulo["total_casos_provaveis"], 3)
        self.assertEqual(sao_paulo["total_obitos"], 1)
        self.assertEqual(sao_paulo["nivel_risco"], "critico")
        self.assertEqual(dengue["nome"], "Dengue")
        self.assertEqual(dengue["virus"], "DENV")
        self.assertEqual(dengue["casos_provaveis"], 2)
        self.assertEqual(dengue["casos_descartados"], 0)
        self.assertEqual(dengue["casos_graves"], 1)
        self.assertEqual(dengue["obitos"], 1)
        self.assertEqual(dengue["nivel_risco"], "critico")
        self.assertIn("Dengue", [item["nome"] for item in sao_paulo["doencas_altas"]])

    def test_build_epidemiology_report_lists_high_alerts(self) -> None:
        report = app.build_epidemiology_report(
            {
                "DENG": [
                    {
                        "ID_MN_RESI": "355030",
                        "SG_UF": "35",
                        "CLASSI_FIN": "12",
                        "DT_NOTIFIC": "2026-01-10",
                        "EVOLUCAO": "2",
                        "HOSPITALIZ": "1",
                    }
                ],
                "ZIKA": [
                    {
                        "ID_MN_RESI": "330455",
                        "SG_UF": "33",
                        "CLASSI_FIN": "2",
                        "DT_NOTIFIC": "2026-01-11",
                        "EVOLUCAO": "1",
                    }
                ],
            },
            year=2026,
            municipality_lookup={
                "355030": {"municipio": "São Paulo", "estado": "SP"},
                "330455": {"municipio": "Rio de Janeiro", "estado": "RJ"},
            },
        )

        alerts = report["alertas_altos"]

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["municipio"], "São Paulo")
        self.assertEqual(alerts[0]["doenca"], "Dengue")
        self.assertEqual(alerts[0]["nivel_risco"], "critico")
        self.assertEqual(alerts[0]["obitos"], 1)

    def test_state_from_record_prefers_residence_municipality_code(self) -> None:
        state = app.state_from_record({"ID_MN_RESI": "355030", "SG_UF": "26"})

        self.assertEqual(state, "SP")

    def test_decode_json_payload_accepts_gzip_responses(self) -> None:
        payload = gzip.compress(
            json.dumps([{"id": 3550308, "nome": "São Paulo"}]).encode("utf-8")
        )

        decoded = app.decode_json_payload(payload)

        self.assertEqual(decoded[0]["nome"], "São Paulo")

    def test_legacy_dengue_severe_classifications_count_as_grave(self) -> None:
        report = app.build_epidemiology_report(
            {
                "DENG": [
                    {
                        "ID_MN_RESI": "355030",
                        "CLASSI_FIN": "3",
                        "DT_NOTIFIC": "2026-01-10",
                        "EVOLUCAO": "1",
                    }
                ]
            },
            year=2026,
            municipality_lookup={
                "355030": {"municipio": "São Paulo", "estado": "SP"}
            },
        )

        disease = report["municipios"][0]["doencas"][0]

        self.assertEqual(disease["casos_graves"], 1)
        self.assertEqual(
            disease["classificacoes"], {"Febre hemorrágica do dengue": 1})

    def test_dashboard_uses_extracted_static_assets(self) -> None:
        """Phase 0 safety check: dashboard should load CSS/JS from /static instead of huge inlines."""
        client = TestClient(app.app)
        resp = client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # Must reference the extracted static files
        self.assertIn("/static/css/main.css", html)
        self.assertIn("/static/js/dashboard.js", html)

        # Should NOT contain the old giant inline <style> with thousands of chars of BASE_CSS
        style_tag_count = html.count("<style>")
        self.assertLess(style_tag_count, 3, "Too many inline style blocks - CSS extraction incomplete")

        # Sanity: key UI patterns from the extracted CSS/JS must still be present
        self.assertIn("class=\"panel", html)  # CSS classes are served
        self.assertIn("risk-dashboard", html)

    def test_filter_risk_index_resolves_perus_to_sao_paulo_municipality(self) -> None:
        rows = [
            {
                "municipio": "São Paulo",
                "codigo_municipio": "355030",
                "estado": "SP",
                "nivel_risco": "critico",
                "doencas_altas": [{"nome": "Dengue"}],
            }
        ]

        result = app.filter_risk_index(rows, municipio="perus", estado="SP")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["municipio"], "São Paulo")
        self.assertEqual(
            result[0]["filtro_localidade"],
            {
                "consulta": "perus",
                "tipo": "distrito",
                "localidade": "Perus",
                "municipio_resolvido": "São Paulo",
                "codigo_municipio": "355030",
                "estado": "SP",
                "granularidade_disponivel": "municipio",
            },
        )

    def test_filter_risk_index_resolves_sao_paulo_districts_to_municipality(self) -> None:
        rows = [
            {
                "municipio": "São Paulo",
                "codigo_municipio": "355030",
                "estado": "SP",
                "nivel_risco": "critico",
                "doencas_altas": [{"nome": "Dengue"}],
            }
        ]

        for district in ("jaragua", "jaraguá", "lapa", "barra funda", "freguesia do o"):
            with self.subTest(district=district):
                result = app.filter_risk_index(rows, municipio=district, estado="SP")
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["municipio"], "São Paulo")
                self.assertEqual(
                    result[0]["filtro_localidade"]["granularidade_disponivel"],
                    "municipio",
                )

    def test_zika_classification_does_not_use_dengue_labels(self) -> None:
        report = app.build_epidemiology_report(
            {
                "ZIKA": [
                    {
                        "ID_MN_RESI": "355030",
                        "CLASSI_FIN": "2",
                        "DT_NOTIFIC": "2026-01-10",
                        "EVOLUCAO": "1",
                    }
                ]
            },
            year=2026,
            municipality_lookup={
                "355030": {"municipio": "São Paulo", "estado": "SP"}
            },
        )

        disease = report["municipios"][0]["doencas"][0]

        self.assertEqual(disease["nome"], "Zika")
        self.assertEqual(disease["classificacoes"], {"Zika": 1})

    def test_yellow_fever_human_cases_are_supported_from_direct_csv_shape(self) -> None:
        report = app.build_epidemiology_report(
            {
                "YF": [
                    {
                        "COD_MUN_LPI": "355210",
                        "UF_LPI": "SP",
                        "MUN_LPI": "SOCORRO",
                        "ANO_IS": "2025",
                        "DT_IS": "03/01/2025",
                        "OBITO": "SIM",
                    }
                ]
            },
            year=2025,
            municipality_lookup={
                "355210": {"municipio": "Socorro", "estado": "SP"}
            },
        )

        disease = report["municipios"][0]["doencas"][0]

        self.assertEqual(disease["codigo"], "YF")
        self.assertEqual(disease["nome"], "Febre Amarela")
        self.assertEqual(disease["virus"], "YFV")
        self.assertEqual(disease["casos_provaveis"], 1)
        self.assertEqual(disease["obitos"], 1)
        self.assertEqual(disease["ultima_notificacao"], "2025-01-03")
        self.assertEqual(disease["classificacoes"], {"Febre amarela confirmada": 1})

    def test_filter_records_by_latest_available_year_uses_previous_year(self) -> None:
        from ingestion.sinan_loader import filter_records_by_latest_available_year
        year, rows = filter_records_by_latest_available_year(
            [
                {"ANO_IS": "2024", "ID": "1"},
                {"ANO_IS": "2025", "ID": "2"},
                {"ANO_IS": "2027", "ID": "3"},
            ],
            target_year=2026,
            year_field="ANO_IS",
        )

        self.assertEqual(year, 2025)
        self.assertEqual(rows, [{"ANO_IS": "2025", "ID": "2"}])

    def test_leptospirosis_uses_disease_specific_risk_profile(self) -> None:
        report = app.build_epidemiology_report(
            {
                "LEPT": [
                    {
                        "ID_MN_RESI": "410690",
                        "DT_NOTIFIC": "2024-01-02",
                        "DT_SIN_PRI": "2024-01-01",
                        "CLI_ICTERI": "1",
                        "CLI_RENAL": "1",
                        "ATE_HOSP": "1",
                        "EVOLUCAO": "2",
                    }
                ]
            },
            year=2024,
            municipality_lookup={
                "410690": {"municipio": "Curitiba", "estado": "PR"}
            },
        )

        disease = report["municipios"][0]["doencas"][0]

        self.assertEqual(disease["codigo"], "LEPT")
        self.assertEqual(disease["sinais_alarme"], 1)
        self.assertEqual(disease["casos_graves"], 1)
        self.assertEqual(disease["hospitalizacoes"], 1)
        self.assertEqual(disease["obitos"], 1)
        self.assertEqual(disease["risk_score"], 48)
        self.assertEqual(disease["nivel_risco"], "critico")
        self.assertEqual(
            disease["formula_risco"],
            "casos_provaveis + 6*sinais_alarme + 12*casos_graves + 25*obitos + 4*hospitalizacoes",
        )

    def test_animal_accident_profile_uses_accident_severity_classification(self) -> None:
        report = app.build_epidemiology_report(
            {
                "ANIM": [
                    {
                        "ID_MN_RESI": "270430",
                        "DT_NOTIFIC": "2022-01-02",
                        "TRA_CLASSI": "3",
                        "EVOLUCAO": "1",
                    }
                ]
            },
            year=2022,
            municipality_lookup={
                "270430": {"municipio": "Maceió", "estado": "AL"}
            },
        )

        disease = report["municipios"][0]["doencas"][0]

        self.assertEqual(disease["codigo"], "ANIM")
        self.assertEqual(disease["casos_graves"], 1)
        self.assertEqual(disease["risk_score"], 11)
        self.assertEqual(disease["nivel_risco"], "alto")

    def test_dashboard_page_serves_html_with_seo_and_data_hooks(self) -> None:
        seed_web_globals()
        client = TestClient(app.app)

        response = client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("<title>", response.text)
        self.assertIn("application/ld+json", response.text)
        self.assertIn('id="risk-dashboard"', response.text)
        self.assertIn("/v1/risk-index", response.text)
        # A coluna lateral "Alertas altos" foi removida: repetia, em outro
        # corte, o mesmo dado que a tabela ja ordenava por risco.
        self.assertNotIn("Alertas altos", response.text)
        # No lugar entrou a idade do dado, que era o unico fato invisivel.
        self.assertIn("data-status", response.text)
        self.assertIn("signal-strip", response.text)
        # E o Chart.js de dois pontos deixou de ser carregado.
        self.assertNotIn("cdn.jsdelivr.net", response.text)

    def test_explanation_page_documents_sources_formula_and_granularity(self) -> None:
        seed_web_globals()
        client = TestClient(app.app)

        response = client.get("/sobre")

        self.assertEqual(response.status_code, 200)
        self.assertIn("SINAN/OpenDataSUS", response.text)
        self.assertIn(app.RISK_FORMULA, response.text)
        self.assertIn("granularidade municipal", response.text.lower())
        self.assertIn("Dados não substituem vigilância epidemiológica oficial", response.text)

    def test_agents_page_and_agent_manifest_expose_consumption_contract(self) -> None:
        seed_web_globals()
        client = TestClient(app.app)

        page = client.get("/agentes")
        manifest = client.get("/agent.json")
        llms = client.get("/llms.txt")

        self.assertEqual(page.status_code, 200)
        self.assertIn("/agent.json", page.text)
        self.assertIn("/llms.txt", page.text)
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["name"], "Epidemiology Intelligence API")
        self.assertIn("/v1/high-alerts", manifest.json()["endpoints"])
        self.assertIn("/v1/diseases", manifest.json()["endpoints"])
        self.assertEqual(llms.status_code, 200)
        self.assertIn("Use /v1/high-alerts", llms.text)

    def test_diseases_endpoint_lists_supported_sources(self) -> None:
        seed_web_globals()
        client = TestClient(app.app)

        response = client.get("/v1/diseases")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        codes = {item["codigo"] for item in payload["doencas"]}
        self.assertIn("DENG", codes)
        self.assertIn("CHIK", codes)
        self.assertIn("ZIKA", codes)
        self.assertIn("YF", codes)
        self.assertIn("LEPT", codes)
        self.assertIn("MENI", codes)
        self.assertIn("ANIM", codes)
        self.assertEqual(payload["total"], len(app.DISEASE_SOURCES))
        animals = next(item for item in payload["doencas"] if item["codigo"] == "ANIM")
        self.assertFalse(animals["habilitado_por_padrao"])
        lept = next(item for item in payload["doencas"] if item["codigo"] == "LEPT")
        self.assertIn("formula_risco", lept)
        self.assertIn("12*casos_graves", lept["formula_risco"])

    def test_build_dbc_source_url_uses_datasus_ftp_pattern(self) -> None:
        from ingestion.sinan_loader import build_dbc_source_url
        source = app.DISEASE_SOURCES["LEPT"]

        url = build_dbc_source_url(source, 2024)

        self.assertEqual(
            url,
            "ftp://ftp.datasus.gov.br/dissemin/publicos/SINAN/DADOS/FINAIS/LEPTBR24.dbc",
        )

    def test_normalize_dbf_record_serializes_dates_and_none(self) -> None:
        from ingestion.sinan_loader import normalize_dbf_record
        row = {
            "DT_NOTIFIC": date(2024, 1, 2),
            "DT_OBITO": None,
            "NU_ANO": "2024",
        }

        normalized = normalize_dbf_record(row)

        self.assertEqual(
            normalized,
            {"DT_NOTIFIC": "2024-01-02", "DT_OBITO": "", "NU_ANO": "2024"},
        )

    def test_report_cache_roundtrip_and_global_state_application(self) -> None:
        report = sample_report()

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "report.json"
            app.save_report_cache(report, cache_path)
            loaded = app.load_report_cache(cache_path)

        self.assertEqual(loaded["metadata"]["status"], "ok")

        app.apply_report_state(loaded, cache_hit=True)

        self.assertEqual(app.db_clini[0]["municipio"], "São Paulo")
        self.assertEqual(app.db_alertas[0]["doenca"], "Dengue")
        self.assertTrue(app.db_metadata["cache"]["hit"])
        self.assertEqual(app.db_metadata["cache"]["source"], "disk")

    def test_report_cache_path_depends_on_year_and_enabled_diseases(self) -> None:
        first = app.report_cache_path(2026, ["DENG", "LEPT"])
        second = app.report_cache_path(2026, ["LEPT", "DENG"])
        other = app.report_cache_path(2025, ["DENG", "LEPT"])

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(first.suffix, ".json")

    def test_load_or_refresh_report_uses_aggregate_cache_before_fetching(self) -> None:
        report = sample_report()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(app, "SINAN_CACHE_DIR", Path(tmpdir)):
                cache_path = app.report_cache_path(2026, ["DENG"])
                app.save_report_cache(report, cache_path)

                with (
                    patch.object(
                        app,
                        "enabled_disease_sources",
                        return_value=[app.DISEASE_SOURCES["DENG"]],
                    ),
                    patch.object(
                        app,
                        "fetch_epidemiology_report",
                        side_effect=AssertionError("should not fetch"),
                    ),
                ):
                    loaded = app.load_or_refresh_report(2026)

        self.assertEqual(loaded["metadata"]["status"], "ok")
        self.assertTrue(app.db_metadata["cache"]["hit"])
        self.assertEqual(app.db_metadata["cache"]["source"], "disk")

    def test_load_or_refresh_report_uses_embedded_snapshot_before_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(app, "SINAN_CACHE_DIR", Path(tmpdir)),
                patch.object(
                    app,
                    "fetch_epidemiology_report",
                    side_effect=AssertionError("should not fetch"),
                ),
            ):
                loaded = app.load_or_refresh_report(2026)

        self.assertEqual(loaded["metadata"]["status"], "ok")
        self.assertEqual(app.db_metadata["cache"]["source"], "bundled")
        self.assertEqual(app.db_clini[0]["municipio"], "São Paulo")

    def test_refresh_endpoint_reprocesses_report_without_blocking_event_loop(self) -> None:
        def fake_refresh(year: int, *, force_refresh: bool) -> dict[str, object]:
            report = sample_report()
            app.apply_report_state(report, cache_hit=False, cache_source="refresh")
            return report

        client = TestClient(app.app)

        # /v1/refresh reprocessa a carga inteira: exige chave com escrita.
        with patch.object(app, "load_or_refresh_report", side_effect=fake_refresh) as mocked:
            response = client.post(
                "/v1/refresh", headers={"X-API-Key": "premium_partner_key"}
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["municipios"], 1)
        self.assertEqual(payload["alertas_altos"], 1)
        self.assertFalse(payload["metadata"]["cache"]["hit"])
        mocked.assert_called_once_with(app.DEFAULT_YEAR, force_refresh=True)

    def test_bootstrap_middleware_rehydrates_empty_state_on_request(self) -> None:
        def fake_refresh(year: int, *, force_refresh: bool) -> dict[str, object]:
            report = sample_report()
            app.apply_report_state(report, cache_hit=True, cache_source="disk")
            return report

        with patch.object(app, "load_or_refresh_report", side_effect=fake_refresh) as mocked:
            with TestClient(app.app) as client:
                app.db_clini = []
                app.db_alertas = []
                app.db_metadata = {
                    "status": "not_loaded",
                    "periodo": {"ano": 2026},
                    "fontes": [],
                    "erros": [],
                }

                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_status"], "ok")
        self.assertEqual(app.db_clini[0]["municipio"], "São Paulo")
        self.assertGreaterEqual(mocked.call_count, 2)

    def test_robots_and_sitemap_advertise_public_pages(self) -> None:
        seed_web_globals()
        client = TestClient(app.app)

        robots = client.get("/robots.txt")
        sitemap = client.get("/sitemap.xml")

        self.assertEqual(robots.status_code, 200)
        self.assertIn("Sitemap:", robots.text)
        self.assertEqual(sitemap.status_code, 200)
        self.assertIn("<loc>http://testserver/dashboard</loc>", sitemap.text)
        self.assertIn("<loc>http://testserver/sobre</loc>", sitemap.text)
        self.assertIn("<loc>http://testserver/agentes</loc>", sitemap.text)


if __name__ == "__main__":
    unittest.main()


def sample_report() -> dict[str, object]:
    return {
        "metadata": {
            "status": "ok",
            "periodo": {"ano": 2026},
            "fonte": "test",
            "fontes": [],
            "erros": [],
        },
        "municipios": [
            {
                "codigo_municipio": "355030",
                "municipio": "São Paulo",
                "estado": "SP",
                "risk_score": 10,
                "nivel_risco": "alto",
                "doencas": [],
                "doencas_altas": [],
            }
        ],
        "alertas_altos": [
            {
                "codigo_municipio": "355030",
                "municipio": "São Paulo",
                "estado": "SP",
                "doenca": "Dengue",
                "codigo_doenca": "DENG",
                "risk_score": 10,
                "nivel_risco": "alto",
            }
        ],
    }


def seed_web_globals() -> None:
    app.db_metadata = {
        "status": "ok",
        "periodo": {"ano": 2026},
        "carregado_em": "2026-04-25T12:00:00+00:00",
        "formula_risco": app.RISK_FORMULA,
        "fontes": [
            {
                "codigo": "DENG",
                "nome": "Dengue",
                "ano": 2026,
                "url": "https://example.test/dengue.csv.zip",
                "registros": 100,
            }
        ],
    }
    app.db_clini = [
        {
            "codigo_municipio": "355030",
            "municipio": "São Paulo",
            "estado": "SP",
            "periodo": {"ano": 2026},
            "total_casos_provaveis": 20,
            "total_obitos": 1,
            "risk_score": 52,
            "nivel_risco": "critico",
            "doencas": [
                {
                    "codigo": "DENG",
                    "nome": "Dengue",
                    "virus": "DENV",
                    "tipo": "Arbovirose urbana",
                    "casos_provaveis": 20,
                    "casos_graves": 2,
                    "sinais_alarme": 3,
                    "hospitalizacoes": 2,
                    "obitos": 1,
                    "risk_score": 52,
                    "nivel_risco": "critico",
                    "ultima_notificacao": "2026-04-21",
                    "classificacoes": {"Dengue": 20},
                }
            ],
            "doencas_altas": [{"nome": "Dengue", "nivel_risco": "critico"}],
        }
    ]
    app.db_alertas = [
        {
            "codigo_municipio": "355030",
            "municipio": "São Paulo",
            "estado": "SP",
            "codigo_doenca": "DENG",
            "doenca": "Dengue",
            "virus": "DENV",
            "tipo": "Arbovirose urbana",
            "casos_provaveis": 20,
            "casos_graves": 2,
            "sinais_alarme": 3,
            "hospitalizacoes": 2,
            "obitos": 1,
            "risk_score": 52,
            "nivel_risco": "critico",
            "ultima_notificacao": "2026-04-21",
        }
    ]

