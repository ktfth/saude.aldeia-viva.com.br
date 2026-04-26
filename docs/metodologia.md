# Metodologia

O projeto transforma notificações epidemiológicas em uma visão operacional por município.

## Fontes

- SINAN/OpenDataSUS para os registros reais
- DATASUS para agravos que chegam em formato DBC
- IBGE para enriquecimento de município e UF

## Granularidade

A granularidade pública é municipal.

Quando a consulta é feita por distrito ou bairro conhecido, a API pode resolver a busca para o município correspondente. Isso melhora a usabilidade, mas não cria granularidade nova.

## Score

O score é calculado por agravo, com pesos diferentes conforme o perfil da doença.

O projeto não usa uma fórmula única para todos os casos. Isso evita comparar doenças com lógicas epidemiológicas diferentes como se fossem idênticas.

## O que entra no risco

- casos prováveis
- sinais de alarme
- casos graves
- hospitalizações
- óbitos

Cada agravo pode ponderar esses sinais de forma diferente.

## Leitura dos níveis

- baixo
- moderado
- alto
- critico

## Como interpretar

- `doencas_altas` mostra o que está elevando o município
- `risk_score` permite ordenação
- `nivel_risco` facilita priorização
- `formula_risco` explica o cálculo usado naquele agravo

## Limitações

- os dados dependem da disponibilidade das fontes reais
- a carga pode mudar conforme o ano e a abrangência disponível
- a interpretação não substitui vigilância epidemiológica oficial

