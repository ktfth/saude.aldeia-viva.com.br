"""
TDD Safety Net for Ingestion Layer - Fase 0 (Ingestão)

Protects the critical logic for loading and parsing raw SINAN/OpenDataSUS data
(DBC + CSV). This is one of the most fragile and important parts of the system.

These tests must pass after extracting to ingestion/sinan_loader.py and related modules.
"""

import unittest
from unittest.mock import patch, MagicMock

# These will come from the new ingestion module after extraction
# For now we test against the current app behavior to create the safety net.


class TestMunicipalityCodeExtraction(unittest.TestCase):
    """Critical: many records have messy municipality identifiers."""

    def test_extract_from_id_mn_resi(self):
        # Will be implemented properly in ingestion module
        from app import extract_municipality_code

        record = {"ID_MN_RESI": "355030", "SG_UF": "35"}
        self.assertEqual(extract_municipality_code(record), "355030")

    def test_extract_fallback_fields(self):
        from app import extract_municipality_code

        record = {"ID_MUNICIP": "3304557", "MUNICIPIO": "Rio de Janeiro"}
        code = extract_municipality_code(record)
        self.assertTrue(code.startswith("330455") or code == "3304557")


class TestDateParsing(unittest.TestCase):
    """Dates come in multiple inconsistent formats from DATASUS."""

    def test_parse_iso_date(self):
        from app import parse_date_value

        self.assertEqual(parse_date_value("2025-03-15"), "2025-03-15")

    def test_parse_brazilian_date(self):
        from app import parse_date_value

        self.assertEqual(parse_date_value("15/03/2025"), "2025-03-15")

    def test_parse_invalid_returns_original(self):
        from app import parse_date_value

        self.assertEqual(parse_date_value("nonsense"), "nonsense")


class TestTruthinessAndFlags(unittest.TestCase):
    """SINAN uses many different ways to say 'yes'."""

    def test_is_truthy_variations(self):
        from aggregation.utils import is_truthy_code

        self.assertTrue(is_truthy_code("1"))
        self.assertTrue(is_truthy_code("Sim"))
        self.assertTrue(is_truthy_code("S"))
        self.assertFalse(is_truthy_code("0"))
        self.assertFalse(is_truthy_code("Não"))


class TestUrlBuilders(unittest.TestCase):
    def test_build_source_url_for_dengue(self):
        from app import DISEASE_SOURCES
        from ingestion.sinan_loader import build_source_url

        source = DISEASE_SOURCES["DENG"]
        url = build_source_url(source, 2025)
        self.assertIn("Dengue/csv/DENGBR25.csv.zip", url)

    def test_build_dbc_source_url_for_lept(self):
        from app import DISEASE_SOURCES
        from ingestion.sinan_loader import build_dbc_source_url

        source = DISEASE_SOURCES["LEPT"]
        url = build_dbc_source_url(source, 2024)
        self.assertIn("LEPTBR24.dbc", url)


class TestDownloadAndLoadingFunctions(unittest.TestCase):
    """Tests for the core download + parsing layer (moved to ingestion)."""

    @patch("ingestion.sinan_loader.urllib.request.urlopen")
    def test_load_csv_records_from_url_basic(self, mock_urlopen):
        # Simulate a small CSV response
        from ingestion.sinan_loader import load_csv_records_from_url

        fake_csv = b"ID;VALOR\n1;10\n2;20\n"
        mock_response = MagicMock()
        mock_response.read.return_value = fake_csv
        mock_urlopen.return_value.__enter__.return_value = mock_response

        records = load_csv_records_from_url("https://example.com/data.csv", encoding="utf-8")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["ID"], "1")

    def test_load_functions_are_callable_after_extraction(self):
        from ingestion.sinan_loader import (
            download_bytes,
            load_csv_records_from_url,
            load_csv_records_from_zip,
            load_dbc_records_from_url,
        )
        self.assertTrue(callable(download_bytes))
        self.assertTrue(callable(load_csv_records_from_url))
        self.assertTrue(callable(load_csv_records_from_zip))
        self.assertTrue(callable(load_dbc_records_from_url))


class TestLoadLatestAvailableRecords(unittest.TestCase):
    """Higher-level tests for the main ingestion orchestration function."""

    @patch("ingestion.sinan_loader.load_csv_records_from_url")
    def test_load_latest_with_direct_csv_source(self, mock_load_csv):
        from app import DISEASE_SOURCES
        from ingestion.sinan_loader import load_latest_available_records

        source = DISEASE_SOURCES["YF"]  # has direct_csv_url
        mock_load_csv.return_value = [
            {"ANO_IS": "2025", "ID": "1"},
            {"ANO_IS": "2024", "ID": "2"},
        ]

        year, url, records = load_latest_available_records(source, target_year=2026)

        self.assertEqual(year, 2025)
        self.assertIn("Febre+Amarela", url)
        self.assertEqual(len(records), 1)  # filtered to latest year <= 2026
        mock_load_csv.assert_called_once()


if __name__ == "__main__":
    unittest.main()