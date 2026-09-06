"""Entrypoint da função Python na Vercel.

Existia com esta única linha e foi removido pelo commit e129698
("fix deploy and improved docs", 2026-04-26), junto com o vercel.json — no
mesmo commit que escreveu a documentação afirmando que o deploy era na
Vercel. Restaurado.
"""

# `app` parece nao usado aqui, e nao e: a Vercel procura este objeto no
# modulo apontado por `vercel.json`. O `ruff --fix` removeu esta linha numa
# limpeza automatica de imports, o que deixaria a funcao sem app nenhum e
# derrubaria o site inteiro no deploy seguinte. O `noqa` existe para o
# proximo `--fix` nao repetir.
from app import app  # noqa: F401 - objeto procurado pela plataforma
