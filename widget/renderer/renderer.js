// Widget renderer. Step 2b: typed questions go to the twin (Hermes) and answers stream back.
// Voice (2c), actions and the confirm card (2d), and live Today data (slice 3) come later.
const conv = document.getElementById('conversation');
const input = document.getElementById('input');
const dot = document.getElementById('dot');
const statusText = document.getElementById('status');

const STATES = { idle: 'Idle', listening: 'Listening…', working: 'Working…', 'needs-you': 'Needs you' };
function setState(name) {
  dot.className = `dot ${name}`;
  statusText.textContent = STATES[name];
}

function card(kind, label, text) {
  const el = document.createElement('div');
  el.className = `card ${kind}`;
  const l = document.createElement('div');
  l.className = 'label';
  l.textContent = label;
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = text;
  el.append(l, body);
  conv.append(el);
  conv.scrollTop = conv.scrollHeight;
  return el;
}

// Minimal, safe Markdown: escape everything, then allow **bold**, `code` and line breaks.
function renderMarkdown(text) {
  const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

const answers = new Map(); // request id -> { el, text }
let current = null; // request id in flight

function newId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function ask(text, label = 'You typed') {
  const id = newId();
  card('you', label, text);
  const el = card('answer', 'Twin', '');
  el.querySelector('.body').classList.add('streaming');
  answers.set(id, { el, text: '' });
  current = id;
  setState('working');
  window.twin.ask(id, text);
}

window.twin.onDelta(({ id, text }) => {
  const a = answers.get(id);
  if (!a) return;
  a.text += text;
  a.el.querySelector('.body').innerHTML = renderMarkdown(a.text);
  conv.scrollTop = conv.scrollHeight;
});

function finish(id) {
  const a = answers.get(id);
  if (a) a.el.querySelector('.body').classList.remove('streaming');
  if (id === current) { current = null; setState('idle'); }
}

window.twin.onDone(({ id, cancelled }) => {
  const a = answers.get(id);
  if (a && cancelled) a.el.querySelector('.label').textContent = 'Twin (stopped)';
  finish(id);
});

window.twin.onError(({ id, message }) => {
  const a = answers.get(id);
  if (a) {
    a.el.classList.add('error');
    a.el.querySelector('.label').textContent = 'Problem';
    a.el.querySelector('.body').textContent = message;
  }
  finish(id);
});

function today(items) {
  const list = document.getElementById('today-list');
  list.replaceChildren(...items.map(([when, what]) => {
    const row = document.createElement('div');
    row.className = 'today-item';
    row.innerHTML = '<span class="what"></span><span class="when"></span>';
    row.querySelector('.what').textContent = what;
    row.querySelector('.when').textContent = when;
    return row;
  }));
}

// Hand-copied from the SIF1014 course-information form until slice 3 fills this from the store.
today([
  ['Tue 27 Oct', 'SIF1014 Test 1 · 10%'],
  ['Tue 17 Nov', 'SIF1014 Tutorial 1 presentation'],
]);

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
  meter.classList.remove('on');
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

const heard = new Map(); // transcription id -> card element

window.twin.onMic(async (action) => {
  try {
    if (action === 'prepare') {
      if (!current) await micOpen();
    } else if (action === 'start') {
      if (current) return; // one request at a time
      await micOpen();
      mic.live = true;
      meter.classList.add('on');
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
      heard.set(id, card('hint', 'Listening…', 'Transcribing'));
      setState('working');
      window.twin.sendAudio(id, bytes);
    }
  } catch (err) {
    await micClose();
    card('answer error', 'Microphone', `Couldn't use the microphone (${err.name}).`);
    setState('idle');
  }
});

window.twin.onTranscript(({ id, ok, text, error }) => {
  const placeholder = heard.get(id);
  heard.delete(id);
  if (placeholder) placeholder.remove();
  if (!ok) {
    card('answer error', 'Voice', `Couldn't transcribe that (${error || 'unknown error'}).`);
    setState('idle');
  } else if (!text) {
    card('hint', 'Voice', "Didn't catch that. Hold the key and try again.");
    setState('idle');
  } else {
    ask(text, 'You said');
  }
});

window.twin.onVoiceStatus(({ ok, message }) => {
  hint.querySelector('.body').textContent = ok
    ? 'Type a question below, or hold numpad . (or Del) and speak.'
    : message;
});

const hint = card('hint', 'Connected to your twin', 'Type a question below. Voice is starting…');
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
