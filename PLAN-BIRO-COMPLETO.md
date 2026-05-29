# Plano de Evolução do BIRO do SUS — Aldeia Viva Saúde
**Versão:** 1.0  
**Data:** 2026-05-28  
**Status:** Proposta para Aprovação  
**Solicitante:** Usuário (via scoping explícito)  
**Prioridade #1 declarada:** Drill-down + comparações (nacional → UF → município, ano vs ano)  
**Restrição principal:** Manter foco operacional e simplicidade para agentes comunitários e vigilância municipal

---

## 1. Diagnóstico do Estado Atual (BIRO existente)

O projeto **Aldeia Viva Saúde** (app.py + bundled_report_snapshot.py) é atualmente o "BIRO do SUS" operacional do time.

### Pontos Fortes (manter e evoluir)
- Ingestão real de SINAN/OpenDataSUS (DBC via datasus_dbc + CSVs diretos)
- Modelagem excelente de **RiskProfile por agravo** (fórmulas diferentes para arbovírus, leptospirose, febre amarela, meningite, etc.)
- Agregação por município com enriquecimento IBGE
- Degradação elegante (snapshot embarcado quando fontes externas falham)
- API limpa e contratada (`/v1/risk-index`, `/v1/high-alerts`, `/v1/diseases`, `/v1/metadata`, `/v1/refresh`)
- Foco em "priorização operacional" (nível de risco + doenças altas por município)
- Dashboard atual é leve (sem dependências pesadas de frontend)

### Limitações Graves para "Visão Completa de Epidemiologia"
- **Visualização:** Apenas tabela HTML + lista de alertas + filtros básicos (vanilla JS). Zero gráficos, zero mapas, zero séries temporais.
- **Análise temporal:** Inexistente (não há comparação ano a ano, curva epidêmica, ou semana epidemiológica).
- **Drill-down geográfico:** Inexistente (não navega BR → UF → município de forma estruturada).
- **Métricas epidemiológicas clássicas:** Sem taxas de incidência por 100.000 habitantes.
- **Estratifação:** Sem faixa etária, sexo, raça/cor.
- **Cobertura de agravos:** Limitada a 10 códigos (DEFAULT_DISEASE_CODES). SINAN tem muito mais.
- **Exportação:** Básica (JSON da API). Sem relatórios visuais ou PDF com contexto.
- **Arquitetura:** Monólito de ~3000+ linhas em app.py (viola guideline de arquivos pequenos 200-400 linhas).

**Conclusão do diagnóstico:** O produto atual é um excelente "sistema de alerta e priorização de risco municipal". Ele não é (ainda) um BI de epidemiologia completo.

---

## 2. Visão de Produto (o que significa "completo" neste contexto)

**Definição alinhada com o scoping:**
> "Um BIRO de epidemiologia completo que permite drill-down e comparações temporais/geográficas, mantendo simplicidade operacional para agentes comunitários e equipes de vigilância municipal."

### Princípios de Design (não negociáveis)
1. **Operacional primeiro** — A interface principal deve continuar sendo de leitura rápida em campo (celular, baixa conectividade, decisão em < 10 segundos).
2. **Drill-down como estrela do norte** — Nacional → UF → Município → Agravo → Sinais (casos, graves, óbitos, datas).
3. **Comparação temporal nativa** — Ano atual vs ano anterior, mesmo município ou mesmo agravo.
4. **Taxas antes de absolutos** — Incidência por 100k deve ser o padrão (com população IBGE ou estimativa).
5. **Degradação elegante** — Manter o comportamento atual quando fontes SINAN estiverem indisponíveis.
6. **Imutabilidade e TDD** — Todo novo código segue as regras do repositório (80%+ cobertura, testes antes de implementação, sem mutação).

### O que NÃO é prioridade agora (para não inflar escopo)
- 40+ agravos completos na Fase 1 (começar com os atuais + 3-4 novos de alto impacto)
- Dashboards de pesquisa/acadêmicos pesados
- Autenticação complexa ou RBAC fino (manter modelo atual de tiers anônimo/free/professional)
- Previsão/modelos de ML (fora do escopo desta evolução)

---

## 3. Arquitetura Recomendada (Target)

### Opção Escolhida: **Híbrido Progressivo (Recomendado)**

**Fase 1 (curto prazo):**
- Manter backend Python/FastAPI como fonte de verdade
- Extrair lógica de domínio para pacotes pequenos (`domain/`, `ingestion/`, `aggregation/`, `risk/`)
- Adicionar camada de agregação temporal (pre-computada ou sob demanda com cache forte)
- Melhorar API com novos endpoints de drill-down e comparação
- Dashboard atual continua existindo (para agentes que precisam de algo ultra-leve)

**Fase 2+ (quando aprovado):**
- Frontend híbrido **Hotwire (Turbo + Stimulus) + React islands** sobre o backend FastAPI existente
- O backend continua servindo HTML (Jinja2 ou templates simples) com Turbo Drive para navegação rápida sem recarregamento completo
- React é usado de forma cirúrgica apenas onde a complexidade justifica ("islands"):
  - Mapa coroplético interativo (BR → UF → município)
  - Gráficos de série temporal e comparadores
  - Componentes de drill-down avançado e filtros complexos
- Visualizações leves (SVG sparklines, tabelas progressivas) continuam server-rendered para o modo operacional rápido

**Alternativa rejeitada:** Aplicação SPA completa com Next.js ou Vite + React como app separado. Aumenta complexidade de deploy, quebra a simplicidade operacional para agentes de campo e vai contra o desejo explícito de "Hotwire + React, nada de Next".

**Justificativa:** Combina o melhor dos dois mundos — velocidade e simplicidade de Hotwire para a maior parte da experiência operacional + poder de React apenas nos pontos que realmente precisam de interatividade rica (mapas e gráficos comparativos). Mantém o backend Python como fonte de verdade e reduz drasticamente a superfície de manutenção para o time.

---

## 4. Estratégia de Dados e Modelo

### Evolução do Modelo de Dados (imutável)

**Modelo atual (manter compatibilidade):**
- `municipios[]` com `doencas_por_codigo`
- `risk_score` + `nivel_risco` por município (agregado)

**Novo modelo necessário:**
- `Period` (ano + optional mês/semana)
- `Geography` (nível: nacional | uf | municipio)
- `Observation` por (geography, period, disease)
  - casos_provaveis, sinais_alarme, casos_graves, obitos, hospitalizacoes
  - populacao (para taxa)
  - risk_score (preservado por compatibilidade)
- `Comparison` (current vs previous period) — calculado na API ou materializado

**Fontes de população:** Usar estimativas IBGE (já tem lookup de municípios; estender para trazer população).

**Armazenamento:**
- Manter JSON reports atuais como cache de "última carga"
- Adicionar estrutura de snapshots por ano (`data/snapshots/{year}/`)
- Para drill-down multi-ano: pré-agregar ou usar DuckDB leve (avaliar) — evitar pandas pesado inicialmente

**Expansão de agravos:**
- Fase 1: Manter os 10 atuais + adicionar 2-3 de alto impacto (ex: Tuberculose, HIV recente, Sífilis congênita)
- Fase 2: Tornar o catálogo configurável via arquivo YAML + permitir mais DISEASE_SOURCES

---

## 5. Roadmap em Fases (com Gates de TDD e Agentes)

### Fase 0 — Fundação (1-2 semanas)
**Objetivo:** Preparar o terreno sem quebrar nada existente.

- Refatorar app.py em módulos pequenos (domain/, ingestion/, api/, web/, risk/)
- Extrair todos os dataclasses e funções puras de risco para `domain/risk_profiles.py`
- Aumentar cobertura de testes existentes para 80%+ nas partes que serão tocadas
- Criar contrato de versão da API (`/v1/risk-index` deve continuar funcionando idêntico)

**Entregáveis:**
- Estrutura de pastas limpa
- Testes passando com 80%+ cobertura na camada de domínio
- Nenhum breaking change na API pública atual

**Agentes obrigatórios:**
1. `tdd-guide` (para escrever testes da refatoração)
2. `code-reviewer` (revisão final da Fase 0)
3. `security-reviewer` (validar inputs e degradação)

**Gate:** Aprovação explícita + todos os testes verdes + review sem CRITICAL.

### Fase 1 — Drill-down + Comparação Temporal (Core da Prioridade #1)
**Objetivo:** Entregar o que o usuário pediu como #1.

Entregáveis:
- Novos endpoints:
  - `GET /v1/trends?municipio=...&doenca=...&anos=2024,2025`
  - `GET /v1/drilldown?uf=SP&ano=2025` (retorna municípios da UF com breakdown por agravo)
  - `GET /v1/comparison?municipio=...&ano_base=2025&ano_comparado=2024`
- Cálculo de taxas de incidência (por 100k) — exige trazer população no lookup
- Materialização de snapshots por ano (para comparação rápida)
- Dashboard leve atualizado com:
  - Seletor de ano (ou range)
  - Botão "Comparar com ano anterior"
  - Tabela expandida mostrando variação % 

**Visualizações mínimas (server-rendered + Hotwire progressivo):**
- Sparklines simples (SVG inline) nas linhas da tabela para tendência
- Cards de "Variação vs ano anterior"
- Atualizações parciais via Turbo Frames quando o usuário interage com filtros de ano/UF

**TDD obrigatório:**
- Testes de propriedade para fórmulas de risco (nunca quebrar comportamento existente)
- Testes de integração para novos endpoints
- Testes de contrato da API (snapshot dos payloads)

**Agentes:**
- `tdd-guide` (obrigatório antes de qualquer implementação)
- `python-reviewer` + `code-reviewer`
- `security-reviewer` (novos inputs de ano/município/uf)

**Gate:** 
- 80%+ cobertura nos novos módulos
- Comparação ano a ano funcionando para pelo menos 3 agravos
- Drill-down UF → municípios funcionando
- Nenhum impacto negativo no dashboard atual para agentes

### Fase 2 — Visualização Moderna (Hotwire + React Islands)
**Objetivo:** Entregar visualização rica de epidemiologia (mapas + gráficos + drill-down) mantendo o modo operacional ultra-leve para agentes de campo.

Entregáveis:
- Evolução do backend FastAPI para suportar Hotwire:
  - Respostas HTML parciais (Turbo Frames / Turbo Streams quando aplicável)
  - Endpoints otimizados para atualizações parciais (ex: `/partials/municipio-trends`)
- Integração de **React islands** apenas nos componentes complexos:
  - Mapa coroplético do Brasil com drill (BR → UF → municípios) usando Leaflet + React (ou MapLibre)
  - Gráficos de série temporal e comparador ano a ano (Recharts ou Chart.js dentro de componentes React)
  - Filtros avançados e tabela de drill-down interativa (React)
- Página principal de exploração que combina:
  - Turbo Drive para navegação e formulários (sensação de SPA sem ser SPA)
  - React islands montados progressivamente nos containers certos
- Modo "Operacional Rápido" preservado e melhorado (dashboard atual ou versão otimizada com Turbo, funciona bem mesmo com JS parcial)
- Export de imagens/PDF dos componentes React (html-to-canvas ou backend rendering)

**Stack frontend sugerido (alinhado com sua orientação):**
- **Hotwire** (Turbo 8 + Stimulus) para a maior parte da experiência
- **React 19** (somente islands / componentes isolados)
- TypeScript para os componentes React
- Tailwind CSS
- Recharts ou Chart.js (dentro dos islands React)
- Leaflet (ou MapLibre GL) + React para o mapa
- Sem Next.js, sem roteamento de cliente pesado, sem app separado

**Backend (FastAPI):**
- Jinja2 (ou similar) para templates base + partials
- Respostas HTML + Turbo-compatible
- Novos endpoints de partials e dados agregados (GeoJSON, séries temporais)
- Manter toda a lógica de domínio e risco no Python

**TDD / Testes:**
- Testes de componente React com Vitest + React Testing Library (apenas para os islands)
- Testes de integração Hotwire/Turbo (Playwright focado em fluxos de drill-down e comparação)
- Testes de backend (FastAPI TestClient) para os novos endpoints de partials
- Testes E2E dos fluxos principais (drill-down geográfico + comparação temporal)

**Agentes obrigatórios:**
- `tdd-guide`
- `python-reviewer` (backend + templates)
- `typescript-reviewer` (apenas para os componentes React)
- `e2e-runner`
- `code-reviewer` com atenção especial à separação clara entre Hotwire e React islands

**Gate:** 
- Fluxo completo de drill-down + comparação funcionando com Hotwire + React islands
- Mapa e gráficos carregando com dados reais
- Dashboard operacional rápido continua rápido e útil (mesmo sem JS avançado)
- Performance boa em conexões modestas (agentes de campo)

### Fase 3 — Expansão de Agravos + Polimento Operacional
- Adicionar mais 5-8 agravos de alto interesse (Tuberculose, Sífilis, etc.)
- Notificações / alertas por município (webhook ou e-mail simples para profissionais)
- Relatório municipal gerável em PDF (usando WeasyPrint ou similar leve)
- Melhorias de performance e cache (Redis opcional ou file-based forte)
- Documentação completa para agentes (guia de uso do novo BIRO)

---

## 6. Riscos e Mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|-------|---------------|---------|-----------|
| Fontes SINAN/DATASUS instáveis ou mudam formato | Alta | Alto | Manter snapshot + fallback + testes de contrato de ingestão |
| Volume de dados explode com multi-ano + mais agravos | Média | Médio | Pré-agregação por ano/UF + cache agressivo + paginação |
| Agentes de campo rejeitam interface mais complexa | Média | Alto | Manter rota `/dashboard` atual como "Modo Rápido" para sempre |
| Refatoração do monólito introduz bugs em fórmulas de risco | Média | Crítico | TDD com property-based tests nas RiskProfiles + revisão por `code-reviewer` |
| Performance de drill-down ruim | Média | Médio | Materializar agregados + limite de granularidade (nunca bairro) |
| Divergência entre frontend e backend | Média | Médio | Contrato OpenAPI + testes de contrato + versionamento |

---

## 7. Processo de Execução (Obrigatório)

Toda fase seguirá este fluxo (sem exceções):

1. **Planner** (este documento) → Aprovação do usuário
2. **tdd-guide** → Escreve testes primeiro (RED)
3. Implementação mínima para passar nos testes (GREEN)
4. **code-reviewer** + **security-reviewer** (ou `python-reviewer` / `typescript-reviewer`)
5. Refatoração + melhoria (apenas se cobertura e reviews passarem)
6. Gate de aprovação explícita do usuário antes da próxima fase

**Nunca pular testes ou reviews.**

---

## 8. Critérios de Aprovação do Plano

Para considerar este plano aprovado e autorizar início da Fase 0:

- [ ] Usuário confirma que a definição de "completo" + restrição operacional está correta
- [ ] Usuário aprova a arquitetura híbrida progressiva (Fase 1 ainda majoritariamente Python)
- [ ] Usuário confirma que drill-down + comparação temporal é a prioridade #1
- [ ] Usuário aceita o processo de agentes + TDD obrigatório
- [ ] Usuário aprova o escopo de Fase 1 (sem mapas ainda)

---

## 9. Próximos Passos Imediatos (após aprovação)

1. Usuário responde: **"Aprovado. Inicie Fase 0"** (ou pede ajustes)
2. Criação de branch `feat/biro-evolucao-fase-0`
3. Execução do fluxo TDD-guide → implementação → reviews
4. Entrega de PR com relatório de cobertura e evidências

---

**Documento gerado seguindo rigorosamente as regras do projeto (AGENTS.md + Claude.md).**

Nenhum código de produção foi escrito. Este é um plano para revisão e aprovação.

---

*Assinado pelo processo de planejamento — pronto para decisão do usuário.*