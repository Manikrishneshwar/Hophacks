/* Tablet drawing surface.
 *
 * Strokes are kept as vectors in CSS-pixel space and rendered to a
 * device-pixel-ratio scaled canvas. Keeping the vectors means undo, resize and
 * the final PNG export all redraw from the same source of truth.
 *
 * After IDLE_MS with no pen contact the canvas is exported, uploaded and wiped.
 */

const board = document.getElementById('board');
const ctx = board.getContext('2d', { willReadFrequently: false });
const statusEl = document.getElementById('status');
const dotEl = document.getElementById('dot');
const countdownEl = document.getElementById('countdown');
const flashEl = document.getElementById('flash');
const toastEl = document.getElementById('toast');
const promptEl = document.getElementById('prompt');
const promptQuestionEl = document.getElementById('prompt-question');
const promptHintEl = document.getElementById('prompt-hint');
const promptTapsEl = document.getElementById('prompt-taps');

// Bumped whenever this file changes in a way a stale tablet would get wrong.
// Must match CLIENT_VERSION in server/app.py.
const CLIENT_VERSION = '2';

const PEN_COLOR = '#111318';
const BASE_WIDTH = 2.6;
const BACKGROUND = '#ffffff';

// A contact only counts as a tap if it is this brief and this still. Anything
// longer or looser is someone drawing.
const TAP_MAX_MS = 320;
const TAP_MAX_TRAVEL = 14;

let IDLE_MS = 20000;
let TAP_WINDOW_MS = 420;
let TAP_ALWAYS_LISTEN = false;

const sessionId = `${new Date().toISOString().slice(0, 10)}-${Math.random().toString(36).slice(2, 8)}`;

let strokes = [];          // committed strokes
let current = null;        // stroke in progress
let sessionStart = 0;      // timestamp of the first point since the last clear
let lastActivity = Date.now();
let penSeen = false;       // once a stylus is used, fingers stop drawing
let width = 0, height = 0;
let contact = null;        // the in-progress pointer contact, for tap detection

/* ---------------- canvas sizing ---------------- */

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  width = Math.round(board.clientWidth);
  height = Math.round(board.clientHeight);
  board.width = Math.round(width * dpr);
  board.height = Math.round(height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  redraw();
  send({ type: 'hello', w: width, h: height });
}

function paintBackground() {
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = BACKGROUND;
  ctx.fillRect(0, 0, board.width, board.height);
  ctx.restore();
}

function redraw() {
  paintBackground();
  for (const stroke of strokes) drawStroke(stroke);
  if (current) drawStroke(current);
}

/* ---------------- stroke rendering ---------------- */

function strokeWidth(pressure) {
  // Styluses that report no pressure send 0.5; this keeps them at base width.
  return BASE_WIDTH * (0.45 + 1.1 * (pressure || 0.5));
}

function drawSegment(from, to, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = strokeWidth((from[2] + to[2]) / 2);
  ctx.beginPath();
  ctx.moveTo(from[0], from[1]);
  ctx.lineTo(to[0], to[1]);
  ctx.stroke();
}

function drawStroke(stroke) {
  const pts = stroke.points;
  if (pts.length === 1) {
    // A tap still deserves a dot.
    ctx.fillStyle = stroke.color;
    ctx.beginPath();
    ctx.arc(pts[0][0], pts[0][1], strokeWidth(pts[0][2]) / 2, 0, Math.PI * 2);
    ctx.fill();
    return;
  }
  for (let i = 1; i < pts.length; i++) drawSegment(pts[i - 1], pts[i], stroke.color);
}

/* ---------------- pointer input ---------------- */

function round(n) { return Math.round(n * 10) / 10; }

function pointFrom(event) {
  const rect = board.getBoundingClientRect();
  return [
    round(event.clientX - rect.left),
    round(event.clientY - rect.top),
    Math.round((event.pressure || 0.5) * 100) / 100,
    Date.now() - sessionStart,
  ];
}

function shouldIgnore(event) {
  if (event.pointerType === 'pen') return false;
  // Palm rejection: as soon as the stylus has been seen, fingers are ignored.
  return penSeen && event.pointerType === 'touch';
}

board.addEventListener('pointerdown', (event) => {
  if (shouldIgnore(event)) return;
  if (event.pointerType === 'pen') penSeen = true;
  event.preventDefault();
  board.setPointerCapture(event.pointerId);

  if (!sessionStart) sessionStart = Date.now();
  // Remember whether the canvas was empty when this contact started; only a
  // contact that began on an empty canvas can turn out to be an answer.
  contact = { start: Date.now(), onEmptyCanvas: strokes.length === 0 };
  current = { tool: event.pointerType, color: PEN_COLOR, width: BASE_WIDTH, points: [pointFrom(event)] };
  drawStroke(current);
  touch();
  send({ type: 'begin', color: PEN_COLOR, width: BASE_WIDTH, pt: current.points[0] });
});

board.addEventListener('pointermove', (event) => {
  if (!current || event.pointerId === undefined) return;
  if (shouldIgnore(event)) return;
  event.preventDefault();

  // Coalesced events recover the full sampling rate of a high-Hz stylus.
  const events = event.getCoalescedEvents ? event.getCoalescedEvents() : [event];
  const batch = [];
  for (const e of (events.length ? events : [event])) {
    const pt = pointFrom(e);
    const prev = current.points[current.points.length - 1];
    // Drop sub-pixel jitter; it bloats the stroke file for no visual gain.
    if (Math.abs(pt[0] - prev[0]) < 0.4 && Math.abs(pt[1] - prev[1]) < 0.4) continue;
    current.points.push(pt);
    drawSegment(prev, pt, current.color);
    batch.push(pt);
  }
  if (batch.length) send({ type: 'points', pts: batch });
  touch();
});

function endStroke(event) {
  if (!current) return;
  if (event && shouldIgnore(event)) return;
  const finished = current;
  strokes.push(finished);
  current = null;

  // A qualifying tap is retracted rather than kept as a dot.
  if (takeAsTap(finished)) return;

  // Ink now on the canvas may have made a pending question unanswerable.
  renderPrompt();
  touch();
  send({ type: 'end' });
}

board.addEventListener('pointerup', endStroke);
board.addEventListener('pointercancel', endStroke);
board.addEventListener('pointerleave', endStroke);

// Belt and braces against iPadOS scroll/zoom gestures over the canvas.
for (const type of ['touchstart', 'touchmove', 'touchend', 'gesturestart']) {
  board.addEventListener(type, (e) => e.preventDefault(), { passive: false });
}

/* ---------------- tap answers ----------------
 *
 * On an empty canvas a single tap means yes and a double tap means no. This is
 * only listening while a question is actually pending, so tapping out a field
 * of dots on a blank canvas still draws dots. Set INK_TAP_ALWAYS_LISTEN=1 on
 * the server to make an empty canvas always listen, accepting that trade.
 *
 * The public surface is window.ink; see the bottom of this section.
 */

let question = null;       // { id, text, context, resolve } while one is pending
let tapCount = 0;
let tapTimer = null;

function isTap(stroke) {
  if (!contact || !contact.onEmptyCanvas) return false;
  if (Date.now() - contact.start > TAP_MAX_MS) return false;

  // Measured against the first point rather than end to end, so a quick
  // there-and-back flick cannot masquerade as a stationary tap.
  const [ox, oy] = stroke.points[0];
  for (const [x, y] of stroke.points) {
    if (Math.hypot(x - ox, y - oy) > TAP_MAX_TRAVEL) return false;
  }
  return true;
}

function takeAsTap(stroke) {
  if (question === null && !TAP_ALWAYS_LISTEN) return false;
  // strokes still holds the stroke that just finished, so an otherwise empty
  // canvas means exactly one entry: this one.
  if (strokes.length !== 1) return false;
  if (!isTap(stroke)) return false;

  strokes.pop();
  sessionStart = 0;
  redraw();

  tapCount += 1;
  renderPrompt();
  clearTimeout(tapTimer);
  tapTimer = setTimeout(settleTaps, TAP_WINDOW_MS);
  return true;
}

function settleTaps() {
  const taps = tapCount;
  tapCount = 0;

  if (taps === 1) answer('yes');
  else if (taps === 2) answer('no');
  else {
    // Three or more is ambiguous; better to ask again than to guess.
    toast('too many taps, try again');
    renderPrompt();
  }
}

function answer(value) {
  const asked = question;
  question = null;
  ink.pending = false;
  ink.question = null;
  ink.answer = value;
  ink.answeredAt = Date.now();

  renderPrompt();
  toast(value === 'yes' ? 'Yes' : 'No');
  touch();

  send({
    type: 'answer',
    id: asked ? asked.id : null,
    question: asked ? asked.text : '',
    value,
    session_id: sessionId,
    context: asked ? asked.context : {},
  });

  asked?.resolve?.(value);
  ink.onAnswer?.(value, asked);
}

function renderPrompt() {
  if (!question) {
    promptEl.hidden = true;
    return;
  }

  promptEl.hidden = false;
  promptQuestionEl.textContent = question.text || 'Confirm?';

  // Ink on the canvas blocks answering, otherwise a stray tap while drawing
  // would silently resolve the question.
  const blocked = strokes.length > 0;
  promptEl.classList.toggle('blocked', blocked);
  promptHintEl.innerHTML = blocked
    ? 'Clear the canvas to answer'
    : '<b>1 tap</b> yes &nbsp;·&nbsp; <b>2 taps</b> no';

  promptTapsEl.innerHTML = '<i></i>'.repeat(tapCount);
}

function ask(text, options = {}) {
  clearTimeout(tapTimer);
  tapCount = 0;
  question = {
    id: options.id || `q-${Date.now()}`,
    text: text || 'Confirm?',
    context: options.context || {},
    resolve: null,
  };
  ink.pending = true;
  ink.question = question.text;
  ink.answer = null;
  renderPrompt();

  return new Promise((resolve) => { question.resolve = resolve; });
}

function cancelQuestion(id) {
  if (!question || (id && question.id !== id)) return;
  clearTimeout(tapTimer);
  tapCount = 0;
  question = null;
  ink.pending = false;
  ink.question = null;
  renderPrompt();
}

/* The flag step 2 reads. ink.answer holds the most recent yes/no; ink.ask()
 * puts a question up and resolves once it is tapped out. */
const ink = {
  answer: null,
  answeredAt: 0,
  pending: false,
  question: null,
  onAnswer: null,
  ask,
  cancel: cancelQuestion,
};
window.ink = ink;

/* ---------------- idle timer ---------------- */

function touch() { lastActivity = Date.now(); }

function hasInk() { return strokes.length > 0 || (current && current.points.length > 0); }

setInterval(() => {
  if (!hasInk() || current) {
    countdownEl.textContent = '';
    return;
  }
  const remaining = IDLE_MS - (Date.now() - lastActivity);
  if (remaining <= 0) {
    capture('idle');
    return;
  }
  countdownEl.textContent = `${Math.ceil(remaining / 1000)}s`;
}, 200);

/* ---------------- capture ---------------- */

function flash() {
  flashEl.classList.add('on');
  requestAnimationFrame(() => requestAnimationFrame(() => flashEl.classList.remove('on')));
}

let toastTimer = null;
function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('on'), 2200);
}

function capture(trigger) {
  endStroke(null);
  if (!hasInk()) return;

  const payload = {
    strokes: { strokes },
    meta: {
      session_id: sessionId,
      trigger,
      width,
      height,
      dpr: Math.min(window.devicePixelRatio || 1, 3),
      duration_ms: Date.now() - sessionStart,
      idle_timeout_ms: IDLE_MS,
      user_agent: navigator.userAgent,
    },
  };

  board.toBlob((blob) => {
    if (blob) enqueue(blob, payload);
  }, 'image/png');

  // Wipe immediately so the next drawing can start even if the upload is slow.
  strokes = [];
  current = null;
  sessionStart = 0;
  countdownEl.textContent = '';
  redraw();
  renderPrompt();
  flash();
  send({ type: 'clear' });
}

/* Uploads are queued and retried so a Wi-Fi hiccup cannot lose a drawing. */
const queue = [];
let uploading = false;

function enqueue(blob, payload) {
  queue.push({ blob, payload, attempts: 0 });
  drain();
}

async function drain() {
  if (uploading || !queue.length) return;
  uploading = true;

  while (queue.length) {
    const job = queue[0];
    const form = new FormData();
    form.append('image', job.blob, 'capture.png');
    form.append('strokes', JSON.stringify(job.payload.strokes));
    form.append('meta', JSON.stringify(job.payload.meta));

    try {
      const response = await fetch('/api/capture', { method: 'POST', body: form });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      queue.shift();
      toast(queue.length ? `saved (${queue.length} queued)` : 'saved');
    } catch (err) {
      job.attempts += 1;
      toast(`upload failed, retrying (${job.attempts})`);
      await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** job.attempts, 15000)));
    }
  }

  uploading = false;
}

/* ---------------- live mirror socket ---------------- */

let socket = null;
let backoff = 500;

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

function setStatus(text, state) {
  statusEl.textContent = text;
  dotEl.className = `dot ${state || ''}`;
}

/* The socket reconnects itself after a server restart, so a tablet can happily
 * run code older than the server without any outward sign. If the server
 * reports a different version, reload once to pick up the new frontend. */
function checkVersion(serverVersion) {
  if (!serverVersion || serverVersion === CLIENT_VERSION) {
    sessionStorage.removeItem('ink-reloaded-for');
    return;
  }
  // Reload at most once per server version, so a bad deploy cannot loop.
  if (sessionStorage.getItem('ink-reloaded-for') === serverVersion) {
    setStatus(`stale page (v${CLIENT_VERSION} vs v${serverVersion})`, 'off');
    toast('Page is out of date - reload manually');
    return;
  }
  sessionStorage.setItem('ink-reloaded-for', serverVersion);
  toast('Updating...');
  setTimeout(() => location.reload(), 400);
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/ws?role=tablet`);

  socket.addEventListener('open', () => {
    backoff = 500;
    setStatus('connected', 'on');
    send({ type: 'hello', w: width, h: height });
  });

  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    // Step 2 will handle a 'speak' message here.
    if (message.type === 'welcome') {
      setStatus('connected', 'on');
      checkVersion(message.version);
    }
    if (message.type === 'ask') ask(message.question, { id: message.id, context: message.context });
    if (message.type === 'ask_cancel') cancelQuestion(message.id);
  });

  socket.addEventListener('close', () => {
    setStatus('offline (drawings still save)', 'off');
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  });

  socket.addEventListener('error', () => socket.close());
}

/* ---------------- controls ---------------- */

document.getElementById('undo').addEventListener('click', () => {
  strokes.pop();
  if (!strokes.length) sessionStart = 0;
  redraw();
  renderPrompt();
  touch();
  send({ type: 'replace', strokes });
});

document.getElementById('clear').addEventListener('click', () => {
  strokes = [];
  current = null;
  sessionStart = 0;
  countdownEl.textContent = '';
  redraw();
  renderPrompt();
  send({ type: 'clear' });
});

document.getElementById('send').addEventListener('click', () => capture('manual'));

/* ---------------- boot ---------------- */

async function boot() {
  try {
    const settings = await (await fetch('/api/config')).json();
    IDLE_MS = settings.idle_timeout_ms ?? IDLE_MS;
    TAP_WINDOW_MS = settings.tap_window_ms ?? TAP_WINDOW_MS;
    TAP_ALWAYS_LISTEN = settings.tap_always_listen ?? TAP_ALWAYS_LISTEN;
  } catch { /* defaults are fine */ }

  resize();
  connect();

  // Best effort; needs a secure context, so it silently no-ops over plain LAN.
  try { await navigator.wakeLock?.request('screen'); } catch { /* ignore */ }
}

window.addEventListener('resize', () => setTimeout(resize, 120));
window.addEventListener('orientationchange', () => setTimeout(resize, 300));
boot();
