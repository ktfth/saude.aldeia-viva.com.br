# Design System — Aldeia Viva Saúde

**Versão:** 0.1 (pós Fase 0 de extração de interface)

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
5. Para ilhas React: usar os mesmos tokens via CSS variables (evitar hardcode de cores no JS).

## Estado Atual (pós Fase 0 + Refatoração CSS)

- CSS extraído de dentro do `app.py` para `web/static/css/main.css` (entry point)
- Estrutura modular criada:
  - `tokens.css`
  - `base.css`
  - `layout.css`
  - `components/` (badge, button, panel, table, detail-panel)
- JS interativo do dashboard em `web/static/js/dashboard.js`
- Infraestrutura de `/static` montada e servida
- `DESIGN.md` criado como fonte da verdade

**Como trabalhar em UI agora:**
1. Tokens → `tokens.css`
2. Novos componentes → pasta `components/`
3. Nunca mais colar CSS gigante dentro do Python

## Status Atual (Sequência Recomendada Executada)

- **A** concluída: CSS completamente modularizado em arquivos pequenos (< 200 linhas cada)
- **B** em andamento: Primeiro React island leve implementado
  - Gráfico de evolução temporal (Chart.js) no painel "Visão Completa por Município"
  - Carregamento dinâmico do Chart.js (só quando necessário)
  - Renderiza automaticamente com os dados de ano atual + anterior
- **C** concluída: DESIGN.md atualizado + estrutura de contribuição documentada

## Como adicionar novo React Island

1. Criar componente em `frontend/islands/nome-do-island/`
2. Build com esbuild/Vite gerando bundle em `web/static/js/islands/`
3. Montar no HTML via placeholder + script de inicialização no `dashboard.js`
4. Documentar em DESIGN.md

## Próximos Passos Imediatos Recomendados

- Melhorar o island atual (suporte a mais anos + animações)
- Adicionar build step simples para ilhas React/Preact
- Refatorar o HTML do detail panel para componentes Jinja parciais
- Adicionar mais documentação de contribuição no `/agentes`

---

**Mantido atualizado.** Qualquer mudança visual relevante deve ter entrada aqui.