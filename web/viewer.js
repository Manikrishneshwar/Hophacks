/* Optional desktop view.
 *
 * Purely a listener: it mirrors strokes relayed from the tablet and appends
 * captures as they are saved. Closing this page changes nothing about the
 * capture pipeline.
 */

const live = document.getElementById('live');
const ctx = live.getContext('2d');
const dot = document.getElementById('dot');
const meta = document.getElementById('meta');
const emptyLabel = document.getElementById('live-empty');
const gallery = document.getElementById('gallery');
const qa = document.getElementById('qa');

const PEN_COLOR = '#111318';
let stroke = null;
let inked = false;

function clearLive() {
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, live.width, live.height);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  stroke = null;
  inked = false;
  emptyLabel.style.display = '';
}

function resizeLive(w, h) {
  if (live.width === w && live.height === h) return;
  live.width = w;
  live.height = h;
  clearLive();
}

function segment(from, to, color, base) {
  ctx.strokeStyle = color;
  ctx.lineWidth = base * (0.45 + 1.1 * ((from[2] + to[2]) / 2 || 0.5));
  ctx.beginPath();
  ctx.moveTo(from[0], from[1]);
  ctx.lineTo(to[0], to[1]);
  ctx.stroke();
  inked = true;
  emptyLabel.style.display = 'none';
}

function drawStroke(s) {
  const pts = s.points || [];
  for (let i = 1; i < pts.length; i++) segment(pts[i - 1], pts[i], s.color || PEN_COLOR, s.width || 2.6);
}

function handle(message) {
  switch (message.type) {
    case 'hello':
      resizeLive(message.w, message.h);
      meta.textContent = `tablet connected · ${message.w}×${message.h}`;
      break;

    case 'begin':
      stroke = { color: message.color || PEN_COLOR, width: message.width || 2.6, points: [message.pt] };
      break;

    case 'points':
      if (!stroke) stroke = { color: PEN_COLOR, width: 2.6, points: message.pts.slice(0, 1) };
      for (const pt of message.pts) {
        const prev = stroke.points[stroke.points.length - 1];
        stroke.points.push(pt);
        if (prev) segment(prev, pt, stroke.color, stroke.width);
      }
      break;

    case 'end':
      stroke = null;
      break;

    case 'replace':
      clearLive();
      for (const s of message.strokes || []) drawStroke(s);
      break;

    case 'clear':
      clearLive();
      break;

    case 'capture':
      addCapture(message.record, true);
      break;

    case 'ask':
      showQa(`${message.question || 'Confirm?'} — waiting for a tap`, 'waiting');
      break;

    case 'answer':
      showQa(`${message.question || 'Answered'} — ${message.value.toUpperCase()}`, message.value);
      break;

    case 'ask_cancel':
      showQa('Question timed out', 'no');
      break;
  }
}

function showQa(text, state) {
  qa.hidden = false;
  qa.textContent = text;
  qa.className = `qa ${state}`;
}

/* ---------------- gallery ---------------- */

function addCapture(record, prepend) {
  gallery.querySelector('.empty')?.remove();

  const figure = document.createElement('figure');
  const link = document.createElement('a');
  link.href = `/captures/${record.png}`;
  link.target = '_blank';

  const img = document.createElement('img');
  img.src = `/captures/${record.png}`;
  img.alt = record.id;
  link.appendChild(img);

  const caption = document.createElement('figcaption');
  const time = new Date(record.created_at);
  caption.textContent = `${time.toLocaleTimeString()} · ${record.stroke_count} strokes · ${record.trigger}`;

  figure.append(link, caption);
  if (prepend) gallery.prepend(figure);
  else gallery.append(figure);
}

async function loadHistory() {
  try {
    const { captures } = await (await fetch('/api/captures?limit=60')).json();
    for (const record of captures) addCapture(record, false);
  } catch { /* nothing stored yet */ }
}

/* ---------------- socket ---------------- */

let backoff = 500;

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws?role=viewer`);

  socket.addEventListener('open', () => {
    backoff = 500;
    dot.className = 'dot on';
    meta.textContent = 'connected · waiting for the tablet';
  });

  socket.addEventListener('message', (event) => {
    try { handle(JSON.parse(event.data)); } catch { /* ignore malformed frames */ }
  });

  socket.addEventListener('close', () => {
    dot.className = 'dot off';
    meta.textContent = 'server offline';
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  });

  socket.addEventListener('error', () => socket.close());
}

clearLive();
loadHistory();
connect();
