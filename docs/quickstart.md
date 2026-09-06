# Guia Rápido

Este guia mostra o caminho mínimo para colocar o projeto em execução local.

## Avaliar sem chave

Uma consulta **dirigida** devolve a resposta completa no acesso anônimo. O
teto de 5 registros corta varredura ampla, não a pergunta "como está a minha
cidade". Medido em produção:

```
?municipio=Goiania            ->  3 de 3     completo
?municipio=Recife&estado=PE   ->  1 de 1     completo
?municipio=355030             ->  1 de 1     completo
?doenca=dengue&estado=SP      ->  5 de 634   cortado
```

```bash
curl "https://saude.aldeia-viva.com.br/v1/risk-index?municipio=Goiania"
```

A resposta traz o que diferencia este serviço de baixar o SINAN direto:
`incidencia.por_100k` com denominador do IBGE, `nivel_risco_fonte_atual`
separado do histórico, e `recencia` dizendo de quando é cada sinal. Leia
`X-Total-Results` para saber se houve corte.

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
- `http://127.0.0.1:8000/planos`
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

`POST /v1/refresh` é o único endpoint que dispara carga pesada, então exige
uma chave com permissão de escrita. Sem ela responde 403.

```bash
curl -X POST -H "X-API-Key: premium_partner_key" "http://127.0.0.1:8000/v1/refresh"
```

Na maioria dos casos não é preciso chamá-lo: a cache agregada expira sozinha
(padrão de 7 dias, ajustável por `SINAN_REPORT_CACHE_MAX_AGE_DAYS`) e o
serviço tenta renovar na próxima inicialização.

## Sem chave, os resultados vêm cortados

Sem o cabeçalho `X-API-Key` a requisição é anônima e `limite` é rebaixado a
5 resultados, em silêncio. Os cabeçalhos `X-Total-Results` e
`X-Limit-Applied` revelam o corte:

```bash
curl -i "http://127.0.0.1:8000/v1/risk-index?limite=100"
```

## Quando os dados parecem vazios

- confira se o app terminou de inicializar
- verifique `GET /health` — o bloco `carga` diz a idade do dado
- valide se o ano configurado tem fontes disponíveis
- confirme se a cache local não está desabilitada por variável de ambiente

## Sem as extensões nativas de DBC

`datasus-dbc` e `dbfread` são extensões nativas e nem sempre têm wheel para a
versão de Python em uso. Sem elas o serviço carrega apenas as fontes CSV
(Dengue, Chikungunya, Zika e Febre Amarela) e registra o motivo em
`metadata.erros`. Uma carga assim **não substitui** uma carga mais completa
que já esteja em cache.

