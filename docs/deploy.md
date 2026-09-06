# Deploy

## Estratégia

- a aplicação principal fica em `app.py`
- o snapshot consolidado é embarcado em `bundled_report_snapshot.py`
- as páginas públicas e os endpoints consomem o mesmo estado agregado

O snapshot reduz a dependência de download no runtime e evita que o deploy
suba vazio quando uma fonte externa estiver indisponível.

## Como a carga se comporta

Na inicialização o serviço procura, nesta ordem:

1. **cache em disco**, se ainda estiver dentro do prazo de validade
2. **recarga das fontes**, se a cache estiver vencida
3. **snapshot embarcado**, se não houver cache utilizável

Duas regras que sustentam o comportamento:

- Se a recarga falhar, a cache vencida volta a ser usada. Dado velho e
  rotulado é melhor que painel vazio, e o rótulo está em `metadata.carga`.
- Se a recarga **perder cobertura de agravos** — uma fonte fora do ar, uma
  extensão nativa ausente — ela é descartada e a base anterior permanece. A
  degradação fica declarada em `metadata.carga.cobertura_degradada`.

## Vercel

1. Conecte o repositório ao projeto da Vercel.
2. Faça o deploy pela CLI ou pelo painel.
3. Rode as verificações pós-deploy abaixo.

## Docker

O `Dockerfile` e o `docker-compose.yml` na raiz sobem o mesmo serviço, com um
`HEALTHCHECK` apontando para `/health`.

```bash
docker compose up --build
```

## Variáveis de ambiente

Todas são opcionais; os valores entre parênteses são os padrões.

| Variável | Efeito |
|---|---|
| `SINAN_YEAR` | ano-base da carga (ano corrente) |
| `SINAN_DISEASE_CODES` | lista de agravos habilitados, separada por vírgula |
| `SINAN_REQUEST_TIMEOUT_SECONDS` | timeout das requisições externas (45) |
| `SINAN_CACHE_DIR` | diretório da cache em disco (`.cache/datasus`) |
| `SINAN_REPORT_CACHE_MAX_AGE_DAYS` | dias até a cache agregada expirar (7). `0` desliga a expiração — use em ambientes sem rede |
| `SINAN_FORCE_REFRESH` | `1` força recarga na inicialização |
| `SINAN_DISABLE_CACHE` | `1` desliga a cache de arquivos brutos |
| `SINAN_DISABLE_REPORT_CACHE` | `1` desliga a cache agregada |
| `PUBLIC_BASE_URL` | URL pública usada em links canônicos e no `agent.json` |
| `USERS_DB_PATH` | arquivo de chaves de API (`data/users.json`) |
| `USAGE_LOG_PATH` | log de uso (`data/usage.jsonl`). Grava a impressão digital da chave, nunca a chave |

## Verificações pós-deploy

```bash
curl "https://SEU-DOMINIO/health"
```

O que olhar na resposta, em ordem de importância:

1. **`carga.idade_dias`** — há quantos dias o serviço não busca dados novos.
   Um deploy pode subir servindo uma cache de meses sem que nada falhe; este
   é o número que revela isso. `carga.atualizada` resume o veredito.
2. **`carga.cobertura_degradada`** — verdadeiro quando a última tentativa de
   recarga perdeu agravos e a base anterior foi mantida.
3. `data_status` — `ok` quando há conteúdo carregado.

Depois:

```bash
curl "https://SEU-DOMINIO/v1/diseases"
curl "https://SEU-DOMINIO/v1/risk-index?estado=SP&limite=5"
curl -i "https://SEU-DOMINIO/v1/risk-index?limite=100"
```

A última confirma os cabeçalhos de corte (`X-Total-Results`,
`X-Limit-Applied`), que revelam o teto do tier anônimo.

Por fim, abra `/dashboard` e confirme que a barra de estado no topo mostra a
idade da carga e a cobertura das fontes.

## Dependências nativas

`datasus-dbc` e `dbfread` são extensões nativas e nem sempre têm wheel para a
versão de Python do ambiente. Sem elas o serviço sobe normalmente e carrega
apenas as fontes CSV, registrando o motivo em `metadata.erros` — e a regra de
cobertura impede que essa carga parcial substitua uma base mais completa.
