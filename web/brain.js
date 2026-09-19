const title = document.getElementById('side-title');
const typeEl = document.getElementById('side-type');
const facts = document.getElementById('side-facts');
const links = document.getElementById('side-links');
const legend = document.getElementById('legend');
const demoBtn = document.getElementById('demo');
const dailyMeta = document.getElementById('daily-meta');
const historyMeta = document.getElementById('history-meta');

let activePayload = { nodes: [], edges: [] };

function setFacts(pairs) {
  facts.innerHTML = '';
  for (const [key, value] of pairs) {
    if (value == null || value === '') continue;
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = Array.isArray(value) ? value.join(', ') : String(value);
    facts.append(dt, dd);
  }
}

function showNode(node, data) {
  if (!node) {
    title.textContent = 'No memory yet';
    typeEl.textContent = 'Load the sample week';
    return;
  }
  activePayload = data || activePayload;
  title.textContent = node.label;
  typeEl.textContent = node.group;
  setFacts([
    ['type', node.group],
    ['weight', Number(node.weight || 0).toFixed(2)],
    ['last seen', node.last_seen || ''],
    ...Object.entries(node.props || {}),
  ]);
  const neighbors = (activePayload.edges || [])
    .filter((edge) => edge.from === node.id || edge.to === node.id)
    .map((edge) => {
      const otherId = edge.from === node.id ? edge.to : edge.from;
      const other = (activePayload.nodes || []).find((item) => item.id === otherId);
      return `${(edge.relation || '').replaceAll('_', ' ')} → ${other ? other.label : otherId}`;
    });
  links.innerHTML = neighbors.length
    ? neighbors.map((line) => `<span class="chip">${line}</span>`).join('')
    : '<span class="meta">No links yet</span>';
}

function drawLegend(items) {
  legend.innerHTML = (items || []).map(
    (item) => `<span><i style="background:${item.color}"></i>${item.type}</span>`
  ).join('');
}

const dailyView = new MemoryGraphView(document.getElementById('daily-canvas'), showNode);
const historyView = new MemoryGraphView(document.getElementById('history-canvas'), showNode);
const views = { daily: dailyView, history: historyView };

document.querySelectorAll('.graph-tools button').forEach((button) => {
  button.addEventListener('click', () => {
    const view = views[button.dataset.target];
    if (!view) return;
    view.rotateBy((Number(button.dataset.deg) || 20) * Math.PI / 180);
  });
});

async function fetchGraph(scope) {
  const response = await fetch(`/api/memory-graph?scope=${scope}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`graph ${scope} ${response.status}`);
  return response.json();
}

function apply(scope, payload) {
  const stats = payload.stats || {};
  const label = `${stats.nodes || 0} nodes · ${stats.edges || 0} links`;
  if (scope === 'daily') {
    dailyMeta.textContent = label;
    dailyView.render(payload);
  } else {
    historyMeta.textContent = label;
    historyView.render(payload);
    drawLegend(payload.legend || []);
  }
}

async function loadBoth() {
  const [daily, history] = await Promise.all([
    fetchGraph('daily'),
    fetchGraph('history'),
  ]);
  apply('daily', daily);
  apply('history', history);
}

demoBtn.addEventListener('click', async () => {
  demoBtn.disabled = true;
  demoBtn.textContent = 'Seeding…';
  try {
    await fetch('/api/memory-graph/demo', { method: 'POST' });
    await loadBoth();
  } finally {
    demoBtn.disabled = false;
    demoBtn.textContent = 'Load sample week';
  }
});

loadBoth().catch((error) => {
  title.textContent = 'Graph failed';
  typeEl.textContent = String(error);
});
