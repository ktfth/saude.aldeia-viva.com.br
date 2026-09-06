"""
O que os docs prometem tem que funcionar.

`docs/` estava congelado em 2026-04-26, antes de seis iterações de mudanças.
Medido antes desta rede existir:

  - Dois exemplos `curl` escritos nos docs devolviam **403**:
    `POST /v1/refresh` em `api-referencia.md` e em `quickstart.md`. A
    proteção do endpoint foi acrescentada na iteração 3 — correta — e os
    docs nunca foram atualizados. Dívida criada por mim.
  - Três parâmetros de `/v1/risk-index` não documentados: `ano`, que sempre
    existiu, mais `ordenar` e `pagina`, acrescentados nas iterações 2 e 4.
  - Três endpoints ausentes da referência: `/v1/bairros`,
    `/v1/professional-report` e `/v1/export/pdf`.

Um exemplo de documentação que falha é pior que a ausência dele: é a
primeira coisa que alguém copia. Esta rede executa todos.
"""

import inspect
import re
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from fastapi.testclient import TestClient

import app
from tests import ensure_real_report

DOCS = Path(__file__).resolve().parent.parent / "docs"
REFERENCE = DOCS / "api-referencia.md"

CURL = re.compile(r"curl\s+(?:-X\s+(\w+)\s+)?(?:-H\s+\"[^\"]+\"\s+)*\"([^\"]+)\"")
HEADER = re.compile(r'-H\s+"([^:]+):\s*([^"]+)"')

# Exemplos que demonstram deliberadamente uma resposta de erro. Cada entrada
# precisa de motivo — a lista existe para não virar tapete.
EXPECTED_ERRORS: dict[str, str] = {}


def curl_examples() -> list[tuple[str, str, str, dict[str, str]]]:
    """(documento, método, caminho, cabeçalhos) de cada exemplo em docs/."""
    found: list[tuple[str, str, str, dict[str, str]]] = []
    for doc in sorted(DOCS.glob("*.md")):
        text = doc.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = CURL.search(line)
            if not match:
                continue
            method = (match.group(1) or "GET").upper()
            parsed = urlparse(match.group(2))
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            headers = dict(HEADER.findall(line))
            found.append((doc.name, method, path, headers))
    return found


def documented_parameters(endpoint: str) -> set[str]:
    """Parâmetros listados sob o cabeçalho daquele endpoint na referência."""
    text = REFERENCE.read_text(encoding="utf-8")
    heading = f"## `GET {endpoint}`"
    # Começa DEPOIS da linha do próprio cabeçalho, senão o corte abaixo casa
    # com ele mesmo na posição zero e a fatia sai vazia.
    rest = text[text.index(heading) + len(heading) :]
    # Para no próximo cabeçalho de qualquer nível: a seção "### Campos que
    # exigem atenção" também usa lista com crase e seria lida como parâmetro.
    proximo = re.search(r"^#{1,6} ", rest, re.M)
    end = proximo.start() if proximo else len(rest)
    return set(re.findall(r"^- `(\w+)`", rest[:end], re.M))


def implemented_parameters(handler) -> set[str]:
    return {
        name
        for name in inspect.signature(handler).parameters
        if name not in ("user", "response")
    }


class TestDocumentedExamplesRun(unittest.TestCase):
    def setUp(self) -> None:
        ensure_real_report()
        app.rate_limiter.reset()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_there_are_examples_to_check(self) -> None:
        """Guarda contra a extração silenciosamente parar de achar exemplos."""
        self.assertGreaterEqual(len(curl_examples()), 8)

    def test_every_curl_example_succeeds(self) -> None:
        # A recarga real e' testada em test_cache_freshness; aqui o que
        # importa e' a forma da requisicao documentada ser aceita.
        with patch.object(app, "load_or_refresh_report", return_value=None):
            for doc, method, path, headers in curl_examples():
                with self.subTest(doc=doc, method=method, path=path):
                    app.rate_limiter.reset()
                    response = self.client.request(method, path, headers=headers)
                    motivo = EXPECTED_ERRORS.get(f"{method} {path}")
                    if motivo:
                        self.assertGreaterEqual(response.status_code, 400, motivo)
                    else:
                        self.assertLess(
                            response.status_code,
                            400,
                            f"{doc}: `curl {method} {path}` devolve "
                            f"{response.status_code}",
                        )

    def test_expected_errors_are_justified(self) -> None:
        for key, reason in EXPECTED_ERRORS.items():
            self.assertTrue(reason, f"exceção de {key} sem motivo escrito")


class TestReferenceMatchesTheCode(unittest.TestCase):
    def test_risk_index_parameters_are_all_documented(self) -> None:
        documented = documented_parameters("/v1/risk-index")
        implemented = implemented_parameters(app.get_risk_index)
        self.assertEqual(
            implemented - documented,
            set(),
            "parâmetros que existem e não estão na referência",
        )

    def test_reference_does_not_invent_parameters(self) -> None:
        documented = documented_parameters("/v1/risk-index")
        implemented = implemented_parameters(app.get_risk_index)
        self.assertEqual(
            documented - implemented,
            set(),
            "parâmetros documentados que o endpoint não aceita",
        )

    def test_high_alerts_parameters_are_all_documented(self) -> None:
        documented = documented_parameters("/v1/high-alerts")
        implemented = implemented_parameters(app.get_high_alerts)
        self.assertEqual(implemented - documented, set())

    def test_every_v1_endpoint_appears_in_the_reference(self) -> None:
        text = REFERENCE.read_text(encoding="utf-8")
        rotas = {
            route.path
            for route in app.app.routes
            if getattr(route, "path", "").startswith("/v1/")
        }
        ausentes = sorted(path for path in rotas if path not in text)
        self.assertEqual(ausentes, [], "endpoints /v1 fora da referência")

    def test_response_headers_are_documented(self) -> None:
        """Sem eles o cliente não sabe que a resposta foi cortada."""
        text = REFERENCE.read_text(encoding="utf-8")
        for header in (
            "X-Total-Results",
            "X-Returned-Results",
            "X-Limit-Applied",
            "X-Page",
            "X-Has-More",
        ):
            self.assertIn(header, text)

    def test_tier_ceiling_is_documented(self) -> None:
        """Pedir 100 sem chave devolve 5; isso precisa estar escrito."""
        from domain.tiers import tier_by_code

        text = REFERENCE.read_text(encoding="utf-8")
        self.assertIn(str(tier_by_code("anonymous").max_results), text)
        self.assertIn("X-API-Key", text)


class TestPresentationRoutesAreListed(unittest.TestCase):
    def test_every_html_page_is_listed(self) -> None:
        text = REFERENCE.read_text(encoding="utf-8")
        for path in ("/dashboard", "/sobre", "/planos", "/agentes"):
            self.assertIn(path, text)



class TestNoDocRecommendsOrderingByScore(unittest.TestCase):
    """A mesma afirmação errada apareceu em três lugares diferentes.

    "`risk_score` para ordenação" estava em `/agentes` (corrigido na iteração
    5), em `/sobre` (iteração 6) e em `docs/metodologia.md`. `risk_score` é
    soma de contagens absolutas e correlaciona 0,822 com a população: ordenar
    por ele é aproximadamente ordenar por tamanho de cidade.

    Este teste não caça a frase — caça a *família*. Qualquer arquivo que
    mencione `risk_score` precisa mencionar também a ressalva, senão o leitor
    sai dali achando que aquele é o eixo de prioridade.
    """

    CAVEAT = ("0,82", "populaç", "absolut")

    def _files(self) -> list[Path]:
        root = DOCS.parent
        return [
            *sorted(DOCS.glob("*.md")),
            root / "README.md",
            root / "DESIGN.md",
        ]

    def test_every_mention_of_risk_score_carries_the_caveat(self) -> None:
        for path in self._files():
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if "risk_score" not in text:
                continue
            with self.subTest(doc=path.name):
                self.assertTrue(
                    any(termo in text for termo in self.CAVEAT),
                    f"{path.name} menciona risk_score sem a ressalva de que ele "
                    "é contagem absoluta e correlaciona com a população",
                )

    def test_no_doc_presents_score_as_the_ordering_axis(self) -> None:
        proibidas = (
            "`risk_score` permite ordenação",
            "risk_score para ordenação",
            "O score organiza prioridade",
        )
        for path in self._files():
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for frase in proibidas:
                with self.subTest(doc=path.name, frase=frase):
                    self.assertNotIn(frase, text)

    def test_the_comparable_measure_is_named_where_it_matters(self) -> None:
        """A referência e a metodologia precisam apontar a incidência."""
        for nome in ("api-referencia.md", "metodologia.md"):
            with self.subTest(doc=nome):
                text = (DOCS / nome).read_text(encoding="utf-8")
                self.assertIn("por_100k", text)


class TestDeployDocumentsEveryEnvVar(unittest.TestCase):
    """Uma variável de ambiente que ninguém documenta é um botão escondido.

    `docs/deploy.md` listava sete variáveis e o código lê onze. As quatro
    ausentes incluem `SINAN_REPORT_CACHE_MAX_AGE_DAYS`, que controla quando a
    cache expira — exatamente o parâmetro cuja ausência manteve a base parada
    por 133 dias.
    """

    # Lidas pelo código mas fora do escopo de deploy. Cada uma com motivo.
    EXEMPT = {
        "SIGNAL_REFERENCE_DATE": "fixa a data de referência; uso de teste e reprodução",
    }

    def _project_modules(self) -> list[Path]:
        root = DOCS.parent
        pastas = ("domain", "ingestion", "aggregation", "presentation")
        return [root / "app.py", *(p for d in pastas for p in (root / d).glob("*.py"))]

    def _env_vars(self) -> set[str]:
        found: set[str] = set()
        for path in self._project_modules():
            if path.exists():
                found |= set(
                    re.findall(r'os\.getenv\(\s*"([A-Z_]+)"', path.read_text(encoding="utf-8"))
                )
        return found

    def test_the_scan_finds_the_known_ones(self) -> None:
        """Guarda contra o teste virar vácuo se a varredura quebrar."""
        found = self._env_vars()
        for esperada in ("SINAN_YEAR", "SINAN_CACHE_DIR", "SINAN_REPORT_CACHE_MAX_AGE_DAYS"):
            self.assertIn(esperada, found)

    def test_every_env_var_is_documented(self) -> None:
        text = (DOCS / "deploy.md").read_text(encoding="utf-8")
        ausentes = sorted(
            var for var in self._env_vars() if var not in text and var not in self.EXEMPT
        )
        self.assertEqual(
            ausentes, [], "variáveis lidas pelo código e ausentes de deploy.md"
        )

    def test_exemptions_are_justified(self) -> None:
        for var, motivo in self.EXEMPT.items():
            self.assertTrue(motivo, f"isenção de {var} sem motivo escrito")

    def test_post_deploy_checks_include_the_data_age(self) -> None:
        """Um deploy que sobe com cache de quatro meses precisa ser detectado."""
        text = (DOCS / "deploy.md").read_text(encoding="utf-8")
        self.assertIn("carga", text.lower())


class TestDeployTargetsActuallyExist(unittest.TestCase):
    """O que o deploy.md documenta precisa existir no repositório.

    `api/index.py` continha uma linha — `from app import app` — e era o
    entrypoint Python da Vercel. O commit `e129698`, de 2026-04-26, com a
    mensagem "fix deploy and improved docs", o REMOVEU. Não existe
    `vercel.json`. Sobraram `.vercel/project.json` apontando para um projeto
    ativo, `api/data/reports/*.json` com 17 MB versionados, um `.pyc` órfão
    de `index.py` — e a documentação afirmando que o deploy é na Vercel.

    Esta verificação não decide qual caminho é o certo; ela impede que o
    documento afirme um alvo de deploy cujo artefato não está no repositório.
    """

    ROOT = DOCS.parent

    def _deploy_text(self) -> str:
        return (DOCS / "deploy.md").read_text(encoding="utf-8")

    def test_docker_target_has_its_files(self) -> None:
        text = self._deploy_text()
        if "docker" not in text.lower():
            self.skipTest("deploy.md não documenta Docker")
        self.assertTrue((self.ROOT / "Dockerfile").exists())
        self.assertTrue((self.ROOT / "docker-compose.yml").exists())

    def test_vercel_is_not_presented_as_working_without_its_files(self) -> None:
        """Documentar a ausência é honesto; afirmar que funciona não é."""
        text = self._deploy_text()
        if "vercel" not in text.lower():
            self.skipTest("deploy.md não documenta Vercel")

        tem_artefatos = (self.ROOT / "api" / "index.py").exists() or (
            self.ROOT / "vercel.json"
        ).exists()
        if tem_artefatos:
            return

        # Sem os artefatos, o documento precisa dizer isso em vez de apresentar
        # a Vercel como caminho pronto.
        self.assertIn(
            "configuração ausente",
            text.lower(),
            "deploy.md menciona Vercel sem os artefatos e sem declarar que a "
            "configuração não existe — o leitor conclui que o deploy funciona",
        )

if __name__ == "__main__":
    unittest.main()
