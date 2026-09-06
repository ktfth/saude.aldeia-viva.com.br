# Design System — Aldeia Viva Saúde

**Versão:** 0.4 (minimalismo + dimensão temporal + denominador + carga viva)

Este documento é a fonte da verdade para decisões visuais e de interface.  
Objetivo: permitir melhorias sustentáveis sem que o projeto volte a parecer "vibe codado".

## Princípios

- **Clareza operacional primeiro** — A interface existe para ajudar agentes de vigilância a priorizar rápido.
- **Honestidade** — Nunca fingir granularidade que não existe (dados sempre municipais).
- **Pequenos arquivos** — CSS e componentes em pedaços de 100-300 linhas no máximo.
- **Sem frameworks pesados** — Hotwire + React islands leves quando necessário (nunca Next.js).
- **Imutabilidade de tokens** — Uma vez definido, um token só muda com justificativa forte + atualização em todo o sistema.

## Tokens (Design Tokens)

Todos os tokens estão definidos em `web/static/css/tokens.css` (a ser criado na refatoração).

### Cores

```css
--ink: #17211c;          /* Texto principal */
--muted: #5d6b63;        /* Texto secundário, legendas */
--line: #d9e4dd;         /* Bordas sutis */
--panel: #ffffff;        /* Fundos de cards/panels */
--soft: #f4f8f5;         /* Fundos suaves, hover states */

--green: #146c43;        /* Cor principal da marca (saúde / positivo) */
--teal: #067a76;         /* Acentos, links, destaques */
--amber: #9a5b00;        /* Nível Alto */
--red: #b42318;          /* Nível Crítico */
--blue: #2457a6;         /* Nível Moderado */
```

### Tipografia

- **Família principal:** Inter (system-ui fallback)
- **Tamanho base:** 1rem (16px)
- **Escala responsiva** via `clamp()` para títulos e métricas
- Pesos comuns: 700–800 para cabeçalhos e labels fortes, 500-600 para corpo

### Espaçamento e Ritmo

- Usar `clamp(16px, Xvw, Ypx)` para responsividade
- Ritmo vertical consistente em múltiplos de 4px/8px
- Gap padrão entre seções: 24px–32px

### Sombras e Elevação

- `--shadow`: `0 18px 45px rgba(23, 33, 28, .08)` — elevação padrão de cards
- Painéis de detalhe usam sombras um pouco mais suaves (`0 10px 30px rgba(0,0,0,.06)`)

### Raio de borda

- `--radius`: `16px` (padrão para cards e painéis)
- Botões e badges: `10px` ou `999px` (pill)

## Componentes Principais

### .panel
Base de todos os cards e seções. Sempre usa `--panel`, `--line`, `--shadow`.

### .badge
Níveis de risco (baixo / moderado / alto / critico). Cores semânticas.

### .button
Três variantes principais:
- `.primary` (verde)
- `.ghost`
- Padrão (borda)

### Métricas / .metric
Cards de números grandes no "Radar atual".

### Detail Panel (`#municipio-detail-panel` e `.detail-*`)
Sistema mais refinado (adicionado durante o trabalho de "visão completa por município"). Usa tipografia tabular, contrastes fortes em números importantes (óbitos em vermelho).

## Padrões de Responsividade

- Breakpoints implícitos via `clamp()` e media queries pontuais
- Tabelas: scroll horizontal em telas estreitas + `display: grid` com `data-label` para mobile (ver `.table-wrap` + media query)
- Header: sticky com backdrop blur

## Como Evoluir o Design

1. **Nunca** editar `main.css` diretamente para novas features (após refatoração).
2. Adicionar tokens novos em `tokens.css`.
3. Criar componentes em `components/nome-do-componente.css`.
4. Documentar aqui qualquer decisão que não seja óbvia.
5. Visualizações: SVG inline no servidor por padrão. Ver "Visualização: o critério de admissão".

## A dimensão temporal (adicionada na reconstrução de 2026-09)

O produto exibia alertas "alto/crítico" do ano-base corrente sem revelar que a
maioria vinha de arquivos-fonte de anos anteriores. Três fatos temporais
diferentes eram apresentados como um só. Agora são três, com tratamento visual
distinto e nomes distintos:

| Relógio | Onde aparece | O que é | Componente |
|---|---|---|---|
| **Carga** | Barra fixa no topo do painel | Há quantos dias o serviço buscou dados. Falha operacional do serviço. | `.data-status` |
| **Fonte** | Tira de agravos e coluna do detalhe | De que ano é o arquivo daquele agravo. Cobertura do acervo. | `.signal-strip`, `.source-tag` |
| **Sinal** | Coluna de risco e detalhe | Até quando o município notificou, medido dentro da fonte daquele agravo. Único fato epidemiológico dos três. | `.signal-tag` |

**Regra que não se negocia:** nunca colapsar os três num só indicador. Medir o
sinal contra "hoje" em vez do horizonte da fonte fazia 100% da Meningite
(arquivo de 2022) parecer município omisso e 100% da Dengue (arquivo de 2026)
parecer município ativo — descrevendo qual arquivo foi baixado, não o
comportamento de território algum.

### Um canal visual por variável

- **cor do segmento na tira** → idade da fonte
- **badge** → gravidade
- **rótulo de sinal** → recência dentro da fonte

Os tokens de temporalidade (`--fonte-*`, `--sinal-*`) vivem em
`components/signal.css` e são deliberadamente separados da escala de risco.
"Quão atual é o dado" não é "quão grave é a situação"; misturar os dois no
mesmo canal foi exatamente o defeito que originou o componente.

## Minimalismo: o critério de corte

Um bloco só permanece na tela se mudar uma decisão. O painel foi de oito
blocos para quatro (estado do dado · busca · tabela · detalhe).

Removidos, com o motivo:

| Removido | Por quê |
|---|---|
| Hero de landing page | O `/dashboard` não é página de venda; ocupava a primeira dobra inteira |
| Painel "Radar atual" | Quatro métricas nacionais que não mudam nenhuma decisão municipal |
| Atalhos rápidos | Duplicavam o seletor de nível mínimo, logo abaixo dele |
| Legenda de risco | Os badges já se explicam |
| Coluna "Alertas altos" | Mesmo dado da tabela, noutro corte |
| Banner de comparação | Anunciava ao usuário um detalhe de implementação |
| Coluna "Doenças altas" | Texto longo substituído pela tira, mais densa e mais verdadeira |

## Visualização: o critério de admissão

Uma visualização entra se responder a algo que a tabela não responde, e sai se
custar mais do que entrega.

O gráfico de evolução em Chart.js foi removido: carregava ~200 KB de CDN para
desenhar uma linha de **dois pontos**, e a função de carregamento tinha uma
Promise que nunca resolvia quando o script já estava em voo. Foi substituído
pela tira de agravos em SVG inline, gerada no servidor.

**Padrão para novas visualizações:**

1. SVG inline renderizado no servidor é o default. Sem request, sem JS, sem CDN.
2. Se precisar de interação, renderize também em `dashboard.js` — a tira existe
   nos dois lados porque a primeira tabela vem do servidor e as seguintes do fetch.
3. Biblioteca externa só quando o SVG puro comprovadamente não resolve. Uso em
   campo, celular, conectividade ruim.
4. Ordene os elementos para que a mesma cor caia na mesma posição entre linhas,
   senão a visualização deixa de ser comparável.

## Estado atual do sistema

- CSS modular em `web/static/css/` — tokens, base, layout e `components/`
  (badge, button, panel, table, detail-panel, alerts, **signal**)
- Renderização server-side da camada temporal em `presentation/signal.py`
- `web/static/js/dashboard.js` — 14 KB, zero dependências externas
- `app.py` reduzido de 2.858 para 1.832 linhas: `BASE_CSS` (605 linhas) e
  `DASHBOARD_JS` (342) eram inalcançáveis em runtime
- Tabela vira cartões em telas ≤720px, usando os `data-label` que já existiam
  no HTML e cuja media query este documento prometia sem que ela existisse

**Não há React neste projeto.** `frontend/islands/` está vazio e nunca foi
commitado; a "ilha" descrita em versões anteriores deste documento era uma
função Chart.js, hoje removida. Se um island for realmente necessário no
futuro, ele precisa justificar o próprio peso contra a regra 1 acima.

## Como trabalhar em UI aqui

1. Tokens novos → `tokens.css` (ou `signal.css`, se forem temporais)
2. Componentes novos → `components/`
3. Nada de CSS dentro do Python
4. Todo número exibido carrega a idade do dado que o originou
5. Antes de adicionar um bloco: qual decisão ele muda? Se não houver resposta,
   ele não entra

## O denominador (adicionado na iteração 2)

`risk_score` é soma ponderada de contagens absolutas. Medido no relatório
real, ele tem **correlação de Pearson 0,822 com a população municipal**
(Spearman 0,658): ordenar por score era, em boa medida, ordenar por tamanho
de cidade, e o painel apresentava isso como priorização de risco.

Trocar a ordenação padrão do painel para incidência por 100 mil habitantes
mudou **8 dos 8 municípios da primeira dobra**:

| antes (por score) | agora (por taxa) |
|---|---|
| São Paulo — 86/100k | Sete Quedas/MS — 6.612/100k |
| Goiânia | Caldas Novas/GO — 6.211/100k |
| Porto Alegre | Rialma/GO — 5.910/100k |
| Rio de Janeiro — 63/100k | Firminópolis/GO — 4.772/100k |

Onze dos quinze municípios de maior taxa estavam fora do top 50 por contagem.

**Regras da taxa:**

1. Nunca publicada como número nu — o denominador vem ao lado.
2. Abaixo de 10.000 habitantes (`MIN_RELIABLE_POPULATION`) a taxa é marcada
   como não confiável e desce na ordenação. **Publicada, nunca suprimida em
   silêncio** — omitir sem dizer é tão desonesto quanto publicar sem ressalva.
3. Sem população conhecida, a célula diz "sem denominador" e mostra a
   contagem absoluta. A ausência é um estado nomeado, não um traço.
4. `risk_score` continua existindo e continua sendo contagem absoluta —
   útil para dimensionar resposta, inútil para comparar municípios de portes
   diferentes. O seletor "Ordenar por" deixa isso explícito e reversível.

## Uma autoridade por fato

Regra que emergiu das duas iterações e que evita o defeito mais caro do
projeto: **cada fato tem exatamente um lugar que o produz.**

| Fato | Autoridade |
|---|---|
| Gravidade por agravo | `domain/risk.py` (perfil por agravo) |
| Gravidade municipal | pior nível entre os agravos — nunca fórmula sobre a soma |
| Idade da fonte e do sinal | `aggregation/recency_enrichment.py` |
| População e incidência | `aggregation/population_enrichment.py` |

O `report_builder` tentava computar população e taxa a partir de um lookup
que nunca teve o campo: passava nos testes e produzia `None` em 100% dos
casos em produção. Enriquecer na entrada do relatório em memória faz o
resultado valer para as três origens do dado — carga nova, cache em disco e
snapshot embarcado — em vez de só depois de uma recarga completa.

## O aviso tem que ter conserto

A barra de estado dizia "carga de 132 dias" e nada no sistema jamais buscaria
dado novo. Investigar a causa raiz revelou dois defeitos encadeados, e o
segundo só apareceu porque o primeiro foi corrigido:

1. A cache agregada decidia com `if cache_path.exists()` — era eterna por
   construção, já que a chave é `(versão, ano, agravos)`.
2. Com a expiração ligada, a primeira recarga real levantou
   `NameError: load_latest_available_records`. A função tinha sido movida
   para `ingestion/sinan_loader.py` na "Fase 0" deixando só um comentário; o
   import nunca foi acrescentado. **O serviço era incapaz de buscar dado
   novo desde aquele refactor** — e nunca falhou visivelmente porque a cache
   não expirava e os testes de refresh mockavam a carga inteira.

**Regra que sai daqui:** um aviso na interface sem caminho de conserto é
dívida disfarçada de transparência. Se o painel diz que a carga está velha,
alguma coisa no sistema tem que estar tentando renová-la — e essa tentativa
precisa ser exercitada por teste, não presumida.

Corolário para a suíte: `tests/__init__.py` desliga a expiração, senão cada
`TestClient` baixaria as fontes do SINAN de verdade. A política em si é
testada por parâmetro em `test_cache_freshness.py`.

## Dívida conhecida

- `bundled_report_snapshot.py` tem 893 KB versionados. **Avaliado e mantido**:
  é fallback load-bearing (`app.py:482,500`), custo único de repositório, sem
  dano evidenciado. A hipótese de removê-lo caiu por terra na medição.
- `nivel_risco` histórico ainda deixa 23,5% dos municípios em "crítico". O
  recorte por fonte atual resolve para a operação (8,8%), mas o número
  histórico é pouco discriminante por natureza — ele agrega cinco anos-fonte.
- As fontes DBC exigem `datasus-dbc` e `dbfread`, extensões nativas sem wheel
  para toda versão de Python. Estão em `requirements.txt` e o erro no ponto de
  uso é acionável, mas um ambiente sem elas carrega apenas as fontes CSV.

---

**Mantido atualizado.** Qualquer mudança visual relevante deve ter entrada aqui.
