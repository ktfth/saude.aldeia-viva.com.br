"""Entrypoint da função Python na Vercel.

Existia com esta única linha e foi removido pelo commit e129698
("fix deploy and improved docs", 2026-04-26), junto com o vercel.json — no
mesmo commit que escreveu a documentação afirmando que o deploy era na
Vercel. Restaurado.
"""

from app import app
