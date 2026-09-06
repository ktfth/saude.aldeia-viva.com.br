"""
Renderização da dimensão temporal.

A tira de agravos existe porque um gráfico de linha com dois pontos custava
~200 KB de Chart.js via CDN e respondia a uma pergunta que ninguém faz. A tira
é SVG inline gerado no servidor: 0 KB de JS, funciona sem JS, cabe em 360px, e
mostra o fato que mais muda uma decisão e que era invisível — de que ano é a
fonte de cada agravo deste município.
"""

import unittest

from presentation.signal import (
    render_data_status,
    render_risk_cell,
    render_signal_strip,
    render_signal_tag,
    render_source_tag,
    source_bucket,
)


def _disease(codigo, nome, ano, frescor="vivo", rotulo="ontem"):
    return {
        "codigo": codigo,
        "nome": nome,
        "fonte": {"ano": ano, "rotulo": f"há {2026 - ano} anos", "do_ano_corrente": ano == 2026},
        "recencia": {"frescor": frescor, "rotulo": rotulo},
    }


class TestSourceBucket(unittest.TestCase):
    def test_current_year(self) -> None:
        self.assertEqual(source_bucket({"ano": 2026, "do_ano_corrente": True}, 2026), "atual")

    def test_one_year_behind(self) -> None:
        self.assertEqual(source_bucket({"ano": 2025, "do_ano_corrente": False}, 2026), "recente")

    def test_two_or_more_years_behind(self) -> None:
        self.assertEqual(source_bucket({"ano": 2022, "do_ano_corrente": False}, 2026), "antiga")

    def test_missing_source(self) -> None:
        self.assertEqual(source_bucket({"ano": None, "do_ano_corrente": False}, 2026), "ausente")

    def test_tolerates_missing_block(self) -> None:
        self.assertEqual(source_bucket(None, 2026), "ausente")


class TestRenderSignalStrip(unittest.TestCase):
    def test_one_segment_per_disease(self) -> None:
        html = render_signal_strip(
            [_disease("DENG", "Dengue", 2026), _disease("MENI", "Meningite", 2022)], 2026
        )
        self.assertEqual(html.count("<rect"), 2)

    def test_is_inline_svg_without_scripts(self) -> None:
        html = render_signal_strip([_disease("DENG", "Dengue", 2026)], 2026)
        self.assertIn("<svg", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("http", html)

    def test_each_segment_names_the_disease_and_its_source_year(self) -> None:
        html = render_signal_strip([_disease("MENI", "Meningite", 2022)], 2026)
        self.assertIn("Meningite", html)
        self.assertIn("2022", html)

    def test_caption_counts_current_sources(self) -> None:
        html = render_signal_strip(
            [
                _disease("DENG", "Dengue", 2026),
                _disease("CHIK", "Chikungunya", 2026),
                _disease("MENI", "Meningite", 2022),
            ],
            2026,
        )
        self.assertIn("2 de 3", html)

    def test_empty_list_renders_a_readable_placeholder(self) -> None:
        html = render_signal_strip([], 2026)
        self.assertNotIn("<rect", html)
        self.assertIn("Sem agravos", html)

    def test_escapes_disease_names(self) -> None:
        html = render_signal_strip([_disease("X", '<script>"x"', 2026)], 2026)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_accessible_label(self) -> None:
        html = render_signal_strip([_disease("DENG", "Dengue", 2026)], 2026)
        self.assertIn('role="img"', html)
        self.assertIn("aria-label", html)


class TestRenderSignalTag(unittest.TestCase):
    def test_renders_level_class_and_label(self) -> None:
        html = render_signal_tag({"frescor": "dormente", "rotulo": "há 4 meses"})
        self.assertIn('class="signal-tag dormente"', html)
        self.assertIn("há 4 meses", html)

    def test_handles_missing_block(self) -> None:
        html = render_signal_tag(None)
        self.assertIn("desconhecido", html)

    def test_escapes_label(self) -> None:
        html = render_signal_tag({"frescor": "vivo", "rotulo": "<b>x"})
        self.assertNotIn("<b>", html)


class TestRenderSourceTag(unittest.TestCase):
    def test_current_source_is_plain(self) -> None:
        html = render_source_tag({"ano": 2026, "do_ano_corrente": True, "rotulo": "há 4 meses"})
        self.assertIn("2026", html)
        self.assertNotIn("is-old", html)

    def test_old_source_is_marked(self) -> None:
        html = render_source_tag({"ano": 2022, "do_ano_corrente": False, "rotulo": "há 3 anos"})
        self.assertIn("is-old", html)
        self.assertIn("2022", html)

    def test_missing_source(self) -> None:
        self.assertIn("sem fonte", render_source_tag({"ano": None}))


class TestRenderDataStatus(unittest.TestCase):
    def _metadata(self, **overrides):
        base = {
            "carga": {
                "carregado_em": "2026-04-26",
                "idade_dias": 132,
                "atualizada": False,
                "aviso": "A carga não é atualizada há mais tempo que o recomendado.",
            },
            "recencia": {
                "agravos_com_fonte_do_ano_corrente": 3,
                "agravos_total": 10,
                "fontes_por_ano": {"2026": 3, "2022": 1},
            },
        }
        base.update(overrides)
        return base

    def test_shows_load_age_and_source_coverage(self) -> None:
        html = render_data_status(self._metadata())
        self.assertIn("132", html)
        self.assertIn("3 de 10", html)

    def test_stale_load_is_visually_flagged(self) -> None:
        html = render_data_status(self._metadata())
        self.assertIn("is-stale", html)

    def test_fresh_load_is_not_flagged(self) -> None:
        meta = self._metadata()
        meta["carga"] = {"carregado_em": "2026-09-05", "idade_dias": 0, "atualizada": True, "aviso": None}
        html = render_data_status(meta)
        self.assertNotIn("is-stale", html)

    def test_survives_empty_metadata(self) -> None:
        html = render_data_status({})
        self.assertIn("data-status", html)



class TestRenderRiskCell(unittest.TestCase):
    """A célula de risco mostra o nível acionável, sem esconder o histórico.

    Medido no dado real: usar o nível consolidado de todos os anos-fonte
    deixava 1.296 dos 5.339 municípios em "crítico". Restringir aos agravos
    de fonte do ano corrente leva a 468 — e 828 municípios saíam do topo
    porque o "crítico" deles vinha de Meningite de 2022 ou Leptospirose de
    2024. Esses 828 não podem simplesmente sumir: em 54,5% dos municípios o
    histórico é mais grave que o presente, e isso precisa estar dito.
    """

    def _row(self, atual="alto", historico="critico", mais_grave=True):
        return {
            "nivel_risco": historico,
            "nivel_risco_fonte_atual": atual,
            "historico_mais_grave": mais_grave,
            "recencia": {"frescor": "vivo", "rotulo": "ontem"},
        }

    def test_badge_uses_the_current_source_level(self) -> None:
        html = render_risk_cell(self._row(), lambda level: f"[{level}]")
        self.assertIn("[alto]", html)
        self.assertNotIn("[critico]", html)

    def test_declares_the_worse_history_instead_of_hiding_it(self) -> None:
        html = render_risk_cell(self._row(), lambda level: f"[{level}]")
        self.assertIn("histórico", html.lower())
        self.assertIn("critico", html)

    def test_stays_quiet_when_history_matches_the_present(self) -> None:
        html = render_risk_cell(
            self._row(atual="critico", historico="critico", mais_grave=False),
            lambda level: f"[{level}]",
        )
        self.assertNotIn("histórico", html.lower())

    def test_falls_back_to_nivel_risco_when_the_new_field_is_absent(self) -> None:
        """Dados antigos, sem enriquecimento, não podem quebrar a página."""
        html = render_risk_cell({"nivel_risco": "moderado"}, lambda level: f"[{level}]")
        self.assertIn("[moderado]", html)

    def test_includes_the_signal_tag(self) -> None:
        html = render_risk_cell(self._row(), lambda level: f"[{level}]")
        self.assertIn("signal-tag", html)

if __name__ == "__main__":
    unittest.main()
