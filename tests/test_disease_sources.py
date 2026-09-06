"""
TDD Safety Net for Disease Sources Catalog - Fase 0 (Opção A)

Protects the definition of all supported agravos (diseases), their
metadata, risk profile linkage, and classification label logic.

These tests must pass after extraction to domain/disease_sources.py.
"""

import os
import unittest
from unittest.mock import patch

from app import enabled_disease_sources
from domain.disease_sources import (
    DEFAULT_DISEASE_CODES,
    DISEASE_SOURCES,
    classification_label,
)


class TestDiseaseSourcesCatalog(unittest.TestCase):
    """Basic invariants of the disease catalog."""

    def test_default_disease_codes_are_present(self):
        self.assertGreater(len(DEFAULT_DISEASE_CODES), 5)
        for code in DEFAULT_DISEASE_CODES:
            self.assertIn(code, DISEASE_SOURCES)

    def test_all_sources_have_required_fields(self):
        for code, source in DISEASE_SOURCES.items():
            self.assertEqual(source.codigo, code)
            self.assertTrue(source.nome)
            self.assertTrue(source.virus)
            self.assertIsNotNone(source.first_year)
            self.assertTrue(source.risk_profile)

    def test_dengue_links_to_arbovirus_profile(self):
        deng = DISEASE_SOURCES["DENG"]
        self.assertEqual(deng.risk_profile, "arbovirus")
        self.assertIn("11", deng.warning_codes)
        self.assertIn("12", deng.severe_codes)

    def test_yellow_fever_has_direct_csv_and_custom_year_field(self):
        yf = DISEASE_SOURCES["YF"]
        self.assertEqual(yf.risk_profile, "yellow_fever")
        self.assertIn("Febre+Amarela", yf.direct_csv_url or "")
        self.assertEqual(yf.year_field, "ANO_IS")

    def test_leptospirosis_has_warning_and_severe_fields(self):
        lept = DISEASE_SOURCES["LEPT"]
        self.assertTrue(lept.warning_fields)
        self.assertTrue(lept.severe_fields)
        self.assertEqual(lept.risk_profile, "leptospirosis")


class TestClassificationLabel(unittest.TestCase):
    """Protects the human-readable classification mapping logic."""

    def test_dengue_grave_label(self):
        self.assertEqual(classification_label("DENG", "12"), "Dengue grave")

    def test_dengue_with_alarm_label(self):
        self.assertEqual(
            classification_label("DENG", "11"), "Dengue com sinais de alarme"
        )

    def test_yellow_fever_empty_code(self):
        self.assertIn("Febre amarela", classification_label("YF", ""))

    def test_unknown_code_returns_fallback(self):
        self.assertEqual(classification_label("DENG", "99"), "Classificação 99")

    def test_zika_variants(self):
        self.assertEqual(classification_label("ZIKA", "1"), "Zika")
        self.assertEqual(classification_label("ZIKA", "2"), "Zika")


class TestEnabledDiseaseSources(unittest.TestCase):
    """Tests the runtime configuration of which diseases are active."""

    def test_returns_defaults_when_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            sources = enabled_disease_sources()
            self.assertGreaterEqual(len(sources), 8)
            codes = {s.codigo for s in sources}
            self.assertIn("DENG", codes)

    def test_respects_sinAN_disease_codes_env_var(self):
        with patch.dict(os.environ, {"SINAN_DISEASE_CODES": "DENG,CHIK"}):
            sources = enabled_disease_sources()
            codes = {s.codigo for s in sources}
            self.assertEqual(codes, {"DENG", "CHIK"})

    def test_ignores_unknown_codes_in_env(self):
        with patch.dict(os.environ, {"SINAN_DISEASE_CODES": "DENG,XXX,CHIK"}):
            sources = enabled_disease_sources()
            codes = {s.codigo for s in sources}
            self.assertIn("DENG", codes)
            self.assertIn("CHIK", codes)
            self.assertNotIn("XXX", codes)


if __name__ == "__main__":
    unittest.main()