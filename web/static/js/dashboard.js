/**
 * Dashboard operacional — Aldeia Viva Saúde
 *
 * Removidos nesta reconstrução, com o motivo:
 *   - Chart.js via CDN (~200 KB) para desenhar uma linha de DOIS pontos.
 *     Substituído pela tira de agravos em SVG, gerada no servidor, 0 KB.
 *   - loadChartJs(): a Promise nunca resolvia quando o script já estava em
 *     carregamento (`if (chartJsLoaded) return;` saía sem chamar resolve),
 *     travando o await para sempre ao abrir dois municípios em sequência.
 *   - Comparação automática com o ano anterior: disparava um fetch que, no
 *     servidor, fazia download síncrono de um relatório inteiro dentro de um
 *     `async def`, bloqueando o event loop a cada clique numa linha. E
 *     comparava anos que não são comparáveis: a carga mistura fontes de
 *     2022 a 2026.
 *   - Lista lateral de alertas: mesmo dado da tabela em outro corte.
 *   - Estilos inline com cores fora dos tokens do design system.
 */

const form = document.getElementById('consulta');
const rows = document.getElementById('risk-rows');
const statusLine = document.getElementById('dashboard-status');
const submitButton = form.querySelector('button[type="submit"]');
const ufInput = document.getElementById('estado');
const detailPanel = document.getElementById('municipio-detail-panel');

const fmt = new Intl.NumberFormat('pt-BR');
const levelClass = (value) =>
  String(value || 'baixo').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

// Mesmas cores de presentation/signal.py. Duplicação consciente e mínima:
// a tira precisa existir server-side (sem JS) e client-side (após filtro).
const SOURCE_COLORS = {
  atual: '#146c43',
  recente: '#9a5b00',
  antiga: '#8a6d3b',
  ausente: '#b8c4bd',
};
const SOURCE_LABELS = {
  atual: 'fonte do ano corrente',
  recente: 'fonte do ano anterior',
  antiga: 'fonte de 2 anos ou mais',
  ausente: 'sem fonte',
};
/**
 * Municípios atualmente em memória.
 *
 * A primeira tabela vem renderizada pelo servidor, então nada dela existe em
 * JS até a primeira consulta. Ligar os eventos dentro de renderRows deixava
 * essas linhas anunciando role="button" e foco sem responder a nada — uma
 * promessa de interação falsa, pior para quem navega por teclado do que não
 * anunciar coisa alguma. A delegação no fim do arquivo cobre os dois casos,
 * buscando o município sob demanda quando ele não está em memória.
 */
let cachedItems = [];

const STRIP_WIDTH = 168;
const STRIP_HEIGHT = 22;
const STRIP_GAP = 2;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function badge(value) {
  const level = levelClass(value);
  const label = { critico: 'Crítico', alto: 'Alto', moderado: 'Moderado', baixo: 'Baixo' }[level]
    || value || 'Baixo';
  const marker = { critico: '●', alto: '▲', moderado: '◆', baixo: '●' }[level] || '●';
  return `<span class="badge ${level}">${marker} ${escapeHtml(label)}</span>`;
}

/**
 * Célula de risco: o nível acionável, com o histórico dito quando pior.
 *
 * O badge mostra `nivel_risco_fonte_atual` — o pior nível entre os agravos
 * de fonte do ano corrente. Usar o nível consolidado de todos os anos-fonte
 * deixava 1.296 dos 5.339 municípios em "crítico"; restringir leva a 468.
 * Os que saem estavam lá por Meningite de 2022 ou Leptospirose de 2024.
 * Nada some: quando o histórico é pior, a célula diz qual era.
 */
/**
 * Coluna numérica: incidência primeiro, contagem absoluta abaixo.
 *
 * `risk_score` e a contagem bruta correlacionam 0,82 com a população. São
 * Paulo encabeçava o painel com 86 por 100 mil enquanto Sete Quedas/MS, com
 * 6.612 por 100 mil, não aparecia. A taxa é a única medida comparável entre
 * municípios de portes diferentes.
 */
function incidenceCell(item) {
  const inc = item.incidencia || {};
  const rate = inc.por_100k;
  let html;
  if (rate == null) {
    html = '<strong class="cell-rate is-unreliable">—</strong>'
      + '<span class="cell-sub">sem denominador</span>';
  } else {
    const cls = inc.confiavel ? 'cell-rate' : 'cell-rate is-unreliable';
    const title = inc.confiavel ? '' : ` title="${escapeHtml(inc.ressalva || '')}"`;
    html = `<strong class="${cls}"${title}>${fmt.format(Math.round(rate))}</strong>`
      + '<span class="cell-sub">por 100 mil hab.</span>';
  }
  html += `<span class="cell-sub">${fmt.format(item.total_casos_provaveis || 0)} casos</span>`;
  const deaths = Number(item.total_obitos || 0);
  if (deaths) {
    html += `<span class="cell-sub cell-deaths">${fmt.format(deaths)} óbito(s)</span>`;
  }
  return html;
}

function riskCell(item) {
  const level = item.nivel_risco_fonte_atual || item.nivel_risco;
  let html = badge(level);
  if (item.historico_mais_grave) {
    html += `<span class="cell-sub risk-history"
      title="Nível considerando também agravos de fontes de anos anteriores"
      >histórico: ${escapeHtml(item.nivel_risco)}</span>`;
  }
  return html + `<span class="cell-sub">${signalTag(item.recencia)}</span>`;
}

function signalTag(recencia) {
  const level = (recencia && recencia.frescor) || 'desconhecido';
  const label = (recencia && recencia.rotulo) || 'sem data';
  return `<span class="signal-tag ${escapeHtml(level)}">${escapeHtml(label)}</span>`;
}

function sourceBucket(fonte, currentYear) {
  if (!fonte || fonte.ano == null) return 'ausente';
  if (fonte.do_ano_corrente) return 'atual';
  return currentYear - Number(fonte.ano) <= 1 ? 'recente' : 'antiga';
}

function sourceTag(fonte) {
  if (!fonte || fonte.ano == null) return '<span class="source-tag is-old">sem fonte</span>';
  const old = fonte.do_ano_corrente ? '' : ' is-old';
  const title = `Arquivo-fonte deste agravo: ${fonte.ano} (${fonte.rotulo || ''})`.trim();
  return `<span class="source-tag${old}" title="${escapeHtml(title)}">fonte ${escapeHtml(fonte.ano)}</span>`;
}

/** Ordena por fonte mais atual primeiro, para a tira ser comparável entre linhas. */
function sortForStrip(diseases) {
  return [...diseases].sort((a, b) => {
    const ya = (a.fonte && a.fonte.ano) || 0;
    const yb = (b.fonte && b.fonte.ano) || 0;
    if (ya !== yb) return yb - ya;
    return String(a.nome || '').localeCompare(String(b.nome || ''), 'pt-BR');
  });
}

function signalStrip(diseases, currentYear) {
  const items = sortForStrip(diseases || []);
  if (items.length === 0) {
    return '<span class="strip-caption">Sem agravos registrados</span>';
  }
  const segment = Math.max(3, (STRIP_WIDTH - STRIP_GAP * (items.length - 1)) / items.length);
  let current = 0;
  const rects = items.map((disease, index) => {
    const bucket = sourceBucket(disease.fonte, currentYear);
    if (bucket === 'atual') current += 1;
    const year = disease.fonte && disease.fonte.ano != null ? disease.fonte.ano : 'ausente';
    const title = `${disease.nome || disease.codigo || 'Agravo'} — fonte ${year} (${SOURCE_LABELS[bucket]})`;
    const x = (index * (segment + STRIP_GAP)).toFixed(1);
    return `<rect x="${x}" y="0" width="${segment.toFixed(1)}" height="${STRIP_HEIGHT}" rx="2" `
      + `fill="${SOURCE_COLORS[bucket]}"><title>${escapeHtml(title)}</title></rect>`;
  }).join('');
  const label = `${current} de ${items.length} agravos com fonte do ano corrente`;
  return `<svg class="signal-strip" viewBox="0 0 ${STRIP_WIDTH} ${STRIP_HEIGHT}" `
    + `preserveAspectRatio="none" role="img" aria-label="${escapeHtml(label)}">${rects}</svg>`
    + `<span class="strip-caption">${current} de ${items.length} atuais</span>`;
}

function currentYear() {
  return new Date().getFullYear();
}

function renderRows(items) {
  cachedItems = Array.isArray(items) ? items : [];
  if (cachedItems.length === 0) {
    rows.innerHTML = '<tr><td class="empty-cell" colspan="4"><strong>Nenhum município encontrado.</strong>'
      + 'Remova a UF, amplie o nível mínimo ou busque pelo código IBGE.</td></tr>';
    return;
  }
  const year = currentYear();
  rows.innerHTML = items.map((item) => {
    const filtro = item.filtro_localidade;
    const sub = filtro && (filtro.tipo === 'distrito' || filtro.tipo === 'bairro')
      ? `${escapeHtml(filtro.localidade)} &rarr; ${escapeHtml(item.estado)} &middot; ${escapeHtml(item.codigo_municipio)}`
      : `${escapeHtml(item.estado)} &middot; ${escapeHtml(item.codigo_municipio)}`;

    const altas = (item.doencas_altas || []).map((d) => d.nome || d.doenca);
    const resumo = altas.length
      ? escapeHtml(altas.slice(0, 2).join(', ')) + (altas.length > 2 ? ` +${altas.length - 2}` : '')
      : 'sem agravo em nível alto';

    return `
      <tr class="municipality-row" tabindex="0" role="button"
          data-codigo="${escapeHtml(item.codigo_municipio)}"
          aria-label="Abrir visão completa de ${escapeHtml(item.municipio)}">
        <td data-label="Município"><strong>${escapeHtml(item.municipio)}</strong>
          <span class="cell-sub">${sub}</span>
          <span class="cell-sub">${resumo}</span></td>
        <td data-label="Risco">${riskCell(item)}</td>
        <td data-label="Incidência" class="num">${incidenceCell(item)}</td>
        <td data-label="Agravos">${signalStrip(item.doencas, year)}</td>
      </tr>`;
  }).join('');
}

async function openMunicipality(codigo) {
  const cached = cachedItems.find((item) => item.codigo_municipio === codigo);
  if (cached) {
    showMunicipioDetail(cached);
    return;
  }
  statusLine.textContent = 'Carregando município...';
  try {
    const response = await fetch(
      `/v1/risk-index?municipio=${encodeURIComponent(codigo)}&limite=1`
    );
    const payload = await response.json();
    const item = Array.isArray(payload)
      ? payload.find((i) => i.codigo_municipio === codigo) || payload[0]
      : null;
    if (item) {
      showMunicipioDetail(item);
      statusLine.textContent = `${item.municipio} / ${item.estado}`;
    } else {
      statusLine.textContent = 'Não foi possível carregar este município.';
    }
  } catch (error) {
    statusLine.textContent = 'Não foi possível carregar este município.';
  }
}

document.addEventListener('click', (event) => {
  const row = event.target.closest('.municipality-row');
  if (row) openMunicipality(row.dataset.codigo);
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest && event.target.closest('.municipality-row');
  if (!row) return;
  event.preventDefault();
  openMunicipality(row.dataset.codigo);
});

function showMunicipioDetail(item) {
  if (!detailPanel || !item) return;

  document.getElementById('detail-municipio-title').textContent =
    `${item.municipio} / ${item.estado}`;

  const rec = item.recencia || {};
  const vivos = item.agravos_com_sinal_vivo;
  const atuais = item.agravos_com_fonte_atual;
  const total = item.agravos_total;
  const cobertura = total != null
    ? ` · ${atuais} de ${total} agravos com fonte do ano corrente`
    : '';
  const historico = item.historico_mais_grave
    ? ` · histórico: ${item.nivel_risco}`
    : '';
  document.getElementById('detail-municipio-subtitle').textContent =
    `Código ${item.codigo_municipio}${cobertura}${historico}`;

  document.getElementById('detail-summary').innerHTML = `
    <div class="stat-item"><span class="stat-label">Casos prováveis</span>
      <strong class="stat-value">${fmt.format(item.total_casos_provaveis || 0)}</strong></div>
    <div class="stat-item"><span class="stat-label">Óbitos</span>
      <strong class="stat-value is-critical">${fmt.format(item.total_obitos || 0)}</strong></div>
    <div class="stat-item"><span class="stat-label">Hospitalizações</span>
      <strong class="stat-value">${fmt.format(item.total_hospitalizacoes || 0)}</strong></div>
    <div class="stat-item"><span class="stat-label">Sinal mais recente</span>
      <span class="stat-value">${signalTag(rec)}</span></div>
    ${vivos != null ? `<div class="stat-item"><span class="stat-label">Agravos com sinal vivo</span>
      <strong class="stat-value">${vivos} de ${total}</strong></div>` : ''}
  `;

  const tbody = document.getElementById('detail-diseases-body');
  const doencas = sortForStrip(item.doencas || []);
  tbody.innerHTML = doencas.map((d) => `
    <tr>
      <td><strong>${escapeHtml(d.nome)}</strong>
        <span class="cell-sub">${escapeHtml(d.virus || '')}</span></td>
      <td>${sourceTag(d.fonte)}</td>
      <td>${signalTag(d.recencia)}</td>
      <td class="num">${fmt.format(d.casos_provaveis || 0)}</td>
      <td class="num">${fmt.format(d.casos_graves || 0)}</td>
      <td class="num">${fmt.format(d.obitos || 0)}</td>
      <td>${badge(d.nivel_risco)}</td>
    </tr>`).join('')
    || '<tr><td class="empty-cell" colspan="7">Sem agravos registrados para este município.</td></tr>';

  detailPanel.hidden = false;
  detailPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeDetailPanel() {
  if (detailPanel) detailPanel.hidden = true;
}

document.addEventListener('click', (event) => {
  if (event.target.closest('#close-detail')) {
    closeDetailPanel();
    return;
  }
  if (!detailPanel || detailPanel.hidden) return;
  if (!detailPanel.contains(event.target) && !event.target.closest('.municipality-row')) {
    closeDetailPanel();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && detailPanel && !detailPanel.hidden) closeDetailPanel();
});

function setLoading(isLoading) {
  submitButton.disabled = isLoading;
  submitButton.textContent = isLoading ? 'Consultando...' : 'Consultar';
  form.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}

function renderSkeleton() {
  rows.innerHTML = Array.from({ length: 4 }, () => `
    <tr aria-hidden="true">
      <td class="empty-cell" colspan="4"><span class="skeleton-line"></span>
        <span class="skeleton-line short"></span></td>
    </tr>`).join('');
}

async function loadDashboard(event) {
  if (event) event.preventDefault();
  ufInput.value = ufInput.value.toUpperCase().trim();

  const params = new URLSearchParams();
  for (const [key, value] of new FormData(form).entries()) {
    if (value) params.set(key, String(value).trim());
  }
  if (!params.get('ordenar')) params.set('ordenar', 'taxa');
  params.set('limite', '25');

  statusLine.textContent = 'Consultando...';
  setLoading(true);
  renderSkeleton();
  try {
    const response = await fetch(`/v1/risk-index?${params.toString()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const items = Array.isArray(payload) ? payload : [];
    renderRows(items);
    closeDetailPanel();

    // Honestidade sobre o corte: o servidor rebaixa o limite por tier, e o
    // painel dizia "N município(s)" sem revelar que N era um teto, não um total.
    const total = Number(response.headers.get('X-Total-Results'));
    const limited = response.headers.get('X-Limit-Applied');
    if (Number.isFinite(total) && total > items.length) {
      statusLine.textContent = `${items.length} de ${fmt.format(total)} município(s)`
        + (limited ? ` — limite de ${limited} por consulta.` : '.');
    } else if (items.length) {
      statusLine.textContent = `${items.length} município(s).`;
    } else {
      statusLine.textContent = payload && payload.message
        ? payload.message
        : 'Nenhum município para este filtro.';
    }
  } catch (error) {
    renderRows([]);
    statusLine.textContent = 'Não foi possível consultar agora. Tente novamente em instantes.';
  } finally {
    setLoading(false);
  }
}

ufInput.addEventListener('input', () => { ufInput.value = ufInput.value.toUpperCase(); });
form.addEventListener('submit', loadDashboard);
form.addEventListener('reset', () => {
  window.setTimeout(() => {
    document.getElementById('municipio').value = '';
    ufInput.value = '';
    document.getElementById('nivel_minimo').value = '';
    loadDashboard();
  });
});
