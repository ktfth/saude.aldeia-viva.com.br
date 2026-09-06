"""
A cache agregada em disco não tinha prazo de validade.

`load_or_refresh_report` fazia `if cache_path.exists(): usar` — sem olhar a
idade. A chave da cache é `(versão, ano, agravos habilitados)`, então
enquanto o ano não virar, o mesmo arquivo é servido para sempre. Medido: o
relatório em produção foi carregado em 2026-04-26 e continuava sendo servido
132 dias depois. A barra de estado avisava o usuário, mas nada no sistema
jamais buscaria dado novo.

Duas regras que não podem ser trocadas uma pela outra:

  1. Cache vencida não é servida sem antes tentar buscar dado novo.
  2. Se a busca falhar, a cache vencida VOLTA a ser usada. Dado velho e
     rotulado é melhor que painel vazio — e o rótulo já existe em
     `metadata.carga`.
"""

import unittest
from datetime import date

from tests import preserved_report_state

from aggregation.cache_policy import (
    DEFAULT_MAX_AGE_DAYS,
    cache_age_days,
    cache_is_fresh,
)


def _report(loaded_at):
    return {"metadata": {"carregado_em": loaded_at, "status": "ok"}}


TODAY = date(2026, 9, 5)


class TestCacheAge(unittest.TestCase):
    def test_age_in_days(self) -> None:
        self.assertEqual(
            cache_age_days(_report("2026-04-26T02:06:07+00:00"), TODAY), 132
        )

    def test_same_day_is_zero(self) -> None:
        self.assertEqual(cache_age_days(_report("2026-09-05"), TODAY), 0)

    def test_missing_timestamp_is_unknown(self) -> None:
        self.assertIsNone(cache_age_days({"metadata": {}}, TODAY))
        self.assertIsNone(cache_age_days({}, TODAY))

    def test_garbage_timestamp_is_unknown(self) -> None:
        self.assertIsNone(cache_age_days(_report("nao-e-data"), TODAY))


class TestCacheFreshness(unittest.TestCase):
    def test_recent_cache_is_fresh(self) -> None:
        self.assertTrue(cache_is_fresh(_report("2026-09-01"), TODAY, max_age_days=7))

    def test_boundary_is_inclusive(self) -> None:
        self.assertTrue(cache_is_fresh(_report("2026-08-29"), TODAY, max_age_days=7))
        self.assertFalse(cache_is_fresh(_report("2026-08-28"), TODAY, max_age_days=7))

    def test_the_real_production_cache_is_stale(self) -> None:
        self.assertFalse(cache_is_fresh(_report("2026-04-26T02:06:07+00:00"), TODAY))

    def test_unknown_age_is_treated_as_stale(self) -> None:
        """Sem carimbo não há como afirmar frescor; tentar buscar é o correto."""
        self.assertFalse(cache_is_fresh({"metadata": {}}, TODAY))

    def test_zero_disables_the_expiry(self) -> None:
        """Escape hatch para ambientes sem rede — nunca expira."""
        self.assertTrue(
            cache_is_fresh(_report("2020-01-01"), TODAY, max_age_days=0)
        )

    def test_default_max_age_is_declared(self) -> None:
        self.assertGreater(DEFAULT_MAX_AGE_DAYS, 0)


class TestRefreshBehaviour(unittest.TestCase):
    """O comportamento que importa: vencida tenta atualizar, e cai de volta."""

    def setUp(self) -> None:
        import os
        import app

        self.app = app
        # A suíte roda com a expiração desligada (ver tests/__init__.py).
        # Estes testes são justamente sobre a expiração, então a religam.
        self._previous = os.environ.get("SINAN_REPORT_CACHE_MAX_AGE_DAYS")
        os.environ["SINAN_REPORT_CACHE_MAX_AGE_DAYS"] = "7"

    def tearDown(self) -> None:
        import os

        if self._previous is None:
            os.environ.pop("SINAN_REPORT_CACHE_MAX_AGE_DAYS", None)
        else:
            os.environ["SINAN_REPORT_CACHE_MAX_AGE_DAYS"] = self._previous

    def test_stale_cache_triggers_a_refresh_attempt(self) -> None:
        from unittest.mock import patch
        import tempfile
        from pathlib import Path

        with preserved_report_state(), tempfile.TemporaryDirectory() as tmp:
            stale = {
                "metadata": {"carregado_em": "2020-01-01", "status": "ok"},
                "municipios": [{"codigo_municipio": "355030", "doencas": []}],
                "alertas_altos": [],
            }
            path = Path(tmp) / "cache.json"
            self.app.save_report_cache(stale, path)

            fresh = {
                "metadata": {"carregado_em": "2026-09-05", "status": "ok"},
                "municipios": [{"codigo_municipio": "999999", "doencas": []}],
                "alertas_altos": [],
            }
            with patch.object(self.app, "report_cache_path", return_value=path), patch.object(
                self.app, "fetch_epidemiology_report", return_value=fresh
            ) as fetch:
                self.app.load_or_refresh_report(2026)
            fetch.assert_called_once()

    def test_failed_refresh_falls_back_to_the_stale_cache(self) -> None:
        """Nunca trocar dado velho rotulado por painel vazio."""
        from unittest.mock import patch
        import tempfile
        from pathlib import Path

        with preserved_report_state(), tempfile.TemporaryDirectory() as tmp:
            stale = {
                "metadata": {"carregado_em": "2020-01-01", "status": "ok"},
                "municipios": [{"codigo_municipio": "355030", "doencas": []}],
                "alertas_altos": [],
            }
            path = Path(tmp) / "cache.json"
            self.app.save_report_cache(stale, path)

            with patch.object(self.app, "report_cache_path", return_value=path), patch.object(
                self.app, "fetch_epidemiology_report", side_effect=OSError("sem rede")
            ), patch.object(self.app, "load_embedded_report_snapshot", None):
                report = self.app.load_or_refresh_report(2026)

            self.assertTrue(self.app.report_has_content(report))
            self.assertEqual(report["municipios"][0]["codigo_municipio"], "355030")

    def test_fresh_cache_does_not_hit_the_network(self) -> None:
        from unittest.mock import patch
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with preserved_report_state(), tempfile.TemporaryDirectory() as tmp:
            fresh = {
                "metadata": {
                    "carregado_em": datetime.now(UTC).isoformat(),
                    "status": "ok",
                },
                "municipios": [{"codigo_municipio": "355030", "doencas": []}],
                "alertas_altos": [],
            }
            path = Path(tmp) / "cache.json"
            self.app.save_report_cache(fresh, path)

            with patch.object(self.app, "report_cache_path", return_value=path), patch.object(
                self.app, "fetch_epidemiology_report"
            ) as fetch:
                self.app.load_or_refresh_report(2026)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestEnrichedReportIsTheOnlyShape(unittest.TestCase):
    """`load_or_refresh_report` devolvia o relatório CRU enquanto o estado
    global recebia o enriquecido: duas formas para o mesmo fato, que é
    exatamente a armadilha que o DESIGN.md proíbe em "uma autoridade por
    fato". Quem usasse o valor de retorno não veria recência, população nem
    incidência.
    """

    ENRICHED_FIELDS = ("recencia", "populacao", "incidencia", "nivel_risco_fonte_atual")

    def test_return_value_carries_the_same_enrichment_as_the_globals(self) -> None:
        import app

        with preserved_report_state():
            report = app.load_or_refresh_report(2026)
        municipality = report["municipios"][0]
        for field in self.ENRICHED_FIELDS:
            self.assertIn(field, municipality, f"faltou {field} no retorno")

    def test_metadata_of_the_return_value_carries_the_clocks(self) -> None:
        import app

        with preserved_report_state():
            metadata = app.load_or_refresh_report(2026)["metadata"]
        self.assertIn("carga", metadata)
        self.assertIn("recencia", metadata)
        self.assertIn("populacao", metadata)

    def test_apply_report_state_returns_what_it_applied(self) -> None:
        import app

        with preserved_report_state():
            applied = self._apply(app)
        self.assertIn("recencia", applied["municipios"][0])

    @staticmethod
    def _apply(app):
        return app.apply_report_state(
            {
                "metadata": {"status": "ok"},
                "municipios": [{"codigo_municipio": "355030", "doencas": []}],
                "alertas_altos": [],
            },
            cache_hit=False,
        )


class TestLifespanIsIdempotent(unittest.TestCase):
    """Reentrar no lifespan não pode reparsear o relatório inteiro.

    Cada `TestClient(app.app)` sobe o lifespan, que chamava
    `load_or_refresh_report` incondicionalmente: 1,64s para ler e converter os
    17 MB da cache, vinte vezes na suíte. O estado já está em memória — só a
    primeira entrada precisa carregá-lo.

    Vale além do teste: um processo que reinicie o ciclo de vida da aplicação
    sem reiniciar o interpretador não deveria pagar de novo por um dado que
    não mudou. `SINAN_FORCE_REFRESH=1` continua forçando.
    """

    def test_second_client_does_not_reload(self) -> None:
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        import app

        with preserved_report_state():
            with TestClient(app.app):
                pass  # primeira entrada carrega
            with patch.object(app, "load_or_refresh_report") as carga:
                with TestClient(app.app):
                    pass
            carga.assert_not_called()

    def test_an_empty_state_still_loads(self) -> None:
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        import app

        with preserved_report_state():
            app.db_clini = []
            with patch.object(app, "load_or_refresh_report") as carga:
                with TestClient(app.app):
                    pass
            carga.assert_called_once()

    def test_force_refresh_still_forces(self) -> None:
        import os
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        import app

        with preserved_report_state():
            with TestClient(app.app):
                pass
            os.environ["SINAN_FORCE_REFRESH"] = "1"
            try:
                with patch.object(app, "load_or_refresh_report") as carga:
                    with TestClient(app.app):
                        pass
                carga.assert_called_once()
            finally:
                os.environ.pop("SINAN_FORCE_REFRESH", None)
