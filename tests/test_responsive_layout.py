"""
As telas precisam caber no aparelho em que são usadas.

Medido com cada página dentro de um iframe de 390px. É o detalhe que faz a
medição valer: dentro de um iframe as media queries disparam contra a largura
do iframe, não da janela. Constranger uma `<div>` a 390px não dispara nada, e
a auditoria anterior mediu errado por isso.

O resultado antes destas regras existirem:

    /dashboard   588px de largura numa tela de 390px   (+198)
    /sobre       588px                                 (+198)
    /planos      588px                                 (+198)
    /agentes     588px                                 (+198)

Não era um defeito do painel — era o cabeçalho, que aparece nas quatro. A
`.brand` combinava `flex-shrink: 0` com uma assinatura de até 360px: nunca
cedia, e empurrava o `nav` para fora junto. A aplicação inteira andava de
lado em qualquer telefone.

`.hero` e `.text-layout` não apareciam na medição porque se resolviam contra
os 588px errados. Segundas colunas com mínimo de 360px e 320px numa tela de
390px: só ficaram visíveis depois que o cabeçalho parou de mentir sobre a
largura disponível.

A causa comum dos dois: o layout tinha uma única media query, escrita na
véspera para a toolbar. Todo o esqueleto — cabeçalho, hero, colunas — era
desktop puro. Esta é a rede que faltava.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_DIR = ROOT / "web" / "static" / "css"

# A escala declarada no topo do bloco responsivo de layout.css.
#
# Antes disto havia quatro valores avulsos (560, 640, 720, 820) espalhados por
# cinco arquivos, cada um escolhido no momento em que alguém tratou um
# componente. Nenhum deles cobria o cabeçalho, e não havia onde olhar para
# perceber a falta.
ESCALA = {860, 720, 560}

# Largura mínima que uma segunda coluna pode exigir e ainda caber num telefone
# junto da primeira. Acima disto o grid precisa colapsar em alguma media query.
LIMITE_COLUNA_RIGIDA = 300


def _css_sem_comentarios(caminho: Path) -> str:
    return re.sub(r"/\*.*?\*/", "", caminho.read_text(encoding="utf-8"), flags=re.S)


def _arquivos() -> list[Path]:
    return sorted(CSS_DIR.rglob("*.css"))


def _blocos_de_media(texto: str) -> list[tuple[int, str]]:
    """Cada `@media (max-width: N)` com o corpo entre chaves balanceadas."""
    blocos = []
    for m in re.finditer(r"@media[^{]*max-width:\s*(\d+)px[^{]*\{", texto):
        largura, i, profundidade = int(m.group(1)), m.end(), 1
        while i < len(texto) and profundidade:
            profundidade += (texto[i] == "{") - (texto[i] == "}")
            i += 1
        blocos.append((largura, texto[m.end():i - 1]))
    return blocos


def _regras(texto: str) -> list[tuple[str, str]]:
    """Pares (seletor, declarações) de um trecho sem aninhamento de media."""
    return [
        (s.strip(), d)
        for s, d in re.findall(r"([^{}@]+)\{([^{}]*)\}", texto)
        if s.strip()
    ]


class TestEscalaDeBreakpoints(unittest.TestCase):
    def test_apenas_os_valores_declarados(self) -> None:
        fora = {}
        for arquivo in _arquivos():
            for largura, _ in _blocos_de_media(_css_sem_comentarios(arquivo)):
                if largura not in ESCALA:
                    fora.setdefault(arquivo.name, set()).add(largura)
        self.assertEqual(
            fora,
            {},
            f"breakpoint fora da escala {sorted(ESCALA, reverse=True)}: {fora}",
        )

    def test_a_escala_esta_toda_em_uso(self) -> None:
        """Um valor declarado e não usado é documentação que mente."""
        usados = {
            largura
            for arquivo in _arquivos()
            for largura, _ in _blocos_de_media(_css_sem_comentarios(arquivo))
        }
        self.assertEqual(usados, ESCALA)


class TestColunasRigidasColapsam(unittest.TestCase):
    """Toda coluna que exige mais que um telefone tem que ter como colapsar."""

    def _grids_rigidos(self) -> dict[str, set[str]]:
        achados: dict[str, set[str]] = {}
        for arquivo in _arquivos():
            texto = _css_sem_comentarios(arquivo)
            dentro_de_media = "".join(c for _, c in _blocos_de_media(texto))
            fora = texto.replace(dentro_de_media, "") if dentro_de_media else texto
            for seletor, decls in _regras(fora):
                valor = re.search(r"grid-template-columns:([^;}]*)", decls)
                if not valor:
                    continue
                minimos = [int(n) for n in re.findall(r"minmax\(\s*(\d+)px", valor.group(1))]
                if any(n >= LIMITE_COLUNA_RIGIDA for n in minimos):
                    for parte in seletor.split(","):
                        achados.setdefault(parte.strip(), set()).add(arquivo.name)
        return achados

    def _seletores_que_colapsam(self) -> set[str]:
        colapsam = set()
        for arquivo in _arquivos():
            for _, corpo in _blocos_de_media(_css_sem_comentarios(arquivo)):
                for seletor, decls in _regras(corpo):
                    if "grid-template-columns" in decls:
                        colapsam.update(p.strip() for p in seletor.split(","))
        return colapsam

    def test_todo_grid_rigido_tem_regra_de_colapso(self) -> None:
        rigidos = self._grids_rigidos()
        colapsam = self._seletores_que_colapsam()
        sem_saida = {s: v for s, v in rigidos.items() if s not in colapsam}
        self.assertEqual(
            sem_saida,
            {},
            "grid com coluna de mínimo rígido e sem media query que o colapse: "
            f"{sem_saida}",
        )

    def test_o_limite_reflete_um_telefone_real(self) -> None:
        """390px de tela menos o respiro lateral da `.page` deixa ~354px."""
        self.assertLess(LIMITE_COLUNA_RIGIDA, 354)


# Elementos que pertencem a um componente, e não à tipografia da página.
# Uma regra sobre eles precisa dizer DE QUAL componente está falando.
ESTRUTURA_DE_TABELA = ("table", "thead", "tbody", "tr", "td", "th")


class TestRegraDeComponenteNaoVazaParaOutro(unittest.TestCase):
    """A tabela do detalhe virou cartão porque a regra não tinha dono.

    O bloco mobile de `table.css` usava seletores de elemento nus — `table`,
    `tbody tr`, `td[data-label]` — escritos pensando na listagem de
    municípios. Eles alcançam TODA tabela da página. A `.detail-table`, que
    tem `min-width: 720px` e foi desenhada para rolar dentro do próprio
    invólucro, virava uma pilha de células de 720px com vãos enormes.

    Medido no painel de detalhe aberto em 390px, e invisível em qualquer
    varredura da página fechada: o painel só existe depois do clique.
    """

    def test_regra_de_tabela_em_media_query_declara_o_componente(self) -> None:
        sem_dono = {}
        for arquivo in _arquivos():
            for largura, corpo in _blocos_de_media(_css_sem_comentarios(arquivo)):
                for seletor, _ in _regras(corpo):
                    for parte in (p.strip() for p in seletor.split(",")):
                        primeiro = parte.split()[0] if parte.split() else ""
                        alvo = re.split(r"[.\[:#]", primeiro)[0]
                        tem_classe = "." in parte or "#" in parte
                        if alvo in ESTRUTURA_DE_TABELA and not tem_classe:
                            sem_dono.setdefault(f"{arquivo.name}@{largura}", []).append(parte)
        self.assertEqual(
            sem_dono,
            {},
            "seletor de tabela sem componente que o delimite — vale para "
            f"qualquer tabela da página: {sem_dono}",
        )

    def test_a_listagem_continua_virando_cartao(self) -> None:
        """Escopar não pode ter desligado a transformação que existia."""
        tabela = _css_sem_comentarios(CSS_DIR / "components" / "table.css")
        corpo = "".join(c for largura, c in _blocos_de_media(tabela) if largura <= 720)
        self.assertIn(".table-wrap tbody tr", corpo)
        self.assertIn("display: grid", corpo)
        self.assertIn('data-label="Município"', corpo)


class TestCabecalhoCede(unittest.TestCase):
    """O defeito que derrubou as quatro páginas, escrito como asserção."""

    def _layout(self) -> str:
        return _css_sem_comentarios(CSS_DIR / "layout.css")

    def test_a_marca_deixa_de_ser_rigida_em_tela_estreita(self) -> None:
        estreito = "".join(
            corpo for largura, corpo in _blocos_de_media(self._layout()) if largura <= 860
        )
        regras = dict(_regras(estreito))
        marca = "".join(d for s, d in regras.items() if s.strip() == ".brand")
        self.assertIn("min-width: 0", marca)
        self.assertRegex(marca, r"flex-shrink:\s*1")

    def test_a_marca_e_rigida_no_desktop_de_proposito(self) -> None:
        """A reserva de largura alinha o nav entre páginas — some só no estreito."""
        fora_de_media = self._layout()
        for _, corpo in _blocos_de_media(fora_de_media):
            fora_de_media = fora_de_media.replace(corpo, "")
        self.assertRegex(fora_de_media, r"\.brand\s*\{[^}]*flex-shrink:\s*0")

    def test_o_cabecalho_para_de_ser_fixo_no_telefone(self) -> None:
        """Cabeçalho fixo com nav em duas linhas come um quinto da tela."""
        corpo = "".join(
            c for largura, c in _blocos_de_media(self._layout()) if largura <= 720
        )
        cabecalho = "".join(d for s, d in _regras(corpo) if s.strip() == ".site-header")
        self.assertIn("position: static", cabecalho)


if __name__ == "__main__":
    unittest.main()
