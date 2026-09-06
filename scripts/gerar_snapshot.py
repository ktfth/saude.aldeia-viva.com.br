#!/usr/bin/env python3
"""Regenera o snapshot embarcado a partir das fontes reais.

`bundled_report_snapshot.py` e o que producao serve: `.vercelignore` exclui
`data/reports`, entao o modulo embarcado e a unica origem de dado num deploy.
Ele nao tinha gerador -- foi criado a mao em algum momento e ficou parado em
2026-04-26.

Quanto isso custava, medido em 2026-09-06 recarregando as fontes de verdade:

    DENG 2026   253.941 -> 449.101   +195.160  (+76,9%)
    CHIK 2026    31.909 ->  65.619   + 33.710  (+105,6%)
    ZIKA 2026     3.658 ->  11.267   +  7.609  (+208,0%)
    obitos por dengue:  87 -> 316    +229
    municipios com caso: 5.339 -> 5.408

Producao estava sem 60% dos casos e sem 229 obitos por dengue. Nao e
defasagem cosmetica: e uma epidemia diferente da real.

Os sete agravos de fonte DBC nao mudaram um caso, e nao mudariam: o DATASUS
publica os arquivos "FINAIS" com anos de atraso. Ver `scripts/auditar_fontes.py`.

Por que fora do servico: a recarga com download real levou 248s, e o
`maxDuration` da funcao na Vercel e 60s. Nao e questao de gosto -- cron
chamando `/v1/refresh` em producao nao tem como completar, e o container
serverless e efemero, entao o resultado seria descartado de qualquer forma.

Uso:
    python scripts/gerar_snapshot.py            # regenera e escreve
    python scripts/gerar_snapshot.py --conferir # so compara, nao escreve

Sai com 1 quando a carga nova perde cobertura em relacao ao snapshot atual.
Um pipeline que publica cego pode degradar a base -- ja aconteceu neste
projeto, com uma recarga parcial trocando 5.339 municipios e 10 agravos por
4.462 e 4.
"""

import base64
import gzip
import json
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

DESTINO = RAIZ / "bundled_report_snapshot.py"

CABECALHO = '''"""Embedded epidemiology snapshot used when runtime file access is unavailable."""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

BUNDLED_REPORT_GZ_B64 = """'''

RODAPE = '''"""


def load_embedded_report_snapshot() -> dict[str, Any]:
    payload = gzip.decompress(base64.b64decode(BUNDLED_REPORT_GZ_B64))
    report = json.loads(payload.decode("utf-8"))
    if not isinstance(report, dict):
        raise ValueError("Snapshot embarcado invalido.")
    return report
'''


def totais(relatorio):
    """Casos e obitos por agravo, e o ano-fonte de cada um."""
    anos = {
        f.get("codigo"): f.get("ano")
        for f in (relatorio.get("metadata") or {}).get("fontes", [])
    }
    por_agravo = {}
    for municipio in relatorio.get("municipios") or []:
        for agravo in municipio.get("doencas") or []:
            acumulado = por_agravo.setdefault(
                agravo["codigo"], {"casos": 0, "obitos": 0}
            )
            acumulado["casos"] += agravo.get("casos_provaveis") or 0
            acumulado["obitos"] += agravo.get("obitos") or 0
    return anos, por_agravo


def comparar(anterior, novo):
    anos_a, tot_a = totais(anterior) if anterior else ({}, {})
    anos_n, tot_n = totais(novo)
    print()
    print(
        f"{'AGRAVO':<7} {'ANO':<12} {'ANTES':>12} {'DEPOIS':>12} {'DELTA':>11}"
    )
    print("-" * 60)
    for codigo in sorted(set(tot_a) | set(tot_n)):
        ca = tot_a.get(codigo, {}).get("casos", 0)
        cn = tot_n.get(codigo, {}).get("casos", 0)
        aa, an = anos_a.get(codigo), anos_n.get(codigo)
        ano = str(an) if aa == an else f"{aa}->{an}"
        print(f"{codigo:<7} {ano:<12} {ca:>12,} {cn:>12,} {cn - ca:>+11,}")
    ma = len(anterior.get("municipios") or []) if anterior else 0
    mn = len(novo.get("municipios") or [])
    print("-" * 60)
    print(f"municipios com caso: {ma:,} -> {mn:,}  ({mn - ma:+,})")


def escrever(relatorio) -> None:
    bruto = json.dumps(relatorio, ensure_ascii=False, separators=(",", ":"))
    # `mtime=0` fixa o cabecalho do gzip, que por padrao carrega a hora da
    # compressao. Higiene de reprodutibilidade, mas NAO explica o que foi
    # observado: o runner reproduziu os mesmos 10 agravos e os mesmos 5.408
    # municipios e ainda assim gerou um arquivo 3 KB menor. Uma diferenca
    # dessa ordem nao cabe num cabecalho de 4 bytes -- provavelmente e build
    # de zlib diferente entre Windows e Linux, e isso nao foi confirmado.
    # A consequencia pratica: igualdade byte a byte entre plataformas nao e
    # garantida, entao "o snapshot mudou?" nao deve ser decidido por bytes.
    comprimido = gzip.compress(bruto.encode("utf-8"), compresslevel=9, mtime=0)
    codificado = base64.b64encode(comprimido).decode("ascii")
    DESTINO.write_text(CABECALHO + codificado + RODAPE, encoding="utf-8")
    print()
    print(
        f"{DESTINO.name}: {len(bruto)/1048576:.1f} MB de JSON -> "
        f"{DESTINO.stat().st_size/1024:.0f} KB no modulo"
    )


def main() -> int:
    conferir = "--conferir" in sys.argv

    # O cache de download bruto e indexado por URL: sem desliga-lo, a recarga
    # reprocessa os arquivos de abril e conclui que nada mudou. Foi assim que
    # a primeira medicao desta mudanca saiu errada.
    os.environ["SINAN_DISABLE_CACHE"] = "1"
    os.environ["SINAN_REPORT_CACHE_MAX_AGE_DAYS"] = "1"

    import app
    from aggregation.cache_policy import is_regression, missing_sources

    anterior = None
    try:
        from bundled_report_snapshot import load_embedded_report_snapshot

        anterior = load_embedded_report_snapshot()
    except Exception as erro:
        print(f"Sem snapshot anterior para comparar ({erro}).")

    print("Recarregando as fontes reais (leva alguns minutos)...")
    inicio = time.time()
    novo = app.load_or_refresh_report(app.DEFAULT_YEAR, force_refresh=True)
    print(f"Carga concluida em {time.time() - inicio:.0f}s.")

    comparar(anterior, novo)

    if is_regression(novo, anterior):
        faltando = missing_sources(novo, anterior)
        print()
        print(f"REGRESSAO: a carga nova perde as fontes {sorted(faltando)}.")
        print("O snapshot NAO foi reescrito.")
        return 1

    if conferir:
        print()
        print("--conferir: nada foi escrito.")
        return 0

    escrever(novo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
