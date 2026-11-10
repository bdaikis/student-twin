"""Student Twin voice helper: push-to-talk hook + local transcription.

Run by the widget with Hermes's Python (so faster-whisper comes from Hermes's own environment).
Protocol: JSON lines. Commands on stdin, events on stdout.

  events:   {"event": "ready"} | {"event": "press"} | {"event": "hold"} | {"event": "release"}
            {"event": "fullscreen", "busy": true|false} (a full-screen app is in front, or not)
            {"event": "tap"} | {"event": "transcript", "id", "ok", "text", "error", "ms"}
            {"event": "captured", "talk": {...}} | {"event": "captured", "cancelled": true}
            {"event": "snip", "phase": "arm"|"cancel"|"release"}   (the Ctrl gesture, below)
            {"event": "snip_ready", "source": "snipping"}   (a Win+Shift+S image reached the clipboard)
            {"event": "grabbed", "id", "ok": true, "width", "height"} | {"event": "grabbed", "id", "ok": false, "error"}
            {"event": "foreground", "id", "title", "process", "messaging", "desktop"}
            {"event": "pointer_clear", "reason": "input"|"focus"|"moved"|"closed"}
            {"event": "log", "message"}
  commands: {"cmd": "transcribe", "id": "...", "path": "C:/...wav"} | {"cmd": "restore_focus"}
            {"cmd": "foreground", "id": "..."}   (the window in front, for the widget's request rules)
            {"cmd": "pointer", "hwnd": 123 | null}   (a pointer is shown on that window, or gone)
            {"cmd": "configure", "talk": {...}, "hold_ms": 300, "snip": true}
            {"cmd": "capture", "cancel"?: true}
            {"cmd": "grab", "id": "...", "points": [[x, y], ...]} or "rect": [left, top, right, bottom]
                (physical pixels; points: a freeform trace, closed back to its start)
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

Snipping (2026-11-03, freeform since 11-04): holding Ctrl on its own and moving the pointer
traces a shape on the screen, which goes to the twin when Ctrl is released. This process only
tells Ctrl alone apart from Ctrl as part of a shortcut: "arm" when Ctrl goes down with no other
modifier, "cancel" as soon as another key, a mouse button or the wheel is used (Ctrl+C,
Ctrl+click, Ctrl+scroll), and "release" when Ctrl comes up still alone. The widget follows the
pointer and draws the trace, and asks for the pixels with "grab", which keeps what is inside the
shape and turns the rest white. Nothing is swallowed: Ctrl and everything pressed with it
reach the app as usual. It never arms while a full-screen app is in front or the pointer is
hidden (games hold Ctrl to crouch or sprint while turning). Win+Shift+S is watched too: the next
clipboard change within 90 s that holds an image is reported as "snip_ready".

The window in front (2026-11-11): "foreground" reports the last window Bhanu used that isn't the
widget, so a question about "this" can go straight to that window. A messaging window's title is
left out (mcp_servers/messaging.py). While the widget shows a pointer (desktop.py's point_at),
"pointer_clear" says it has gone stale: a click or the wheel, another window to the front, or the
pointed window moving, resizing, minimizing or closing.
"""
import ctypes
import ctypes.wintypes as wt
import importlib.util
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

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
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x0201, 0x0204, 0x0207
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
SNIP_CANCELS = {WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN, WM_MOUSEWHEEL, WM_MOUSEHWHEEL}
VK_LCONTROL, VK_RCONTROL, VK_S = 0xA2, 0xA3, 0x53
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


# Taps the hooks held back are re-sent from their own thread, never from inside a hook. SendInput
# waits for every low-level hook to see the input, this helper's own included. On 10 Nov 2026 a
# back-button tap was re-sent from the mouse hook under the push-to-talk lock; a back-button press
# that arrived while SendInput waited needed that lock, and the hook thread waited on itself. Push
# to talk, the keyboard hook, the settings capture and every other program's injected input stayed
# stuck until the helper was restarted.
_resends = queue.Queue()


def resend(*inputs):
    _resends.put(inputs)


def run_resends(jobs=_resends):
    while True:
        try:
            send_keys(*jobs.get())
        except Exception as exc:
            emit(event="log", message=f"re-send failed: {type(exc).__name__}")


def modifier_down():
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))


# --- Tap-hold state machine -------------------------------------------------------------------
class HookLock:
    """A lock as taken inside a hook callback: given up after HOOK_LOCK_S, when the event passes
    through untouched, rather than wait and hold up all input on the desktop."""
    HOOK_LOCK_S = 0.2

    def __init__(self, lock):
        self.lock = lock
        self.held = False

    def __enter__(self):
        self.held = self.lock.acquire(timeout=self.HOOK_LOCK_S)
        if not self.held:
            emit(event="log", message="push-to-talk busy: an input event passed through")
        return self.held

    def __exit__(self, *exc):
        if self.held:
            self.lock.release()


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
            resend(*resend_tap)
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
        with HookLock(self.lock) as held:
            if not held:
                return False
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
                resend(_key(self.vk, scan, extended=t_ext), _key(self.vk, scan, up=True, extended=t_ext),
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
        with HookLock(self.lock) as held:
            if not held:
                return False
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


# --- Snipping: Ctrl held alone + pointer movement, and Win+Shift+S -----------------------------
class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hCursor", wt.HANDLE), ("ptScreenPos", wt.POINT)]


CURSOR_SHOWING = 0x01
CLIPBOARD_IMAGE_FORMATS = (8, 17, 2)  # CF_DIB, CF_DIBV5, CF_BITMAP
SNIPPING_WATCH_S = 90
user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetClipboardSequenceNumber.restype = wt.DWORD
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
_fullscreen = False  # set by run_fullscreen_watch


def _down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def cursor_showing():
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    return not user32.GetCursorInfo(ctypes.byref(ci)) or bool(ci.flags & CURSOR_SHOWING)


class SnipGesture:
    IDLE, ARMED, BLOCKED = range(3)

    def __init__(self):
        self.enabled = True
        self.state = self.IDLE
        self.lock = threading.Lock()
        self.ctrl_up_seen = 0  # watchdog: consecutive looks that found Ctrl up while not IDLE
        self.watch = threading.Event()
        self.watch_seq = 0
        self.watch_until = 0.0

    def _set(self, state, phase=None):
        """Under the lock."""
        self.state = state
        if phase:
            emit(event="snip", phase=phase)
        _hook_jobs.put(None)  # the mouse hook is wanted only while armed

    def key(self, msg, kb):
        """Keyboard events from the hook. Never swallows anything, and stays fast."""
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        with self.lock:
            if kb.vkCode in (VK_LCONTROL, VK_RCONTROL, VK_CONTROL):
                if down:
                    if self.state == self.IDLE:  # a new press; key repeat finds it ARMED or BLOCKED
                        alone = not any(_down(vk) for vk in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN))
                        if self.enabled and alone and not _fullscreen and cursor_showing():
                            self._set(self.ARMED, "arm")
                        else:
                            self.state = self.BLOCKED
                elif self.state == self.ARMED:
                    self._set(self.IDLE, "release")
                else:
                    self.state = self.IDLE
                return
            if down and self.state == self.ARMED:
                self._set(self.BLOCKED, "cancel")  # Ctrl+C, Ctrl+Z, Ctrl+Shift...: a shortcut
            if down and kb.vkCode == VK_S and self.enabled and (_down(VK_LWIN) or _down(VK_RWIN)) and _down(VK_SHIFT):
                self.watch_seq = user32.GetClipboardSequenceNumber()
                self.watch_until = time.monotonic() + SNIPPING_WATCH_S
                self.watch.set()

    def mouse(self):
        """A button or the wheel was used: Ctrl+click and Ctrl+scroll are not snips."""
        with self.lock:
            if self.state == self.ARMED:
                self._set(self.BLOCKED, "cancel")

    def watchdog(self):
        """A Ctrl release the hook never saw (the secure desktop, a dropped hook) would leave the
        gesture stuck. Two looks half a second apart, so a release still on its way to the hook
        isn't mistaken for a lost one."""
        while True:
            time.sleep(0.5)
            self.watchdog_look()

    def watchdog_look(self):
        with self.lock:
            if self.state == self.IDLE or _down(VK_CONTROL):
                self.ctrl_up_seen = 0
                return
            self.ctrl_up_seen += 1
            if self.ctrl_up_seen >= 2:
                self.ctrl_up_seen = 0
                if self.state == self.ARMED:
                    self._set(self.IDLE, "cancel")
                else:
                    self.state = self.IDLE

    def clipboard_watch(self):
        """After Win+Shift+S: the next clipboard change, if it is an image, is the snip."""
        while True:
            self.watch.wait()
            time.sleep(0.2)
            with self.lock:
                seq, until = self.watch_seq, self.watch_until
            if time.monotonic() > until:
                self.watch.clear()
                continue
            if user32.GetClipboardSequenceNumber() == seq:
                continue
            time.sleep(0.3)  # let the Snipping Tool finish writing every format
            with self.lock:
                if self.watch_until != until:
                    continue  # pressed again meanwhile: wait for that snip instead
                self.watch.clear()
            if any(user32.IsClipboardFormatAvailable(f) for f in CLIPBOARD_IMAGE_FORMATS):
                emit(event="snip_ready", source="snipping")


snip = SnipGesture()
_hook_jobs = queue.Queue()  # mouse-hook installs and removals, kept off the hook threads


def mouse_wanted():
    return ptt.wants_mouse() or snip.state == SnipGesture.ARMED or pointer.active()


def run_hook_jobs():
    while True:
        _hook_jobs.get()
        if HOOKS:
            mouse_hook.sync(mouse_wanted())


CF_DIB, GMEM_MOVEABLE, HWND_MESSAGE = 8, 0x0002, wt.HWND(-3)
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]


def put_image_on_clipboard(image):
    """Copy an image the way the Snipping Tool does: PNG (which latest_screenshot reads first)
    plus a DIB for apps that only paste bitmaps. Done here because Electron's clipboard.writeImage
    hangs the widget's main process on this laptop (Electron 44.9.3, 3 Nov; text writes work)."""
    import io
    png, dib = io.BytesIO(), io.BytesIO()
    image.save(png, "PNG")
    image.convert("RGB").save(dib, "DIB")
    formats = ((user32.RegisterClipboardFormatW("PNG"), png.getvalue()), (CF_DIB, dib.getvalue()))
    # The clipboard needs an owner window for SetClipboardData; a message-only one is enough.
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        for _ in range(20):  # another app may have it open for a moment
            if user32.OpenClipboard(owner):
                break
            time.sleep(0.05)
        else:
            raise OSError("the clipboard is busy")
        try:
            user32.EmptyClipboard()
            for fmt, data in formats:
                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(fmt, handle):  # on success the clipboard owns it
                    kernel32.GlobalFree(handle)
                    raise OSError(f"SetClipboardData failed: error {ctypes.get_last_error()}")
        finally:
            user32.CloseClipboard()
    finally:
        if owner:
            user32.DestroyWindow(owner)


MAX_SNIP_POINTS = 5000


def cut_out(image, points, left, top):
    """Keep what is inside the traced shape (closed back to its start) and turn the rest white,
    as the Snipping Tool's freeform mode does. points are screen pixels; (left, top) is the
    image's top-left corner on the screen."""
    from PIL import Image, ImageDraw
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon([(x - left, y - top) for x, y in points], fill=255, outline=255)
    return Image.composite(image.convert("RGB"), Image.new("RGB", image.size, "white"), mask)


def grab(cmd):
    """A freeform shape ("points") or a rectangle ("rect") of the screen, in physical pixels, put
    on the clipboard for the twin's latest_screenshot tool; nothing is written to disk."""
    try:
        from PIL import ImageGrab
        points = None
        if "points" in cmd:
            points = [(int(x), int(y)) for x, y in cmd["points"]][:MAX_SNIP_POINTS]
            if len(points) < 3:
                raise ValueError("the trace is too short")
            xs, ys = [x for x, _ in points], [y for _, y in points]
            left, top, right, bottom = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        else:
            left, top, right, bottom = (int(v) for v in cmd["rect"])
        if right - left < 4 or bottom - top < 4:
            raise ValueError("the selection is too small")
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        if points:
            image = cut_out(image, points, left, top)
        put_image_on_clipboard(image)
        emit(event="grabbed", id=cmd.get("id"), ok=True, width=image.width, height=image.height)
    except Exception as exc:
        emit(event="grabbed", id=cmd.get("id"), ok=False, error=f"{type(exc).__name__}: {exc}")


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
                if not kb.flags & LLKHF_INJECTED:
                    snip.key(w_param, kb)  # watches only; never swallows
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
    """The mouse hook runs only while a side button is the talk key, a capture is waiting or Ctrl
    is held for a snip: it is called for every mouse move, and costs nothing when it isn't
    installed. Installed and removed from one thread (run_hook_jobs), never from a hook."""

    def __init__(self):
        self.thread_id = None
        self.proc = None
        self.lock = threading.Lock()

    def sync(self, wanted):
        with self.lock:
            if wanted and self.thread_id is None:
                self._start()
            elif not wanted and self.thread_id is not None:
                tid, self.thread_id = self.thread_id, None
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    def _start(self):
        ready = threading.Event()

        @HOOKPROC
        def proc(n_code, w_param, l_param):
            if n_code == 0 and w_param in SNIP_CANCELS:
                snip.mouse()  # Ctrl+click or Ctrl+scroll: not a snip; the event goes on as usual
                if pointer.hwnd is not None:
                    pointer.touched.set()  # the shown pointer goes; its watcher reports it, not this hook
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


# --- The window in front, for widget requests (2026-11-11) -------------------------------------
# The widget adds its title to each request, so "what does this mean?" can go straight to
# screen_look on it. Messaging windows are recognised by the same rule desktop.py refuses them with.
def _load_messaging():
    spec = importlib.util.spec_from_file_location(
        "student_twin_messaging", Path(__file__).resolve().parents[2] / "mcp_servers" / "messaging.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_messaging


is_messaging = _load_messaging()
DESKTOP_CLASSES = {"Progman", "WorkerW"}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _process(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(260), wt.DWORD(260)
        return os.path.basename(buf.value) if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def foreground_info(hwnd=None):
    """The last window Bhanu used that isn't the widget: {"title", "process", "messaging",
    "desktop"}, with no title for a messaging window; {} when there is none."""
    hwnd = hwnd or _last_other
    if not hwnd or not user32.IsWindow(hwnd):
        return {}
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in DESKTOP_CLASSES:
        return {"title": "", "process": "explorer.exe", "messaging": False, "desktop": True}
    title, process = " ".join(_title(hwnd).split()), _process(hwnd)
    messaging = is_messaging(title, process)
    return {"title": "" if messaging else title[:200], "process": process, "messaging": messaging, "desktop": False}


# --- A pointer the widget shows (desktop.py's point_at, 2026-11-11) ----------------------------
RECT = wt.RECT
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.IsIconic.argtypes = [wt.HWND]


def _rect(hwnd):
    r = RECT()
    return (r.left, r.top, r.right, r.bottom) if user32.GetWindowRect(hwnd, ctypes.byref(r)) else None


def _counts_as_front(hwnd):
    """Whether a foreground change to hwnd is Bhanu moving on: not the widget, not the taskbar
    (which takes the foreground for a moment when focus changes hands)."""
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    return pid.value != WIDGET_PID and cls.value not in TASKBAR_CLASSES


class Pointer:
    """Watches the window a pointer is shown on, every POLL_S, and reports once when the pointer
    should go. The mouse hook only sets `touched` (a click or the wheel); it never waits here."""
    POLL_S = 0.1

    def __init__(self):
        self.hwnd = None
        self.rect = None
        self.front = None  # the foreground window when the pointer appeared
        self.lock = threading.Lock()
        self.touched = threading.Event()
        self.wake = threading.Event()

    def active(self):
        return self.hwnd is not None

    def show(self, hwnd):
        with self.lock:
            self.touched.clear()
            self.hwnd = hwnd if hwnd and user32.IsWindow(hwnd) else None
            self.rect = _rect(hwnd) if self.hwnd else None
            self.front = user32.GetForegroundWindow()
        _hook_jobs.put(None)  # the mouse hook is wanted while a pointer is shown
        self.wake.set()

    def stale(self):
        """Why the shown pointer should go, or None."""
        hwnd = self.hwnd
        if hwnd is None:
            return None
        if self.touched.is_set():
            return "input"
        if not user32.IsWindow(hwnd):
            return "closed"
        if user32.IsIconic(hwnd) or _rect(hwnd) != self.rect:
            return "moved"
        fg = user32.GetForegroundWindow()
        if fg not in (self.front, hwnd) and _counts_as_front(fg):
            return "focus"
        return None

    def check(self):
        with self.lock:
            reason = self.stale()
            if reason:
                self.hwnd = None
                emit(event="pointer_clear", reason=reason)
        if reason:
            _hook_jobs.put(None)
        return reason

    def watch(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            while self.hwnd is not None:
                time.sleep(self.POLL_S)
                self.check()


pointer = Pointer()


# --- Full-screen detection, so the widget's top-edge reveal stays out of games and videos --------
# Windows sends no event when a full-screen app starts, so ask once a second and report changes.
# QUNS_BUSY (2), QUNS_RUNNING_D3D_FULL_SCREEN (3), QUNS_PRESENTATION_MODE (4), QUNS_APP (7: a Store
# app full screen) mean something full screen is in front.
FULLSCREEN_STATES = {2, 3, 4, 7}


def run_fullscreen_watch():
    global _fullscreen
    shell32 = ctypes.WinDLL("shell32")
    state = ctypes.c_int()
    last = None
    while True:
        busy = shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in FULLSCREEN_STATES
        _fullscreen = busy
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


def _private_stdin():
    """Commands arrive on stdin, but child processes must not inherit that pipe: Hermes's import
    runs `git` (version_info), and on Windows a child sharing a pipe this process is blocked
    reading from waits until the widget writes a line, so the speech model never loaded. Read
    from a private copy and point the standard input at NUL."""
    commands = open(os.dup(0), encoding="utf-8", errors="replace")
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)
    os.close(nul)
    return commands


_commands_in = None


def run_commands():
    for line in _commands_in:
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
        elif cmd.get("cmd") == "foreground":
            try:
                info = foreground_info()
            except Exception as exc:
                info = {}
                emit(event="log", message=f"foreground failed: {type(exc).__name__}")
            emit(event="foreground", id=cmd.get("id"), **info)
        elif cmd.get("cmd") == "pointer":
            hwnd = cmd.get("hwnd")
            pointer.show(hwnd if isinstance(hwnd, int) else None)
        elif cmd.get("cmd") == "configure":
            ptt.configure(cmd.get("talk"), cmd.get("hold_ms"))
            if "snip" in cmd:
                snip.enabled = cmd["snip"] is not False
            _hook_jobs.put(None)
        elif cmd.get("cmd") == "grab":
            threading.Thread(target=grab, args=(cmd,), daemon=True).start()
        elif cmd.get("cmd") == "capture":
            if cmd.get("cancel"):
                ptt.cancel_capture()
            elif not HOOKS:
                emit(event="captured", cancelled=True)
            else:
                ptt.capture()
                _hook_jobs.put(None)
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
    global _commands_in
    _commands_in = _private_stdin()
    threading.Thread(target=_writer, daemon=True).start()
    try:
        start = json.loads(os.environ.get("STUDENT_TWIN_VOICE") or "{}")
    except ValueError:
        start = {}
    ptt.configure(start.get("talk"), start.get("hold_ms"))
    snip.enabled = start.get("snip", True) is not False
    threading.Thread(target=run_resends, daemon=True).start()
    if HOOKS:
        threading.Thread(target=run_hook, daemon=True).start()
        mouse_hook.sync(mouse_wanted())
    threading.Thread(target=run_hook_jobs, daemon=True).start()
    threading.Thread(target=snip.watchdog, daemon=True).start()
    threading.Thread(target=snip.clipboard_watch, daemon=True).start()
    threading.Thread(target=run_focus_tracker, daemon=True).start()
    threading.Thread(target=pointer.watch, daemon=True).start()
    threading.Thread(target=run_fullscreen_watch, daemon=True).start()
    # Commands are read from the start, so a settings change or a capture needn't wait for the model.
    threading.Thread(target=run_commands, daemon=True).start()
    emit(event="ready")
    warm_up()
    run_transcriber()


if __name__ == "__main__":
    main()
