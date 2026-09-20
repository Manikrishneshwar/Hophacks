/* Caretaker phone: a live feed of finished drawings.

 * The pad confirms a request, then this page gets the PNG, the spoken
 * lines, and the yes/no taps. Open it on the same Wi-Fi, tap Enable
 * alerts once, and add it to the home screen. Lock-screen push needs
 * HTTPS, so on a plain LAN the alert is this page, a chime, and vibration.
 */

const CARETAKER_VERSION = '3';

const dotEl = document.getElementById('dot');
const patientEl = document.getElementById('patient');
const feedEl = document.getElementById('feed');
const bannerEl = document.getElementById('banner');
const alertsButton = document.getElementById('alerts');
const hintEl = document.getElementById('hint');

const events = [];
const seen = new Set();
const messages = [];
let alertsOn = false;
let audioCtx = null;
let socket = null;
let backoff = 500;

window.caretaker = { events, last: null, connected: false, alertsOn: false, messages };

function setStatus(text, state) {
  patientEl.dataset.status = text;
  if (state) dotEl.className = `dot ${state}`;
}

function beep() {
  try {
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const now = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, now);
    osc.frequency.setValueAtTime(1320, now + 0.12);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.18, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.35);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + 0.36);
  } catch { /* no audio context yet */ }
}

function vibrate() {
  try { navigator.vibrate?.([180, 70, 180]); } catch { /* iOS, or no gesture */ }
}

function notifySystem(event) {
  if (!alertsOn || !window.isSecureContext || Notification.permission !== 'granted') return;
  const answer = event.answer ? String(event.answer).toUpperCase() : 'unanswered';
  const body = [event.text, answer !== 'unanswered' ? `Answer: ${answer}` : null]
    .filter(Boolean).join('\n');
  try {
    const n = new Notification(event.kind === 'emergency' ? 'HELP — tap now' : 'New drawing', {
      body: body || 'A drawing just finished.',
      icon: event.image,
      image: event.image,
      tag: event.id,
      renotify: true,
    });
    n.onclick = () => { window.focus(); n.close(); };
  } catch { /* HTTP LAN cannot show OS notifications */ }
}

async function enableAlerts() {
  alertsOn = true;
  window.caretaker.alertsOn = true;
  alertsButton.textContent = 'Alerts on';
  alertsButton.setAttribute('aria-pressed', 'true');
  try { await navigator.wakeLock?.request('screen'); } catch { /* HTTP or unsupported */ }
  try {
    if (window.isSecureContext && Notification.permission === 'default') {
      await Notification.requestPermission();
    }
  } catch { /* ignore */ }
  beep();
  vibrate();
}

function checkVersion(serverVersion) {
  if (!serverVersion || serverVersion === CARETAKER_VERSION) {
    sessionStorage.removeItem('ink-caretaker-reloaded-for');
    return;
  }
  if (sessionStorage.getItem('ink-caretaker-reloaded-for') === serverVersion) {
    setStatus(`stale page (v${CARETAKER_VERSION})`, 'off');
    return;
  }
  sessionStorage.setItem('ink-caretaker-reloaded-for', serverVersion);
  setTimeout(() => location.reload(), 400);
}

function answerLabel(value) {
  if (value === 'yes') return 'YES';
  if (value === 'no') return 'NO';
  return '—';
}

function card(event) {
  const article = document.createElement('article');
  article.className = 'caretaker-card';
  if (event.kind === 'emergency') article.classList.add('call');
  if (event.answer === 'yes') article.classList.add('yes');
  if (event.answer === 'no') article.classList.add('no');
  article.dataset.id = event.id || '';

  const when = event.created_at
    ? new Date(event.created_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : '';
  const badge = answerLabel(event.answer);
  const prompts = (event.prompts && event.prompts.length)
    ? event.prompts
    : (event.text ? [event.text] : []);
  const answers = event.answers || [];

  article.innerHTML = `
    <div class="caretaker-card-head">
      <span class="when">${when}</span>
      <span class="badge">${badge}</span>
    </div>
    ${event.image ? `<img src="${event.image}" alt="drawing">` : ''}
    <div class="caretaker-card-body">
      ${event.kind === 'emergency' && event.call
        ? `<p class="call-line">HELP · ${escapeHtml(event.call.name || 'caretaker')} has been alerted</p>`
        : ''}
      ${prompts.map((line) => `<p class="prompt-line">${escapeHtml(line)}</p>`).join('')}
      ${answers.map((row) => `<p class="qa-line"><span>${escapeHtml(row.question || '')}</span> <b class="${row.answer || ''}">${answerLabel(row.answer)}</b></p>`).join('')}
      ${event.tag || event.detail ? `<p class="meta-line">${escapeHtml([event.tag, event.detail].filter(Boolean).join(' · '))}</p>` : ''}
    </div>
  `;
  return article;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function render() {
  if (!events.length) {
    feedEl.innerHTML = '<div class="empty">No events yet.</div>';
    return;
  }
  feedEl.innerHTML = '';
  for (const event of events) feedEl.appendChild(card(event));
}

function upsert(event, { alert = false } = {}) {
  if (!event || !event.id) return;
  const index = events.findIndex((row) => row.id === event.id);
  if (index >= 0) events[index] = { ...events[index], ...event };
  else events.unshift(event);
  window.caretaker.last = events[0];
  render();
  if (alert && !seen.has(event.id + ':' + String(event.answer))) {
    seen.add(event.id + ':' + String(event.answer));
    showBanner(event);
    if (alertsOn) {
      beep();
      vibrate();
      notifySystem(event);
    }
  }
}

function showBanner(event) {
  const answer = answerLabel(event.answer);
  bannerEl.hidden = false;
  bannerEl.className = event.kind === 'emergency'
    ? 'emergency'
    : (event.answer === 'yes' ? 'yes' : (event.answer === 'no' ? 'no' : ''));
  bannerEl.textContent = event.kind === 'emergency'
    ? `HELP · ${event.call?.name || 'caretaker'} — tap to open`
    : `${answer} · ${event.text || 'Drawing finished'}`;
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/ws?role=caretaker`);

  socket.addEventListener('open', () => {
    backoff = 500;
    window.caretaker.connected = true;
    setStatus('live', 'on');
    refreshFeed();
  });

  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    messages.push(message);
    if (message.type === 'welcome') {
      setStatus('live', 'on');
      checkVersion(message.version);
      if (message.patient?.name) {
        patientEl.textContent = message.patient.name;
      }
    }
    if (message.type === 'caretaker') upsert(message, { alert: true });
  });

  socket.addEventListener('close', () => {
    window.caretaker.connected = false;
    setStatus('reconnecting', 'off');
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  });

  socket.addEventListener('error', () => socket.close());
}

async function refreshFeed() {
  try {
    const payload = await (await fetch('/api/caretaker/events')).json();
    if (payload.patient?.name) patientEl.textContent = payload.patient.name;
    for (const event of (payload.events || []).slice().reverse()) {
      upsert(event, { alert: false });
      seen.add(event.id + ':' + String(event.answer));
    }
  } catch { /* empty feed is fine */ }
}

async function boot() {
  await refreshFeed();

  if (!window.isSecureContext) {
    hintEl.textContent = 'Keep this page open on the same Wi-Fi. Alerts are a chime and vibration here; lock-screen banners need HTTPS.';
  }

  alertsButton.addEventListener('click', enableAlerts);
  connect();
  try { await navigator.wakeLock?.request('screen'); } catch { /* ignore */ }
}

setInterval(() => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'ping' }));
  }
}, 25000);

boot();
