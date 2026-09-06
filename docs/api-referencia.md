# Referência da API

Referência prática dos endpoints públicos.

Todo exemplo desta página é executado por `tests/test_docs_contract.py`, que
também compara os parâmetros documentados com a assinatura real de cada
endpoint. Um exemplo que pare de funcionar quebra a suíte.

## Estabilidade de `/v1` — o que prometemos

Se você vai embarcar esta API no seu produto, é isto que precisa saber.

**O que não muda dentro de `/v1`:**

- Nenhum campo de resposta é removido ou renomeado.
- Nenhum endpoint publicado desaparece.
- Nenhum parâmetro obrigatório é acrescentado a um endpoint existente.

**O que pode mudar sem aviso:**

- Campos novos aparecem em respostas. Ignore o que não conhece.
- Endpoints novos aparecem.
- Os **valores** mudam quando a base é recarregada — é o ponto do serviço.
  Trate `metadata.carga` e `recencia` como parte do dado, não como enfeite:
  eles dizem de quando é o número que você está exibindo.

**Como a promessa é sustentada.** A forma de resposta de todos os endpoints
públicos está gravada em `contrato-v1.json` — 754 campos — e
`tests/test_contract_stability.py` compara o serviço vivo com esse arquivo a
cada execução da suíte. Um campo que suma reprova, nomeando qual. Regravar o
arquivo é ato deliberado, não conserto de teste.

O `/openapi.json` **não** descreve os corpos de resposta: o FastAPI os publica
como `schema: {}`. Se você precisa saber quais campos existem, use
`contrato-v1.json`, e não o schema.

**Depreciação.** Um campo que precise sair é anunciado aqui antes, e continua
respondendo durante a transição. Uma quebra sem esse caminho é defeito nosso.

## Autenticação e limites

Sem o cabeçalho `X-API-Key` a requisição é atendida como anônima, e o
parâmetro `limite` é **rebaixado silenciosamente** ao teto do tier. Leia os
cabeçalhos de resposta para saber se houve corte.

| Tier | Cabeçalho | Resultados por chamada | Requisições por minuto |
|---|---|---|---|
| Sem chave | — | 5 | 10 |
| Chave gratuita | `X-API-Key` | 20 | 100 |
| Profissional | `X-API-Key` | até 1.000 | 10.000 |

Os números vivem em `domain/tiers.py` e alimentam esta tabela, a página
`/planos` e o `agent.json`. Solicite uma chave em `/planos`.

## Cabeçalhos de resposta

Presentes em `/v1/risk-index` e `/v1/high-alerts`:

| Cabeçalho | Significado |
|---|---|
| `X-Total-Results` | total de registros que satisfazem o filtro |
| `X-Returned-Results` | quantos vieram nesta resposta |
| `X-Limit-Applied` | teto efetivamente aplicado |
| `X-Page` | página devolvida |
| `X-Has-More` | `true` quando existe página seguinte |

Sem eles não há como distinguir "só existem 5 resultados" de "você foi
truncado".

## `GET /health`

Estado do serviço, situação e **idade da carga**.

```bash
curl "http://127.0.0.1:8000/health"
```

O bloco `carga` diz há quantos dias o serviço não busca dados novos. É falha
operacional do serviço, não fato epidemiológico sobre os municípios.

## `GET /v1/risk-index`

Índice enriquecido por município.

Parâmetros:

- `ano` — carrega outro ano-base. Omitido, usa o ano configurado.
- `municipio` — nome parcial, código IBGE de 6 dígitos, ou nome de bairro
  nas cidades suportadas.
- `estado` — UF. Também **desambigua** bairros homônimos entre cidades.
- `somente_altos`
- `nivel_minimo` — `baixo`, `moderado`, `alto` ou `critico`. Filtra por
  `nivel_risco_fonte_atual`, **o mesmo campo que o painel exibe**. Antes usava
  o nível histórico, e 54% dos municípios devolvidos vinham com badge
  diferente do nível pedido.
- `ordenar` — `score` (padrão), `taxa`, `casos` ou `obitos`.
- `pagina` — usada com `limite` para percorrer a base completa.
- `limite`

```bash
curl "http://127.0.0.1:8000/v1/risk-index?municipio=perus&estado=SP&somente_altos=false&limite=10"
```

Ordenado por incidência, que é a única medida comparável entre municípios de
portes diferentes:

```bash
curl "http://127.0.0.1:8000/v1/risk-index?ordenar=taxa&limite=10"
```

Segunda página:

```bash
curl "http://127.0.0.1:8000/v1/risk-index?ordenar=taxa&limite=10&pagina=2"
```

### Campos que exigem atenção

- `incidencia.por_100k` — a medida comparável. Vem com `populacao`,
  `confiavel` e `ressalva`; abaixo de 10.000 habitantes a taxa é marcada
  como não confiável, publicada mas não promovida na ordenação.
- `risk_score` — soma ponderada de contagens absolutas. Correlaciona 0,82
  com a população: responde "onde há mais casos", não "onde é pior".
- `nivel_risco` — pior nível entre os agravos, considerando todos os
  anos-fonte. Gravidade histórica.
- `nivel_risco_fonte_atual` — o mesmo, restrito aos agravos cujo arquivo é do
  ano corrente. É o que responde "exige ação agora?".
- `doencas[].fonte.ano` — ano do arquivo SINAN daquele agravo. O relatório
  reúne anos diferentes; não presuma que `periodo.ano` vale para todos.
- `doencas[].recencia` — até quando o município notificou, medido **dentro**
  da fonte daquele agravo.
- `doencas[].sinais_sem_dados` — termos da `formula_risco` que a fonte daquele
  agravo não alimenta, e que ficam sempre zero. Oito dos dez agravos têm
  alguma lacuna. Scores de agravos com lacunas diferentes **não são
  comparáveis**, e um zero nesses campos pode significar "não houve" ou "a
  fonte não traz".
- `doencas[].composicao` — quanto de `casos_provaveis` ainda não tem
  classificação final. Varia de 0% a 100% entre agravos, e `casos_provaveis` é
  o numerador da incidência: frações muito diferentes produzem incidências
  fora da mesma escala. `casos_descartados` zero costuma significar "nada
  encerrado ainda".
- `filtro_localidade` — presente quando a consulta usou nome de bairro. Se o
  nome existir em mais de uma cidade suportada e a UF não for informada,
  traz `ambiguidade` com as alternativas.

## `GET /v1/high-alerts`

Alertas altos e críticos por município e agravo.

Parâmetros:

- `municipio`
- `estado`
- `doenca` — código exato (`DENG`, `CHIK`, `YF`...) ou parte do nome, sem
  acento. `chikungunya`, `amarela` e `toxoplasmose` funcionam; antes só o nome
  completo ou o código casavam.
- `pagina`
- `limite`

```bash
curl "http://127.0.0.1:8000/v1/high-alerts?estado=SP&limite=10"
```

## `GET /v1/diseases`

Catálogo das doenças e agravos suportados.

```bash
curl "http://127.0.0.1:8000/v1/diseases"
```

## `GET /v1/bairros`

Bairros e distritos resolvidos para município em São Paulo, Rio de Janeiro,
Belo Horizonte e Recife. Filtre com `uf` ou `municipio`.

```bash
curl "http://127.0.0.1:8000/v1/bairros?uf=RJ"
```

Declara `total_bairros`, `total_nomes_distintos` e `nomes_ambiguos` — nomes
que existem em mais de uma cidade e exigem a UF para resolver sem ambiguidade.

## `GET /v1/metadata`

Metadados da carga: fontes com o ano real de cada arquivo, fórmulas,
cobertura populacional e os blocos `carga` e `recencia`.

```bash
curl "http://127.0.0.1:8000/v1/metadata"
```

## `GET /v1/professional-report`

Relatório detalhado. Exige chave com tier `premium` ou `admin`; responde 403
caso contrário.

```bash
curl -H "X-API-Key: premium_partner_key" "http://127.0.0.1:8000/v1/professional-report?estado=SP"
```

## `GET /v1/export/pdf`

**Não implementado.** Responde 501 apontando para
`/v1/professional-report`, que devolve os mesmos dados em JSON para
renderização do lado do cliente.

## `POST /v1/refresh`

Reprocessa as fontes reais e atualiza o estado agregado. É o único endpoint
que dispara carga pesada, então exige chave com permissão de escrita
(`premium` ou `admin`) — sem ela responde 403.

```bash
curl -X POST -H "X-API-Key: premium_partner_key" "http://127.0.0.1:8000/v1/refresh"
```

A cache agregada expira sozinha (padrão: 7 dias, via
`SINAN_REPORT_CACHE_MAX_AGE_DAYS`), e uma recarga que perca cobertura de
agravos é descartada: a base anterior permanece e a degradação é declarada em
`metadata.carga.cobertura_degradada`.

## Endpoints de apresentação

- `/dashboard` — painel operacional
- `/sobre` — metodologia
- `/planos` — níveis de acesso e chaves
- `/agentes` — contrato de consumo para agentes
- `/agent.json` — manifesto legível por máquina
- `/llms.txt` — instruções para LLMs
- `/robots.txt`
- `/sitemap.xml`
- `/docs` e `/openapi.json` — contrato OpenAPI
