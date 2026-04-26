# Guia Rápido

Este guia mostra o caminho mínimo para colocar o projeto em execução local.

## Requisitos

- Python 3.12 ou superior
- acesso à internet para carregar as fontes reais na primeira execução

## Instalação

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Execução

```bash
uvicorn app:app --reload
```

## Primeira verificação

Abra no navegador:

- `http://127.0.0.1:8000/dashboard`
- `http://127.0.0.1:8000/sobre`
- `http://127.0.0.1:8000/agentes`
- `http://127.0.0.1:8000/docs`

## Primeiras chamadas

```bash
curl "http://127.0.0.1:8000/health"
curl "http://127.0.0.1:8000/v1/diseases"
curl "http://127.0.0.1:8000/v1/high-alerts?estado=SP&limite=5"
curl "http://127.0.0.1:8000/v1/risk-index?municipio=perus&estado=SP&limite=5"
```

## Atualização manual

```bash
curl -X POST "http://127.0.0.1:8000/v1/refresh"
```

## Quando os dados parecem vazios

- confira se o app terminou de inicializar
- verifique `GET /health`
- valide se o ano configurado tem fontes disponíveis
- confirme se a cache local não está desabilitada por variável de ambiente

