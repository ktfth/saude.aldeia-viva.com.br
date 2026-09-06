# Aldeia Viva Saúde

Inteligência epidemiológica com dados reais do SINAN/OpenDataSUS, enriquecidos por município e organizados para consumo humano, técnico e por agentes.

O projeto entrega uma API FastAPI, um dashboard público e páginas de apoio para explicação, integração e descoberta do contrato.

## O que existe no projeto

- `app.py`: aplicação FastAPI — rotas, páginas públicas e orquestração da carga.
- `domain/`: regras puras — perfis de risco por agravo, taxas, recência do sinal.
- `ingestion/`: leitura das fontes (SINAN/OpenDataSUS, municípios e população do IBGE).
- `aggregation/`: agregação por município, enriquecimento temporal e populacional,
  política de validade da cache e ordenação.
- `presentation/`: renderização server-side da dimensão temporal (SVG inline, sem JS).
- `web/static/`: CSS modular e o JavaScript do painel (14 KB, sem dependências externas).
- `bundled_report_snapshot.py`: snapshot embarcado, usado quando não há cache utilizável.
- `tests/`: 299 testes.

## Acesso público

- Dashboard: `/dashboard`
- Explicação e metodologia: `/sobre`
- Página para agentes: `/agentes`
- Manifesto para agentes: `/agent.json`
- Instruções para LLMs: `/llms.txt`
- Documentação da API: `/docs`

## Endpoints principais

- `GET /health`: estado do serviço e metadados de carga
- `GET /v1/risk-index`: índice enriquecido por município.
  Aceita `ordenar=score|taxa|casos|obitos` — `taxa` usa incidência por 100 mil
  habitantes e é a única medida comparável entre municípios de portes diferentes
- `GET /v1/high-alerts`: alertas altos e críticos
- `GET /v1/diseases`: catálogo de doenças e agravos suportados
- `GET /v1/metadata`: metadados da última carga
- `POST /v1/refresh`: reprocessa os dados e atualiza o cache agregado

## Como rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Depois, abra:

- `http://127.0.0.1:8000/dashboard`
- `http://127.0.0.1:8000/docs`

## Configuração útil

- `SINAN_YEAR`: ano base da carga
- `SINAN_DISEASE_CODES`: lista de códigos habilitados, separados por vírgula
- `SINAN_REQUEST_TIMEOUT_SECONDS`: timeout das requisições externas
- `SINAN_CACHE_DIR`: diretório da cache em disco
- `SINAN_FORCE_REFRESH=1`: força recarga na inicialização
- `SINAN_DISABLE_CACHE=1`: desliga a cache de arquivos brutos
- `SINAN_DISABLE_REPORT_CACHE=1`: desliga a cache agregada
- `SINAN_REPORT_CACHE_MAX_AGE_DAYS`: dias até a cache agregada expirar (padrão 7; `0` desliga)
- `SIGNAL_REFERENCE_DATE=AAAA-MM-DD`: fixa a data de referência e torna a resposta reproduzível

## Como os dados funcionam

O serviço combina fontes reais do SINAN/OpenDataSUS, dados do DATASUS e o lookup
de municípios e a estimativa populacional do IBGE para produzir:

- score de risco e nível por município, e por agravo com perfil próprio
- incidência por 100 mil habitantes, com o denominador e a ressalva sempre juntos
- alertas altos e críticos
- metadados de carga com rastreabilidade

**Três fatos temporais, deliberadamente separados** — confundi-los produz
diagnóstico falso:

- `metadata.carga` — há quanto tempo o serviço buscou dados. Falha operacional.
- `doenca.fonte` — de que ano é o arquivo daquele agravo. O relatório reúne
  anos diferentes por agravo; não presuma que `periodo.ano` vale para todos.
- `doenca.recencia` — até quando o município notificou, medido dentro da fonte
  daquele agravo. É o único dos três que é fato epidemiológico sobre o município.

Se o runtime não conseguir acessar as fontes no momento da execução, o projeto usa o snapshot embarcado para continuar exibindo dados reais já consolidados.

## Documentação

- [Visão geral](docs/visao-geral.md)
- [Guia rápido](docs/quickstart.md)
- [Referência da API](docs/api-referencia.md)
- [Metodologia](docs/metodologia.md)
- [Deploy](docs/deploy.md)

## Estrutura

```text
.
├── app.py
├── bundled_report_snapshot.py
├── docs/
├── tests/
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## Validação

```bash
python -m unittest discover -s tests
```

A suíte roda offline: `tests/__init__.py` desliga a expiração da cache para
que nenhum teste dependa de rede.

## Observação

Os dados servem para leitura operacional e integração de sistemas. Não substituem vigilância epidemiológica oficial nem investigação local.
