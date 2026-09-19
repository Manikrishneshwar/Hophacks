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
const speechEl = document.getElementById('speech');
const speechTextEl = document.getElementById('speech-text');
const speechSourceEl = document.getElementById('speech-source');
const gameEl = document.getElementById('game');
const gameTextEl = document.getElementById('game-text');

// Bumped whenever this file changes in a way a stale tablet would get wrong.
// Must match CLIENT_VERSION in server/app.py.
const CLIENT_VERSION = '10';

const PEN_COLOR = '#111318';
const BASE_WIDTH = 2.6;
const BACKGROUND = '#ffffff';

// A contact only counts as a tap if it is this brief and this still. Anything
// longer or looser is someone drawing.
const TAP_MAX_MS = 320;
const TAP_MAX_TRAVEL = 14;

let IDLE_MS = 5000;
let TAP_WINDOW_MS = 420;
let TAP_ALWAYS_LISTEN = false;
let SMOOTHING = null;      // One Euro parameters, or null when disabled
let SMOOTHING_NAME = 'off';

const sessionId = `${new Date().toISOString().slice(0, 10)}-${Math.random().toString(36).slice(2, 8)}`;

let strokes = [];          // committed strokes
let current = null;        // stroke in progress
let sessionStart = 0;      // timestamp of the first point since the last clear
let lastActivity = Date.now();
let penSeen = false;       // once a stylus is used, fingers stop drawing
let width = 0, height = 0;
let contact = null;        // the in-progress pointer contact, for tap detection
let activePointerId = null; // only this pointer draws; others are ignored
let smoother = null;       // per-stroke tremor filter

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

/* ---------------- stroke feedback ----------------
 *
 * A soft tone when a stroke starts and a brighter one when it ends, so the user
 * knows the tablet registered the contact without having to look for ink. Tones
 * are synthesised rather than loaded, so there are no audio files to serve and
 * no delay on the first play.
 *
 * This lives only in the tablet page; the desktop viewer stays silent.
 */

const START_TONE = { freq: 587, seconds: 0.10, gain: 0.16 };  // D5, a soft ding
const END_TONE = { freq: 880, seconds: 0.07, gain: 0.11 };    // A5, a shorter ting

let audioCtx = null;
let soundOn = localStorage.getItem('ink-sound') !== 'off';
let lastToneAt = 0;

function unlockAudio() {
  // Browsers only allow an AudioContext to start inside a user gesture, so
  // this is called from pointerdown rather than at load.
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  if (!audioCtx) audioCtx = new Ctx();
  if (audioCtx.state === 'suspended') audioCtx.resume();
}

function tone({ freq, seconds, gain }) {
  if (!soundOn || !audioCtx || audioCtx.state !== 'running') return;

  // Rapid strokes would otherwise stack into a buzz.
  const now = Date.now();
  if (now - lastToneAt < 45) return;
  lastToneAt = now;

  const t = audioCtx.currentTime;
  const osc = audioCtx.createOscillator();
  const envelope = audioCtx.createGain();
  osc.type = 'sine';
  osc.frequency.setValueAtTime(freq, t);
  // Quick attack, exponential decay: a percussive blip rather than a beep.
  envelope.gain.setValueAtTime(0.0001, t);
  envelope.gain.exponentialRampToValueAtTime(gain, t + 0.006);
  envelope.gain.exponentialRampToValueAtTime(0.0001, t + seconds);
  osc.connect(envelope).connect(audioCtx.destination);
  osc.start(t);
  osc.stop(t + seconds + 0.02);
}

function strokeStartFeedback() {
  tone(START_TONE);
  // Android only; iPadOS has no vibration API, so this is a silent no-op there.
  if (soundOn) navigator.vibrate?.(50);
}

function strokeEndFeedback() {
  tone(END_TONE);
}

/* ---------------- tremor smoothing ----------------
 *
 * A One Euro filter: a low-pass whose cutoff rises with pointer speed, so slow
 * unsteady movement is smoothed hard while deliberate fast strokes stay
 * responsive. A plain low-pass would either leave tremor in or make the line
 * lag badly behind the fingertip.
 *
 * The raw samples are kept alongside the filtered ones. For stroke patients the
 * tremor itself may be the interesting signal, so it must not be discarded.
 */

function makeSmoother(params) {
  if (!params) return null;
  const { min_cutoff: minCutoff, beta, d_cutoff: dCutoff } = params;
  const alpha = (cutoff, dt) => 1 / (1 + (1 / (2 * Math.PI * cutoff)) / dt);

  let tPrev = null;
  const value = { x: 0, y: 0 };
  const rate = { x: 0, y: 0 };

  // Each axis gets its own filter driven by its own speed. Sharing one speed
  // across both would let a fast horizontal drag raise the cutoff on the
  // vertical axis and wave the tremor straight through.
  const step = (axis, raw, dt) => {
    const ad = alpha(dCutoff, dt);
    rate[axis] = ad * ((raw - value[axis]) / dt) + (1 - ad) * rate[axis];
    const a = alpha(minCutoff + beta * Math.abs(rate[axis]), dt);
    value[axis] = a * raw + (1 - a) * value[axis];
    return value[axis];
  };

  return (rawX, rawY, tMs) => {
    if (tPrev === null) {
      tPrev = tMs; value.x = rawX; value.y = rawY;
      return [rawX, rawY];
    }
    // Clamp dt: coalesced samples can share a timestamp, and dt of zero would
    // make the filter coefficient blow up.
    const dt = Math.max((tMs - tPrev) / 1000, 1 / 250);
    tPrev = tMs;
    return [step('x', rawX, dt), step('y', rawY, dt)];
  };
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

/* The filter is fed the event's own high-resolution timestamp rather than the
 * stored millisecond one. Coalesced samples all arrive in the same tick, so
 * wall-clock time would collapse their spacing to nothing and leave the filter
 * with no idea how fast the signal is really moving. */
function smoothed(raw, timeStamp) {
  if (!smoother) return raw;
  const [x, y] = smoother(raw[0], raw[1], timeStamp || performance.now());
  return [round(x), round(y), raw[2], raw[3]];
}

function shouldIgnore(event) {
  if (event.pointerType === 'pen') return false;
  // Palm rejection: as soon as the stylus has been seen, fingers are ignored.
  return penSeen && event.pointerType === 'touch';
}

board.addEventListener('pointerdown', (event) => {
  // One contact at a time. Without this, a second finger landing mid-stroke
  // appends its moves to the same stroke, drawing a line between the two.
  if (activePointerId !== null) return;
  if (shouldIgnore(event)) return;
  if (event.pointerType === 'pen') penSeen = true;
  event.preventDefault();
  unlockAudio();
  activePointerId = event.pointerId;
  board.setPointerCapture(event.pointerId);

  if (!sessionStart) sessionStart = Date.now();
  contact = { start: Date.now() };
  smoother = makeSmoother(SMOOTHING);

  const raw = pointFrom(event);
  current = {
    tool: event.pointerType,
    color: PEN_COLOR,
    width: BASE_WIDTH,
    smoothing: SMOOTHING_NAME,
    points: [smoothed(raw, event.timeStamp)],
  };
  if (smoother) current.raw = [raw];

  drawStroke(current);
  touch();
  strokeStartFeedback();
  send({ type: 'begin', color: PEN_COLOR, width: BASE_WIDTH, pt: current.points[0] });
});

board.addEventListener('pointermove', (event) => {
  // Moves from any other finger are discarded, not folded into this stroke.
  if (!current || event.pointerId !== activePointerId) return;
  event.preventDefault();

  // Coalesced events recover the full sampling rate of a high-Hz stylus.
  const events = event.getCoalescedEvents ? event.getCoalescedEvents() : [event];
  const batch = [];
  for (const e of (events.length ? events : [event])) {
    const raw = pointFrom(e);
    if (current.raw) current.raw.push(raw);

    const pt = smoothed(raw, e.timeStamp);
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
  // A lifted finger that was never the drawing one must not end the stroke.
  if (event && event.pointerId !== activePointerId) return;
  activePointerId = null;
  if (!current) return;

  const finished = current;
  strokes.push(finished);
  current = null;
  smoother = null;
  strokeEndFeedback();

  // A qualifying tap is retracted rather than kept as a dot.
  if (takeAsTap(finished)) return;

  hideSpeech();
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
  if (!contact) return false;
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
  if (question === null) {
    // With nothing being asked, only listen if configured to, and only on an
    // otherwise empty canvas. strokes still holds the stroke that just
    // finished, so empty means exactly one entry: this one.
    if (!TAP_ALWAYS_LISTEN || strokes.length !== 1) return false;
  }
  if (!isTap(stroke)) return false;

  strokes.pop();
  // Existing ink keeps its timeline; only a now-empty canvas restarts it.
  if (!strokes.length) sessionStart = 0;
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
  promptHintEl.innerHTML = '<b>1 tap</b> yes &nbsp;·&nbsp; <b>2 taps</b> no';
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
  speech: null,
  ask,
  cancel: cancelQuestion,
};
window.ink = ink;

/* ---------------- speech ----------------
 *
 * The server sends the sentence it made of the drawing, with a url when it
 * managed to synthesise the audio itself. A missing url is not a failure: the
 * phone then reads the text with its own voice engine, so this never goes
 * silent. Either way the sentence is also shown, because the tap that follows
 * confirms it and the user has to be able to check what was heard.
 */

let audio = null;          // element playing server audio, if any
let lastSpoken = null;     // { text, url }, so the sentence can be spoken again

function speak(text, url) {
  if (!text) return;
  lastSpoken = { text, url: url || null };
  speechEl.hidden = false;
  speechTextEl.textContent = text;
  play();
}

function play() {
  if (!lastSpoken) return;
  const { text, url } = lastSpoken;
  stopSpeaking();

  if (!soundOn) {
    speechSourceEl.textContent = 'sound is off · tap Sound';
    ink.speech = { text, url, via: 'muted' };
    return;
  }

  if (url) {
    // Autoplay is allowed here because drawing counted as the user gesture, but
    // a page that has only been looked at is still refused; hence the fallback.
    audio = new Audio(url);
    audio.play().catch(() => speakOnDevice(text));
    speechSourceEl.textContent = 'Powered by ElevenLabs';
    ink.speech = { text, url, via: 'elevenlabs' };
    return;
  }

  speakOnDevice(text);
}

function speakOnDevice(text) {
  const synth = window.speechSynthesis;
  if (!synth) {
    speechSourceEl.textContent = 'no voice available on this device';
    ink.speech = { text, url: null, via: 'none' };
    return;
  }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 0.95;   // a little under default, easier to follow
  synth.speak(utterance);
  speechSourceEl.textContent = 'spoken by this device';
  ink.speech = { text, url: null, via: 'browser' };
}

function stopSpeaking() {
  window.speechSynthesis?.cancel();
  if (audio) {
    audio.pause();
    audio = null;
  }
}

/* Drawing again makes the last sentence stale. Taps are exempt: those are the
   answer to it, and a second tap must not be left reading a blank panel. */
function hideSpeech() {
  speechEl.hidden = true;
  stopSpeaking();
}

function setGame(target) {
  if (!target) {
    gameEl.hidden = true;
    gameTextEl.textContent = '';
    return;
  }
  gameTextEl.textContent = `a ${target}`;
  gameEl.hidden = false;
}

function startCall(message) {
  // Help is an emergency alert on the caretaker phone, not a dialer on this
  // pad — switching apps here is too much work for the person drawing.
  const name = message.name || 'your caretaker';
  toast(`Help is on the way · ${name}`);
}

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
  hideSpeech();
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
    if (message.type === 'speak') speak(message.text, message.url);
    if (message.type === 'game') setGame(message.target);
    if (message.type === 'call') startCall(message);
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

const soundButton = document.getElementById('sound');
soundButton.setAttribute('aria-pressed', String(soundOn));
soundButton.addEventListener('click', () => {
  soundOn = !soundOn;
  localStorage.setItem('ink-sound', soundOn ? 'on' : 'off');
  soundButton.setAttribute('aria-pressed', String(soundOn));
  if (soundOn) { unlockAudio(); tone(START_TONE); }
  else stopSpeaking();
});

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
  hideSpeech();
  send({ type: 'clear' });
});

/* ---------------- boot ---------------- */

async function boot() {
  try {
    const settings = await (await fetch('/api/config')).json();
    IDLE_MS = settings.idle_timeout_ms ?? IDLE_MS;
    TAP_WINDOW_MS = settings.tap_window_ms ?? TAP_WINDOW_MS;
    TAP_ALWAYS_LISTEN = settings.tap_always_listen ?? TAP_ALWAYS_LISTEN;
    SMOOTHING = settings.smoothing_params ?? null;
    SMOOTHING_NAME = settings.smoothing ?? 'off';
  } catch { /* defaults are fine */ }

  resize();
  connect();

  // Best effort; needs a secure context, so it silently no-ops over plain LAN.
  try { await navigator.wakeLock?.request('screen'); } catch { /* ignore */ }
}

window.addEventListener('resize', () => setTimeout(resize, 120));
window.addEventListener('orientationchange', () => setTimeout(resize, 300));
boot();
