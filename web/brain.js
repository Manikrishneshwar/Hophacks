const title = document.getElementById('side-title');
const typeEl = document.getElementById('side-type');
const facts = document.getElementById('side-facts');
const links = document.getElementById('side-links');
const legend = document.getElementById('legend');
const demoBtn = document.getElementById('demo');
const dailyMeta = document.getElementById('daily-meta');
const historyMeta = document.getElementById('history-meta');
const dot = document.getElementById('dot');
const meta = document.getElementById('meta');
const sideImage = document.getElementById('side-image');

if (new URLSearchParams(location.search).get('embed')) {
  document.body.classList.add('embed');
}
const embed = document.body.classList.contains('embed');

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
  const linksKicker = document.getElementById('side-links-kicker');
  if (!node) {
    title.textContent = 'No memory yet';
    typeEl.textContent = 'Tap a node on the graph';
    if (sideImage) {
      sideImage.removeAttribute('src');
      sideImage.hidden = true;
    }
    facts.innerHTML = '';
    links.innerHTML = '';
    if (linksKicker) linksKicker.hidden = true;
    return;
  }
  activePayload = data || activePayload;
  title.textContent = node.label;
  typeEl.textContent = node.group;

  // Phone embed keeps this short: title, type, drawing thumb, linked chips.
  if (embed) {
    facts.innerHTML = '';
  } else {
    setFacts([
      ['type', node.group],
      ['weight', Number(node.weight || 0).toFixed(2)],
      ['last seen', node.last_seen || ''],
      ...Object.entries(node.props || {}).filter(([key]) => key !== 'image'),
    ]);
  }

  const neighbors = (activePayload.edges || [])
    .filter((edge) => edge.from === node.id || edge.to === node.id)
    .map((edge) => {
      const otherId = edge.from === node.id ? edge.to : edge.from;
      const other = (activePayload.nodes || []).find((item) => item.id === otherId);
      const label = other ? other.label : otherId;
      if (embed) return label;
      return `${(edge.relation || '').replaceAll('_', ' ')} → ${label}`;
    })
    .filter((label, index, all) => label && all.indexOf(label) === index)
    .slice(0, embed ? 6 : 50);

  if (linksKicker) linksKicker.hidden = neighbors.length === 0;
  links.innerHTML = neighbors.length
    ? neighbors.map((line) => `<span class="chip">${line}</span>`).join('')
    : (embed ? '' : '<span class="meta">No links yet</span>');

  const image = (node.props || {}).image
    || (node.group === 'drawing' && /T/.test(String(node.label || ''))
      ? `/captures/${node.label}.png`
      : '');
  if (sideImage) {
    if (image) {
      sideImage.hidden = false;
      sideImage.onerror = () => { sideImage.hidden = true; };
      sideImage.src = image;
    } else {
      sideImage.removeAttribute('src');
      sideImage.hidden = true;
    }
  }
}

function drawLegend(items) {
  legend.innerHTML = (items || []).map(
    (item) => `<span><i style="background:${item.color}"></i>${item.type}</span>`
  ).join('');
}

const dailyView = new MemoryGraphView(document.getElementById('daily-canvas'), showNode);
const historyView = new MemoryGraphView(document.getElementById('history-canvas'), showNode);
const views = { daily: dailyView, history: historyView };
const PAGE = 5;
let dailyFull = { nodes: [], edges: [] };
let dailyLimit = PAGE;
const loadMoreBtn = document.getElementById('load-more');

function recency(node) {
  return String((node && (node.last_seen || (node.props || {}).iso_date || node.label)) || '');
}

function primaryNodes(nodes) {
  const events = nodes.filter((node) => node.group === 'event');
  const drawings = nodes.filter((node) => node.group === 'drawing');
  const ranked = (events.length ? events : drawings)
    .slice()
    .sort((a, b) => recency(b).localeCompare(recency(a)));
  return ranked;
}

function slicePayload(payload, limit) {
  const nodes = payload.nodes || [];
  const edges = payload.edges || [];
  const ranked = primaryNodes(nodes);
  const shown = ranked.slice(0, limit);
  const keep = new Set(shown.map((node) => node.id));
  for (const node of nodes) {
    if (node.group === 'person') keep.add(node.id);
  }
  for (const edge of edges) {
    if (shown.some((node) => node.id === edge.from || node.id === edge.to)) {
      keep.add(edge.from);
      keep.add(edge.to);
    }
  }
  return {
    ...payload,
    nodes: nodes.filter((node) => keep.has(node.id)),
    edges: edges.filter((edge) => keep.has(edge.from) && keep.has(edge.to)),
    page: { shown: shown.length, total: ranked.length, limit },
  };
}

function updateLoadMore() {
  if (!loadMoreBtn) return;
  if (!embed) {
    loadMoreBtn.hidden = true;
    return;
  }
  const total = primaryNodes(dailyFull.nodes || []).length;
  if (dailyLimit >= total) {
    loadMoreBtn.hidden = true;
    return;
  }
  loadMoreBtn.hidden = false;
  loadMoreBtn.disabled = false;
  const left = total - dailyLimit;
  loadMoreBtn.textContent = `Show ${Math.min(PAGE, left)} older events`;
}

document.querySelectorAll('.graph-tools button').forEach((button) => {
  button.addEventListener('click', () => {
    const view = views[button.dataset.target];
    if (!view) return;
    if (button.dataset.act === 'in') view.zoomBy(1.25);
    else if (button.dataset.act === 'out') view.zoomBy(0.8);
    else if (button.dataset.act === 'fit') view.resetView();
    else view.rotateBy((Number(button.dataset.deg) || 20) * Math.PI / 180);
  });
});

if (loadMoreBtn) {
  loadMoreBtn.addEventListener('click', () => {
    dailyLimit += PAGE;
    // keepView: false so the new nodes get a fresh fit; limit must NOT be reset in apply.
    apply('daily', dailyFull, false, { preserveLimit: true });
  });
}

async function fetchGraph(scope) {
  const response = await fetch(`/api/memory-graph?scope=${scope}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`graph ${scope} ${response.status}`);
  return response.json();
}

function apply(scope, payload, keepView, options) {
  const source = payload || { nodes: [], edges: [] };
  let drawn = source;
  if (scope === 'daily') {
    dailyFull = source;
    // Only start back at the newest 5 on a real reload — never after "Load more".
    if (embed && !keepView && !(options && options.preserveLimit)) {
      dailyLimit = PAGE;
    }
    drawn = embed ? slicePayload(source, dailyLimit) : source;
    const page = drawn.page;
    const stats = source.stats || {};
    dailyMeta.textContent = page
      ? `${page.shown} of ${page.total} recent events`
      : `${stats.nodes || 0} nodes · ${stats.edges || 0} links`;
    dailyView.render(drawn, { keepView });
    updateLoadMore();
    return;
  }
  const stats = source.stats || {};
  historyMeta.textContent = `${stats.nodes || 0} nodes · ${stats.edges || 0} links`;
  historyView.render(source, { keepView });
  drawLegend(source.legend || []);
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

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws?role=viewer`);
  socket.addEventListener('open', () => {
    dot.className = 'dot on';
    meta.textContent = 'live · confirmed drawings appear here';
    loadBoth().catch(() => {});
  });
  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type !== 'graph') return;
    if (message.daily) apply('daily', message.daily, true);
    if (message.history) apply('history', message.history, true);
  });
  socket.addEventListener('close', () => {
    dot.className = 'dot off';
    meta.textContent = 'offline · graphs will refresh when you reload';
    setTimeout(connect, 1500);
  });
  socket.addEventListener('error', () => socket.close());
}

loadBoth()
  .then(connect)
  .catch((error) => {
    title.textContent = 'Graph failed';
    typeEl.textContent = String(error);
  });
