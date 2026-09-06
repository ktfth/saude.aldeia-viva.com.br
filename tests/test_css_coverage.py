"""
Toda classe usada no HTML precisa existir em algum CSS.

CSS ausente não levanta erro: a página simplesmente renderiza sem estilo, e
ninguém percebe até alguém abrir aquela tela. A extração da "Fase 0" moveu o
`BASE_CSS` do app.py para arquivos modulares e portou o que o /dashboard
usava, deixando para trás `.link-grid`, `.link-card`, `.prose` e
`.code-block` — as páginas /planos, /sobre e /agentes renderizaram cartões e
blocos de código sem estilo desde então.

É a mesma família de defeito do import perdido em `fetch_epidemiology_report`
e da media query prometida no DESIGN.md e nunca escrita: um refactor que move
coisas e deixa parte para trás, sem nada que falhe.

Este teste é a rede que faltava.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Classes aplicadas por JavaScript ou por bibliotecas, sem marcação estática.
# Cada isenção precisa de motivo — a lista existe para não crescer sozinha.
EXEMPT = {
    "active": "aplicada na navegação por comparação de rota",
}


def _markup_sources() -> list[Path]:
    return [ROOT / "app.py", ROOT / "web" / "static" / "js" / "dashboard.js"]


def _css_files() -> list[Path]:
    return sorted((ROOT / "web" / "static" / "css").rglob("*.css"))


def classes_used_in_markup() -> dict[str, set[str]]:
    """Classes escritas em atributos `class="..."` literais."""
    used: dict[str, set[str]] = {}
    for source in _markup_sources():
        text = source.read_text(encoding="utf-8")
        # Ignora valores com interpolação (${...} ou {...}), que são dinâmicos.
        for match in re.finditer(r'class="([^"{}$]+)"', text):
            for name in match.group(1).split():
                used.setdefault(name, set()).add(source.name)
        # Prefixo estático antes de uma interpolação: class="badge ${nivel}"
        for match in re.finditer(r'class="([a-z][a-z0-9 _-]*?)\s*[${]', text):
            for name in match.group(1).split():
                used.setdefault(name, set()).add(source.name)
    return used


def classes_defined_in_css() -> set[str]:
    defined: set[str] = set()
    for css in _css_files():
        for match in re.finditer(r"\.([a-zA-Z][\w-]*)", css.read_text(encoding="utf-8")):
            defined.add(match.group(1))
    return defined


class TestCssCoverage(unittest.TestCase):
    def test_every_markup_class_has_a_rule(self) -> None:
        used = classes_used_in_markup()
        defined = classes_defined_in_css()
        orphans = {
            name: sources
            for name, sources in used.items()
            if name not in defined and name not in EXEMPT
        }
        self.assertEqual(
            orphans,
            {},
            "Classes usadas no HTML sem nenhuma regra CSS — a página renderiza "
            "sem estilo e nada falha:\n"
            + "\n".join(
                f"  .{name} (em {', '.join(sorted(src))})"
                for name, src in sorted(orphans.items())
            ),
        )

    def test_the_audit_actually_finds_things(self) -> None:
        """Guarda contra o teste virar vácuo se a extração de classes quebrar."""
        used = classes_used_in_markup()
        self.assertGreater(len(used), 40)
        for expected in ("panel", "badge", "signal-strip", "link-card", "prose"):
            self.assertIn(expected, used)

    def test_every_css_file_is_imported(self) -> None:
        """Um componente novo que ninguém importa é igual a não existir."""
        main = (ROOT / "web" / "static" / "css" / "main.css").read_text(
            encoding="utf-8"
        )
        imported = set(re.findall(r'@import url\("\./([^"]+)"\)', main))
        on_disk = {
            str(path.relative_to(ROOT / "web" / "static" / "css")).replace("\\", "/")
            for path in _css_files()
            if path.name != "main.css"
        }
        self.assertEqual(
            on_disk - imported,
            set(),
            "Arquivos CSS que existem mas nunca são importados por main.css",
        )

    def test_exemptions_are_justified(self) -> None:
        for name, reason in EXEMPT.items():
            self.assertTrue(reason, f"isenção de .{name} sem motivo escrito")



class TestAssetVersioning(unittest.TestCase):
    """O navegador precisa saber quando o CSS mudou.

    `main.css` é servido por StaticFiles com ETag e Last-Modified, mas um
    navegador que já tem a folha em cache pode continuar usando a antiga
    depois de um deploy — foi exatamente o que aconteceu ao verificar a
    correção do /planos: o arquivo novo estava no servidor e a página
    renderizava com o antigo. Em produção isso significa um deploy de CSS
    que não chega ao usuário.

    A impressão digital no `href` resolve porque muda a URL.
    """

    def test_stylesheet_link_carries_a_fingerprint(self) -> None:
        from fastapi.testclient import TestClient

        import app

        app.rate_limiter.reset()
        with TestClient(app.app) as client:
            html = client.get("/dashboard").text
        self.assertRegex(html, r'href="/static/css/main\.css\?v=[0-9a-f]{8}"')

    def test_fingerprint_changes_when_a_component_changes(self) -> None:
        import app

        first = app.static_asset_version()
        app._ASSET_VERSION = None
        self.assertEqual(first, app.static_asset_version())
        self.assertEqual(len(first), 8)

    def test_fingerprint_survives_a_missing_directory(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        import app

        app._ASSET_VERSION = None
        with patch.object(app, "STATIC_DIR", Path("/nao/existe")):
            self.assertIsInstance(app.static_asset_version(), str)
        app._ASSET_VERSION = None

if __name__ == "__main__":
    unittest.main()
