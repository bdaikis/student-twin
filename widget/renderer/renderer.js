// Widget renderer: typed and spoken requests go to the twin (Hermes) and answers stream back;
// the confirm card guards irreversible tools. Redesign step 1 (2026-10-18): no title, status
// words, labels, hint card or Today strip; the dot's shape carries the state.
const conv = document.getElementById('conversation');
const input = document.getElementById('input');
const dot = document.getElementById('dot');

const STATES = { idle: 'Idle', listening: 'Listening', working: 'Working', 'needs-you': 'Waiting for you' };
let voiceStarting = true; // hollow idle dot until the speech model is loaded
function setState(name) {
  dot.className = `dot ${name}${name === 'idle' && voiceStarting ? ' starting' : ''}`;
  dot.setAttribute('aria-label', STATES[name]);
}

function add(kind, text = '') {
  const el = document.createElement('div');
  el.className = kind;
  el.textContent = text;
  conv.append(el);
  conv.scrollTop = conv.scrollHeight;
  return el;
}
const showError = (text) => add('error', text);

// Minimal, safe Markdown: escape everything, then allow **bold**, `code` and line breaks.
function renderMarkdown(text) {
  const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

const answers = new Map(); // request id -> { you, el, text }
let current = null; // request id in flight

// A command that succeeded as asked gets this reply alone (see WIDGET_RULES in main.js): the
// answer card is dropped and the request's card gets a tick.
const DONE_MARK = '✓';
const isDoneMark = (text) => text.trim().replace(/\.$/, '') === DONE_MARK;

function newId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function ask(text) {
  const id = newId();
  const you = add('you', text);
  const el = add('answer streaming');
  answers.set(id, { you, el, text: '', interrupted: false });
  current = id;
  setState('working');
  window.twin.ask(id, text);
}

window.twin.onDelta(({ id, text }) => {
  const a = answers.get(id);
  if (!a) return;
  a.text += text;
  // Hold back a reply that is still only the done mark, so it never flashes up as text.
  if (DONE_MARK.startsWith(a.text.trim())) return;
  a.el.innerHTML = renderMarkdown(a.text.trimStart()); // replies often open with blank lines
  conv.scrollTop = conv.scrollHeight;
});

function finish(id) {
  const a = answers.get(id);
  if (a) a.el.classList.remove('streaming');
  if (id === current) { current = null; settleState(); }
}

window.twin.onDone(({ id, cancelled }) => {
  const a = answers.get(id);
  if (a && cancelled) {
    const note = document.createElement('span');
    note.className = 'note';
    note.textContent = a.interrupted ? 'Interrupted' : 'Stopped';
    (a.text ? a.el : a.you).append(note);
  }
  else if (a && isDoneMark(a.text)) {
    a.el.remove();
    const tick = document.createElement('span');
    tick.className = 'tick';
    tick.textContent = '✓';
    tick.setAttribute('aria-label', 'done');
    a.you.append(tick);
  }
  finish(id);
});

window.twin.onError(({ id, message }) => {
  const a = answers.get(id);
  if (a) {
    a.el.className = 'error';
    a.el.textContent = message;
  }
  finish(id);
});

// --- Confirm card (step 2d-ii) ------------------------------------------------------------------
// Shown when an irreversible tool is waiting. Nothing runs until Confirm; Cancel, Esc or the
// 60-second limit cancels. The answer goes to the main process, which holds the request's nonce.
const confirms = new Map(); // request id -> { el, timer, answered }

function settleState() {
  if (mic.live) setState('listening');
  else if (confirms.size) setState('needs-you');
  else setState(current ? 'working' : 'idle');
}

function closeConfirm(id, outcome) {
  const c = confirms.get(id);
  if (!c) return;
  clearInterval(c.timer);
  confirms.delete(id);
  c.el.classList.add('settled');
  c.el.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  const word = document.createElement('span');
  word.className = 'outcome';
  word.textContent = outcome;
  c.el.querySelector('.countdown').replaceWith(word);
  settleState();
}

// Holding the push-to-talk key while the twin is working stops it, like Esc, and cancels any open
// confirm card, so a new command can start at once.
function interrupt() {
  for (const id of [...confirms.keys()]) answer(id, 'cancel');
  // A recording still being transcribed is superseded by the new one.
  for (const id of heard) { heard.delete(id); superseded.add(id); }
  if (current) {
    const a = answers.get(current);
    if (a) a.interrupted = true;
    window.twin.cancel(current);
  }
}

function answer(id, decision) {
  const c = confirms.get(id);
  if (!c || c.answered) return;
  c.answered = true;
  window.twin.answerConfirm(id, decision);
  closeConfirm(id, decision === 'confirm' ? 'Confirmed' : 'Cancelled');
}

window.twin.onConfirm(({ id, summary, details, action, expires }) => {
  const el = add('confirm');
  el.setAttribute('role', 'alertdialog');
  el.setAttribute('aria-label', summary);
  const q = document.createElement('div');
  q.className = 'question';
  q.textContent = summary;
  el.append(q);
  if (details.length) {
    const list = document.createElement('ul');
    list.className = 'details';
    for (const d of details) {
      const li = document.createElement('li');
      li.textContent = d;
      list.append(li);
    }
    el.append(list);
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
  el.append(row);
  const tick = () => {
    const left = Math.max(0, Math.round((expires - Date.now()) / 1000));
    countdown.textContent = `${left} s`;
    // The tool gives up at the same moment; an answer written now would never be read.
    if (left === 0) closeConfirm(id, 'Timed out');
  };
  confirms.set(id, { el, timer: setInterval(tick, 1000), answered: false });
  tick();
  conv.scrollTop = conv.scrollHeight;
  settleState();
});

// The tool stopped waiting (answered, timed out or its server restarted).
window.twin.onConfirmGone(({ id }) => closeConfirm(id, 'Closed'));

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !confirms.size) return;
  e.stopImmediatePropagation(); // Esc cancels the card, not the answer being streamed
  for (const id of [...confirms.keys()]) answer(id, 'cancel');
}, true);

// --- Push-to-talk microphone (step 2c) ------------------------------------------------------
// The mic opens on key press (so the first word isn't clipped while a tap is told apart from a
// hold) and is fully released on key up or tap. Audio goes to the main process as a 16 kHz WAV
// and is transcribed locally; it never leaves the machine.
const meter = document.getElementById('meter');
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
  meter.firstElementChild.style.width = '0';
  const { chunks, ctx } = mic;
  if (mic.proc) mic.proc.disconnect();
  if (mic.stream) mic.stream.getTracks().forEach((t) => t.stop()); // releases the microphone
  if (ctx) await ctx.close();
  Object.assign(mic, { stream: null, ctx: null, proc: null, analyser: null, chunks: [] });
  return { chunks, rate: ctx ? ctx.sampleRate : 48000 };
}

function drawMeter() {
  if (!mic.live || !mic.analyser) return;
  const buf = new Float32Array(mic.analyser.fftSize);
  mic.analyser.getFloatTimeDomainData(buf);
  const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length);
  meter.firstElementChild.style.width = `${Math.min(100, Math.round(rms * 400))}%`;
  requestAnimationFrame(drawMeter);
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

window.twin.onMic(async (action) => {
  try {
    if (action === 'prepare') {
      await micOpen(); // also while the twin is working: a hold interrupts it
    } else if (action === 'start') {
      interrupt();
      await micOpen();
      mic.live = true;
      setState('listening');
      requestAnimationFrame(drawMeter);
    } else if (action === 'discard') {
      await micClose();
    } else if (action === 'stop') {
      const wasLive = mic.live;
      const { chunks, rate } = await micClose();
      if (!wasLive) return;
      const { bytes, seconds } = toWav16k(chunks, rate);
      if (seconds < 0.4) { setState('idle'); return; } // too short to be speech
      const id = newId();
      heard.add(id);
      setState('working');
      window.twin.sendAudio(id, bytes);
    }
  } catch (err) {
    await micClose();
    showError(`Couldn't use the microphone (${err.name}).`);
    setState('idle');
  }
});

window.twin.onTranscript(({ id, ok, text, error }) => {
  if (superseded.delete(id)) return;
  heard.delete(id);
  if (!ok) {
    showError(`Couldn't transcribe that (${error || 'unknown error'}).`);
    setState('idle');
  } else if (!text) {
    showError("Didn't catch that.");
    setState('idle');
  } else {
    ask(text);
  }
});

window.twin.onVoiceStatus(({ ok, message }) => {
  voiceStarting = false;
  if (!ok) showError(message);
  settleState();
});

setState('idle');

document.getElementById('hide').onclick = () => window.twin.hide();

// The window is non-activating; clicking the input box is the one thing that asks for focus.
input.addEventListener('mousedown', () => window.twin.wantFocus());
window.twin.onFocusGranted(() => input.focus());

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && current) {
    window.twin.cancel(current);
    return;
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || current) return; // one question at a time
    input.value = '';
    input.style.height = 'auto';
    ask(text);
  }
});
