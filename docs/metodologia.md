# Metodologia

O projeto transforma notificações epidemiológicas em uma visão operacional por
município.

A página `/sobre` traz esta mesma metodologia com os números da instância em
execução. Este documento é a versão de referência.

## Fontes

- SINAN/OpenDataSUS para os registros reais
- DATASUS para agravos que chegam em formato DBC
- IBGE para nome e UF do município, e para a estimativa populacional
  (SIDRA 6579, variável 9324)

## Granularidade

A granularidade pública é municipal.

Quando a consulta é feita por distrito ou bairro conhecido (São Paulo, Rio de
Janeiro, Belo Horizonte e Recife), a API resolve automaticamente para o
município correspondente e inclui o campo `filtro_localidade` na resposta.
Isso melhora a usabilidade para agentes de campo, mas não cria granularidade
de dados abaixo do nível municipal.

Alguns nomes existem em mais de uma cidade suportada — `penha` e
`campo grande`, hoje. Informe `estado` para escolher; sem a UF a resposta traz
`filtro_localidade.ambiguidade` com as alternativas em vez de escolher em
silêncio.

## O que comparar entre municípios

`incidencia.por_100k` é a única medida comparável entre municípios de portes
diferentes.

`risk_score` é soma ponderada de contagens absolutas. Medido nesta base, tem
**correlação de Pearson 0,82 com a população municipal**: ordenar por ele
responde "onde há mais casos", não "onde é pior". Use `ordenar=taxa` para
priorizar por incidência.

A taxa nunca é publicada sozinha. Abaixo de 10.000 habitantes um único caso a
desloca o bastante para desestabilizá-la, então esses municípios vêm com
`incidencia.confiavel = false` e uma `ressalva`, e ficam abaixo dos confiáveis
na ordenação. São publicados, nunca suprimidos em silêncio.

## Score por agravo

O score é calculado por agravo, com pesos diferentes conforme o perfil da
doença. Não existe fórmula única: comparar doenças com lógicas epidemiológicas
diferentes como se fossem idênticas seria erro de categoria.

Entram no cálculo, com pesos que variam por agravo:

- casos prováveis
- sinais de alarme
- casos graves
- hospitalizações
- óbitos

`formula_risco` acompanha cada agravo e explica o cálculo daquele caso.

## Como o nível de risco nasce

Cada agravo tem o seu nível, calculado com o seu próprio perfil. O nível do
município é o **pior nível entre os agravos** dele.

Não há fórmula aplicada sobre a soma. Somar dez agravos e cinco anos-fonte num
único número deixava 24,3% dos 5.339 municípios em "crítico", e a variável
deixava de discriminar.

- `nivel_risco` — considera todos os anos-fonte. Gravidade histórica.
- `nivel_risco_fonte_atual` — restrito aos agravos cujo arquivo é do ano
  corrente. É o que responde "exige ação agora?".
- `historico_mais_grave` — verdadeiro quando o município já esteve em nível
  pior por conta de fontes antigas. Contexto, não prioridade.

Níveis possíveis: `baixo`, `moderado`, `alto`, `critico`.

## Três relógios

Três fatos temporais diferentes, que não podem ser confundidos:

| Campo | O que mede | Natureza |
|---|---|---|
| `metadata.carga` | há quanto tempo o serviço buscou dados | falha operacional do serviço |
| `doencas[].fonte` | de que ano é o arquivo daquele agravo | cobertura do acervo |
| `doencas[].recencia` | até quando o município notificou, dentro daquela fonte | fato epidemiológico |

O relatório reúne **anos diferentes por agravo**. Um agravo com sinal recente
cuja fonte é de 2022 continua sendo dado de 2022: confira `fonte.ano` antes de
datar qualquer afirmação.

Medir a recência contra a data de hoje, em vez de contra o horizonte da fonte,
transforma um pipeline parado em falso diagnóstico sobre milhares de
municípios. Por isso os três são publicados separadamente.

## Como interpretar

- `incidencia.por_100k` compara municípios; `risk_score` dimensiona a resposta
- `nivel_risco_fonte_atual` prioriza ação de hoje
- `doencas_altas` mostra o que está elevando o município
- `fonte.ano` data a afirmação
- `formula_risco` explica o cálculo daquele agravo

## Limitações

- os dados dependem da disponibilidade das fontes reais
- a carga pode mudar conforme o ano e a abrangência disponível
- o SINAN registra o que foi **notificado**: incidência baixa pode significar
  poucos casos ou pouca notificação, e o número não distingue os dois
- sem as extensões nativas de DBC, apenas as fontes CSV são carregadas
- a interpretação não substitui vigilância epidemiológica oficial
