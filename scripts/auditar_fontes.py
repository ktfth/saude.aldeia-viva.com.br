#!/usr/bin/env python3
"""Confere os tetos de ano declarados contra o que o DATASUS publica hoje.

`DiseaseSource.latest_year` e um numero cravado no codigo sobre um fato do
mundo exterior: ate que ano aquele agravo foi publicado. Ele nao pode ser
verificado por teste unitario -- exige rede -- e por isso apodrece calado.

Medido em 2026-09-06, quando este script foi escrito: seis dos sete tetos
estavam corretos, e o BOTU estava um ano atras do que o servidor oferecia. O
carregador comecava ja defasado e nunca alcancava o arquivo novo.

E confirmou o mais importante para o produto: a defasagem dos DBC "FINAIS"
nao e nossa. MENI e ANIM param em 2022 NO SERVIDOR. Nenhum pipeline conserta
isso -- o que da para fazer e declarar, que e o que o modelo de recencia ja
faz.

Uso:
    python scripts/auditar_fontes.py

Sai com codigo 1 quando algum teto esta desatualizado, para poder rodar em
tarefa agendada.
"""

import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.disease_sources import DISEASE_SOURCES  # noqa: E402

FTP_TIMEOUT = 90


def anos_publicados_por_prefixo() -> dict[str, list[int]]:
    """Uma listagem do diretorio FINAIS responde por todos os agravos."""
    import app

    url = app.DATASUS_SINAN_DBC_BASE + "/"
    with urllib.request.urlopen(url, timeout=FTP_TIMEOUT) as resposta:
        listagem = resposta.read().decode("latin-1", "replace")

    por_prefixo: dict[str, list[int]] = {}
    for nome in re.findall(r"([A-Z]{3,5})BR(\d{2})\.dbc", listagem, re.I):
        prefixo, sufixo = nome[0].upper(), nome[1]
        por_prefixo.setdefault(prefixo, []).append(int("20" + sufixo))
    return {p: sorted(a) for p, a in por_prefixo.items()}


def auditar() -> int:
    publicados = anos_publicados_por_prefixo()
    desatualizados = []

    print(f"{'AGRAVO':<8} {'TETO':<7} {'PUBLICADO':<11} SITUACAO")
    print("-" * 58)
    for fonte in DISEASE_SOURCES.values():
        if not fonte.dbc_prefix:
            continue
        anos = publicados.get(fonte.dbc_prefix.upper(), [])
        if not anos:
            print(f"{fonte.codigo:<8} {str(fonte.latest_year):<7} {'nenhum':<11} prefixo nao encontrado")
            continue
        disponivel = max(anos)
        if fonte.latest_year is None or disponivel > fonte.latest_year:
            desatualizados.append((fonte.codigo, fonte.latest_year, disponivel))
            situacao = f"DESATUALIZADO: existe {disponivel}"
        elif fonte.latest_year > disponivel:
            situacao = f"teto acima do publicado (o carregador desce sozinho)"
        else:
            situacao = "confere"
        print(f"{fonte.codigo:<8} {str(fonte.latest_year):<7} {disponivel:<11} {situacao}")

    if not desatualizados:
        print()
        print("Todos os tetos conferem com o servidor.")
        return 0

    print()
    print("Tetos a atualizar em domain/disease_sources.py:")
    for codigo, teto, disponivel in desatualizados:
        print(f"  {codigo}: latest_year={teto} -> {disponivel}")
    return 1


if __name__ == "__main__":
    raise SystemExit(auditar())
