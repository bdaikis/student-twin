"""Student Twin voice helper: push-to-talk hook + local transcription.

Run by the widget with Hermes's Python (so faster-whisper comes from Hermes's own environment).
Protocol: JSON lines. Commands on stdin, events on stdout.

  events:   {"event": "ready"} | {"event": "press"} | {"event": "hold"} | {"event": "release"}
            {"event": "fullscreen", "busy": true|false} (a full-screen app is in front, or not)
            {"event": "tap"} | {"event": "transcript", "id", "ok", "text", "error", "ms"}
            {"event": "captured", "talk": {...}} | {"event": "captured", "cancelled": true}
            {"event": "log", "message"}
  commands: {"cmd": "transcribe", "id": "...", "path": "C:/...wav"} | {"cmd": "restore_focus"}
            {"cmd": "configure", "talk": {...}, "hold_ms": 300} | {"cmd": "capture", "cancel"?: true}
            {"cmd": "quit"}

restore_focus gives the keyboard back to the app that was in front before the widget took focus
for typing. Electron can only blur its window, which hands focus to whatever Windows picks (often
the desktop), so this process tracks the last foreground window that isn't the widget's.

Push-to-talk follows wiki/projects/student-twin/voice-widget-spec.md, section 3. The talk key is
set in the widget's settings (per machine, <data>/widget.json) and arrives in STUDENT_TWIN_VOICE at
start and in "configure" afterwards:
  {"device": "key", "scan": 83, "extended": false}   a keyboard key, by scan code and extended flag
  {"device": "mouse", "button": 1}                    a mouse side button (1 = back, 2 = forward)
The default is the numpad '.'/'Del' key (scan code 0x53, NOT extended; the main Delete key is 0x53
with the extended flag). The press is held back; released before the hold time it is re-sent as a
normal tap; held past it, it means "talk". A press made with any modifier down is never touched.
"capture" reports the next key or side button pressed (Esc cancels) instead of acting on it.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time

NUMPAD_DOT = {"device": "key", "scan": 0x53, "extended": False}
CAPTURE_TIMEOUT_S = 15

# The protocol owns the real stdout; anything else that prints (Hermes logging) goes to stderr.
_protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr
_out = queue.Queue()


def emit(**event):
    _out.put(event)


def _writer():
    while True:
        event = _out.get()
        _protocol.write(json.dumps(event) + "\n")
        _protocol.flush()


# --- Win32 ------------------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_QUIT = 0x0012
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
LLMHF_INJECTED = 0x01
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP = 0x0080, 0x0100
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_ESCAPE = 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x1B
SIDE_BUTTONS = {1: "Mouse back button", 2: "Mouse forward button"}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyNameTextW.argtypes = [wt.LONG, wt.LPWSTR, ctypes.c_int]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


def _key(vk, scan, up=False, extended=False):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


def _side_button(button, up=False):
    flags = MOUSEEVENTF_XUP if up else MOUSEEVENTF_XDOWN
    return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(mouseData=button, dwFlags=flags)))


def key_name(scan, extended):
    """What Windows calls the key on this keyboard layout ("Right Alt", "Insert", ...)."""
    if scan == 0x53 and not extended:
        return "Numpad ."
    buf = ctypes.create_unicode_buffer(64)
    n = user32.GetKeyNameTextW(((scan & 0xFF) << 16) | (1 << 24 if extended else 0), buf, 64)
    return buf.value[:n] if n else f"Key {scan:#04x}"


def valid_talk(talk):
    """The binding if it is well formed, else None."""
    if not isinstance(talk, dict):
        return None
    if talk.get("device") == "mouse" and talk.get("button") in SIDE_BUTTONS:
        return {"device": "mouse", "button": talk["button"]}
    if talk.get("device") == "key" and isinstance(talk.get("scan"), int) and 0 < talk["scan"] < 0x100:
        return {"device": "key", "scan": talk["scan"], "extended": bool(talk.get("extended"))}
    return None


def send_keys(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def modifier_down():
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))


# --- Tap-hold state machine -------------------------------------------------------------------
class PushToTalk:
    IDLE, PENDING, HOLDING, PASSTHROUGH = range(4)

    def __init__(self):
        self.state = self.IDLE
        self.vk = 0
        self.lock = threading.Lock()
        self.timer = None
        self.talk = dict(NUMPAD_DOT)
        self.hold_s = 0.30
        self.capturing = None  # the capture's timeout timer while waiting for a key or button
        self.swallow_up = None  # the captured key or button, whose release must not reach the app

    def configure(self, talk=None, hold_ms=None):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.state = self.IDLE
            self.talk = valid_talk(talk) or self.talk
            if isinstance(hold_ms, (int, float)) and 100 <= hold_ms <= 2000:
                self.hold_s = hold_ms / 1000

    def wants_mouse(self):
        return self.talk["device"] == "mouse" or self.capturing is not None

    def capture(self):
        with self.lock:
            if self.capturing:
                self.capturing.cancel()
            self.capturing = threading.Timer(CAPTURE_TIMEOUT_S, self._capture_timeout)
            self.capturing.start()

    def _capture_timeout(self):
        with self.lock:
            if self.capturing:
                self.capturing = None
                emit(event="captured", cancelled=True)

    def cancel_capture(self):
        with self.lock:
            if self.capturing:
                self.capturing.cancel()
                self.capturing = None
                emit(event="captured", cancelled=True)

    def _captured(self, talk, swallow):
        """Under the lock: report what was pressed; its release is swallowed too."""
        self.capturing.cancel()
        self.capturing = None
        self.swallow_up = swallow
        if talk is None:
            emit(event="captured", cancelled=True)
        else:
            name = SIDE_BUTTONS[talk["button"]] if talk["device"] == "mouse" else key_name(talk["scan"], talk["extended"])
            emit(event="captured", talk={**talk, "name": name})
        return True

    def _threshold(self):
        with self.lock:
            if self.state == self.PENDING:
                self.state = self.HOLDING
                emit(event="hold")

    def _target_down(self, vk=0):
        """Under the lock: the talk key or button went down. Returns True to swallow it."""
        if self.state == self.IDLE:
            if modifier_down():
                self.state = self.PASSTHROUGH  # a shortcut, not push-to-talk
                return False
            self.state, self.vk = self.PENDING, vk
            self.timer = threading.Timer(self.hold_s, self._threshold)
            self.timer.start()
            emit(event="press")
            return True
        return self.state != self.PASSTHROUGH  # swallow key-repeat while pending/holding

    def _target_up(self, resend_tap):
        state, self.state = self.state, self.IDLE
        if state == self.PENDING:
            self.timer.cancel()
            send_keys(*resend_tap)
            emit(event="tap")
            return True
        if state == self.HOLDING:
            emit(event="release")
            return True
        return False  # PASSTHROUGH (or stray up): let it through

    def handle(self, msg, kb):
        """Keyboard events. Return True to swallow the event. Must stay fast: Windows drops slow hooks."""
        if kb.flags & LLKHF_INJECTED:
            return False  # our own re-sent keys (and other software's) pass through
        ext = bool(kb.flags & LLKHF_EXTENDED)
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        with self.lock:
            if self.capturing and down:
                if kb.vkCode == VK_ESCAPE:
                    return self._captured(None, ("key", kb.scanCode, ext))
                return self._captured({"device": "key", "scan": kb.scanCode, "extended": ext}, ("key", kb.scanCode, ext))
            if self.swallow_up == ("key", kb.scanCode, ext):
                if not down:
                    self.swallow_up = None
                return True
            talk = self.talk
            if talk["device"] != "key":
                return False
            scan, t_ext = talk["scan"], talk["extended"]
            if kb.scanCode == scan and ext == t_ext:
                if down:
                    return self._target_down(kb.vkCode)
                return self._target_up((_key(self.vk, scan, extended=t_ext), _key(self.vk, scan, up=True, extended=t_ext)))
            # Another key while the talk key is held back: it was typed (a '.' before a '5'), keep the order.
            if down and self.state == self.PENDING:
                self.timer.cancel()
                self.state = self.PASSTHROUGH
                send_keys(_key(self.vk, scan, extended=t_ext), _key(self.vk, scan, up=True, extended=t_ext),
                          _key(kb.vkCode, kb.scanCode, extended=ext))
                emit(event="tap")
                return True
        return False

    def handle_mouse(self, msg, ms):
        """Mouse events: only side buttons matter. Called for every mouse move, so it returns early."""
        if msg != WM_XBUTTONDOWN and msg != WM_XBUTTONUP:
            return False
        if ms.flags & LLMHF_INJECTED:
            return False
        button = (ms.mouseData >> 16) & 0xFFFF
        down = msg == WM_XBUTTONDOWN
        with self.lock:
            if self.capturing and down and button in SIDE_BUTTONS:
                return self._captured({"device": "mouse", "button": button}, ("mouse", button))
            if self.swallow_up == ("mouse", button):
                if not down:
                    self.swallow_up = None
                return True
            if self.talk["device"] != "mouse" or button != self.talk["button"]:
                return False
            if down:
                return self._target_down()
            return self._target_up((_side_button(button), _side_button(button, up=True)))


ptt = PushToTalk()


def _message_loop():
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def run_hook():
    @HOOKPROC
    def proc(n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            try:
                if ptt.handle(w_param, kb):
                    return 1
            except Exception as exc:  # never let an error kill keyboard input
                emit(event="log", message=f"hook error: {type(exc).__name__}")
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        emit(event="log", message=f"keyboard hook failed: error {ctypes.get_last_error()}")
        return
    _message_loop()


class MouseHook:
    """The mouse hook runs only while a side button is the talk key or a capture is waiting: it is
    called for every mouse move, and costs nothing when it isn't installed."""

    def __init__(self):
        self.thread_id = None
        self.proc = None

    def sync(self, wanted):
        if wanted and self.thread_id is None:
            self._start()
        elif not wanted and self.thread_id is not None:
            tid, self.thread_id = self.thread_id, None
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    def _start(self):
        ready = threading.Event()

        @HOOKPROC
        def proc(n_code, w_param, l_param):
            if n_code == 0 and (w_param == WM_XBUTTONDOWN or w_param == WM_XBUTTONUP):
                ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                try:
                    if ptt.handle_mouse(w_param, ms):
                        return 1
                except Exception as exc:  # never let an error kill mouse input
                    emit(event="log", message=f"mouse hook error: {type(exc).__name__}")
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        def run():
            hook = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, kernel32.GetModuleHandleW(None), 0)
            if not hook:
                emit(event="log", message=f"mouse hook failed: error {ctypes.get_last_error()}")
                ready.set()
                return
            self.thread_id = kernel32.GetCurrentThreadId()
            ready.set()
            _message_loop()
            user32.UnhookWindowsHookEx(hook)

        self.proc = proc  # keep the callback alive
        threading.Thread(target=run, daemon=True).start()
        ready.wait(2)


mouse_hook = MouseHook()


# --- Foreground tracking, for handing the keyboard back after typing ---------------------------
# The widget's main process owns its window. Not os.getppid(): a venv's python.exe is a launcher
# that starts the real interpreter as its own child.
WIDGET_PID = int(os.environ.get("STUDENT_TWIN_WIDGET_PID") or os.getppid())
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
user32.SetWinEventHook.restype = wt.HANDLE
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_last_other = None  # last foreground window that isn't the widget's
TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _note_foreground(hwnd):
    global _last_other
    if not hwnd:
        return
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    # The taskbar takes the foreground for a moment when focus changes hands; it is never where
    # the keyboard should go back to. Nor is an invisible window, or the widget itself.
    if pid.value != WIDGET_PID and user32.IsWindowVisible(hwnd) and cls.value not in TASKBAR_CLASSES:
        _last_other = hwnd


def run_focus_tracker():
    @WINEVENTPROC
    def on_foreground(_hook, _event, hwnd, _obj, _child, _thread, _time):
        _note_foreground(hwnd)

    _note_foreground(user32.GetForegroundWindow())
    if not user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, on_foreground, 0, 0, 0):
        emit(event="log", message="foreground tracking failed")
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.DispatchMessageW(ctypes.byref(msg))


def restore_focus():
    hwnd = _last_other
    if not hwnd or not user32.IsWindow(hwnd):
        return
    # If Bhanu has already moved on to another app, leave him there.
    fg = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(fg, cls, 64)
    if fg and pid.value != WIDGET_PID and cls.value not in TASKBAR_CLASSES:
        return
    # Only the process that received the last input may change the foreground window; an empty
    # mouse input sent from here counts as that (the workaround desktop.py's _focus uses).
    empty = INPUT(type=0)  # INPUT_MOUSE
    user32.SendInput(1, ctypes.byref(empty), ctypes.sizeof(INPUT))
    user32.SetForegroundWindow(hwnd)


# --- Full-screen detection, so the widget's top-edge reveal stays out of games and videos --------
# Windows sends no event when a full-screen app starts, so ask once a second and report changes.
# QUNS_BUSY (2), QUNS_RUNNING_D3D_FULL_SCREEN (3), QUNS_PRESENTATION_MODE (4), QUNS_APP (7: a Store
# app full screen) mean something full screen is in front.
FULLSCREEN_STATES = {2, 3, 4, 7}


def run_fullscreen_watch():
    shell32 = ctypes.WinDLL("shell32")
    state = ctypes.c_int()
    last = None
    while True:
        busy = shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in FULLSCREEN_STATES
        if busy != last:
            emit(event="fullscreen", busy=busy)
            last = busy
        time.sleep(1)


# --- Transcription (Hermes's own faster-whisper path) -----------------------------------------
def transcribe(path):
    from tools.transcription_tools import transcribe_audio  # imported lazily: heavy
    return transcribe_audio(path, source="student-twin-widget")


_jobs = queue.Queue()


def run_transcriber():
    while True:
        cmd = _jobs.get()
        t0 = time.perf_counter()
        try:
            result = transcribe(cmd["path"])
        except Exception as exc:
            result = {"success": False, "transcript": "", "error": f"{type(exc).__name__}: {exc}"}
        emit(event="transcript", id=cmd.get("id"), ok=bool(result.get("success")),
             text=(result.get("transcript") or "").strip(), error=result.get("error"),
             ms=round((time.perf_counter() - t0) * 1000))


def run_commands():
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        if cmd.get("cmd") == "quit":
            os._exit(0)
        if cmd.get("cmd") == "transcribe":
            _jobs.put(cmd)  # transcribed in order on its own thread, so focus commands never wait
        elif cmd.get("cmd") == "restore_focus":
            restore_focus()
        elif cmd.get("cmd") == "configure":
            ptt.configure(cmd.get("talk"), cmd.get("hold_ms"))
            if HOOKS:
                mouse_hook.sync(ptt.wants_mouse())
        elif cmd.get("cmd") == "capture":
            if cmd.get("cancel"):
                ptt.cancel_capture()
            elif not HOOKS:
                emit(event="captured", cancelled=True)
            else:
                ptt.capture()
                mouse_hook.sync(True)
    os._exit(0)  # widget closed our stdin


def warm_up():
    """Load the whisper model now so the first spoken command doesn't wait for it."""
    import tempfile
    import wave
    path = os.path.join(tempfile.gettempdir(), "student-twin-warmup.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)  # 0.5 s of silence
    t0 = time.perf_counter()
    try:
        transcribe(path)
    except Exception:
        pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    emit(event="log", message=f"speech model ready in {time.perf_counter() - t0:.1f}s")


HOOKS = "--no-hook" not in sys.argv


def main():
    threading.Thread(target=_writer, daemon=True).start()
    try:
        start = json.loads(os.environ.get("STUDENT_TWIN_VOICE") or "{}")
    except ValueError:
        start = {}
    ptt.configure(start.get("talk"), start.get("hold_ms"))
    if HOOKS:
        threading.Thread(target=run_hook, daemon=True).start()
        mouse_hook.sync(ptt.wants_mouse())
    threading.Thread(target=run_focus_tracker, daemon=True).start()
    threading.Thread(target=run_fullscreen_watch, daemon=True).start()
    # Commands are read from the start, so a settings change or a capture needn't wait for the model.
    threading.Thread(target=run_commands, daemon=True).start()
    emit(event="ready")
    warm_up()
    run_transcriber()


if __name__ == "__main__":
    main()
