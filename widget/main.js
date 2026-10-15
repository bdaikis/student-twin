// Student Twin widget — main process (slice 2: 2a window shell, 2b connection to Hermes).
// Behaviour follows wiki/projects/student-twin/voice-widget-spec.md v0.6.
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage, session } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');

// Hermes's OpenAI-compatible api_server, loopback only. Its key never leaves this process.
const HERMES_URL = 'http://127.0.0.1:8642/v1/chat/completions';
const HERMES_ENV = path.join(process.env.HERMES_HOME || path.join(process.env.LOCALAPPDATA, 'hermes'), '.env');
let sessionId = null; // X-Hermes-Session-Id: one conversation per widget run
const inflight = new Map(); // request id -> AbortController

const WIDTH = 360;
const HEIGHT = 750;
const TOP_OFFSET = 100;
const SLIDE_MS = 160;
const REVEAL_POLL_MS = 100;
const REVEAL_DWELL_MS = 400;
const CAPTION_BUTTONS_DIP = 3 * 46; // minimise, maximise, close of a maximised window

let win = null;
let tray = null;
let hidden = false;
let homeX = null; // where the widget sits when shown (follows drags)
let dwellStart = null;

function primaryRightEdge() {
  const { workArea } = screen.getPrimaryDisplay();
  return { x: workArea.x + workArea.width - WIDTH, y: workArea.y + TOP_OFFSET };
}

function createWindow() {
  const start = primaryRightEdge();
  win = new BrowserWindow({
    width: WIDTH,
    height: HEIGHT,
    x: start.x,
    y: start.y,
    frame: false,
    transparent: true,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    // Non-activating: clicks on cards and buttons work without taking focus from the app
    // Bhanu is using. The input box asks for focus explicitly (widget:want-focus).
    focusable: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  // Keep the widget out of screenshots and screen recordings (incl. the agent's own, later).
  win.setContentProtection(true);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => {
    win.showInactive(); // never steal focus from the app Bhanu is using
    homeX = win.getBounds().x;
  });
  win.on('moved', () => {
    if (!hidden) homeX = win.getBounds().x;
  });
  // Give focus back the moment Bhanu leaves the input box for another app.
  win.on('blur', () => win.setFocusable(false));
}

function wantFocus() {
  if (!win || hidden) return;
  win.setFocusable(true);
  win.focus();
  win.webContents.send('focus-granted');
}

function animateX(fromX, toX, done) {
  const y = win.getBounds().y;
  const t0 = Date.now();
  const step = () => {
    const t = Math.min(1, (Date.now() - t0) / SLIDE_MS);
    const eased = 1 - Math.pow(1 - t, 3);
    win.setBounds({ x: Math.round(fromX + (toX - fromX) * eased), y, width: WIDTH, height: HEIGHT });
    if (t < 1) setTimeout(step, 10);
    else if (done) done();
  };
  step();
}

function hideWidget() {
  if (hidden || !win) return;
  hidden = true;
  const display = screen.getDisplayMatching(win.getBounds());
  const offscreen = display.bounds.x + display.bounds.width;
  animateX(win.getBounds().x, offscreen, () => win.hide());
  win.webContents.send('visibility', false);
}

function showWidget() {
  if (!hidden || !win) return;
  hidden = false;
  const display = screen.getDisplayMatching({ x: homeX, y: win.getBounds().y, width: WIDTH, height: HEIGHT });
  const offscreen = display.bounds.x + display.bounds.width;
  win.setBounds({ x: offscreen, y: win.getBounds().y, width: WIDTH, height: HEIGHT });
  win.showInactive();
  win.setAlwaysOnTop(true, 'screen-saver');
  animateX(offscreen, homeX, null);
  win.webContents.send('visibility', true);
}

// Top-edge reveal: a fixed stretch of the primary display's top edge, above the widget's
// default position (not wherever it was dragged), minus the caption-button corner, after a
// short dwell. Polled, so no invisible strip window can swallow clicks on tabs or title bars.
function isTopEdgeOfDesktop(display) {
  // With monitors stacked vertically, only the top display's top edge counts.
  return !screen.getAllDisplays().some((d) =>
    d.id !== display.id &&
    d.bounds.y + d.bounds.height <= display.bounds.y &&
    d.bounds.x < display.bounds.x + display.bounds.width &&
    d.bounds.x + d.bounds.width > display.bounds.x);
}

function pollReveal() {
  if (!hidden || !win || homeX === null) { dwellStart = null; return; }
  const p = screen.getCursorScreenPoint();
  const display = screen.getPrimaryDisplay();
  const right = display.bounds.x + display.bounds.width;
  const zoneLeft = right - WIDTH;
  const zoneRight = right - CAPTION_BUTTONS_DIP;
  const inZone = p.y <= display.bounds.y + 1 && p.x >= zoneLeft && p.x < zoneRight && isTopEdgeOfDesktop(display);
  // TODO(2e): suppress while a full-screen app is in front (SHQueryUserNotificationState).
  if (!inZone) { dwellStart = null; return; }
  if (dwellStart === null) dwellStart = Date.now();
  if (Date.now() - dwellStart >= REVEAL_DWELL_MS) { dwellStart = null; showWidget(); }
}

function hermesKey() {
  const line = fs.readFileSync(HERMES_ENV, 'utf8').split(/\r?\n/).find((l) => l.startsWith('API_SERVER_KEY='));
  return line ? line.slice('API_SERVER_KEY='.length).trim() : null;
}

function explain(err) {
  const code = err && (err.cause?.code || err.code);
  if (code === 'ECONNREFUSED') return "The twin isn't running. Start it with: hermes gateway start";
  if (err && err.status === 401) return "The widget's key doesn't match Hermes's API key.";
  if (err && err.code === 'ENOENT') return "Hermes's settings file wasn't found.";
  return `Couldn't reach the twin (${err && err.message ? err.message : 'unknown error'}).`;
}

// Sent with every widget question as a system message. Hermes does not pass MCP server
// instructions to the model, so the desktop guidance lives here; Telegram never sees it.
// DONE_MARK must match the renderer's.
const DONE_MARK = '✓';
const WIDGET_RULES = [
  "You are answering through Bhanu's desktop widget. Requests arrive typed or as speech-recognition",
  'transcripts, which can mishear names and short commands.',
  `When a request is a command (open, close, play, pause, arrange, focus, minimize, delete and the like) and every action it needed succeeded exactly as asked, reply with only the single character ${DONE_MARK} and nothing else; the widget shows a tick instead of text. This includes an irreversible action he confirmed on the card: a successful delete also gets only ${DONE_MARK}.`,
  'Reply in words instead when an action failed or only partly worked, when you had to guess what he meant (say what you assumed), when you need to ask him something, or when he asked for information such as the weather, what is posted in a course, or what a page says.',
  "Prefer the desktop tools over screen control. Use the weather tool for weather, rain and air quality, and web search for other facts. For anything posted in a course use course_materials and open_course_item.",
  'When a window or app name matches nothing, check list_windows and ask whether he meant the closest-sounding one, naming it.',
  "Irreversible tools such as delete_files show him a confirm card themselves and wait for his click: call them directly, without asking for confirmation in chat, and never retry one he cancelled. Never claim something was deleted unless the tool result says so.",
].join(' ');

// Stream one answer from Hermes, forwarding text deltas to the renderer as they arrive.
async function ask(sender, id, text) {
  const controller = new AbortController();
  inflight.set(id, controller);
  try {
    const key = hermesKey();
    if (!key) throw Object.assign(new Error('no API key'), { status: 401 });
    const headers = { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' };
    if (sessionId) headers['X-Hermes-Session-Id'] = sessionId;
    const res = await fetch(HERMES_URL, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        model: 'hermes-agent',
        stream: true,
        messages: [{ role: 'system', content: WIDGET_RULES }, { role: 'user', content: text }],
      }),
      signal: controller.signal,
    });
    if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
    sessionId = res.headers.get('x-hermes-session-id') || sessionId;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') continue;
        const delta = JSON.parse(data).choices?.[0]?.delta?.content;
        if (delta) sender.send('twin:delta', { id, text: delta });
      }
    }
    sender.send('twin:done', { id });
  } catch (err) {
    if (err.name === 'AbortError') sender.send('twin:done', { id, cancelled: true });
    else sender.send('twin:error', { id, message: explain(err) });
  } finally {
    inflight.delete(id);
  }
}

// --- Voice helper (step 2c): numpad push-to-talk hook + Hermes's local faster-whisper ----------
const HERMES_HOME = path.dirname(HERMES_ENV);
let helper = null;
const pendingAudio = new Map(); // transcription id -> temp wav path

function hermesPython() {
  // Hermes records its active environment in installs/<id>/facts.json; it changes on every pm install.
  const installs = path.join(HERMES_HOME, 'installs');
  const facts = fs.readdirSync(installs)
    .map((d) => path.join(installs, d, 'facts.json'))
    .filter((f) => fs.existsSync(f))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
  const venv = JSON.parse(fs.readFileSync(facts, 'utf8')).packages.venv.environment;
  return path.join(venv, 'Scripts', 'python.exe');
}

function toRenderer(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function startHelper() {
  let python;
  try { python = hermesPython(); } catch (err) {
    toRenderer('voice:status', { ok: false, message: "Voice is off: Hermes's Python wasn't found." });
    return;
  }
  helper = spawn(python, [path.join(__dirname, 'helper', 'voice_helper.py')], {
    env: { ...process.env, HERMES_HOME, PYTHONPATH: path.join(HERMES_HOME, 'hermes-agent'), PYTHONIOENCODING: 'utf-8' },
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'ignore'],
  });
  readline.createInterface({ input: helper.stdout }).on('line', (line) => {
    let ev;
    try { ev = JSON.parse(line); } catch { return; }
    switch (ev.event) {
      case 'press': toRenderer('mic', 'prepare'); break;
      case 'hold': showWidget(); toRenderer('mic', 'start'); break;
      case 'release': toRenderer('mic', 'stop'); break;
      case 'tap': toRenderer('mic', 'discard'); break;
      case 'transcript': {
        const file = pendingAudio.get(ev.id);
        pendingAudio.delete(ev.id);
        if (file) fs.rm(file, { force: true }, () => {});
        toRenderer('voice:transcript', ev);
        break;
      }
      case 'log':
        if (ev.message.startsWith('speech model ready')) toRenderer('voice:status', { ok: true, message: 'Voice ready: hold numpad . to talk' });
        else if (ev.message.startsWith('keyboard hook failed')) toRenderer('voice:status', { ok: false, message: 'Voice is off: the push-to-talk key could not be registered.' });
        break;
      default: break;
    }
  });
  helper.on('exit', () => {
    helper = null;
    toRenderer('voice:status', { ok: false, message: 'Voice stopped. Restart the widget to turn it back on.' });
  });
}

function transcribeWav(id, wavBytes) {
  if (!helper) return toRenderer('voice:transcript', { id, ok: false, text: '', error: 'voice helper not running' });
  const file = path.join(os.tmpdir(), `student-twin-${id}.wav`);
  fs.writeFileSync(file, Buffer.from(wavBytes));
  pendingAudio.set(id, file);
  helper.stdin.write(`${JSON.stringify({ cmd: 'transcribe', id, path: file })}\n`);
}

// --- Confirm card (step 2d-ii) ------------------------------------------------------------------
// Irreversible tools write a request to <data>/confirm/pending and wait (mcp_servers/confirm.py).
// This process shows it as a card and writes the answer with the request's nonce; the nonce never
// reaches the page or the model. widget.alive tells the tools a widget is here to answer.
const TWIN_DATA = process.env.STUDENT_TWIN_HOME || path.join(process.env.LOCALAPPDATA, 'student-twin');
const CONFIRM_DIR = path.join(TWIN_DATA, 'confirm');
const PENDING_DIR = path.join(CONFIRM_DIR, 'pending');
const ANSWERS_DIR = path.join(CONFIRM_DIR, 'answers');
const ALIVE_FILE = path.join(CONFIRM_DIR, 'widget.alive');
const confirmShown = new Map(); // request id -> nonce

function touchAlive() {
  try {
    fs.mkdirSync(CONFIRM_DIR, { recursive: true });
    fs.writeFileSync(ALIVE_FILE, String(process.pid));
  } catch {}
}

function pollConfirm() {
  let names = [];
  try { names = fs.readdirSync(PENDING_DIR).filter((n) => n.endsWith('.json')); } catch {}
  const present = new Set();
  for (const name of names) {
    const id = name.slice(0, -5);
    present.add(id);
    if (confirmShown.has(id)) continue;
    let req;
    try { req = JSON.parse(fs.readFileSync(path.join(PENDING_DIR, name), 'utf8')); } catch { continue; }
    if (req.id !== id || !req.nonce) continue;
    confirmShown.set(id, req.nonce);
    showWidget();
    toRenderer('confirm:show', {
      id, summary: String(req.summary), details: (req.details || []).map(String),
      action: String(req.action || 'Confirm'), expires: Number(req.expires) * 1000,
    });
  }
  // A request the tool has removed (answered, timed out, or its server stopped) leaves the screen.
  for (const id of [...confirmShown.keys()]) {
    if (!present.has(id)) { confirmShown.delete(id); toRenderer('confirm:gone', { id }); }
  }
}

function answerConfirm(id, decision) {
  const nonce = confirmShown.get(id);
  if (!nonce) return;
  try {
    fs.mkdirSync(ANSWERS_DIR, { recursive: true });
    const file = path.join(ANSWERS_DIR, `${id}.json`);
    fs.writeFileSync(`${file}.tmp`, JSON.stringify({ nonce, decision: decision === 'confirm' ? 'confirm' : 'cancel' }));
    fs.renameSync(`${file}.tmp`, file);
  } catch {}
}

function trayIcon() {
  // 16x16 indigo dot, generated so the repo needs no binary asset for step 2a.
  const size = 16;
  const buf = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const i = (y * size + x) * 4;
    const inside = (x - 7.5) ** 2 + (y - 7.5) ** 2 <= 36;
    buf[i] = 0xe5; buf[i + 1] = 0x46; buf[i + 2] = 0x4f; buf[i + 3] = inside ? 255 : 0; // BGRA
  }
  return nativeImage.createFromBitmap(buf, { width: size, height: size });
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip('Student Twin');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show', click: showWidget },
    { label: 'Hide', click: hideWidget },
    { type: 'separator' },
    { label: 'Quit Student Twin', click: () => app.quit() },
  ]));
  tray.on('click', () => (hidden ? showWidget() : hideWidget()));
}

app.whenReady().then(() => {
  // Microphone only, only for this app's own page; nothing else (camera, screen, etc.).
  session.defaultSession.setPermissionRequestHandler((_wc, permission, cb, details) =>
    cb(permission === 'media' && (details.mediaTypes || []).every((t) => t === 'audio')));
  session.defaultSession.setPermissionCheckHandler((_wc, permission) => permission === 'media');
  createWindow();
  win.webContents.once('did-finish-load', startHelper);
  createTray();
  setInterval(pollReveal, REVEAL_POLL_MS);
  ipcMain.on('widget:hide', hideWidget);
  ipcMain.on('widget:want-focus', wantFocus);
  ipcMain.on('twin:ask', (e, { id, text }) => ask(e.sender, id, String(text).slice(0, 8000)));
  ipcMain.on('twin:cancel', (_e, { id }) => inflight.get(id)?.abort());
  ipcMain.on('mic:audio', (_e, { id, wav }) => transcribeWav(id, wav));
  ipcMain.on('confirm:answer', (_e, { id, decision }) => answerConfirm(String(id), decision));
  touchAlive();
  setInterval(touchAlive, 5000);
  setInterval(pollConfirm, 250);
});

// A tray app stays alive with its window hidden.
app.on('window-all-closed', (e) => e.preventDefault());
// Unhook the keyboard on exit, whatever the reason.
app.on('will-quit', () => {
  try { fs.rmSync(ALIVE_FILE, { force: true }); } catch {} // no widget left to answer a confirm card
  if (helper) { try { helper.stdin.write('{"cmd":"quit"}\n'); } catch {} helper.kill(); }
});

if (!app.requestSingleInstanceLock()) app.quit();
