"""
Tratamento de credenciais e honestidade de endpoints.

Dois defeitos verificados no código em produção:

1. `UsageTracker.log_usage` gravava a chave de API CRUA em
   `data/usage.jsonl`. O middleware passa `request.headers.get("X-API-Key")`
   direto. O arquivo é append-only, sem rotação e sem lock — qualquer envio
   de logs, backup ou compartilhamento do diretório de dados vaza credencial
   em texto puro. E `UsageTracker`, ao contrário de `RateLimiter`, não tinha
   `threading.Lock`, então escritas concorrentes podiam se intercalar.

2. `/v1/export/pdf` respondia 200 com "Relatório PDF gerado com sucesso" e
   uma `download_url` apontando para `/reports/custom/...`, rota que não
   existe no app. Um 404 garantido, vendido como sucesso, atrás de um gate
   premium. Para um agente autônomo isso é pior que um erro: ele registra a
   operação como concluída.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app


class TestUsageLogNeverStoresTheKey(unittest.TestCase):
    def _tracker(self, path):
        return app.UsageTracker(path)

    def test_raw_key_never_reaches_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            self._tracker(path).log_usage(
                "premium_partner_key", "/v1/risk-index", "GET", 200
            )
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("premium_partner_key", content)

    def test_a_stable_fingerprint_keeps_usage_attributable(self) -> None:
        """Sem identificador não há como medir uso por cliente; o objetivo é
        remover o segredo, não a capacidade de contar."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            tracker = self._tracker(path)
            tracker.log_usage("chave-a", "/v1/x", "GET", 200)
            tracker.log_usage("chave-a", "/v1/x", "GET", 200)
            tracker.log_usage("chave-b", "/v1/x", "GET", 200)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["api_key"], rows[1]["api_key"])
            self.assertNotEqual(rows[0]["api_key"], rows[2]["api_key"])

    def test_anonymous_stays_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            self._tracker(path).log_usage("anonymous", "/v1/x", "GET", 200)
            row = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(row["api_key"], "anonymous")

    def test_concurrent_writes_do_not_interleave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            tracker = self._tracker(path)

            def hammer():
                for _ in range(40):
                    tracker.log_usage("chave", "/v1/x", "GET", 200)

            threads = [threading.Thread(target=hammer) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines), 160)
            for line in lines:
                json.loads(line)  # cada linha e' um JSON valido e completo

    def test_the_real_endpoint_does_not_log_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            original = app.usage_tracker
            app.usage_tracker = app.UsageTracker(path)
            try:
                with TestClient(app.app) as client:
                    client.get(
                        "/v1/risk-index?limite=1",
                        headers={"X-API-Key": "premium_partner_key"},
                    )
            finally:
                app.usage_tracker = original
            if path.exists():
                self.assertNotIn("premium_partner_key", path.read_text(encoding="utf-8"))


class TestASuiteNaoUsaAsChavesDaMaquina(unittest.TestCase):
    """A suite tem que trazer as proprias chaves.

    `data/users.json` guarda credenciais e nao e versionado. A suite lia esse
    arquivo do disco, entao passava aqui e falhava em qualquer outro lugar --
    medido num clone limpo: 8 testes.

    A primeira tentativa de corrigir instalou um fixture em
    `tests/__init__.py` e NAO funcionou, porque `unittest discover` importa
    os modulos como top-level: `tests/__init__.py` so roda quando alguem faz
    `from tests import ...`, o que em varios arquivos acontece depois de
    `import app`. O caminho ja tinha sido congelado no import. A suite
    continuou verde aqui, pelo arquivo real -- verde pelo motivo errado, que
    e a unica coisa pior que vermelho.

    Estas asseroes falham nesse cenario em vez de deixa-lo passar.
    """

    def test_o_fixture_esta_realmente_em_uso(self) -> None:
        from pathlib import Path

        usado = app.api_key_manager.path
        self.assertNotEqual(
            usado, Path(app.DEFAULT_USERS_DB_PATH),
            "a suite esta lendo o arquivo de chaves da maquina",
        )

    def test_as_chaves_carregadas_sao_as_do_fixture(self) -> None:
        from tests import TEST_USERS

        self.assertEqual(
            set(app.api_key_manager.keys), set(TEST_USERS["keys"])
        )

    def test_o_caminho_e_resolvido_a_cada_uso(self) -> None:
        """Congelar no import foi exatamente o defeito."""
        import os
        from unittest.mock import patch

        from pathlib import Path

        with patch.dict(os.environ, {"USERS_DB_PATH": "outro/caminho.json"}):
            self.assertEqual(app.users_db_path(), Path("outro/caminho.json"))

    def test_sem_nenhuma_origem_o_servico_nao_finge_ter_chaves(self) -> None:
        """Um deploy sem chaves precisa ficar sem chaves, e nao herdar as daqui."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"USERS_DB_PATH": "nao/existe.json"}, clear=False):
            os.environ.pop("USERS_DB_JSON", None)
            manager = app.APIKeyManager()
            self.assertEqual(manager.keys, {})
            self.assertIsNone(manager.validate_key("qualquer"))


class TestOArtefatoNaoCarregaCredencial(unittest.TestCase):
    """A imagem Docker nao pode sair com a chave de quem a construiu.

    Verificado construindo de verdade: a imagem gerada a partir desta arvore
    de trabalho continha `data/users.json`, porque o `.dockerignore` nao o
    excluia. Quem recebesse a imagem receberia a chave premium junto.

    Junto saiu o `.cache` local -- 42 MB da maquina de quem constroi, e pior
    que o peso: o container carregava o relatorio DAQUELE cache em vez do
    snapshot versionado, entao a imagem nao era reproduzivel. Medido depois
    da correcao: 345 MB -> 274 MB, e o log passa a dizer "carregado do
    snapshot embarcado".

    Nota deliberada sobre o `.vercelignore`: ele NAO exclui o arquivo, porque
    hoje e assim que producao recebe as chaves. Tirar de la sem antes definir
    `USERS_DB_JSON` no ambiente deixaria a API sem chave alguma. A correcao
    correta e mover para o segredo, e isso e decisao de quem opera.
    """

    def test_o_dockerignore_exclui_o_arquivo_de_chaves(self) -> None:
        from pathlib import Path

        raiz = Path(__file__).resolve().parent.parent
        regras = {
            linha.strip()
            for linha in (raiz / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if linha.strip() and not linha.strip().startswith("#")
        }
        self.assertIn(app.DEFAULT_USERS_DB_PATH, regras)

    def test_o_dockerignore_exclui_o_cache_local(self) -> None:
        from pathlib import Path

        raiz = Path(__file__).resolve().parent.parent
        regras = {
            linha.strip()
            for linha in (raiz / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if linha.strip() and not linha.strip().startswith("#")
        }
        self.assertIn(".cache", regras)

    def test_o_deploy_md_ensina_a_passar_as_chaves_sem_o_arquivo(self) -> None:
        from pathlib import Path

        texto = (
            Path(__file__).resolve().parent.parent / "docs" / "deploy.md"
        ).read_text(encoding="utf-8")
        self.assertIn("dockerignore", texto)
        self.assertIn("USERS_DB_JSON", texto)


class TestChavesPorVariavelDeAmbiente(unittest.TestCase):
    """`USERS_DB_JSON` e o caminho recomendado no deploy.md.

    Foi acrescentado junto com o aviso de chaves ausentes, e documentado como
    a forma preferida de carregar credenciais num deploy pelo git -- sem
    nenhum teste que o exercitasse. Anunciar uma saida de emergencia sem
    verificar que ela abre e a mesma familia de defeito que o `/planos`
    prometendo limites que o codigo nao aplicava.
    """

    CHAVES = {
        "keys": {
            "chave-do-ambiente": {
                "owner": "Teste",
                "tier": "premium",
                "rate_limit": 10000,
            }
        }
    }

    def test_carrega_as_chaves_do_ambiente(self) -> None:
        import json
        import os
        from unittest.mock import patch

        with patch.dict(
            os.environ, {"USERS_DB_JSON": json.dumps(self.CHAVES)}
        ):
            manager = app.APIKeyManager()
            self.assertIsNotNone(manager.validate_key("chave-do-ambiente"))
            self.assertEqual(
                manager.validate_key("chave-do-ambiente")["tier"], "premium"
            )

    def test_tem_precedencia_sobre_o_arquivo(self) -> None:
        """O deploy.md afirma isso; se mudar, a documentacao passa a mentir."""
        import json
        import os
        from unittest.mock import patch

        do_arquivo = app.api_key_manager.path
        self.assertTrue(do_arquivo.exists(), "fixture da suite sumiu")

        with patch.dict(
            os.environ, {"USERS_DB_JSON": json.dumps(self.CHAVES)}
        ):
            manager = app.APIKeyManager()
            self.assertEqual(set(manager.keys), {"chave-do-ambiente"})

    def test_json_invalido_nao_derruba_o_servico(self) -> None:
        """Segredo mal colado nao pode impedir o processo de subir."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"USERS_DB_JSON": "{isto nao e json"}):
            manager = app.APIKeyManager()
            self.assertEqual(manager.keys, {})
            self.assertIsNone(manager.validate_key("qualquer"))

    def test_o_deploy_md_descreve_a_precedencia_que_o_codigo_aplica(self) -> None:
        from pathlib import Path

        texto = (
            Path(__file__).resolve().parent.parent / "docs" / "deploy.md"
        ).read_text(encoding="utf-8")
        self.assertIn("USERS_DB_JSON", texto)
        self.assertIn("precedência sobre `USERS_DB_PATH`", texto)


class TestKeyValidation(unittest.TestCase):
    def test_accepts_a_plaintext_key_for_compatibility(self) -> None:
        manager = app.APIKeyManager.from_keys({"chave": {"tier": "free", "rate_limit": 10}})
        self.assertIsNotNone(manager.validate_key("chave"))

    def test_rejects_an_unknown_key(self) -> None:
        manager = app.APIKeyManager.from_keys({"chave": {"tier": "free", "rate_limit": 10}})
        self.assertIsNone(manager.validate_key("outra"))

    def test_accepts_a_hashed_key_entry(self) -> None:
        """Permite migrar users.json para hashes sem quebrar quem já usa."""
        import hashlib

        digest = hashlib.sha256(b"segredo").hexdigest()
        manager = app.APIKeyManager.from_keys({f"sha256:{digest}": {"tier": "premium", "rate_limit": 100}})
        self.assertIsNotNone(manager.validate_key("segredo"))
        self.assertIsNone(manager.validate_key("errado"))

    def test_empty_key_is_rejected(self) -> None:
        manager = app.APIKeyManager.from_keys({"chave": {"tier": "free"}})
        self.assertIsNone(manager.validate_key(""))


class TestPdfExportIsHonest(unittest.TestCase):
    def setUp(self) -> None:
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_does_not_claim_success_for_something_it_cannot_do(self) -> None:
        response = self.client.get(
            "/v1/export/pdf", headers={"X-API-Key": "premium_partner_key"}
        )
        self.assertEqual(response.status_code, 501)

    def test_never_returns_a_url_that_is_guaranteed_to_404(self) -> None:
        response = self.client.get(
            "/v1/export/pdf", headers={"X-API-Key": "premium_partner_key"}
        )
        self.assertNotIn("download_url", response.text)

    def test_points_to_the_endpoint_that_actually_works(self) -> None:
        response = self.client.get(
            "/v1/export/pdf", headers={"X-API-Key": "premium_partner_key"}
        )
        self.assertIn("/v1/professional-report", response.text)


if __name__ == "__main__":
    unittest.main()


class TestRefreshPathIsWired(unittest.TestCase):
    """O caminho de recarga estava morto desde o refactor da "Fase 0".

    `fetch_epidemiology_report` chama `load_latest_available_records`, que foi
    movida para `ingestion/sinan_loader.py` deixando no lugar apenas um
    comentário dizendo que mudou — o import nunca foi acrescentado. Toda
    chamada levantava NameError.

    Isso nunca apareceu porque a cache em disco não expirava: a função só
    seria alcançada quando a cache falhasse, e ela nunca falhava. E os testes
    de `/v1/refresh` sempre mockavam `load_or_refresh_report`. A causa raiz
    dos "132 dias" não era operacional — o serviço era incapaz de buscar
    dado novo.
    """

    def test_the_loader_is_importable_from_app(self) -> None:
        self.assertTrue(callable(getattr(app, "load_latest_available_records", None)))

    def test_fetch_does_not_raise_name_error(self) -> None:
        from unittest.mock import patch

        with patch.object(
            app, "load_latest_available_records", return_value=(2026, "http://x", [])
        ) as loader:
            report = app.fetch_epidemiology_report(2026)
        self.assertGreater(loader.call_count, 0)
        self.assertIn("metadata", report)
        self.assertEqual(report["metadata"]["erros"], [])

    def test_source_errors_are_collected_not_swallowed_silently(self) -> None:
        from unittest.mock import patch

        with patch.object(
            app, "load_latest_available_records", side_effect=OSError("fonte fora")
        ):
            report = app.fetch_epidemiology_report(2026)
        self.assertTrue(report["metadata"]["erros"])
        self.assertEqual(report["metadata"]["status"], "empty")


class TestUsageLogNeverBreaksTheRequest(unittest.TestCase):
    """Telemetria não pode derrubar a API.

    Encontrado no deploy: em produção na Vercel o sistema de arquivos é
    somente leitura fora de `/tmp`, e `UsageTracker.log_usage` abria o arquivo
    para escrita sem proteção. O middleware só registra caminhos `/v1/*`, então
    o efeito era exato e desconcertante:

        /dashboard        -> 200
        /v1/risk-index    -> 500
        /v1/metadata      -> 500
        /v1/diseases      -> 500

    O painel funcionava e a API inteira caía. Contar requisições é telemetria;
    falhar ao contar não pode custar a resposta.
    """

    def test_a_read_only_destination_does_not_raise(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        tracker = app.UsageTracker.__new__(app.UsageTracker)
        tracker.log_path = Path("/caminho/somente/leitura/usage.jsonl")
        tracker._lock = threading.Lock()

        with patch("builtins.open", side_effect=OSError("read-only file system")):
            tracker.log_usage("anonymous", "/v1/risk-index", "GET", 200)

    def test_the_endpoint_answers_even_when_logging_fails(self) -> None:
        from unittest.mock import patch

        app.rate_limiter.reset()
        with patch.object(
            app.usage_tracker, "log_usage", side_effect=OSError("read-only")
        ):
            with TestClient(app.app) as client:
                response = client.get("/v1/diseases")
        self.assertEqual(response.status_code, 200)

    def test_a_directory_that_cannot_be_created_does_not_break_startup(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        with patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            tracker = app.UsageTracker(Path("/caminho/impossivel/usage.jsonl"))
        self.assertIsNotNone(tracker)
