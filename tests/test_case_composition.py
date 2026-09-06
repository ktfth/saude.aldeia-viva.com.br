"""
Quanto de "caso provável" ainda é notificação não investigada.

`casos_provaveis` é notificações menos descartados — definição padrão do
SINAN, e correta. O defeito não é a contagem: é que a composição do número
nunca foi declarada.

Medido no relatório real:

    Dengue        45,4% dos casos prováveis sem classificação final
    Chikungunya   41,2%
    Zika          25,5%
    Hanseníase   100,0%
    Meningite      2,8%
    total         41,6% de 389.531

E `casos_descartados` é zero em oito dos dez agravos — não porque nada foi
descartado, mas porque nada foi encerrado.

Isso importa porque `casos_provaveis` é o numerador de `incidencia.por_100k`,
que a iteração 2 promoveu a "única medida comparável entre municípios". Duas
incidências construídas sobre frações pendentes de 3% e de 100% não estão na
mesma escala, e nada dizia isso.

Como no caso da cobertura de sinais: o módulo não conserta a fonte, declara a
composição.
"""

import unittest

from aggregation.case_composition import (
    UNRESOLVED_LABELS,
    composition_of,
    with_case_composition,
)


def _disease(codigo, casos, **classificacoes):
    return {
        "codigo": codigo,
        "nome": codigo,
        "casos_provaveis": casos,
        "casos_descartados": 0,
        "classificacoes": dict(classificacoes),
    }


class TestCompositionOf(unittest.TestCase):
    def test_counts_the_unresolved_labels(self) -> None:
        got = composition_of(
            _disease("DENG", 100, **{"Dengue": 60, "Sem classificação final": 30, "Inconclusivo": 10})
        )
        self.assertEqual(got["sem_classificacao_final"], 40)
        self.assertAlmostEqual(got["proporcao_pendente"], 0.4)

    def test_fully_resolved(self) -> None:
        got = composition_of(_disease("DENG", 50, **{"Dengue": 50}))
        self.assertEqual(got["sem_classificacao_final"], 0)
        self.assertEqual(got["proporcao_pendente"], 0.0)
        self.assertTrue(got["comparavel"])

    def test_fully_unresolved_is_flagged(self) -> None:
        """Hanseníase: 100% sem classificação em 30.114 casos."""
        got = composition_of(_disease("HANS", 80, **{"Sem classificação final": 80}))
        self.assertEqual(got["proporcao_pendente"], 1.0)
        self.assertFalse(got["comparavel"])
        self.assertIn("100", got["ressalva"])

    def test_threshold_marks_high_pending_fractions(self) -> None:
        alto = composition_of(_disease("DENG", 100, **{"Sem classificação final": 45}))
        baixo = composition_of(_disease("MENI", 100, **{"Sem classificação final": 3}))
        self.assertFalse(alto["comparavel"])
        self.assertTrue(baixo["comparavel"])

    def test_no_cases_degrades_gracefully(self) -> None:
        got = composition_of(_disease("X", 0))
        self.assertEqual(got["proporcao_pendente"], 0.0)
        self.assertIsNone(got["ressalva"])

    def test_missing_classificacoes_does_not_break(self) -> None:
        got = composition_of({"codigo": "X", "casos_provaveis": 10})
        self.assertEqual(got["sem_classificacao_final"], 0)

    def test_unresolved_labels_are_declared(self) -> None:
        self.assertIn("Sem classificação final", UNRESOLVED_LABELS)
        self.assertIn("Inconclusivo", UNRESOLVED_LABELS)


class TestWithCaseComposition(unittest.TestCase):
    def _report(self):
        return {
            "municipios": [
                {
                    "codigo_municipio": "355030",
                    "doencas": [
                        _disease("DENG", 100, **{"Dengue": 55, "Sem classificação final": 45}),
                        _disease("MENI", 100, **{"Meningite": 97, "Sem classificação final": 3}),
                    ],
                    "doencas_altas": [],
                }
            ],
            "metadata": {},
        }

    def test_attaches_the_block_to_every_disease(self) -> None:
        got = with_case_composition(self._report())
        deng = got["municipios"][0]["doencas"][0]
        self.assertAlmostEqual(deng["composicao"]["proporcao_pendente"], 0.45)

    def test_does_not_mutate_the_input(self) -> None:
        report = self._report()
        with_case_composition(report)
        self.assertNotIn("composicao", report["municipios"][0]["doencas"][0])

    def test_metadata_summarizes_by_disease(self) -> None:
        resumo = with_case_composition(self._report())["metadata"]["composicao_dos_casos"]
        self.assertAlmostEqual(resumo["por_agravo"]["DENG"]["proporcao_pendente"], 0.45)
        self.assertEqual(resumo["agravos_pouco_comparaveis"], ["DENG"])
        self.assertIn("aviso", resumo)

    def test_the_warning_names_the_consequence(self) -> None:
        resumo = with_case_composition(self._report())["metadata"]["composicao_dos_casos"]
        self.assertIn("incidência", resumo["aviso"].lower())

    def test_national_proportion_is_published(self) -> None:
        resumo = with_case_composition(self._report())["metadata"]["composicao_dos_casos"]
        self.assertAlmostEqual(resumo["proporcao_pendente_geral"], 0.24)


if __name__ == "__main__":
    unittest.main()
