"""
TDD Safety Net for Risk Domain Extraction - Fase 0

These tests protect the core epidemiological risk calculation logic.
They must continue passing after any refactoring or extraction.

This file follows the TDD approach defined in the approved PLAN-BIRO-COMPLETO.md.
"""

import unittest

# Importa de quem é dono, e não do `app` como passagem: a suíte dependia
# de o app reexportar um símbolo que ele próprio não usa.
from domain.risk import RISK_PROFILES, finalize_disease_summary, risk_level


class TestRiskProfiles(unittest.TestCase):
    """Ensure all defined risk profiles exist and have sane values."""

    def test_all_default_profiles_exist(self):
        expected = {"arbovirus", "yellow_fever", "leptospirosis", "meningitis", "animal_accident", "rare_severe", "chronic"}
        self.assertTrue(expected.issubset(set(RISK_PROFILES.keys())))

    def test_arbovirus_profile_has_reasonable_weights(self):
        p = RISK_PROFILES["arbovirus"]
        self.assertGreater(p.death_weight, p.severe_weight)
        self.assertGreater(p.severe_weight, p.warning_weight)
        self.assertGreater(p.case_weight, 0)

    def test_yellow_fever_profile_treats_deaths_as_extremely_high(self):
        p = RISK_PROFILES["yellow_fever"]
        self.assertEqual(p.death_weight, 30.0)
        self.assertEqual(p.warning_weight, 0.0)  # No warning weight


class TestRiskLevelLogic(unittest.TestCase):
    """Direct tests of the risk_level function (critical for operational decisions)."""

    def test_critical_when_deaths_and_death_is_critical(self):
        profile = RISK_PROFILES["arbovirus"]
        level = risk_level(10.0, 5, 0, 1, profile)
        self.assertEqual(level, "critico")

    def test_critical_when_score_exceeds_critical_threshold(self):
        profile = RISK_PROFILES["arbovirus"]
        level = risk_level(150.0, 0, 0, 0, profile)
        self.assertEqual(level, "critico")

    def test_high_when_severe_cases_and_severe_is_high(self):
        profile = RISK_PROFILES["arbovirus"]
        level = risk_level(10.0, 5, 1, 0, profile)
        self.assertEqual(level, "alto")

    def test_high_when_score_exceeds_high_threshold(self):
        profile = RISK_PROFILES["arbovirus"]
        level = risk_level(30.0, 0, 0, 0, profile)
        self.assertEqual(level, "alto")

    def test_moderado_when_probable_cases_above_minimum(self):
        profile = RISK_PROFILES["arbovirus"]
        level = risk_level(2.0, 6, 0, 0, profile)
        self.assertEqual(level, "moderado")

    def test_baixo_for_zero_activity(self):
        profile = RISK_PROFILES["arbovirus"]
        level = risk_level(0.0, 0, 0, 0, profile)
        self.assertEqual(level, "baixo")


class TestDiseaseRiskScoring(unittest.TestCase):
    """Tests the actual weighted scoring used in finalize_disease_summary."""

    def _make_disease(self, **overrides):
        base = {
            "codigo": "DENG",
            "nome": "Dengue",
            "perfil_risco": "arbovirus",
            "casos_provaveis": 10,
            "sinais_alarme": 2,
            "casos_graves": 1,
            "obitos": 0,
            "hospitalizacoes": 3,
            "classificacoes": {},
        }
        base.update(overrides)
        return base

    def test_arbovirus_scoring_formula(self):
        disease = self._make_disease()
        result = finalize_disease_summary(disease)

        # Expected: 1*10 + 4*2 + 8*1 + 20*0 + 2*3 = 10 + 8 + 8 + 0 + 6 = 32
        self.assertEqual(result["risk_score"], 32.0)
        self.assertEqual(result["nivel_risco"], "alto")  # 32 > 25

    def test_yellow_fever_heavily_weights_deaths(self):
        disease = self._make_disease(perfil_risco="yellow_fever", obitos=1, casos_provaveis=5)
        result = finalize_disease_summary(disease)

        # Expected for yellow_fever: 2*5 + 30*1 = 10 + 30 = 40
        self.assertEqual(result["risk_score"], 40.0)
        self.assertEqual(result["nivel_risco"], "critico")

    def test_chronic_profile_only_uses_cases_and_deaths(self):
        disease = self._make_disease(
            perfil_risco="chronic",
            casos_provaveis=20,
            obitos=1,
            sinais_alarme=10,
            casos_graves=5,
            hospitalizacoes=8,
        )
        result = finalize_disease_summary(disease)

        # chronic formula: casos_provaveis + 20*obitos = 20 + 20 = 40
        self.assertEqual(result["risk_score"], 40.0)

    def test_risk_level_respects_profile_thresholds(self):
        """Different profiles have different high/critical thresholds."""
        # Yellow fever has lower critical threshold (60) than arbovirus (100)
        disease = self._make_disease(perfil_risco="yellow_fever", casos_provaveis=40)
        result = finalize_disease_summary(disease)

        # 2*40 = 80 → should be "alto" for yellow_fever (high=20, critical=60)
        self.assertEqual(result["nivel_risco"], "critico")  # 80 >= 60


class TestRiskProfileImmutability(unittest.TestCase):
    """Enforce the project immutability rule on RiskProfile."""

    def test_risk_profile_is_frozen_dataclass(self):
        p = RISK_PROFILES["arbovirus"]
        with self.assertRaises(Exception):  # frozen dataclass should raise on mutation
            p.case_weight = 99.0  # type: ignore


if __name__ == "__main__":
    unittest.main()