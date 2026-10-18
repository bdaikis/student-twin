// Widget renderer. One shape at a time: a pill (rest, listening, transcribing, working, success)
// or a card (answer, error, confirm, typing); the window is sized to it (Ghost Overlay UI
// Redesign, 2026-10-18). Typed and spoken requests go to the twin (Hermes) and answers stream
// back; the confirm card guards irreversible tools.
const shell = document.getElementById('shell');
const inner = document.getElementById('inner');
const pill = document.getElementById('pill');
const line = document.getElementById('line');
const echo = document.getElementById('echo');
const body = document.getElementById('body');
const confirmBox = document.getElementById('confirm');
const input = document.getElementById('input');

const CARD_VIEWS = new Set(['answer', 'error', 'confirm', 'typing']);
const LABELS = {
  rest: 'Idle', listening: 'Listening', transcribing: 'Transcribing', working: 'Working',
  success: 'Done', answer: 'Answer', error: 'Problem', confirm: 'Waiting for you', typing: 'Typing',
};
const SUCCESS_MS = 900;
const ERROR_MS = 12000;
const OUTCOME_MS = 1500;
const RECALL_MS = 10 * 60 * 1000; // the last exchange can be reopened for this long
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

let view = 'rest';
let viewTimer = null; // collapse timer for success, answer, error and settled confirm views
let timerLeft = 0;
let timerStarted = 0;
let hovering = false;

// ---- Layout: measure the shape, size the window to it, then animate the shell --------------
// Growing: enlarge the window first (the new area is transparent), then morph the shell.
// Shrinking: morph the shell first, then shrink the window when the morph ends. While an answer
// streams the window keeps 96 px of headroom so most tokens cause no native resize.
const HEADROOM = 96;
let win = { w: 28, h: 28 }; // the shape size the window was last sized for
let shrinkTimer = null;
let layoutQueued = false;

function setBodyMax() {
  const max = Math.min(420, Math.round(screen.availHeight * 0.5));
  document.documentElement.style.setProperty('--body-max', `${max - 60}px`);
}
setBodyMax();

function sendLayout(w, h) {
  win = { w, h };
  window.twin.layout({ w, h });
}

function relayout() {
  layoutQueued = false;
  const w = Math.ceil(inner.offsetWidth);
  const h = Math.ceil(inner.offsetHeight);
  if (!w || !h) return;
  const streaming = !!body.querySelector('.streaming') || body.classList.contains('streaming');
  const instant = streaming || reducedMotion.matches || shell.classList.contains('hidden');
  shell.classList.toggle('instant', instant);
  clearTimeout(shrinkTimer);
  if (streaming) {
    if (w > win.w || h > win.h) sendLayout(Math.max(w, win.w), h + HEADROOM);
    shell.style.width = `${w}px`;
    shell.style.height = `${h}px`;
    return;
  }
  if (w > win.w || h > win.h) sendLayout(Math.max(w, win.w), Math.max(h, win.h));
  // Two frames, so the window has its new size before the shell grows into it.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    shell.style.width = `${w}px`;
    shell.style.height = `${h}px`;
    const ms = instant ? 0 : parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--t-morph')) || 160;
    shrinkTimer = setTimeout(() => { if (w !== win.w || h !== win.h) sendLayout(w, h); }, ms + 40);
  }));
}

function queueLayout() {
  if (layoutQueued) return;
  layoutQueued = true;
  requestAnimationFrame(relayout);
}
new ResizeObserver(queueLayout).observe(inner);

// ---- Views ----------------------------------------------------------------------------------
function setView(name) {
  clearTimer();
  hovering = false; // counts again from the next pointer move
  view = name;
  shell.dataset.view = name;
  shell.classList.toggle('card-view', CARD_VIEWS.has(name));
  pill.setAttribute('aria-label', LABELS[name]);
  if (name === 'rest') { line.textContent = restLabel(); }
  queueLayout();
}

function restLabel() {
  if (shell.dataset.online === '0') return "The twin isn't running";
  if (shell.dataset.voice === 'off') return voiceMessage || 'Voice is off';
  if (shell.dataset.voice === 'starting') return 'Voice starting';
  return '';
}

function clearTimer() { clearTimeout(viewTimer); viewTimer = null; timerLeft = 0; }

// A collapse timer that pauses while the pointer is over the widget.
function collapseIn(ms, then = () => setView('rest')) {
  clearTimer();
  timerLeft = ms;
  const run = () => {
    timerStarted = Date.now();
    viewTimer = setTimeout(() => { viewTimer = null; timerLeft = 0; then(); }, timerLeft);
  };
  collapseIn.run = run;
  if (!hovering) run();
}
function pauseTimer() {
  if (!viewTimer) return;
  clearTimeout(viewTimer);
  viewTimer = null;
  timerLeft = Math.max(0, timerLeft - (Date.now() - timerStarted));
}
function resumeTimer() {
  if (viewTimer || !timerLeft) return;
  timerLeft = Math.max(timerLeft, 3000); // give a moment after the pointer leaves
  collapseIn.run();
}

// Only a pointer that moves over the widget pauses the timer: a card that opens under a mouse
// left resting there would otherwise never close.
let hoverOff = null;
shell.addEventListener('mouseenter', () => {
  clearTimeout(hoverOff);
  shell.classList.add('hover');
  queueLayout();
});
// Chromium also sends moves when the layout changes under a still cursor, so the screen position
// has to change.
let lastPointer = null;
shell.addEventListener('pointermove', (e) => {
  const pos = `${e.screenX},${e.screenY}`;
  const moved = lastPointer !== null && pos !== lastPointer;
  lastPointer = pos;
  if (hovering || !moved) return;
  hovering = true;
  pauseTimer();
});
shell.addEventListener('mouseleave', () => {
  hovering = false;
  lastPointer = null;
  // A short delay so crossing the pill's edge doesn't make it flicker.
  hoverOff = setTimeout(() => { shell.classList.remove('hover'); queueLayout(); }, 150);
  resumeTimer();
});

// ---- Requests and answers ---------------------------------------------------------------------
// Minimal, safe Markdown: escape everything, then allow **bold**, `code` and line breaks.
function renderMarkdown(text) {
  const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

const answers = new Map(); // request id -> { request, text, interrupted }
let current = null; // request id in flight
let last = null; // { request, kind: 'done'|'answer'|'error', text, at } for recall

// A command that succeeded as asked gets this reply alone (see WIDGET_RULES in main.js): no text,
// just the tick.
const DONE_MARK = '✓';
const isDoneMark = (text) => text.trim().replace(/\.$/, '') === DONE_MARK;
const isHeldBack = (text) => DONE_MARK.startsWith(text.trim());

function newId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function ask(text) {
  const id = newId();
  answers.set(id, { request: text, text: '', interrupted: false });
  current = id;
  line.textContent = text;
  if (!confirms.size) setView('working');
  window.twin.ask(id, text);
}

function showAnswer(a, streaming) {
  echo.textContent = a.request;
  body.className = streaming ? 'body streaming' : 'body';
  body.innerHTML = renderMarkdown(a.text.trimStart()); // replies often open with blank lines
  if (view !== 'answer') setView('answer');
  else queueLayout();
  body.scrollTop = body.scrollHeight;
}

function showError(message, request = '') {
  echo.textContent = request;
  body.className = 'body';
  body.textContent = message;
  setView('error');
  last = { request, kind: 'error', text: message, at: Date.now() };
  collapseIn(ERROR_MS);
}

window.twin.onDelta(({ id, text }) => {
  const a = answers.get(id);
  if (!a) return;
  a.text += text;
  // Hold back a reply that is still only the done mark, so it never flashes up as text.
  if (isHeldBack(a.text) || id !== current || confirms.size || a.interrupted || a.dismissed || a.confirmCancelled) return;
  showAnswer(a, true);
});

// Answer stays up for 4 s plus a quarter-second a word, between 6 and 45 s; hover pauses it.
function readingTime(text) {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.min(45000, Math.max(6000, 4000 + 250 * words));
}

window.twin.onDone(({ id, cancelled }) => {
  const a = answers.get(id);
  answers.delete(id);
  if (id === current) current = null;
  if (!a) return;
  if (a.interrupted) return; // the widget is already listening for the next command
  if (a.dismissed) { if (view === 'working') setView('rest'); return; }
  if (cancelled) {
    if (!a.text.trim() || isHeldBack(a.text)) { if (view === 'working' || view === 'answer') setView('rest'); return; }
    showAnswer(a, false);
    const note = document.createElement('span');
    note.className = 'note';
    note.textContent = 'Stopped';
    body.append(note);
    last = { request: a.request, kind: 'answer', text: a.text, at: Date.now() };
    collapseIn(readingTime(a.text));
  } else if (a.confirmCancelled && (isDoneMark(a.text) || !a.text.includes('?'))) {
    // The card already showed "Cancelled" or "Timed out"; only a question is worth showing.
    if (view === 'working') setView('rest');
  } else if (isDoneMark(a.text)) {
    last = { request: a.request, kind: 'done', text: '', at: Date.now() };
    if (confirms.size || view === 'listening' || view === 'typing') return;
    setView('success');
    collapseIn(SUCCESS_MS);
  } else if (!a.text.trim()) {
    if (view === 'working') setView('rest');
  } else {
    last = { request: a.request, kind: 'answer', text: a.text, at: Date.now() };
    if (confirms.size || view === 'listening' || view === 'typing') return;
    showAnswer(a, false);
    collapseIn(readingTime(a.text));
  }
});

window.twin.onError(({ id, message }) => {
  const a = answers.get(id);
  answers.delete(id);
  if (id === current) current = null;
  if (a && !a.interrupted && view !== 'listening' && view !== 'typing') showError(message, a.request);
});

function stopCurrent() {
  if (current) window.twin.cancel(current);
}

// Closing a card while its answer still streams stops the answer and drops it.
document.getElementById('close').addEventListener('click', () => {
  const a = current && answers.get(current);
  if (a) { a.dismissed = true; stopCurrent(); }
  setView('rest');
});

// ---- Confirm card -----------------------------------------------------------------------------
// Shown when an irreversible tool is waiting. Nothing runs until the confirm button; Cancel, Esc,
// a push-to-talk hold or the 60-second limit cancels. The answer goes to the main process, which
// holds the request's nonce.
const confirms = new Map(); // request id -> { timer, answered, expires }
let confirmShown = null; // the request id on screen

function renderConfirm(id, { summary, details, action, expires }) {
  confirmShown = id;
  confirmBox.className = 'confirm';
  confirmBox.replaceChildren();
  confirmBox.setAttribute('aria-label', summary);
  const q = document.createElement('div');
  q.className = 'question';
  q.textContent = summary;
  confirmBox.append(q);
  if (details.length) {
    const list = document.createElement('ul');
    list.className = 'details';
    for (const d of details) {
      const li = document.createElement('li');
      li.textContent = d;
      list.append(li);
    }
    confirmBox.append(list);
  }
  const row = document.createElement('div');
  row.className = 'buttons';
  const ok = document.createElement('button');
  ok.className = 'confirm-btn';
  ok.textContent = action;
  ok.onclick = () => answer(id, 'confirm');
  const no = document.createElement('button');
  no.textContent = 'Cancel';
  no.onclick = () => answer(id, 'cancel');
  const countdown = document.createElement('span');
  countdown.className = 'countdown';
  row.append(ok, no, countdown);
  const bar = document.createElement('div');
  bar.className = 'bar';
  confirmBox.append(row, bar);
  setView('confirm');
}

function tickConfirm(id) {
  const c = confirms.get(id);
  if (!c) return;
  const left = Math.max(0, Math.round((c.expires - Date.now()) / 1000));
  if (confirmShown === id) {
    const countdown = confirmBox.querySelector('.countdown');
    if (countdown) countdown.textContent = `${left} s`;
    const bar = confirmBox.querySelector('.bar');
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, ((c.expires - Date.now() - 1000) / 60000) * 100))}%`;
  }
  // The tool gives up at the same moment; an answer written now would never be read.
  if (left === 0) { markCancelled(); closeConfirm(id, 'Timed out'); }
}

function closeConfirm(id, outcome) {
  const c = confirms.get(id);
  if (!c) return;
  clearInterval(c.timer);
  confirms.delete(id);
  if (confirmShown !== id) return;
  confirmShown = null;
  confirmBox.classList.add('settled');
  confirmBox.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  const word = document.createElement('span');
  word.className = 'outcome';
  word.textContent = outcome;
  confirmBox.querySelector('.countdown')?.replaceWith(word);
  const next = () => {
    const queued = [...confirms.entries()][0];
    if (queued) return renderConfirm(queued[0], queued[1].data);
    if (outcome === 'Confirmed' && current) return setView('working');
    const a = current && answers.get(current);
    if (a && a.text.trim() && !isHeldBack(a.text)) return showAnswer(a, true);
    setView(current ? 'working' : 'rest');
  };
  if (outcome === 'Confirmed') next();
  else collapseIn(OUTCOME_MS, next);
}

// A cancelled or timed-out card belongs to the request in flight; its reply is only the done mark,
// which must not show as a success tick.
function markCancelled() {
  const a = current && answers.get(current);
  if (a) a.confirmCancelled = true;
}

function answer(id, decision) {
  const c = confirms.get(id);
  if (!c || c.answered) return;
  c.answered = true;
  if (decision !== 'confirm') markCancelled();
  window.twin.answerConfirm(id, decision);
  closeConfirm(id, decision === 'confirm' ? 'Confirmed' : 'Cancelled');
}

window.twin.onConfirm((data) => {
  const c = { answered: false, expires: data.expires, data };
  confirms.set(data.id, c);
  c.timer = setInterval(() => tickConfirm(data.id), 1000);
  if (!confirmShown) renderConfirm(data.id, data);
  tickConfirm(data.id);
});

// The tool stopped waiting (answered, timed out or its server restarted).
window.twin.onConfirmGone(({ id }) => closeConfirm(id, 'Closed'));

// ---- Typing -------------------------------------------------------------------------------------
// The window never takes focus on its own. A click on the resting pill opens a typing card and
// asks for focus; sending, Esc or clicking elsewhere hands the keyboard back.
function openTyping() {
  if (current || confirms.size) return;
  if (last && Date.now() - last.at < RECALL_MS) {
    echo.textContent = last.request;
    body.className = 'body';
    if (last.kind === 'done') body.innerHTML = '<span class="done">✓</span>';
    else body.textContent = last.text.trim();
  } else {
    echo.textContent = '';
    body.textContent = '';
  }
  setView('typing');
  window.twin.wantFocus();
}

function closeTyping() {
  if (view !== 'typing') return;
  setView(current ? 'working' : 'rest');
  window.twin.releaseFocus();
}

window.twin.onFocusGranted(() => { if (view === 'typing') input.focus(); });
window.twin.onFocusLost(() => { if (view === 'typing') setView(current ? 'working' : 'rest'); });

// On press, not click: a press on a pill that appeared under a still cursor makes it widen for
// hover, and the window resize between press and release would cancel the click.
pill.addEventListener('pointerdown', (e) => {
  if (e.button !== 0 || e.target.closest('.chev, .grip')) return;
  if (view === 'rest') openTyping();
});

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = `${Math.min(96, input.scrollHeight)}px`;
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.preventDefault(); closeTyping(); return; }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || current) return; // one request at a time
    input.value = '';
    input.style.height = 'auto';
    window.twin.releaseFocus();
    ask(text);
  }
});

// Esc, while the widget has focus, cancels a confirm card first, then an answer being streamed.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (confirms.size) {
    e.stopImmediatePropagation();
    for (const id of [...confirms.keys()]) answer(id, 'cancel');
  } else if (current && view !== 'typing') {
    stopCurrent();
  }
}, true);

// ---- Dragging by the grip -------------------------------------------------------------------
// Done by the main process from the cursor position: a CSS drag region would swallow the hover
// events that keep the grip on screen.
const grip = document.getElementById('grip');
grip.addEventListener('pointerdown', (e) => {
  grip.setPointerCapture(e.pointerId);
  window.twin.drag(true);
});
const endDrag = () => window.twin.drag(false);
grip.addEventListener('pointerup', endDrag);
grip.addEventListener('lostpointercapture', endDrag);

// ---- Hide and show ------------------------------------------------------------------------------
document.getElementById('hide').addEventListener('click', () => window.twin.hide());
let showWatchdog = null;
window.twin.onVisibility((visible) => {
  clearTimeout(showWatchdog);
  if (!visible) {
    shell.classList.add('hidden');
    return;
  }
  shell.classList.remove('hover');
  hovering = false;
  shell.classList.add('notrans', 'hidden');
  // Start just right of the spot, then slide in.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    shell.classList.remove('notrans', 'hidden');
  }));
  // Never leave the widget stuck invisible after an interrupted fade.
  showWatchdog = setTimeout(() => { shell.classList.remove('notrans', 'hidden'); queueLayout(); }, 1000);
});

// ---- Status: voice and the twin's reachability ----------------------------------------------
let voiceMessage = '';
window.twin.onVoiceStatus(({ ok, message }) => {
  shell.dataset.voice = ok ? 'ok' : 'off';
  voiceMessage = ok ? '' : message;
  if (view === 'rest') setView('rest');
});
window.twin.onOnline((ok) => {
  shell.dataset.online = ok ? '1' : '0';
  if (view === 'rest') setView('rest');
});

// ---- Push-to-talk microphone (step 2c) ------------------------------------------------------
// The mic opens on key press (so the first word isn't clipped while a tap is told apart from a
// hold) and is fully released on key up or tap. Audio goes to the main process as a 16 kHz WAV
// and is transcribed locally; it never leaves the machine.
const bars = [...document.getElementById('bars').children];
const mic = { stream: null, ctx: null, proc: null, analyser: null, chunks: [], opening: null, live: false };

async function micOpen() {
  if (mic.stream || mic.opening) return mic.opening;
  mic.opening = (async () => {
    mic.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    mic.ctx = new AudioContext();
    const src = mic.ctx.createMediaStreamSource(mic.stream);
    mic.analyser = mic.ctx.createAnalyser();
    mic.analyser.fftSize = 512;
    mic.proc = mic.ctx.createScriptProcessor(4096, 1, 1);
    mic.chunks = [];
    mic.proc.onaudioprocess = (e) => mic.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    src.connect(mic.analyser);
    src.connect(mic.proc);
    mic.proc.connect(mic.ctx.destination); // required for processing; the output stays silent
  })();
  try { await mic.opening; } finally { mic.opening = null; }
}

async function micClose() {
  if (mic.opening) { try { await mic.opening; } catch {} }
  mic.live = false;
  const { chunks, ctx } = mic;
  if (mic.proc) mic.proc.disconnect();
  if (mic.stream) mic.stream.getTracks().forEach((t) => t.stop()); // releases the microphone
  if (ctx) await ctx.close();
  Object.assign(mic, { stream: null, ctx: null, proc: null, analyser: null, chunks: [] });
  return { chunks, rate: ctx ? ctx.sampleRate : 48000 };
}

// Five level bars, 4 to 14 px, following five bands of the voice range. At most 30 updates a
// second (10 with reduced motion).
function setBars(levels) {
  bars.forEach((b, i) => { b.style.transform = `scaleY(${1 + 2.5 * Math.min(1, levels[i] || 0)})`; });
}
let lastBars = 0;
function drawBars(now) {
  if (!mic.live || !mic.analyser) return;
  if (now - lastBars >= (reducedMotion.matches ? 100 : 33)) {
    lastBars = now;
    const bins = new Uint8Array(mic.analyser.frequencyBinCount);
    mic.analyser.getByteFrequencyData(bins);
    // Roughly 190 Hz to 3.2 kHz at 48 kHz sampling (93.75 Hz a bin).
    const bands = [[2, 4], [4, 7], [7, 12], [12, 20], [20, 34]];
    setBars(bands.map(([a, b]) => {
      let sum = 0;
      for (let k = a; k < b; k++) sum += bins[k];
      return Math.max(0, (sum / (b - a) - 60) / 140);
    }));
  }
  requestAnimationFrame(drawBars);
}

function toWav16k(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const ratio = rate / 16000;
  const out = new Int16Array(Math.floor(total / ratio));
  let pos = 0;
  let acc = 0;
  let count = 0;
  let next = ratio;
  let i = 0;
  for (const c of chunks) for (const v of c) {
    acc += v; count += 1; i += 1;
    if (i >= next && pos < out.length) {
      const s = Math.max(-1, Math.min(1, acc / count));
      out[pos++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      acc = 0; count = 0; next += ratio;
    }
  }
  const bytes = new ArrayBuffer(44 + pos * 2);
  const v = new DataView(bytes);
  const str = (o, s) => [...s].forEach((ch, k) => v.setUint8(o + k, ch.charCodeAt(0)));
  str(0, 'RIFF'); v.setUint32(4, 36 + pos * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, pos * 2, true);
  new Int16Array(bytes, 44).set(out.subarray(0, pos));
  return { bytes, seconds: pos / 16000 };
}

const heard = new Set(); // transcription ids still being transcribed
const superseded = new Set(); // transcriptions dropped because a newer hold interrupted them

// Holding the push-to-talk key while the twin is working stops it, like Esc, and cancels any open
// confirm card, so a new command can start at once.
function interrupt() {
  for (const id of [...confirms.keys()]) answer(id, 'cancel');
  for (const id of heard) { heard.delete(id); superseded.add(id); }
  if (current) {
    const a = answers.get(current);
    if (a) a.interrupted = true;
    window.twin.cancel(current);
  }
  if (view === 'typing') window.twin.releaseFocus();
}

window.twin.onMic(async (action) => {
  try {
    if (action === 'prepare') {
      await micOpen(); // also while the twin is working: a hold interrupts it
    } else if (action === 'start') {
      interrupt();
      await micOpen();
      mic.live = true;
      setBars([0, 0, 0, 0, 0]);
      setView('listening');
      requestAnimationFrame(drawBars);
    } else if (action === 'discard') {
      await micClose();
    } else if (action === 'stop') {
      const wasLive = mic.live;
      const { chunks, rate } = await micClose();
      if (!wasLive) return;
      const { bytes, seconds } = toWav16k(chunks, rate);
      if (seconds < 0.4) { setView('rest'); return; } // too short to be speech
      const id = newId();
      heard.add(id);
      setView('transcribing');
      window.twin.sendAudio(id, bytes);
    }
  } catch (err) {
    await micClose();
    showError(`Couldn't use the microphone (${err.name}).`);
  }
});

window.twin.onTranscript(({ id, ok, text, error }) => {
  if (superseded.delete(id)) return;
  heard.delete(id);
  if (!ok) showError(`Couldn't transcribe that (${error || 'unknown error'}).`);
  else if (!text) showError("Didn't catch that.");
  else ask(text);
});

setView('rest');
