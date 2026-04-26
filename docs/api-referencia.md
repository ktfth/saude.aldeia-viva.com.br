# Referência da API

Esta é a referência prática dos endpoints públicos mais importantes.

## `GET /health`

Retorna o estado do serviço, a situação da carga e metadados de cache.

Exemplo:

```bash
curl "http://127.0.0.1:8000/health"
```

## `GET /v1/risk-index`

Índice enriquecido por município.

Parâmetros:

- `municipio`
- `estado`
- `somente_altos`
- `nivel_minimo`
- `limite`

Exemplo:

```bash
curl "http://127.0.0.1:8000/v1/risk-index?municipio=perus&estado=SP&somente_altos=false&limite=10"
```

## `GET /v1/high-alerts`

Lista consolidada de alertas altos e críticos por município e doença.

Parâmetros:

- `municipio`
- `estado`
- `doenca`
- `limite`

Exemplo:

```bash
curl "http://127.0.0.1:8000/v1/high-alerts?estado=SP&limite=10"
```

## `GET /v1/diseases`

Catálogo das doenças e agravos suportados.

Exemplo:

```bash
curl "http://127.0.0.1:8000/v1/diseases"
```

## `GET /v1/metadata`

Metadados da última carga, incluindo fontes, período e fórmula.

## `POST /v1/refresh`

Reprocessa a base real e atualiza o estado agregado do serviço.

Exemplo:

```bash
curl -X POST "http://127.0.0.1:8000/v1/refresh"
```

## Endpoints de apresentação

- `/dashboard`
- `/sobre`
- `/agentes`
- `/agent.json`
- `/llms.txt`
- `/robots.txt`
- `/sitemap.xml`

