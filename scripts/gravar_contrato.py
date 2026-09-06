#!/usr/bin/env python3
"""Grava a forma da resposta publica em `contrato-v1.json`.

Rodar isto e um ATO DELIBERADO: significa aceitar a forma atual como o
contrato de `/v1`. `tests/test_contract_stability.py` compara o servico vivo
com este arquivo e falha quando um campo some.

Quando rodar:
  - ao acrescentar campos (a rede permite adicao, mas o arquivo deve
    acompanhar para nao envelhecer);
  - ao remover um campo DE PROPOSITO, junto da nota de depreciacao.

Nunca rodar so para "fazer o teste passar": a rede existe justamente para o
momento em que a remocao nao foi intencional.

Uso:
    python scripts/gravar_contrato.py
    python scripts/gravar_contrato.py --conferir   # so mostra a diferenca
"""

import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

DESTINO = RAIZ / "contrato-v1.json"


def forma_atual() -> dict[str, list[str]]:
    from fastapi.testclient import TestClient

    import app
    from domain.contract_shape import PUBLIC_ROUTES, field_paths
    from domain.disease_sources import DISEASE_SOURCES

    codigos = set(DISEASE_SOURCES)

    # `/v1/professional-report` exige tier premium: com a chave gratuita o
    # gravador tomava 403 e desistia. O contrato precisa da forma COMPLETA,
    # entao a gravacao usa a chave de maior alcance disponivel.
    chave = next(
        (
            k
            for k, entrada in app.api_key_manager.keys.items()
            if (entrada or {}).get("tier") in ("premium", "admin")
        ),
        next(iter(app.api_key_manager.keys), None),
    )
    if chave is None:
        raise SystemExit(
            "Nenhuma chave carregada; defina USERS_DB_PATH ou USERS_DB_JSON."
        )
    cabecalhos = {"X-API-Key": chave}

    forma: dict[str, list[str]] = {}
    with TestClient(app.app) as cliente:
        for rota in PUBLIC_ROUTES:
            app.rate_limiter.reset()
            resposta = cliente.get(rota, headers=cabecalhos)
            if resposta.status_code >= 400:
                raise SystemExit(
                    f"{rota} respondeu {resposta.status_code}; contrato nao gravado."
                )
            forma[rota] = sorted(field_paths(resposta.json(), codigos))
    return forma


def main() -> int:
    from domain.contract_shape import breaking_changes

    atual = forma_atual()
    total = sum(len(c) for c in atual.values())

    gravado = None
    if DESTINO.exists():
        gravado = json.loads(DESTINO.read_text(encoding="utf-8"))["rotas"]

    if gravado is not None:
        perdas = breaking_changes(gravado, atual)
        ganhos = {
            rota: sorted(set(atual[rota]) - set(gravado.get(rota, [])))
            for rota in atual
        }
        ganhos = {r: c for r, c in ganhos.items() if c}
        for rota, campos in sorted(perdas.items()):
            print(f"REMOVIDO  {rota}")
            for campo in campos:
                print(f"    - {campo}")
        for rota, campos in sorted(ganhos.items()):
            print(f"ACRESCENTADO  {rota}")
            for campo in campos:
                print(f"    + {campo}")
        if not perdas and not ganhos:
            print("Contrato inalterado.")

    if "--conferir" in sys.argv:
        print()
        print("--conferir: nada foi escrito.")
        return 0

    DESTINO.write_text(
        json.dumps(
            {
                "versao": "v1",
                "observacao": (
                    "Forma da resposta publica. Acrescentar campo nao quebra "
                    "integrador; remover ou renomear quebra. Regravado por "
                    "scripts/gravar_contrato.py, sempre de proposito."
                ),
                "rotas": atual,
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
        + chr(10),
        encoding="utf-8",
    )
    print()
    print(f"{DESTINO.name}: {len(atual)} rotas, {total} campos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
