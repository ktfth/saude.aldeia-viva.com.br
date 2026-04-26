# Aldeia Viva Saúde

Inteligência epidemiológica com dados reais do SINAN/OpenDataSUS, enriquecidos por município e organizados para consumo humano, técnico e por agentes.

O projeto entrega uma API FastAPI, um dashboard público e páginas de apoio para explicação, integração e descoberta do contrato.

## O que existe no projeto

- `app.py`: aplicação FastAPI completa, com ingestão, agregação, páginas públicas e endpoints da API.
- `bundled_report_snapshot.py`: snapshot embarcado usado quando o runtime não pode depender de leitura externa.
- `tests/`: suíte de validação do comportamento do app e da documentação pública.

## Acesso público

- Dashboard: `/dashboard`
- Explicação e metodologia: `/sobre`
- Página para agentes: `/agentes`
- Manifesto para agentes: `/agent.json`
- Instruções para LLMs: `/llms.txt`
- Documentação da API: `/docs`

## Endpoints principais

- `GET /health`: estado do serviço e metadados de carga
- `GET /v1/risk-index`: índice enriquecido por município
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

## Como os dados funcionam

O serviço combina fontes reais do SINAN/OpenDataSUS, dados do DATASUS e lookup de municípios do IBGE para produzir:

- score de risco por município
- alertas altos e críticos
- enriquecimento por doença, vírus e tipo de agravo
- metadados de carga com rastreabilidade

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

## Observação

Os dados servem para leitura operacional e integração de sistemas. Não substituem vigilância epidemiológica oficial nem investigação local.
