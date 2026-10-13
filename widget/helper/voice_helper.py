"""Student Twin voice helper: numpad push-to-talk hook + local transcription.

Run by the widget with Hermes's Python (so faster-whisper comes from Hermes's own environment).
Protocol: JSON lines. Commands on stdin, events on stdout.

  events:   {"event": "ready"} | {"event": "press"} | {"event": "hold"} | {"event": "release"}
            {"event": "tap"} | {"event": "transcript", "id", "ok", "text", "error", "ms"}
            {"event": "log", "message"}
  commands: {"cmd": "transcribe", "id": "...", "path": "C:/...wav"} | {"cmd": "quit"}

Push-to-talk follows wiki/projects/student-twin/voice-widget-spec.md v0.6, section 3: the numpad
'.'/'Del' key (scan code 0x53, NOT extended) is held back on press; released before the threshold it
is re-sent as a normal tap; held past it, it means "talk". The main Delete key (0x53 + extended flag)
and the key pressed with any modifier are never touched.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time

HOLD_THRESHOLD_S = 0.30
TARGET_SCAN = 0x53

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
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
INPUT_KEYBOARD = 1
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
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
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


def _key(vk, scan, up=False, extended=False):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


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

    def _threshold(self):
        with self.lock:
            if self.state == self.PENDING:
                self.state = self.HOLDING
                emit(event="hold")

    def handle(self, msg, kb):
        """Return True to swallow the event. Must stay fast: Windows drops slow hooks."""
        if kb.flags & LLKHF_INJECTED:
            return False  # our own re-sent keys (and other software's) pass through
        is_target = kb.scanCode == TARGET_SCAN and not (kb.flags & LLKHF_EXTENDED)
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        with self.lock:
            if is_target:
                if down:
                    if self.state == self.IDLE:
                        if modifier_down():
                            self.state = self.PASSTHROUGH  # a shortcut, not push-to-talk
                            return False
                        self.state, self.vk = self.PENDING, kb.vkCode
                        self.timer = threading.Timer(HOLD_THRESHOLD_S, self._threshold)
                        self.timer.start()
                        emit(event="press")
                        return True
                    return self.state != self.PASSTHROUGH  # swallow key-repeat while pending/holding
                # key up
                state, self.state = self.state, self.IDLE
                if state == self.PENDING:
                    self.timer.cancel()
                    send_keys(_key(self.vk, TARGET_SCAN), _key(self.vk, TARGET_SCAN, up=True))
                    emit(event="tap")
                    return True
                if state == self.HOLDING:
                    emit(event="release")
                    return True
                return False  # PASSTHROUGH (or stray up): let it through
            # Another key while the numpad key is held back: it was a typed '.', keep the order.
            if down and self.state == self.PENDING:
                self.timer.cancel()
                self.state = self.PASSTHROUGH
                ext = bool(kb.flags & LLKHF_EXTENDED)
                send_keys(_key(self.vk, TARGET_SCAN), _key(self.vk, TARGET_SCAN, up=True),
                          _key(kb.vkCode, kb.scanCode, extended=ext))
                emit(event="tap")
                return True
        return False


def run_hook():
    ptt = PushToTalk()

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
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# --- Transcription (Hermes's own faster-whisper path) -----------------------------------------
def transcribe(path):
    from tools.transcription_tools import transcribe_audio  # imported lazily: heavy
    return transcribe_audio(path, source="student-twin-widget")


def run_commands():
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        if cmd.get("cmd") == "quit":
            os._exit(0)
        if cmd.get("cmd") == "transcribe":
            t0 = time.perf_counter()
            try:
                result = transcribe(cmd["path"])
            except Exception as exc:
                result = {"success": False, "transcript": "", "error": f"{type(exc).__name__}: {exc}"}
            emit(event="transcript", id=cmd.get("id"), ok=bool(result.get("success")),
                 text=(result.get("transcript") or "").strip(), error=result.get("error"),
                 ms=round((time.perf_counter() - t0) * 1000))
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


def main():
    threading.Thread(target=_writer, daemon=True).start()
    if "--no-hook" not in sys.argv:
        threading.Thread(target=run_hook, daemon=True).start()
    emit(event="ready")
    warm_up()
    run_commands()


if __name__ == "__main__":
    main()
