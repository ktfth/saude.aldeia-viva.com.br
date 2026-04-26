# Visão Geral

O Aldeia Viva Saúde é um serviço de inteligência epidemiológica que organiza dados reais do SINAN/OpenDataSUS em uma camada mais fácil de consumir.

Em vez de expor apenas registros brutos, a aplicação entrega:

- índice de risco por município
- alertas altos e críticos por doença e vírus
- catálogo de agravos suportados
- páginas públicas com contexto, SEO e orientação para agentes

## Para quem este projeto foi feito

- equipes de operação e vigilância
- integrações com outros sistemas
- agentes automatizados que precisam de um contrato estável
- pessoas que precisam entender o cenário sem ler a base bruta

## Fluxo de dados

1. O app identifica as doenças habilitadas.
2. As fontes reais são baixadas e agregadas por município.
3. O score de risco é calculado por agravo e consolidado no município.
4. A API expõe os resultados em JSON.
5. O dashboard e as páginas públicas consomem o mesmo estado agregado.

## Princípios do projeto

- Consumibilidade antes de volume
- Dados reais antes de simulação
- Município como granularidade pública
- Score específico por agravo, não uma fórmula única para tudo
- Degradação elegante quando o ambiente não permite reprocessar tudo na hora

## O que o usuário vê

- uma página inicial operacional, com visualização direta do risco
- uma explicação curta da metodologia
- uma área para agentes consumirem o contrato
- endpoints simples para automação

