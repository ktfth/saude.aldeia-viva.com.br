# Visão Geral

O Aldeia Viva Saúde é um serviço de inteligência epidemiológica que organiza dados reais do SINAN/OpenDataSUS em uma camada mais fácil de consumir.

Em vez de expor apenas registros brutos, a aplicação entrega:

- índice de risco por município, com incidência por 100 mil habitantes
- alertas altos e críticos por doença e vírus
- a idade de cada fonte e a recência de cada sinal, declaradas
- catálogo de agravos suportados
- páginas públicas com contexto e orientação para agentes

## Para quem este projeto foi feito

- equipes de operação e vigilância
- integrações com outros sistemas
- agentes automatizados que precisam de um contrato estável
- pessoas que precisam entender o cenário sem ler a base bruta

## Fluxo de dados

1. O app identifica as doenças habilitadas.
2. As fontes reais são baixadas e agregadas por município.
3. O score é calculado por agravo; o nível do município é o pior entre eles.
4. O denominador populacional do IBGE e a dimensão temporal são aplicados na
   entrada do relatório em memória, valendo para carga nova, cache e snapshot.
5. A API expõe os resultados em JSON, com os cabeçalhos que declaram o corte.
6. O dashboard e as páginas públicas consomem o mesmo estado agregado.

## Princípios do projeto

- Consumibilidade antes de volume
- Dados reais antes de simulação
- Município como granularidade pública
- Score específico por agravo, não uma fórmula única para tudo
- Taxa antes de contagem absoluta: só ela compara municípios de portes diferentes
- Uma autoridade por fato: cada número nasce em um lugar só
- Nenhum número sem a idade do dado que o originou
- Degradação elegante, e declarada: renovar nunca pode perder cobertura

## O que o usuário vê

- uma página inicial operacional, com visualização direta do risco
- uma explicação curta da metodologia
- uma área para agentes consumirem o contrato
- endpoints simples para automação

