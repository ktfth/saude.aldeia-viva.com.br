"""
O filtro precisa concordar com o que a tela mostra.

Dois defeitos medidos em `aggregation/filters.py`:

1. `nivel_minimo` filtrava por `nivel_risco` — o nível histórico, que
   consolida todos os anos-fonte — enquanto o painel passou a exibir
   `nivel_risco_fonte_atual` desde a iteração 2. Medido: pedindo
   "apenas crítico", **54% dos 1.000 municípios devolvidos têm badge
   diferente de crítico**. O usuário filtra por um nível e a tela mostra
   outro. Dívida introduzida ao criar o campo novo sem atualizar o filtro.

2. O filtro de agravo exigia igualdade exata com o nome completo ou o
   código:

       doenca=CHIK                   ->  139 alertas
       doenca=Febre de Chikungunya   ->  139 alertas
       doenca=chikungunya            ->    0 alertas

   `dengue` funcionava por acidente — o nome completo é "Dengue". O defeito
   só aparecia em agravos de nome composto, que são metade do catálogo.
"""

import unittest

from aggregation.filters import filter_alerts, filter_risk_index


def _municipality(codigo, historico, atual=None):
    row = {
        "codigo_municipio": codigo,
        "municipio": f"Cidade {codigo}",
        "estado": "SP",
        "nivel_risco": historico,
        "doencas_altas": [{"nome": "Dengue"}],
    }
    if atual is not None:
        row["nivel_risco_fonte_atual"] = atual
    return row


class TestLevelFilterMatchesWhatIsShown(unittest.TestCase):
    def test_filters_by_the_current_source_level(self) -> None:
        rows = [
            _municipality("1", historico="critico", atual="alto"),
            _municipality("2", historico="critico", atual="critico"),
        ]
        got = filter_risk_index(rows, nivel_minimo="critico")
        self.assertEqual([r["codigo_municipio"] for r in got], ["2"])

    def test_a_municipality_below_the_threshold_today_is_excluded(self) -> None:
        """O caso real: crítico no histórico por Meningite de 2022."""
        rows = [_municipality("1", historico="critico", atual="moderado")]
        self.assertEqual(filter_risk_index(rows, nivel_minimo="alto"), [])

    def test_falls_back_to_the_historical_level_when_absent(self) -> None:
        """Cache antiga, sem enriquecimento, não pode parar de filtrar."""
        rows = [_municipality("1", historico="critico")]
        got = filter_risk_index(rows, nivel_minimo="critico")
        self.assertEqual(len(got), 1)

    def test_minimum_level_still_includes_higher_levels(self) -> None:
        rows = [
            _municipality("1", historico="baixo", atual="critico"),
            _municipality("2", historico="baixo", atual="moderado"),
        ]
        got = filter_risk_index(rows, nivel_minimo="alto")
        self.assertEqual([r["codigo_municipio"] for r in got], ["1"])

    def test_no_filter_returns_everything(self) -> None:
        rows = [
            _municipality("1", historico="critico", atual="baixo"),
            _municipality("2", historico="baixo", atual="baixo"),
        ]
        self.assertEqual(len(filter_risk_index(rows)), 2)


def _alert(doenca, codigo):
    return {
        "codigo_municipio": "355030",
        "municipio": "São Paulo",
        "estado": "SP",
        "doenca": doenca,
        "codigo_doenca": codigo,
    }


class TestDiseaseFilterAcceptsThePartialName(unittest.TestCase):
    ALERTS = [
        _alert("Dengue", "DENG"),
        _alert("Febre de Chikungunya", "CHIK"),
        _alert("Febre Amarela", "YF"),
        _alert("Toxoplasmose Gestacional", "TOXG"),
    ]

    def test_the_code_still_works(self) -> None:
        got = filter_alerts(self.ALERTS, doenca="CHIK")
        self.assertEqual([a["doenca"] for a in got], ["Febre de Chikungunya"])

    def test_the_full_name_still_works(self) -> None:
        got = filter_alerts(self.ALERTS, doenca="Febre de Chikungunya")
        self.assertEqual(len(got), 1)

    def test_the_common_name_now_works(self) -> None:
        """O nome pelo qual a doença é conhecida devolvia zero."""
        got = filter_alerts(self.ALERTS, doenca="chikungunya")
        self.assertEqual([a["doenca"] for a in got], ["Febre de Chikungunya"])

    def test_accent_insensitive(self) -> None:
        got = filter_alerts(self.ALERTS, doenca="gestacional")
        self.assertEqual([a["codigo_doenca"] for a in got], ["TOXG"])

    def test_a_shared_word_matches_both(self) -> None:
        """`febre` casa duas — é busca, e o comportamento tem que ser previsível."""
        got = filter_alerts(self.ALERTS, doenca="febre")
        self.assertEqual(
            sorted(a["codigo_doenca"] for a in got), ["CHIK", "YF"]
        )

    def test_an_unknown_term_returns_nothing(self) -> None:
        self.assertEqual(filter_alerts(self.ALERTS, doenca="malaria"), [])

    def test_a_prefix_of_the_name_matches_by_name(self) -> None:
        """`DEN` casa Dengue — pelo NOME, e isso é a funcionalidade."""
        got = filter_alerts(self.ALERTS, doenca="DEN")
        self.assertEqual([a["codigo_doenca"] for a in got], ["DENG"])

    def test_the_code_comparison_is_equality(self) -> None:
        """Código é identificador. `DENGX` contém `DENG` e não pode casar."""
        self.assertEqual(filter_alerts(self.ALERTS, doenca="DENGX"), [])

    def test_no_filter_returns_everything(self) -> None:
        self.assertEqual(len(filter_alerts(self.ALERTS)), 4)


if __name__ == "__main__":
    unittest.main()
