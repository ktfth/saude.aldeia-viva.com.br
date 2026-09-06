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

## Docker — o alvo que existe no repositório

O `Dockerfile` e o `docker-compose.yml` na raiz sobem o serviço, com um
`HEALTHCHECK` apontando para `/health`.

```bash
docker compose up --build
```

A imagem **não** carrega chaves de API: `data/users.json` está no
`.dockerignore` para que um artefato construído na máquina de quem tem o
arquivo não saia com a credencial dentro. Passe as chaves pelo ambiente:

```bash
docker run -p 8000:8000 -e USERS_DB_JSON='{"keys": {"SUA_CHAVE": {"owner": "...", "tier": "premium", "rate_limit": 10000}}}' av-saude
```

Sem isso o serviço sobe, avisa no log e responde apenas no tier anônimo —
verificado construindo a imagem a partir de um clone limpo.

O `.cache` local também fica de fora: o container carrega o snapshot
versionado em `data/reports`, e não o cache da máquina de quem construiu.

## Vercel

`api/index.py` e `vercel.json` foram removidos pelo commit `e129698`
(2026-04-26, "fix deploy and improved docs") — no mesmo commit que escreveu a
versão anterior deste documento afirmando que o deploy era na Vercel.
Restaurados.

```bash
vercel --prod
```

A função roda com o snapshot embarcado: `.vercelignore` exclui
`data/reports/` e `api/data/`, que somariam 34 MB ao bundle sem acrescentar
dado nenhum — a base inteira já está em
`bundled_report_snapshot.py`.

Duas variáveis vão no `vercel.json` porque o sistema de arquivos é somente
leitura fora de `/tmp`:

- `SINAN_CACHE_DIR=/tmp/datasus` — para onde vai qualquer tentativa de escrita
- `SINAN_REPORT_CACHE_MAX_AGE_DAYS=0` — desliga a expiração, já que não há
  cache persistente entre invocações e cada tentativa de recarga só somaria
  latência ao cold start

Consequência a conhecer: **a instância da Vercel serve o snapshot embarcado**,
que é a carga de abril de 2026. A barra de estado do painel declara essa idade
em todas as páginas. Para dado novo, é preciso regenerar o snapshot e publicar,
ou hospedar num ambiente com disco persistente — o Docker acima.

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
| `USERS_DB_JSON` | as chaves como JSON literal, para carregar por segredo em vez de arquivo. Tem precedência sobre `USERS_DB_PATH` |
| `USAGE_LOG_PATH` | log de uso (`data/usage.jsonl`). Grava a impressão digital da chave, nunca a chave |

## As chaves de API não estão no repositório

`data/users.json` guarda credenciais e por isso não é versionado. A
consequência era silenciosa: **um deploy disparado pelo git sobe sem chave
alguma** — todo endpoint autenticado devolve 401 e apenas o tier anônimo
responde. O deploy atual funciona porque o arquivo existe na máquina de quem
roda `vercel deploy` e sobe junto com o diretório.

Duas formas de resolver, em ordem de preferência:

1. **`USERS_DB_JSON` como segredo do ambiente** — as chaves deixam de depender
   de um arquivo que o repositório não pode transportar:

   ```bash
   vercel env add USERS_DB_JSON production
   # cole: {"keys": {"SUA_CHAVE": {"owner": "...", "tier": "premium", "rate_limit": 10000}}}
   ```

2. **Manter o arquivo** e continuar publicando pela CLI a partir de uma
   máquina que o tenha.

O serviço agora avisa no log quando sobe sem chave nenhuma, em vez de aceitar
a situação em silêncio. Uma entrada cuja chave comece com `sha256:` guarda o
digest em vez do segredo.

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
