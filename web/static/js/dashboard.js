const form = document.getElementById('consulta');
const rows = document.getElementById('risk-rows');
const alerts = document.getElementById('alert-list');
const statusLine = document.getElementById('dashboard-status');
const submitButton = form.querySelector('button[type="submit"]');
const ufInput = document.getElementById('estado');
const levelClass = (value) => String(value || 'baixo').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const fmt = new Intl.NumberFormat('pt-BR');

let chartJsLoaded = false;

function loadChartJs() {
  return new Promise((resolve) => {
    if (window.Chart) {
      resolve();
      return;
    }
    if (chartJsLoaded) return;
    chartJsLoaded = true;

    const script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js';
    script.onload = () => resolve();
    script.onerror = () => resolve(); // fail gracefully
    document.head.appendChild(script);
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function badge(value) {
  const level = levelClass(value);
  const label = { critico: 'Crítico', alto: 'Alto', moderado: 'Moderado', baixo: 'Baixo' }[level] || value || 'Baixo';
  const marker = { critico: '●', alto: '▲', moderado: '◆', baixo: '●' }[level] || '●';
  return `<span class="badge ${level}">${marker} ${escapeHtml(label)}</span>`;
}

function diseaseNames(items) {
  if (!items || items.length === 0) return 'Sem alerta alto';
  return items.map((item) => `${escapeHtml(item.nome || item.doenca)} (${escapeHtml(item.nivel_risco || 'alto')})`).join(', ');
}

function renderRows(items) {
  if (!Array.isArray(items) || items.length === 0) {
    rows.innerHTML = '<tr><td class="empty-cell" colspan="5"><strong>Nenhum município encontrado.</strong>Tente remover a UF, consultar todos os níveis ou buscar pelo código municipal.</td></tr>';
    return;
  }
  rows.innerHTML = items.map((item) => {
    let municipioHtml = `<strong>${escapeHtml(item.municipio)}</strong><br><span class="status-line">${escapeHtml(item.estado)} · ${escapeHtml(item.codigo_municipio)}</span>`;

    const filtro = item.filtro_localidade;
    if (filtro && filtro.tipo === "distrito") {
      municipioHtml = `<strong>${escapeHtml(item.municipio)}</strong><br><span class="status-line">Busca por: ${escapeHtml(filtro.localidade)} → ${escapeHtml(item.estado)} · ${escapeHtml(item.codigo_municipio)}</span>`;
    }

    return `
      <tr class="municipality-row" data-codigo="${escapeHtml(item.codigo_municipio)}" style="cursor: pointer;">
        <td data-label="Município">${municipioHtml}</td>
        <td data-label="Risco">${badge(item.nivel_risco)}<br><span class="status-line">score ${fmt.format(item.risk_score || 0)}</span></td>
        <td data-label="Casos">${fmt.format(item.total_casos_provaveis || 0)}</td>
        <td data-label="Óbitos">${fmt.format(item.total_obitos || 0)}</td>
        <td data-label="Doenças altas">${diseaseNames(item.doencas_altas)}</td>
      </tr>
    `;
  }).join('');

  // Make rows clickable for complete visibility per municipality
  document.querySelectorAll('.municipality-row').forEach(row => {
    row.addEventListener('click', () => {
      const codigo = row.dataset.codigo;
      const fullItem = items.find(i => i.codigo_municipio === codigo);
      if (fullItem) showMunicipioDetail(fullItem);
    });
  });
}

function renderAlerts(items) {
  if (!Array.isArray(items) || items.length === 0) {
    alerts.innerHTML = '<p class="status-line">Nenhum alerta alto encontrado para este filtro. Tente ampliar a consulta ou remover filtros.</p>';
    return;
  }
  alerts.innerHTML = items.map((item) => {
    const level = levelClass(item.nivel_risco);
    return `
      <article class="alert-item ${level}">
        <header><strong>${escapeHtml(item.municipio)}/${escapeHtml(item.estado)}</strong>${badge(item.nivel_risco)}</header>
        <p><strong>${escapeHtml(item.doenca)}</strong> · ${escapeHtml(item.virus)} · ${fmt.format(item.casos_provaveis || 0)} casos prováveis</p>
        <p>Graves ${fmt.format(item.casos_graves || 0)} · Óbitos ${fmt.format(item.obitos || 0)} · Score ${fmt.format(item.risk_score || 0)}</p>
      </article>
    `;
  }).join('');
}

function setLoading(isLoading) {
  submitButton.disabled = isLoading;
  submitButton.textContent = isLoading ? 'Consultando...' : 'Atualizar';
  form.setAttribute('aria-busy', isLoading ? 'true' : 'false');
  rows.closest('.table-wrap')?.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}

function updateQuickFilterState(level) {
  document.querySelectorAll('[data-quick-level]').forEach((button) => {
    button.setAttribute('aria-pressed', (button.dataset.quickLevel || '') === (level || '') ? 'true' : 'false');
  });
}

// Mostrar visão completa por município + carregar ano anterior automaticamente (decisão de alto valor)
async function showMunicipioDetail(item) {
  const panel = document.getElementById('municipio-detail-panel');
  if (!panel || !item) return;

  const currentYear = item.periodo?.ano || new Date().getFullYear();
  const previousYear = currentYear - 1;

  document.getElementById('detail-municipio-title').textContent = `${item.municipio} / ${item.estado}`;
  document.getElementById('detail-municipio-subtitle').textContent = `Código ${item.codigo_municipio} • Ano atual: ${currentYear} (com comparação automática para ${previousYear})`;

  // Resumo visual mais polido
  const summaryHtml = `
    <div class="stat-item">
      <span class="stat-label">Casos Prováveis</span>
      <strong style="font-size:1.35rem; display:block; margin-top:2px;">${fmt.format(item.total_casos_provaveis || 0)}</strong>
    </div>
    <div class="stat-item">
      <span class="stat-label">Óbitos Totais</span>
      <strong style="font-size:1.35rem; display:block; margin-top:2px; color:#b91c1c;">${fmt.format(item.total_obitos || 0)}</strong>
    </div>
    <div class="stat-item">
      <span class="stat-label">Hospitalizações</span>
      <strong style="font-size:1.35rem; display:block; margin-top:2px;">${fmt.format(item.total_hospitalizacoes || 0)}</strong>
    </div>
    <div class="stat-item">
      <span class="stat-label">Nível de Risco</span>
      <div style="margin-top:4px;">${badge(item.nivel_risco)}</div>
    </div>
  `;
  document.getElementById('detail-summary').innerHTML = summaryHtml;

  // Tabela de todas as doenças
  const tbody = document.getElementById('detail-diseases-body');
  const doencas = item.doencas || [];
  tbody.innerHTML = doencas.map(d => `
    <tr>
      <td>
        <div style="font-weight:600; color:#111827;">${escapeHtml(d.nome)}</div>
        <div style="font-size:0.78rem; color:#6b7280; margin-top:1px;">${escapeHtml(d.virus || '')}</div>
      </td>
      <td class="num">${fmt.format(d.casos_provaveis || 0)}</td>
      <td class="num">${fmt.format(d.sinais_alarme || 0)}</td>
      <td class="num">${fmt.format(d.casos_graves || 0)}</td>
      <td class="num">${fmt.format(d.hospitalizacoes || 0)}</td>
      <td class="num" style="font-weight:600;">${fmt.format(d.obitos || 0)}</td>
      <td class="num" style="font-weight:600;">${fmt.format(d.risk_score || 0)}</td>
      <td>${badge(d.nivel_risco)}</td>
    </tr>
  `).join('');

  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // === Decisão não recomendada de alto valor: carregar ano anterior automaticamente ===
  document.getElementById('detail-comparison').innerHTML = `
    <div style="padding: 14px; background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0; font-size: 0.9rem; color: #64748b;">
      Carregando automaticamente os dados de <strong>${previousYear}</strong> para comparação ano a ano...
    </div>
  `;

  try {
    const prevParams = new URLSearchParams({
      municipio: item.codigo_municipio,
      ano: previousYear,
      limite: '30'
    });

    const prevRes = await fetch(`/v1/risk-index?${prevParams.toString()}`);
    if (prevRes.ok) {
      const prevData = await prevRes.json();
      const prevItem = Array.isArray(prevData) ? prevData.find(m => m.codigo_municipio === item.codigo_municipio) : null;

      if (prevItem) {
        renderSimpleYearComparison(item, prevItem, currentYear, previousYear);
      } else {
        document.getElementById('detail-comparison').innerHTML = 
          `<p class="detail-placeholder">Não foram encontrados dados para ${previousYear} neste município.</p>`;
      }
    }
  } catch (e) {
    document.getElementById('detail-comparison').innerHTML = 
      `<p class="detail-placeholder">Não foi possível carregar os dados de ${previousYear}.</p>`;
  }
}

function renderSimpleYearComparison(current, previous, currentYear, previousYear) {
  const container = document.getElementById('detail-comparison');
  if (!container) return;

  const currScore = current.risk_score || 0;
  const prevScore = previous.risk_score || 0;
  const scoreDiff = currScore - prevScore;
  const scorePct = prevScore > 0 ? ((scoreDiff / prevScore) * 100) : 0;

  const currObitos = current.total_obitos || 0;
  const prevObitos = previous.total_obitos || 0;
  const obitosDiff = currObitos - prevObitos;

  const currCasos = current.total_casos_provaveis || 0;
  const prevCasos = previous.total_casos_provaveis || 0;
  const casosDiff = currCasos - prevCasos;
  const casosPct = prevCasos > 0 ? ((casosDiff / prevCasos) * 100) : 0;

  const getColor = (diff) => diff > 0 ? '#b91c1c' : (diff < 0 ? '#15803d' : '#64748b');
  const getArrow = (diff) => diff > 0 ? '▲' : (diff < 0 ? '▼' : '→');
  const getVerb = (diff) => diff > 0 ? 'piorou' : (diff < 0 ? 'melhorou' : 'manteve-se estável');

  const scoreColor = getColor(scoreDiff);
  const obitosColor = getColor(obitosDiff);
  const casosColor = getColor(casosDiff);

  container.innerHTML = `
    <div style="margin-bottom: 12px; font-size: 0.9rem; color: #475569;">
      O risco <strong style="color: ${scoreColor};">${getVerb(scoreDiff)}</strong> 
      ${scoreDiff !== 0 ? `em <strong>${Math.abs(scorePct).toFixed(1)}%</strong>` : ''} 
      em relação a ${previousYear}.
    </div>

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
      <!-- Ano Anterior -->
      <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px;">
        <div style="font-size: 0.7rem; color: #64748b; margin-bottom: 4px;">${previousYear}</div>
        <div style="font-size: 1.1rem; font-weight: 700; color: #334155;">Score: ${fmt.format(prevScore)}</div>
        <div style="font-size: 0.85rem; color: #64748b; margin-top: 4px;">
          ${fmt.format(prevCasos)} casos • ${fmt.format(prevObitos)} óbitos
        </div>
      </div>

      <!-- Ano Atual -->
      <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px;">
        <div style="font-size: 0.7rem; color: #64748b; margin-bottom: 4px;">${currentYear}</div>
        <div style="font-size: 1.1rem; font-weight: 700; color: ${scoreColor};">
          Score: ${fmt.format(currScore)} 
          <span style="font-size: 0.9rem;">${getArrow(scoreDiff)}</span>
        </div>
        <div style="font-size: 0.85rem; color: #64748b; margin-top: 4px;">
          ${fmt.format(currCasos)} casos 
          <span style="color: ${casosColor};">(${getArrow(casosDiff)} ${Math.abs(casosPct).toFixed(0)}%)</span> 
          • ${fmt.format(currObitos)} óbitos 
          <span style="color: ${obitosColor};">(${getArrow(obitosDiff)})</span>
        </div>
      </div>
    </div>

    <div style="margin-top: 10px; font-size: 0.8rem; color: #64748b;">
      Diferença no score: <strong style="color: ${scoreColor};">${scoreDiff > 0 ? '+' : ''}${scoreDiff.toFixed(1)}</strong>
    </div>
  `;

  // Render the evolution chart (first React-island style component)
  renderEvolutionChart(currentYear, previousYear, currScore, prevScore);
}

// Fechar painel de detalhe
function closeDetailPanel() {
  const panel = document.getElementById('municipio-detail-panel');
  if (panel) panel.style.display = 'none';
}

document.addEventListener('click', function(e) {
  if (e.target.id === 'close-detail') {
    closeDetailPanel();
  }
});

// Fechar com ESC (melhor UX)
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const panel = document.getElementById('municipio-detail-panel');
    if (panel && panel.style.display !== 'none') {
      closeDetailPanel();
    }
  }
});

// Fechar ao clicar fora do painel (melhor UX)
document.addEventListener('click', function(e) {
  const panel = document.getElementById('municipio-detail-panel');
  if (!panel || panel.style.display === 'none') return;

  // Fecha se clicar fora do painel e não for em uma linha da tabela
  if (!panel.contains(e.target) && !e.target.closest('.municipality-row')) {
    closeDetailPanel();
  }
});

function renderSkeleton() {
  rows.innerHTML = Array.from({ length: 4 }, () => `
    <tr aria-hidden="true">
      <td class="empty-cell" colspan="5"><span class="skeleton-line"></span><span class="skeleton-line short" style="margin-top: 10px;"></span></td>
    </tr>
  `).join('');
  alerts.innerHTML = '<article class="alert-item" aria-hidden="true"><span class="skeleton-line"></span><span class="skeleton-line short" style="margin-top: 10px;"></span></article>';
}

async function loadDashboard(event) {
  if (event) event.preventDefault();
  ufInput.value = ufInput.value.toUpperCase().trim();
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const [key, value] of data.entries()) {
    if (value && key !== 'somente_altos') params.set(key, String(value).trim());
  }
  params.set('somente_altos', document.getElementById('somente_altos').checked ? 'true' : 'false');
  params.set('limite', '25');

  statusLine.textContent = 'Atualizando dados...';
  setLoading(true);
  renderSkeleton();
  try {
    const riskResponse = await fetch(`/v1/risk-index?${params.toString()}`);
    const alertParams = new URLSearchParams();
    if (params.get('municipio')) alertParams.set('municipio', params.get('municipio'));
    if (params.get('estado')) alertParams.set('estado', params.get('estado'));
    alertParams.set('limite', '10');
    const alertResponse = await fetch(`/v1/high-alerts?${alertParams.toString()}`);
    if (!riskResponse.ok || !alertResponse.ok) throw new Error('Falha na consulta');
    const riskPayload = await riskResponse.json();
    const alertPayload = await alertResponse.json();
    renderRows(Array.isArray(riskPayload) ? riskPayload : []);
    renderAlerts(alertPayload.alerts || []);
    updateQuickFilterState(params.get('nivel_minimo') || '');
    statusLine.textContent = Array.isArray(riskPayload) ? `${riskPayload.length} município(s) retornado(s).` : (riskPayload.message || 'Consulta concluída.');
  } catch (error) {
    renderRows([]);
    renderAlerts([]);
    statusLine.textContent = 'Não foi possível atualizar os dados agora. Tente novamente em alguns instantes.';
  } finally {
    setLoading(false);
  }
}

ufInput.addEventListener('input', () => { ufInput.value = ufInput.value.toUpperCase(); });
form.addEventListener('reset', () => {
  window.setTimeout(() => {
    document.getElementById('municipio').value = '';
    ufInput.value = '';
    document.getElementById('nivel_minimo').value = '';
    document.getElementById('somente_altos').checked = false;
    loadDashboard();
  });
});
document.querySelectorAll('[data-quick-level]').forEach((button) => {
  button.addEventListener('click', () => {
    document.getElementById('nivel_minimo').value = button.dataset.quickLevel || '';
    document.getElementById('somente_altos').checked = ['alto', 'critico'].includes(button.dataset.quickLevel || '');
    updateQuickFilterState(button.dataset.quickLevel || '');
    loadDashboard();
  });
});
form.addEventListener('submit', loadDashboard);


// === Primeiro React Island leve - Gráfico de Evolução (Chart.js) ===
async function renderEvolutionChart(currentYear, previousYear, currScore, prevScore) {
  const container = document.getElementById("evolution-chart-container");
  const canvas = document.getElementById("evolution-chart");
  if (!container || !canvas) return;

  await loadChartJs();

  if (!window.Chart) {
    container.innerHTML = `<p class="detail-placeholder">Gráfico indisponível (Chart.js não carregou).</p>`;
    return;
  }

  if (canvas.chartInstance) {
    canvas.chartInstance.destroy();
  }

  const ctx = canvas.getContext("2d");

  canvas.chartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels: [String(previousYear), String(currentYear)],
      datasets: [{
        label: "Risk Score",
        data: [prevScore, currScore],
        borderColor: "#067a76",
        backgroundColor: "rgba(6, 122, 118, 0.12)",
        borderWidth: 3,
        pointBackgroundColor: "#146c43",
        pointRadius: 5,
        tension: 0.25,
        fill: true
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      scales: {
        y: { beginAtZero: true, grid: { color: "#e2e8f0" }, ticks: { font: { size: 10 } } },
        x: { grid: { color: "#e2e8f0" }, ticks: { font: { size: 10 } } }
      }
    }
  });
}
