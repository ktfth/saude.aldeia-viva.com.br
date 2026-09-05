"""
Fase 2 - Recência do sinal epidemiológico.

Testes para `domain/recency.py`.

Motivação (medida no relatório real em 2026-09):
71,6% dos alertas servidos em /v1/high-alerts como "alto/crítico" do ano-base
2026 tinham `ultima_notificacao` de anos anteriores — 32,4% deles de 2022.
A interface exibia sinal fóssil com a mesma tinta de sinal vivo.

Este módulo existe para que a idade do sinal seja um dado de primeira classe,
nunca um detalhe implícito.
"""

import unittest
from datetime import date

from domain.recency import (
    FOSSIL,
    DORMANT,
    COOLING,
    LIVE,
    UNKNOWN,
    describe_signal,
    signal_age_days,
    signal_freshness,
    with_recency,
)


class TestSignalAgeDays(unittest.TestCase):
    REF = date(2026, 9, 5)

    def test_same_day_is_zero(self) -> None:
        self.assertEqual(signal_age_days("2026-09-05", self.REF), 0)

    def test_counts_calendar_days(self) -> None:
        self.assertEqual(signal_age_days("2026-08-06", self.REF), 30)

    def test_years_old_signal(self) -> None:
        self.assertEqual(signal_age_days("2022-12-30", self.REF), 1345)

    def test_accepts_datetime_prefix(self) -> None:
        self.assertEqual(signal_age_days("2026-09-01T10:33:00", self.REF), 4)

    def test_none_for_missing(self) -> None:
        self.assertIsNone(signal_age_days(None, self.REF))
        self.assertIsNone(signal_age_days("", self.REF))

    def test_none_for_garbage(self) -> None:
        self.assertIsNone(signal_age_days("nao-e-data", self.REF))
        self.assertIsNone(signal_age_days("2026-13-45", self.REF))

    def test_future_date_clamps_to_zero(self) -> None:
        """Digitação com data futura não pode produzir idade negativa."""
        self.assertEqual(signal_age_days("2027-01-01", self.REF), 0)


class TestSignalFreshness(unittest.TestCase):
    def test_live_window(self) -> None:
        self.assertEqual(signal_freshness(0), LIVE)
        self.assertEqual(signal_freshness(30), LIVE)

    def test_cooling_window(self) -> None:
        self.assertEqual(signal_freshness(31), COOLING)
        self.assertEqual(signal_freshness(90), COOLING)

    def test_dormant_window(self) -> None:
        self.assertEqual(signal_freshness(91), DORMANT)
        self.assertEqual(signal_freshness(365), DORMANT)

    def test_fossil_window(self) -> None:
        self.assertEqual(signal_freshness(366), FOSSIL)
        self.assertEqual(signal_freshness(5000), FOSSIL)

    def test_unknown_when_no_age(self) -> None:
        self.assertEqual(signal_freshness(None), UNKNOWN)


class TestDescribeSignal(unittest.TestCase):
    REF = date(2026, 9, 5)

    def test_full_shape(self) -> None:
        got = describe_signal("2026-09-01", self.REF)
        self.assertEqual(got["idade_dias"], 4)
        self.assertEqual(got["frescor"], LIVE)
        self.assertEqual(got["referencia"], "2026-09-05")
        self.assertIn("rotulo", got)
        self.assertIn("confiavel_como_atual", got)

    def test_live_signal_is_trustworthy_as_current(self) -> None:
        self.assertTrue(describe_signal("2026-09-01", self.REF)["confiavel_como_atual"])

    def test_fossil_signal_is_not_trustworthy_as_current(self) -> None:
        got = describe_signal("2022-12-30", self.REF)
        self.assertFalse(got["confiavel_como_atual"])
        self.assertEqual(got["frescor"], FOSSIL)

    def test_unknown_is_not_trustworthy_as_current(self) -> None:
        got = describe_signal(None, self.REF)
        self.assertIsNone(got["idade_dias"])
        self.assertEqual(got["frescor"], UNKNOWN)
        self.assertFalse(got["confiavel_como_atual"])

    def test_label_is_human_readable_portuguese(self) -> None:
        self.assertEqual(describe_signal("2026-09-05", self.REF)["rotulo"], "hoje")
        self.assertEqual(describe_signal("2026-09-04", self.REF)["rotulo"], "ontem")
        self.assertEqual(describe_signal("2026-08-27", self.REF)["rotulo"], "há 9 dias")
        self.assertEqual(describe_signal("2026-06-05", self.REF)["rotulo"], "há 3 meses")
        self.assertEqual(describe_signal("2022-12-30", self.REF)["rotulo"], "há 3 anos")
        self.assertEqual(describe_signal(None, self.REF)["rotulo"], "sem data")


class TestWithRecency(unittest.TestCase):
    """with_recency deve ser puro: retorna cópia nova, jamais muta a entrada."""

    REF = date(2026, 9, 5)

    def test_adds_recencia_key(self) -> None:
        got = with_recency({"nome": "Dengue", "ultima_notificacao": "2026-09-01"}, self.REF)
        self.assertEqual(got["recencia"]["frescor"], LIVE)
        self.assertEqual(got["nome"], "Dengue")

    def test_does_not_mutate_input(self) -> None:
        original = {"nome": "Dengue", "ultima_notificacao": "2026-09-01"}
        snapshot = dict(original)
        with_recency(original, self.REF)
        self.assertEqual(original, snapshot)
        self.assertNotIn("recencia", original)

    def test_falls_back_to_symptom_onset_when_no_notification(self) -> None:
        got = with_recency(
            {"ultima_notificacao": None, "ultimo_inicio_sintomas": "2026-08-20"},
            self.REF,
        )
        self.assertEqual(got["recencia"]["idade_dias"], 16)

    def test_prefers_the_most_recent_of_both_dates(self) -> None:
        got = with_recency(
            {"ultima_notificacao": "2024-01-01", "ultimo_inicio_sintomas": "2026-08-20"},
            self.REF,
        )
        self.assertEqual(got["recencia"]["idade_dias"], 16)

    def test_handles_record_without_any_date(self) -> None:
        got = with_recency({"nome": "Dengue"}, self.REF)
        self.assertEqual(got["recencia"]["frescor"], UNKNOWN)

    def test_custom_date_fields(self) -> None:
        got = with_recency(
            {"quando": "2026-09-04"}, self.REF, date_fields=("quando",)
        )
        self.assertEqual(got["recencia"]["idade_dias"], 1)


if __name__ == "__main__":
    unittest.main()
