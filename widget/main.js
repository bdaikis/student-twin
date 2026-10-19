// Student Twin widget — main process.
// Behaviour follows wiki/projects/student-twin/voice-widget-spec.md; the window and its look follow
// the Ghost Overlay UI Redesign (2026-10-18): one window that fits its content, anchored at the
// visible shape's top-right corner.
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage, session } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');

// One widget at a time. A second launch exits at once, before Chromium opens its caches (which
// the running copy holds, hence a burst of "Unable to move the cache" errors), and the running
// copy comes out of hiding instead.
if (!app.requestSingleInstanceLock()) {
  console.log('Student Twin is already running; showing it.');
  process.exit(0);
}
app.on('second-instance', () => showWidget());

// Hermes's OpenAI-compatible api_server, loopback only. Its key never leaves this process.
const HERMES_URL = 'http://127.0.0.1:8642/v1/chat/completions';
const HERMES_HEALTH = 'http://127.0.0.1:8642/health';
const HERMES_ENV = path.join(process.env.HERMES_HOME || path.join(process.env.LOCALAPPDATA, 'hermes'), '.env');
let sessionId = null; // X-Hermes-Session-Id: one conversation per widget run
const inflight = new Map(); // request id -> AbortController

// Geometry, in DIP. The renderer reports the visible shape's size; the window adds a transparent
// margin on every side for the CSS shadow and is placed so the shape's top-right corner stays put.
const MARGIN = 10;
const RIGHT_INSET = 20; // visible right edge, in from the work area's right edge
const TOP_OFFSET = 100; // visible top edge, down from the work area's top
const REST = 28; // the resting dot
const REVEAL_ZONE_W = 360; // top-edge reveal stretch, fixed whatever the widget's size
const REVEAL_POLL_MS = 100;
const REVEAL_DWELL_MS = 400;
const HIDE_FADE_MS = 200; // renderer fade before the window moves offscreen
const CAPTION_BUTTONS_DIP = 3 * 46; // minimise, maximise, close of a maximised window

let win = null;
let tray = null;
let hidden = false;
let anchor = null; // { right, top }: the visible shape's top-right corner when shown
let shape = { w: REST, h: REST }; // last size the renderer asked for
let dwellStart = null;
let hideTimer = null; // "Hide for 1 hour"

function defaultAnchor() {
  const { workArea } = screen.getPrimaryDisplay();
  return { right: workArea.x + workArea.width - RIGHT_INSET, top: workArea.y + TOP_OFFSET };
}

// Window bounds for a shape of size w x h at the anchor, kept inside the anchor's work area. A
// shape that would run past the bottom grows upward instead.
function boundsFor(w, h, at = anchor) {
  const width = Math.round(w) + 2 * MARGIN;
  const height = Math.round(h) + 2 * MARGIN;
  const { workArea } = screen.getDisplayNearestPoint({ x: Math.round(at.right - 1), y: Math.round(at.top) });
  let x = Math.round(at.right + MARGIN - width);
  let y = Math.round(at.top - MARGIN);
  x = Math.max(workArea.x - MARGIN, Math.min(x, workArea.x + workArea.width + MARGIN - width));
  y = Math.max(workArea.y - MARGIN, Math.min(y, workArea.y + workArea.height + MARGIN - height));
  return { x, y, width, height };
}

function place(bounds) {
  win.setBounds(bounds); // one atomic call: separate move and resize calls flicker
  // Windows enforces a minimum window size (about 51 x 50 DIP at 150%) and grows a smaller window
  // to the right; move it left by the excess so the shape's right edge stays on the anchor.
  const got = win.getBounds();
  if (got.width > bounds.width + 1) win.setBounds({ ...got, x: bounds.x - (got.width - bounds.width) });
}

function createWindow() {
  anchor = defaultAnchor();
  win = new BrowserWindow({
    ...boundsFor(REST, REST),
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false, // the shadow is CSS, inside the margin
    thickFrame: false, // stops DWM flashing the whole window on every resize
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    // Non-activating: clicks on the pill and cards work without taking focus from the app
    // Bhanu is using. Typing asks for focus explicitly (widget:want-focus).
    focusable: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // Moved offscreen to hide, the page is marked hidden and stops getting animation frames,
      // and Chromium doesn't always notice when it comes back; the layout depends on them.
      backgroundThrottling: false,
    },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  // Keep the widget out of screenshots and screen recordings (incl. the agent's own).
  win.setContentProtection(true);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => {
    place(boundsFor(REST, REST)); // applies the minimum-size correction from the start
    win.showInactive(); // never steal focus
  });
  // Give focus back the moment Bhanu leaves the typing box for another app.
  win.on('blur', () => { win.setFocusable(false); toRenderer('focus-lost'); });
  screen.on('display-metrics-changed', reanchor);
  screen.on('display-removed', reanchor);
}

// A display change can leave the anchor off every screen; pull it back to the nearest work area.
function reanchor() {
  if (!win) return;
  const { workArea } = screen.getDisplayNearestPoint({ x: Math.round(anchor.right), y: Math.round(anchor.top) });
  anchor = {
    right: Math.max(workArea.x + REST, Math.min(anchor.right, workArea.x + workArea.width)),
    top: Math.max(workArea.y, Math.min(anchor.top, workArea.y + workArea.height - REST)),
  };
  if (!hidden) place(boundsFor(shape.w, shape.h));
}

function applyLayout({ w, h }) {
  if (!win || !(w > 0 && h > 0)) return;
  shape = { w, h };
  if (hidden) return;
  const next = boundsFor(w, h);
  const cur = win.getBounds();
  // Changes of 2 px or less aren't worth a native resize.
  if (Math.abs(next.width - cur.width) <= 2 && Math.abs(next.height - cur.height) <= 2 &&
      Math.abs(next.x - cur.x) <= 2 && Math.abs(next.y - cur.y) <= 2) return;
  place(next);
}

// Dragging by the grip: follow the cursor from where the drag began. The page captures the
// pointer and says when it ends; the new spot becomes the anchor.
let dragging = null;
function drag(on) {
  if (!win || hidden) return;
  if (on && !dragging) {
    const start = screen.getCursorScreenPoint();
    // The size is fixed for the whole drag and only the position changes. Re-reading it each tick
    // made the window grow while the button was held (getBounds came back slightly larger than
    // the last setBounds at 150% scaling), and the widget crept right with no mouse movement.
    const b = win.getBounds();
    const startAnchor = { ...anchor };
    let last = { x: start.x, y: start.y };
    dragging = setInterval(() => {
      const p = screen.getCursorScreenPoint();
      if (p.x === last.x && p.y === last.y) return; // nothing moved, nothing to do
      last = p;
      win.setBounds({ x: b.x + p.x - start.x, y: b.y + p.y - start.y, width: b.width, height: b.height });
      anchor = { right: startAnchor.right + p.x - start.x, top: startAnchor.top + p.y - start.y };
    }, 16);
  } else if (!on && dragging) {
    clearInterval(dragging);
    dragging = null;
    // Pull it back inside the work area if dropped past an edge; the anchor follows from the
    // bounds asked for, not read back from Windows.
    const nb = boundsFor(shape.w, shape.h);
    place(nb);
    anchor = { right: nb.x + nb.width - MARGIN, top: nb.y + MARGIN };
  }
}

function wantFocus() {
  if (!win || hidden) return;
  win.setFocusable(true);
  win.focus();
  win.webContents.send('focus-granted');
}

// After a typed request is sent, hand the keyboard back to the app Bhanu was using. blur() alone
// lets Windows pick the next window (often the desktop), so the voice helper, which tracks the
// last foreground window, restores it.
function releaseFocus() {
  if (!win) return;
  win.setFocusable(false);
  if (helper) helper.stdin.write(`${JSON.stringify({ cmd: 'restore_focus' })}\n`);
  else win.blur();
}

// Hide: the renderer fades the shape out, then the window moves offscreen in one call and stops
// taking the mouse. The native window is never hidden: hide/show cycles have been tied to clicks
// being swallowed on non-activating windows and to black boxes in screen captures.
function hideWidget() {
  if (hidden || !win) return;
  hidden = true;
  dwellStart = null;
  toRenderer('visibility', false);
  setTimeout(() => {
    if (!hidden) return;
    const b = win.getBounds();
    const display = screen.getDisplayMatching(b);
    win.setIgnoreMouseEvents(true);
    place({ ...b, x: display.bounds.x + display.bounds.width + 50 });
    updateTray();
  }, HIDE_FADE_MS + 20);
}

function showWidget() {
  if (!win) return;
  if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  if (!hidden) return;
  hidden = false;
  place(boundsFor(shape.w, shape.h));
  win.setIgnoreMouseEvents(false);
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setContentProtection(true); // re-assert on every show
  if (!win.isVisible()) win.showInactive();
  toRenderer('visibility', true);
  updateTray();
}

function hideForAnHour() {
  hideWidget();
  hideTimer = setTimeout(() => { hideTimer = null; showWidget(); }, 60 * 60 * 1000);
}

// Top-edge reveal: a fixed stretch of the primary display's top edge above the widget's default
// position (not wherever it was dragged), minus the caption-button corner, after a short dwell.
// Polled, so no invisible strip window can swallow clicks on tabs or title bars.
function isTopEdgeOfDesktop(display) {
  // With monitors stacked vertically, only the top display's top edge counts.
  return !screen.getAllDisplays().some((d) =>
    d.id !== display.id &&
    d.bounds.y + d.bounds.height <= display.bounds.y &&
    d.bounds.x < display.bounds.x + display.bounds.width &&
    d.bounds.x + d.bounds.width > display.bounds.x);
}

function pollReveal() {
  if (!hidden || !win) { dwellStart = null; return; }
  const p = screen.getCursorScreenPoint();
  const display = screen.getPrimaryDisplay();
  const right = display.bounds.x + display.bounds.width;
  const zoneLeft = right - REVEAL_ZONE_W;
  const zoneRight = right - CAPTION_BUTTONS_DIP;
  const inZone = p.y <= display.bounds.y + 1 && p.x >= zoneLeft && p.x < zoneRight && isTopEdgeOfDesktop(display);
  // TODO(2e): suppress while a full-screen app is in front (SHQueryUserNotificationState).
  if (!inZone) { dwellStart = null; return; }
  if (dwellStart === null) dwellStart = Date.now();
  if (Date.now() - dwellStart >= REVEAL_DWELL_MS) { dwellStart = null; showWidget(); }
}

// Whether Hermes is up, for the resting pill's offline mark. /health needs no key.
let online = null;
async function pollOnline() {
  let ok = false;
  try { ok = (await fetch(HERMES_HEALTH, { signal: AbortSignal.timeout(3000) })).ok; } catch {}
  if (ok !== online) { online = ok; toRenderer('twin:online', ok); }
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
  "Prefer the desktop tools over screen control. Use the weather tool for weather, rain and air quality, and web search for other facts. For anything posted in a course use course_materials and open_course_item; for attendance use attendance; for his classes on a day use timetable_day. When he says he is in class (\"here\", \"check me in\"), call mark_attendance. To create a text file use write_text_file.",
  'To see or act inside an open window (a page in Chrome, a list in File Explorer, a button in an app), use ui_read first, then ui_click or ui_type by the element names it returns. Use web_extract only for pages that are not open on screen.',
  'When a window or app name matches nothing, check list_windows and ask whether he meant the closest-sounding one, naming it.',
  "Irreversible tools such as delete_files show him a confirm card themselves and wait for his click: call them directly, without asking for confirmation in chat, and never retry one he cancelled. When he cancels the card or it times out, reply with only ${DONE_MARK}: the card already shows the outcome. Never claim something was deleted unless the tool result says so.",
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
  if (channel === 'voice:status') lastVoiceStatus = payload;
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}
let lastVoiceStatus = null;

function startHelper() {
  let python;
  try { python = hermesPython(); } catch (err) {
    toRenderer('voice:status', { ok: false, message: "Voice is off: Hermes's Python wasn't found." });
    return;
  }
  helper = spawn(python, [path.join(__dirname, 'helper', 'voice_helper.py')], {
    env: { ...process.env, HERMES_HOME, PYTHONPATH: path.join(HERMES_HOME, 'hermes-agent'), PYTHONIOENCODING: 'utf-8',
      STUDENT_TWIN_WIDGET_PID: String(process.pid) },
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
  // 16x16 white dot with a dark outline, legible on light and dark taskbars. Generated, so the
  // repo needs no binary asset.
  const size = 16;
  const buf = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const i = (y * size + x) * 4;
    const r = Math.hypot(x - 7.5, y - 7.5);
    const [v, a] = r <= 4.5 ? [255, 255] : r <= 6 ? [24, 255] : [0, 0];
    buf[i] = v; buf[i + 1] = v; buf[i + 2] = v; buf[i + 3] = a; // BGRA
  }
  return nativeImage.createFromBitmap(buf, { width: size, height: size });
}

// The tray is the one place the product name appears.
function updateTray() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    hidden ? { label: 'Show', click: showWidget } : { label: 'Hide', click: hideWidget },
    { label: 'Hide for 1 hour', click: hideForAnHour, enabled: !hideTimer },
    { type: 'separator' },
    { label: 'Quit Student Twin', click: () => app.quit() },
  ]));
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip('Student Twin');
  updateTray();
  tray.on('click', () => (hidden ? showWidget() : hideWidget()));
}

app.whenReady().then(() => {
  // Microphone only, only for this app's own page; nothing else (camera, screen, etc.).
  session.defaultSession.setPermissionRequestHandler((_wc, permission, cb, details) =>
    cb(permission === 'media' && (details.mediaTypes || []).every((t) => t === 'audio')));
  session.defaultSession.setPermissionCheckHandler((_wc, permission) => permission === 'media');
  createWindow();
  win.webContents.once('did-finish-load', startHelper);
  // On every load (including a reload) tell the page what it may have missed.
  win.webContents.on('did-finish-load', () => {
    if (lastVoiceStatus) toRenderer('voice:status', lastVoiceStatus);
    if (online !== null) toRenderer('twin:online', online);
  });
  createTray();
  setInterval(pollReveal, REVEAL_POLL_MS);
  pollOnline();
  setInterval(pollOnline, 10000);
  ipcMain.on('widget:hide', hideWidget);
  ipcMain.on('widget:want-focus', wantFocus);
  ipcMain.on('widget:release-focus', releaseFocus);
  ipcMain.on('widget:layout', (_e, size) => applyLayout(size));
  ipcMain.on('widget:drag', (_e, on) => drag(on));
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

