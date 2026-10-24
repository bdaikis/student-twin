"""Student Twin desktop tools: direct, reversible actions on this computer (slice 2, step 2d-i).

An MCP server Hermes starts over stdio. Enabled for the desktop widget and the local CLI only,
never for Telegram (platform_toolsets.telegram includes `no_mcp`).

Most tools here are reversible: opening things, media controls, moving, focusing and closing
windows (closing asks the program, as its X button does, so it can still offer to save), listing
files, creating new text files (never overwriting), reading attendance, and reading or clicking a
window's labelled elements through UI Automation (ui_read, ui_click, ui_type), which refuse
messaging windows and elements that look irreversible. Irreversible tools (step 2d-ii:
delete_files) act only after Bhanu clicks Confirm on the widget's confirm card; see confirm.py.
Screen control (screen_look, screen_click, screen_type, screen_scroll) is the last resort, for what
the accessibility tree can't reach, and refuses the same windows and elements.

Course contents come from the portal's Moodle API through moodle.py (read-only allowlist).
Per-user settings (course pages, browser profiles, default music, home city) come from
%LOCALAPPDATA%\\student-twin\\config.json.
Runs in the isolated student-twin venv: mcp, winrt-Windows.Media.Control, uiautomation.
"""
import asyncio
import ctypes
import io
import ctypes.wintypes as wt
import difflib
import json
import os
import re
import subprocess
import time
import urllib.request
import winreg
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from mcp.server.mcpserver import Context, MCPServer

from common import CONFIG_PATH, config as _config, course_contents as _course_contents
from common import match_course as _match_course, norm as _norm


server = MCPServer(
    "desktop",
    instructions=(
        "Direct actions on Bhanu's Windows computer. Prefer these over screen control: they are fast "
        "and reliable. Use the weather tool for weather, rain and air quality; web search for other facts. "
        "For anything posted in a course (notes, slides, lectures, tutorials) use course_materials and "
        "open_course_item. Spoken commands arrive as speech-recognition transcripts and can mishear "
        "names: when a name matches nothing, check list_windows and ask whether Bhanu meant the "
        "closest-sounding window or app, naming it."
    ),
)

# Coordinates in this process are physical pixels on every monitor.
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
user32 = ctypes.windll.user32
psapi_k32 = ctypes.windll.kernel32
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

BROWSERS = ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "vivaldi.exe")

# Programs whose windows usually hold Bhanu's work: documents, code, other tabs, a terminal session.
# The twin closes one of these only if it opened that window itself, or when he names its exact title.
WORK_APPS = set(BROWSERS) | {
    "notepad.exe", "notepad++.exe", "code.exe", "cursor.exe", "sublime_text.exe", "devenv.exe",
    "pycharm64.exe", "idea64.exe", "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe",
    "acrobat.exe", "acrord32.exe", "obsidian.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe",
    "wt.exe", "matlab.exe", "jupyter-lab.exe",
}

# Windows the twin opened while this server runs: hwnd -> {"kind": "window" or "tab", "title", "at"}.
# "window": a new window it opened. "tab": a page it opened as a new tab in a window that was
# already open, so closing it must close that tab only.
_OPENED: dict[int, dict] = {}


def _remember_opened(hwnd, kind: str, title: str) -> None:
    for h in [h for h in _OPENED if not user32.IsWindow(h)]:
        del _OPENED[h]
    _OPENED[hwnd] = {"kind": kind, "title": title, "at": time.time()}


# --- Opening things ---------------------------------------------------------------------------
def _default_browser_exe() -> str | None:
    """The executable behind the default https handler, e.g. chrome.exe."""
    try:
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command") as k:
            command = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', command)
    return (m.group(1) or m.group(2)) if m else None


def _browser_profile(url: str) -> str | None:
    """The browser profile configured for this site (config "browser_profiles": {host: profile folder}),
    so a site opens where Bhanu is signed in to it."""
    host = (urlparse(url).hostname or "").lower()
    for site, profile in _config().get("browser_profiles", {}).items():
        site = site.lower()
        if host == site or host.endswith("." + site):
            return profile
    return None


def _open_web(url: str) -> str:
    """Open `url` in its configured browser profile (default browser otherwise) and bring that
    browser window to the front. Returns a sentence for the tool result."""
    before = {w["hwnd"]: w["title"] for w in _windows()}
    profile = _browser_profile(url)
    exe = _default_browser_exe() if profile else None
    # --profile-directory is a Chromium switch (Chrome, Edge, Brave); other browsers ignore the profile.
    if exe and Path(exe).name.lower() in ("chrome.exe", "msedge.exe", "brave.exe"):
        # stdio must not be inherited: this server's stdout is the MCP channel.
        subprocess.Popen([exe, f"--profile-directory={profile}", url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        where = f"the browser profile {profile!r}"
    else:
        os.startfile(url)
        where = "the browser"
    # A new tab changes its window's title; a new window is a new handle. Either one is the target.
    win = _wait_for(lambda: next((w for w in _windows() if w["process"].lower() in BROWSERS
                                  and before.get(w["hwnd"]) != w["title"]), None), 6)
    if win is None:
        return f"Opened {url} in {where}, but couldn't find its window to bring forward."
    focused = _focus(win["hwnd"])
    # While the page loads the window is titled "Untitled" or "New Tab"; wait for the real title so
    # later commands (arrange, maximize, close tab) can find the window by the page's name.
    title = _wait_for(lambda: (lambda t: t if t and not _loading_title(t, url) else None)(_window_title(win["hwnd"])), 8)
    title = title or _window_title(win["hwnd"])
    _remember_opened(win["hwnd"], "window" if win["hwnd"] not in before else "tab", title)
    return (f"Opened {url} in {where}" + ("" if focused else ", but Windows didn't let me bring it to the front")
            + f". Its window is titled {title!r}.")


def _window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _loading_title(title: str, url: str) -> bool:
    page = re.sub(r"\s+-\s+(Google Chrome|Microsoft​? Edge|Brave|Mozilla Firefox)$", "", title).strip()
    host = urlparse(url).hostname or ""
    # A PDF first shows its address ("host/path/file.pdf") and then the title stored in the PDF.
    # Redirect pages carry placeholder titles: Moodle's file links show "Working...", PyTorch's docs "Redirecting…".
    return (page.lower().rstrip(".… ") in ("", "untitled", "new tab", "loading", "working", "redirecting",
                                           "please wait", "about:blank")
            or page.startswith(("http:", "https:")) or page == host or (bool(host) and page.startswith(host + "/")))


@server.tool()
def open_url(url: str) -> str:
    """Open a web page in the browser and bring it to the front. Only http and https addresses are
    allowed. Sites Bhanu signs in to (the university portal) open in the browser profile he uses for them."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"Refused: only http(s) web addresses can be opened, not {url!r}."
    return _open_web(url.strip())


@server.tool()
def open_portal() -> str:
    """Open Bhanu's university portal (SPeCTRUM, UM's Moodle site) in the browser profile where he is
    signed in to it."""
    portal = _config().get("portal")
    if not portal:
        return "No portal is configured in config.json."
    return f"{portal['name']}: " + _open_web(portal["url"])


@server.tool()
def open_course(course: str) -> str:
    """Open a course's page on the university portal, where its notes and slides are posted.
    `course` is a course code such as "SIF1014" or part of a course name such as "thermal physics".
    To open a specific lecture, file or assignment inside the course, use open_course_item instead."""
    match = _match_course(course)
    if isinstance(match, str):
        return match
    code, info = match
    return f"{code} {info['name']}: " + _open_web(info["url"])


@server.tool()
def open_course_item(course: str, item: str) -> str:
    """Open one item inside a course on the university portal (a lecture's notes or slides, a file,
    folder, link, assignment or quiz page) in the browser profile where Bhanu is signed in, and bring
    it to the front. `item` is part of the item's name, e.g. "Lecture 1", "Tutorial 2", "Course info".
    If several items match, nothing is opened and the candidates are returned."""
    got = _course_contents(course)
    if isinstance(got, str):
        return got
    code, info, sections = got
    q = _norm(item)
    if not q:
        return "Say which item to open."
    items = [(sec["name"], m) for sec in sections for m in sec["modules"] if m.get("url")]
    # Whole words, so "Lecture 1" doesn't also match "Lecture 10"; a file name inside the item counts too.
    pattern = re.compile(rf"(^| ){re.escape(q)}($| )")
    hits = [(s, m) for s, m in items if pattern.search(_norm(m["name"]))]
    hits = hits or [(s, m) for s, m in items if any(pattern.search(_norm(f)) for f in m["files"])]
    # Then shortened words: "course info" finds "Course Information".
    words = q.split()
    hits = hits or [(s, m) for s, m in items
                    if all(any(w.startswith(qw) for w in _norm(m["name"]).split()) for qw in words)]
    if not hits:
        close = difflib.get_close_matches(q, [_norm(m["name"]) for _, m in items], n=5, cutoff=0.5)
        hint = f" Closest: {'; '.join(close)}." if close else " Use course_materials to see what's posted."
        return f"Nothing in {code} is called {item!r}.{hint}"
    if len(hits) > 1:
        return f"Several items in {code} match {item!r}: " + "; ".join(f"{m['name']} ({s})" for s, m in hits[:8]) + ". Say which."
    sec, m = hits[0]
    return f"{code} › {sec} › {m['name']}: " + _open_web(m["url"])


_apps_cache: list[dict] = []


def _start_apps() -> list[dict]:
    if not _apps_cache:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        _apps_cache.extend(json.loads(out) if out.strip() else [])
    return _apps_cache


@server.tool()
def open_app(name: str) -> str:
    """Open an installed app by name, e.g. "Spotify", "VS Code", "Notepad", "Chrome", and bring its
    window to the front. If the app is already running, this brings its window forward.
    If several apps match, nothing is opened and the candidates are returned."""
    aliases = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
               "chrome": "google chrome", "edge": "microsoft edge", "file explorer": "file explorer"}
    want = _norm(aliases.get(_norm(name), name))
    apps = _start_apps()
    exact = [a for a in apps if _norm(a["Name"]) == want]
    partial = exact or [a for a in apps if want and want in _norm(a["Name"])]
    if not partial:
        close = difflib.get_close_matches(want, [_norm(a["Name"]) for a in apps], n=1, cutoff=0.75)
        partial = [a for a in apps if close and _norm(a["Name"]) == close[0]]
    if len(partial) == 1:
        app = partial[0]
        before = {w["hwnd"] for w in _windows()}
        already_open = bool(_find_windows(app["Name"]))
        os.startfile(f"shell:AppsFolder\\{app['AppID']}")
        # A fresh start can take seconds to show a window; a running app may show none at all, in
        # which case its existing window is brought forward instead.
        win = _wait_for(lambda: next((w for w in _find_windows(app["Name"]) if w["hwnd"] not in before), None),
                        2 if already_open else 10)
        if win is not None:
            _remember_opened(win["hwnd"], "window", win["title"])  # a window this call created
        win = win or next(iter(_find_windows(app["Name"])), None)
        if win is None:
            return f"Opened {app['Name']}; its window hasn't appeared yet."
        if _focus(win["hwnd"]):
            return f"Opened {app['Name']} and brought it to the front ({win['title']})."
        return f"Opened {app['Name']}, but Windows didn't let me bring it to the front."
    if len(partial) > 1:
        return "Several apps match: " + ", ".join(a["Name"] for a in partial[:8]) + ". Say which one."
    return f"No installed app called {name!r} was found."


# --- Media ------------------------------------------------------------------------------------
async def _media_session():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    manager = await Manager.request_async()
    return manager.get_current_session()


async def _media_summary(session) -> str:
    try:
        props = await session.try_get_media_properties_async()
        track = f"{props.title} by {props.artist}" if props and props.title else "the current track"
    except Exception:
        track = "the current track"
    status = {4: "playing", 5: "paused"}.get(int(session.get_playback_info().playback_status), "stopped")
    app = session.source_app_user_model_id.split("!")[0].split("_")[0]
    return f"{track} ({status}, {app})"


@server.tool()
async def media(action: str) -> str:
    """Control whatever is playing (Spotify, a browser video, ...). `action`: play, pause, next or previous.
    Play and pause are safe to repeat: play never pauses something already playing."""
    session = await _media_session()
    if session is None:
        return "Nothing is playing or paused right now."
    act = action.strip().lower()
    calls = {"play": session.try_play_async, "pause": session.try_pause_async,
             "next": session.try_skip_next_async, "previous": session.try_skip_previous_async}
    if act not in calls:
        return "Unknown action; use play, pause, next or previous."
    await calls[act]()
    await asyncio.sleep(0.6)
    return f"{act.capitalize()}: {await _media_summary(await _media_session() or session)}."


@server.tool()
async def play_music() -> str:
    """Start Bhanu's music: his Liked Songs in Spotify, from the start of the list. "Play my playlist",
    "play my music" and "play my songs" all mean this; call it without asking which playlist."""
    cfg = _config().get("music", {})
    os.startfile(cfg.get("liked_songs_uri", "spotify:collection:tracks"))
    # Spotify's page exposes a "Play" button through UI Automation; press it rather than a media key,
    # so the Liked Songs list starts instead of whatever was playing before.
    import uiautomation as auto
    deadline = time.time() + 12
    with auto.UIAutomationInitializerInThread():
        while time.time() < deadline:
            # Spotify's title becomes the current song while playing, so find it by process.
            spot = next((w for w in _windows() if w["process"].lower() == "spotify.exe"), None)
            if spot:
                win = auto.ControlFromHandle(spot["hwnd"])
                btn = win.ButtonControl(searchDepth=40, RegexName=r"(?i)^play\b.*liked songs")
                if btn.Exists(1):
                    try:
                        btn.GetInvokePattern().Invoke()
                    except Exception:
                        btn.Click(simulateMove=False)
                    await asyncio.sleep(1.5)
                    session = await _media_session()
                    if session is not None:
                        return f"Playing Liked Songs: {await _media_summary(session)}."
                    return "Pressed play on Liked Songs."
            await asyncio.sleep(0.5)
    session = await _media_session()
    if session is not None:
        await session.try_play_async()
        return ("Opened Liked Songs but couldn't press its play button; resumed what Spotify was playing: "
                f"{await _media_summary(session)}.")
    return "Opened Spotify on Liked Songs, but couldn't start playback."


# --- Windows ----------------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def _process_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = psapi_k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        psapi_k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return Path(buf.value).name
    finally:
        psapi_k32.CloseHandle(h)


def _alt_tab_window(hwnd) -> bool:
    """Roughly what Alt+Tab shows: visible, unowned, not a tool window, not cloaked."""
    if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
        return False
    if user32.GetWindowLongW(hwnd, -20) & 0x80:  # GWL_EXSTYLE & WS_EX_TOOLWINDOW
        return False
    cloaked = wt.DWORD()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return not cloaked.value  # DWMWA_CLOAKED: e.g. suspended Store apps, other desktops


def _windows() -> list[dict]:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if _alt_tab_window(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                found.append({"hwnd": hwnd, "title": buf.value, "process": _process_name(hwnd)})
        return True

    user32.EnumWindows(each, 0)
    return [w for w in found if w["process"].lower() not in ("textinputhost.exe", "applicationframehost.exe")
            and w["title"] not in ("Program Manager", "Student Twin")]


@server.tool()
def list_windows() -> str:
    """List the open windows (title and program), to find what to arrange."""
    return "\n".join(f"- {w['title']} [{w['process']}]" for w in _windows()) or "No windows found."


_PROCESS_ALIASES = {"vs code": "code.exe", "vscode": "code.exe", "visual studio code": "code.exe", "code": "code.exe",
                    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe",
                    "microsoft edge": "msedge.exe", "spotify": "spotify.exe", "file explorer": "explorer.exe",
                    "explorer": "explorer.exe", "terminal": "windowsterminal.exe"}
_GENERIC_WORDS = {"app", "the", "my", "window", "microsoft", "google", "launcher", "desktop", "windows"}


def _find_windows(query: str, loose: bool = True) -> list[dict]:
    """Windows matching a program or title, topmost first. Tries, in order: a known program, the
    whole name in the title, the name in the program file, then (if `loose`) any distinctive word
    of it, so "Minecraft Launcher" finds Minecraft.exe even while its title says "Minecraft Updater"."""
    q = _norm(query)
    if not q:
        return []
    wins = _windows()  # EnumWindows order is z-order, topmost first
    proc = _PROCESS_ALIASES.get(q)
    words = [w for w in q.split() if len(w) >= 3 and w not in _GENERIC_WORDS]
    tests = [lambda w: proc is not None and w["process"].lower() == proc,
             lambda w: q in _norm(w["title"]),
             lambda w: q.replace(" ", "") in w["process"].lower()]
    if loose:
        # Every distinctive word must match: with any one, "twin-test" matched a terminal titled
        # "Student twin project state" (20 Oct 2026).
        tests.append(lambda w: bool(words) and all(word in _norm(w["title"]).split() or word in w["process"].lower()
                                                   for word in words))
    for test in tests:
        hits = [w for w in wins if test(w)]
        if hits:
            return hits
    return []


def _wait_for(probe, timeout: float):
    """Call `probe` every 0.25 s until it returns something or `timeout` seconds pass."""
    deadline = time.time() + timeout
    while True:
        found = probe()
        if found or time.time() >= deadline:
            return found
        time.sleep(0.25)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _press_ctrl(vk: int) -> None:
    """Ctrl+<key> through SendInput."""
    keys = [(0x11, 0), (vk, 0), (vk, 2), (0x11, 2)]  # VK_CONTROL down, key down, key up, Ctrl up (KEYEVENTF_KEYUP)
    arr = (INPUT * len(keys))(*[INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(wVk=v, dwFlags=f))) for v, f in keys])
    user32.SendInput(len(keys), arr, ctypes.sizeof(INPUT))


def _focus(hwnd) -> bool:
    """Bring a window to the front with keyboard focus. Returns whether Windows allowed it."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.GetForegroundWindow() == hwnd:
        return True
    # Windows lets only the process that received the last input event change the foreground window,
    # and this server runs in the background. An empty mouse input sent from here counts as that event
    # (the same workaround PowerToys uses); it doesn't move the pointer or click.
    empty = INPUT(type=0)  # INPUT_MOUSE
    user32.SendInput(1, ctypes.byref(empty), ctypes.sizeof(INPUT))
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() != hwnd:
        # Fallback: share input state with the current foreground thread for the switch.
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        me = psapi_k32.GetCurrentThreadId()
        user32.AttachThreadInput(me, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


@server.tool()
def window(name: str, action: str = "focus") -> str:
    """Bring an open window to the front or change it. `name` is part of its title or its program
    ("Minecraft", "VS Code", "PyTorch", "Spotify"). `action`: focus (bring to the front), maximize,
    minimize, restore, close (asks the program to close, as its X button does, so it can still
    offer to save work), or close_tab (closes one browser tab and leaves the window's other tabs open;
    `name` is part of the tab's page title, and it works on the tab showing in front of its window).
    Closing prefers a window you opened for Bhanu; a page you opened as a tab in his existing window
    closes as that tab only. A browser, editor, document or terminal window you didn't open is not
    closed: the result asks which one, and you close it only after he says so, by passing its exact
    full title. Never close on a guess from an unclear command (a single word like "off")."""
    if (locked := _locked()):
        return locked
    act = action.strip().lower().replace(" ", "_")
    if act not in ("focus", "maximize", "minimize", "restore", "close", "close_tab"):
        return "Unknown action; use focus, maximize, minimize, restore, close or close_tab."
    if act == "close_tab":
        return _close_tab(name)
    hits = _find_windows(name, loose=act != "close")
    if not hits:
        return f"Couldn't find a window for {name!r}. Use list_windows to see what's open."
    win = hits[0]
    if act == "close":
        return _close(name, hits)
    if act == "minimize":
        user32.ShowWindow(win["hwnd"], 6)  # SW_MINIMIZE
        return f"Minimized {win['title']}."
    if act in ("maximize", "restore"):
        user32.ShowWindow(win["hwnd"], 3 if act == "maximize" else 9)  # SW_MAXIMIZE / SW_RESTORE
    focused = _focus(win["hwnd"])
    done = {"focus": "Brought {} to the front", "maximize": "Maximized {}", "restore": "Restored {}"}[act]
    return done.format(win["title"]) + ("." if focused else ", but Windows didn't let me bring it to the front.")


def _post_close(win: dict) -> str:
    user32.PostMessageW(win["hwnd"], 0x0010, 0, 0)  # WM_CLOSE, as the X button does
    _OPENED.pop(win["hwnd"], None)
    return f"Asked {win['title']} to close."


def _close(name: str, hits: list[dict]) -> str:
    """Close the window `name` refers to, without closing Bhanu's own work by mistake (on
    2026-10-15 "close Notepad" closed a Notepad window holding another file of his)."""
    mine = [w for w in hits if _OPENED.get(w["hwnd"], {}).get("kind") == "window"]
    if mine:  # a window the twin opened: the newest one
        return _post_close(max(mine, key=lambda w: _OPENED[w["hwnd"]]["at"]))
    for w in hits:
        rec = _OPENED.get(w["hwnd"])
        # A page the twin opened as a tab in his window, still the tab in front: close that tab only.
        if rec and rec["kind"] == "tab" and _norm(rec["title"]) == _norm(w["title"]):
            if not _focus(w["hwnd"]) or user32.GetForegroundWindow() != w["hwnd"]:
                return f"Windows didn't let me bring {w['title']} to the front, so nothing was closed."
            _press_ctrl(0x57)  # Ctrl+W
            _OPENED.pop(w["hwnd"], None)
            return f"Closed the tab {w['title']} that I opened; the window's other tabs stay open."
    exact = [w for w in hits if _norm(w["title"]) == _norm(name)]
    if len(exact) == 1:  # he named the window by its full title: that is his go-ahead
        return _post_close(exact[0])
    if len(hits) == 1 and hits[0]["process"].lower() not in WORK_APPS:
        return _post_close(hits[0])
    listed = "; ".join(f"{w['title']} [{w['process']}]" for w in hits[:8])
    return ("Not closed: I didn't open " + ("these windows" if len(hits) > 1 else "this window")
            + f" and it may hold Bhanu's work ({listed}). Ask him which to close; then call window "
            "again with that window's exact full title.")


def _close_tab(name: str) -> str:
    # A browser window's title is the page title of its front tab, so a title match is that tab.
    q = _norm(name)
    hits = [w for w in _windows() if w["process"].lower() in BROWSERS and q and q in _norm(w["title"])]
    if not hits:
        return (f"No browser window is showing a tab called {name!r} in front. Only the tab in front "
                "of each window can be closed; bring the tab forward first.")
    if len(hits) > 1:
        return "Several browser windows match; say which tab: " + "; ".join(w["title"] for w in hits[:8]) + "."
    win = hits[0]
    if not _focus(win["hwnd"]):
        return f"Windows didn't let me bring {win['title']} to the front, so no tab was closed."
    # Check again right before the keystroke, so Ctrl+W can only ever reach this window.
    if user32.GetForegroundWindow() != win["hwnd"]:
        return "Another window took the front before the tab could be closed; nothing was closed."
    _press_ctrl(0x57)  # Ctrl+W
    return f"Closed the tab {win['title']}."


def _place(hwnd, rect):
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE: un-maximise and un-minimise first
    user32.SetWindowPos(hwnd, None, rect[0], rect[1], rect[2], rect[3], 0x0004 | 0x0040)  # NOZORDER|SHOWWINDOW


@server.tool()
def arrange_windows(left: str, right: str) -> str:
    """Put two windows side by side, each filling half of the screen, both in front; the left one gets
    keyboard focus. `left` and `right` are part of a window title (e.g. "PyTorch") or a program
    ("VS Code", "Chrome", "Spotify"). Open a page first with open_url if it isn't open yet."""
    if (locked := _locked()):
        return locked
    lw, rw = (next(iter(_find_windows(left)), None), next(iter(_find_windows(right)), None))
    missing = [name for name, w in ((left, lw), (right, rw)) if w is None]
    if missing:
        return f"Couldn't find a window for: {', '.join(missing)}. Use list_windows to see what's open."
    if lw["hwnd"] == rw["hwnd"]:
        return f"{left!r} and {right!r} are the same window ({lw['title']})."
    mon = user32.MonitorFromWindow(lw["hwnd"], 2)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    wa = info.rcWork
    half = (wa.right - wa.left) // 2
    height = wa.bottom - wa.top
    _place(lw["hwnd"], (wa.left, wa.top, half, height))
    _place(rw["hwnd"], (wa.left + half, wa.top, wa.right - wa.left - half, height))
    _focus(rw["hwnd"])  # raise the right one first so both end up in front
    focused = _focus(lw["hwnd"])
    return (f"Arranged: {lw['title']} on the left, {rw['title']} on the right"
            + (f"; {lw['title']} has focus." if focused else ", but Windows didn't let me bring them to the front."))


# --- Weather ----------------------------------------------------------------------------------
_WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
        81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
        95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail"}


def _us_aqi_band(aqi: float) -> str:
    for limit, band in ((50, "good"), (100, "moderate"), (150, "unhealthy for sensitive groups"),
                        (200, "unhealthy"), (300, "very unhealthy")):
        if aqi <= limit:
            return band
    return "hazardous"


def _get_json(url: str, params: dict) -> dict:
    req = urllib.request.Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "student-twin"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


@server.tool()
def weather(place: str = "") -> str:
    """Current weather, today's forecast and air quality (haze) for a place. With no place it uses
    Bhanu's home city from config. Use this for any weather, rain, temperature or air-quality question
    instead of web search."""
    place = place.strip() or _config().get("home", {}).get("place", "")
    if not place:
        return "No place given and no home city in config.json; ask which city."
    try:
        found = _get_json("https://geocoding-api.open-meteo.com/v1/search", {"name": place, "count": 1}).get("results")
        if not found:
            return f"Couldn't find a place called {place!r}."
        loc = found[0]
        at = {"latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto"}
        wx = _get_json("https://api.open-meteo.com/v1/forecast", {
            **at, "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"})
        aq = _get_json("https://air-quality-api.open-meteo.com/v1/air-quality",
                       {**at, "current": "us_aqi,pm2_5,pm10"})
    except (OSError, ValueError) as exc:
        return f"Couldn't reach the weather service: {exc}."
    now, day, air = wx["current"], wx["daily"], aq.get("current", {})
    name = ", ".join(p for p in (loc.get("name"), loc.get("country")) if p)
    text = (f"{name}, {now['time'][-5:]} local: {now['temperature_2m']:.0f} °C (feels like "
            f"{now['apparent_temperature']:.0f} °C), {_WMO.get(now['weather_code'], 'unknown conditions')}, "
            f"humidity {now['relative_humidity_2m']}%, wind {now['wind_speed_10m']:.0f} km/h. "
            f"Today: {day['temperature_2m_min'][0]:.0f}–{day['temperature_2m_max'][0]:.0f} °C, "
            f"{_WMO.get(day['weather_code'][0], 'unknown conditions')}, "
            f"chance of rain up to {day['precipitation_probability_max'][0]}%.")
    if air.get("us_aqi") is not None:
        text += (f" Air quality: US AQI {air['us_aqi']:.0f} ({_us_aqi_band(air['us_aqi'])}), "
                 f"PM2.5 {air['pm2_5']:.0f} µg/m³. This is a model estimate (CAMS via Open-Meteo), not an "
                 "official station reading such as Malaysia's API index.")
    return text + " Source: Open-Meteo."


# --- Files (step 2d-ii): listing is read-only; deleting waits for the confirm card ---------------

_KNOWN_FOLDERS = {  # Windows known-folder IDs, so redirected folders (e.g. to OneDrive) resolve
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}
_MAX_DELETE = 500
_SHORT_SEARCH_S = 5.0  # a recursive search stops here and the twin asks whether to go on
_LONG_SEARCH_S = 120.0  # when he says to keep searching (Hermes allows an MCP call 300 s)


def _known_folder(guid: str) -> Path | None:
    class GUID(ctypes.Structure):
        _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]
    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(guid, ctypes.byref(g))
    out = ctypes.c_wchar_p()
    try:
        ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
        return Path(out.value) if out.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def _protected() -> list[Path]:
    """Folders the file tools never touch: app data, the twin's own code and data, and any folders
    listed under `protected_folders` in the twin's config."""
    home = Path.home()
    fixed = [home / "AppData", Path(__file__).resolve().parents[1], CONFIG_PATH.parent]
    return [p.resolve() for p in fixed] + [Path(p).expanduser().resolve()
                                           for p in _config().get("protected_folders", [])]


_SEARCH_ROOTS = ("downloads", "desktop", "documents", "pictures", "videos", "music")


def _folder_key(name: str) -> str:
    """"Twin Test folder", "twin-test" and "TwinTest" all give "twintest"."""
    key = re.sub(r"(?i)\s+folder$", "", name.strip())
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _find_named_folder(name: str, under: list[Path] | None = None) -> list[Path]:
    """Folders up to two levels inside the usual places (Downloads, Desktop, Documents, Pictures,
    Videos, Music, OneDrive) whose name matches `name` ignoring case, spaces, hyphens and a
    trailing "folder". Spoken names rarely match the real one exactly (on 20 Oct 2026 "the Twin Test
    folder" was Downloads\\twin-test, and the twin searched the whole user folder for 30 s)."""
    key = _folder_key(name)
    if not key:
        return []
    roots = under or [r for r in (_known_folder(_KNOWN_FOLDERS[k]) for k in _SEARCH_ROOTS) if r] + \
        sorted(Path.home().glob("OneDrive*"))
    protected = _protected()
    found = []
    for root in roots:
        level = [root]
        for _ in range(2):
            nxt = []
            for d in level:
                try:
                    for child in d.iterdir():
                        if child.is_dir() and not child.name.startswith(".") and \
                                not any(child.resolve().is_relative_to(p) for p in protected):
                            if _folder_key(child.name) == key and child.resolve() not in found:
                                found.append(child.resolve())
                            nxt.append(child)
                except OSError:
                    continue
            level = nxt
    return found


def _resolve_folder(folder: str) -> Path | str:
    """The folder as a resolved path inside Bhanu's user folder, or a refusal sentence. Besides the
    known folders and paths, a spoken folder name ("the Twin Test folder") is looked up by
    _find_named_folder; "Downloads/Twin Test" matches its last part inside Downloads."""
    name = folder.strip().strip('"')
    if name.lower().startswith("the "):
        name = name[4:]
    guid = _KNOWN_FOLDERS.get(name.lower().removeprefix("my ").strip())
    path = _known_folder(guid) if guid else Path(os.path.expandvars(name)).expanduser()
    if path is None:
        return f"Couldn't find the {name} folder."
    if not path.is_absolute():
        parts = Path(name).parts
        head = _KNOWN_FOLDERS.get(parts[0].lower()) if len(parts) > 1 else None
        path = (_known_folder(head) / Path(*parts[1:])) if head else Path.home() / path
    if not path.is_dir():
        if len(Path(name).parts) == 1 and not guid:
            hits = _find_named_folder(name)
        elif path.parent.is_dir():
            hits = _find_named_folder(path.name, [path.parent])
        else:
            hits = []
        if len(hits) == 1:
            path = hits[0]
        elif len(hits) > 1:
            return f"Several folders are called that: {'; '.join(str(h) for h in hits[:8])}. Say which."
    path = path.resolve()
    home = Path.home().resolve()
    if not path.is_relative_to(home):
        return f"Refused: {path} is outside your user folder."
    if any(part.startswith(".") for part in path.relative_to(home).parts):
        return f"Refused: {path} is a hidden settings folder."
    for p in _protected():
        if path.is_relative_to(p):
            return f"Refused: {path} is inside a protected folder ({p})."
    if not path.is_dir():
        return f"No folder at {path}."
    return path


def _matches(folder: Path, pattern: str, recursive: bool, older_than_days: float | None,
             time_limit: float = 5.0) -> tuple[list[Path], bool]:
    """(matching files, whether the search finished). A recursive search stops after `time_limit`
    seconds: on 20 Oct 2026 one over the whole user folder ran for 30 s before Bhanu stopped it."""
    files = (folder.rglob if recursive else folder.glob)(pattern or "*")
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    protected = _protected()
    deadline = time.time() + time_limit
    out = []
    for f in files:
        if time.time() > deadline:
            return sorted(out, key=lambda f: f.stat().st_mtime, reverse=True), False
        try:
            # A folder may contain protected or hidden ones (Desktop holds the OneDrive vault): skip them.
            inside = f.relative_to(folder).parts[:-1]
            if any(part.startswith(".") for part in inside) or any(f.is_relative_to(p) for p in protected):
                continue
            if f.is_file() and (cutoff is None or f.stat().st_mtime < cutoff):
                out.append(f)
        except OSError:
            pass
    return sorted(out, key=lambda f: f.stat().st_mtime, reverse=True), True


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


@server.tool()
def list_files(folder: str, pattern: str = "*", recursive: bool = False, keep_searching: bool = False) -> str:
    """List files in a folder, newest first, with sizes and dates. `folder` is Downloads, Desktop,
    Documents, Pictures, Videos, Music, a path inside Bhanu's user folder, or a folder's name as he
    says it ("the twin test folder" finds Downloads\\twin-test). `pattern` is a wildcard such as
    "*.zip" or "Screenshot*". Read-only. A recursive search stops after 5 seconds; if it does, ask
    Bhanu whether to keep searching, and only if he says yes call again with keep_searching=True
    (up to 2 minutes)."""
    path = _resolve_folder(folder)
    if isinstance(path, str):
        return path
    limit = _LONG_SEARCH_S if keep_searching else _SHORT_SEARCH_S
    files, complete = _matches(path, pattern, recursive, None, limit)
    stopped = "" if complete else (
        f" (the search stopped after {limit:.0f} seconds and may have missed some. Ask Bhanu whether to keep "
        "searching; if he says yes, call list_files again with keep_searching=true)" if not keep_searching
        else f" (the longer search also stopped, after {limit:.0f} seconds; suggest a narrower folder)")
    if not files:
        return f"No files in {path} match {pattern!r}{stopped}."
    lines = [f"- {f.relative_to(path)} ({_size(f.stat().st_size)}, "
             f"{time.strftime('%d %b %Y %H:%M', time.localtime(f.stat().st_mtime))})" for f in files[:40]]
    more = f"\n…and {len(files) - 40} more." if len(files) > 40 else ""
    return f"{len(files)} file(s) in {path} matching {pattern!r}{stopped}:\n" + "\n".join(lines) + more


def _recycle(paths: list[Path]) -> int:
    """Send files to the Recycle Bin. Returns the shell's error code (0 = success)."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wt.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wt.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    buf = ctypes.create_unicode_buffer("\0".join(str(p) for p in paths) + "\0\0")  # double-null list
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=ctypes.cast(buf, ctypes.c_void_p),
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


@server.tool()
async def delete_files(folder: str, pattern: str, recursive: bool = False,
                       older_than_days: float | None = None, keep_searching: bool = False,
                       ctx: Context | None = None) -> str:
    """Move files to the Recycle Bin, after Bhanu confirms: a card on the widget, or Confirm/Cancel
    buttons on Telegram. The tool asks him itself and waits: never ask him to confirm in chat first,
    and don't retry after a cancel. `folder` as in list_files; `pattern` a wildcard ("*.zip", "*" for every
    file); `older_than_days` keeps newer files. Files only, never folders."""
    path = _resolve_folder(folder)
    if isinstance(path, str):
        return path
    if not pattern.strip():
        return "Give a pattern, e.g. \"*.zip\", or \"*\" for every file in the folder."
    limit = _LONG_SEARCH_S if keep_searching else _SHORT_SEARCH_S
    files, complete = _matches(path, pattern, recursive, older_than_days, limit)
    if not complete:
        if keep_searching:
            return f"Finding the files in {path} took over {limit:.0f} seconds, so nothing was deleted. Narrow the folder or pattern."
        return (f"Finding the files in {path} took over {limit:.0f} seconds, so nothing was deleted. Ask Bhanu whether to "
                "keep searching; if he says yes, call delete_files again with keep_searching=true.")
    if not files:
        return f"No files in {path} match {pattern!r}; nothing to delete."
    if len(files) > _MAX_DELETE:
        return f"{len(files)} files match; the limit is {_MAX_DELETE} at once. Narrow the pattern."
    sizes = [f.stat().st_size for f in files]
    where = path.name or str(path)
    summary = f"Move {len(files)} file{'s' if len(files) != 1 else ''} in {where} to the Recycle Bin?"
    details = [f"{f.relative_to(path)} · {_size(s)}" for f, s in zip(files[:8], sizes)]
    if len(files) > 8:
        details.append(f"…and {len(files) - 8} more")
    details.append(f"Total {_size(sum(sizes))}")
    if max(sizes) >= 1024 ** 3:
        details.append("Files too large for the Recycle Bin are deleted permanently")
    import confirm  # local module, imported lazily like moodle
    decision = await confirm.ask_where(ctx, summary, details, action="Delete")
    if decision != "confirm":
        return confirm.OUTCOME[decision]
    code = _recycle(files)
    left = [f for f in files if f.exists()]
    if code == 0 and not left:
        return (f"Moved {len(files)} file(s) from {path} to the Recycle Bin. It worked as asked: reply with only "
                "the tick mark, no sentence and no further check.")
    return (f"Moved {len(files) - len(left)} of {len(files)} file(s) to the Recycle Bin; "
            f"{len(left)} could not be moved (shell error {code:#x}), e.g. {left[0].name if left else ''}.")


_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".html", ".css", ".js", ".tex", ".log"}


@server.tool()
def write_text_file(folder: str, name: str, text: str) -> str:
    """Create a new text file and write `text` into it. `folder` as in list_files (Downloads,
    Desktop, Documents, ... or a path inside Bhanu's user folder); `name` is a plain file name such
    as "hello.txt" (.txt, .md, .csv, .json, .py, .html, .css, .js, .tex or .log). Never overwrites:
    if the file exists, nothing is written."""
    path = _resolve_folder(folder)
    if isinstance(path, str):
        return path
    name = name.strip().strip('"')
    if not name or Path(name).name != name or name.startswith("."):
        return f"Refused: {name!r} must be a plain file name, without folders."
    if Path(name).suffix.lower() not in _TEXT_EXTENSIONS:
        return f"Refused: only text files ({', '.join(sorted(_TEXT_EXTENSIONS))})."
    if len(text.encode("utf-8")) > 1_000_000:
        return "Refused: the text is over 1 MB."
    target = path / name
    try:
        with open(target, "x", encoding="utf-8", newline="") as f:  # "x" fails if the file exists
            f.write(text)
    except FileExistsError:
        return f"{target} already exists; nothing was written. Choose another name."
    except OSError as exc:
        return f"Couldn't write {target}: {exc.strerror or exc}."
    return f"Created {target} ({_size(target.stat().st_size)})."


# --- Accessibility layer (step 2d-iii, layer 2): read and act on a window's labelled elements ---
# Windows UI Automation exposes most windows as a tree of named elements (buttons, links, list
# items, text boxes), as screen readers see them. Reading it needs no screenshot and one model step.
# Like screen control, this layer only promises to stay away from irreversible actions, so it
# refuses messaging windows and elements whose names suggest one; deleting goes through
# delete_files and its confirm card.

_MESSAGING = re.compile(r"(?i)whatsapp|gmail|outlook|telegram|messenger|discord|slack|signal|teams|\bmail\b|inbox")
_IRREVERSIBLE = re.compile(r"(?i)\b(delete|remove|erase|uninstall|submit|send|pay|purchase|buy|checkout|"
                           r"place order|transfer|unenrol|unenroll|format|reset|discard|empty)\b")
_UI_KINDS = {"ButtonControl": "button", "HyperlinkControl": "link", "MenuItemControl": "menu item",
             "ListItemControl": "list item", "TabItemControl": "tab", "TreeItemControl": "tree item",
             "EditControl": "text box", "CheckBoxControl": "checkbox", "RadioButtonControl": "radio button",
             "ComboBoxControl": "dropdown", "TextControl": "text", "DocumentControl": "document",
             "DataItemControl": "row", "SplitButtonControl": "button"}
_ACTIONABLE = {"button", "link", "menu item", "list item", "tab", "tree item", "checkbox", "radio button", "row"}


# --- Locked laptop (2026-10-24): Telegram requests can arrive while the screen is locked, when
# Windows lets no program read or drive it. Screen tools say so at once instead of failing halfway.

def _locked() -> str | None:
    """A refusal sentence when the session is locked, else None (fails open if Windows won't say)."""
    wtsapi = ctypes.WinDLL("wtsapi32")
    buf, size = ctypes.c_void_p(), wt.DWORD()
    try:
        # WTSSessionInfoEx (25) for the current session: WTSINFOEXW.Level1.SessionFlags at offset 16,
        # 0 = locked, 1 = unlocked on Windows 10 and 11.
        if not wtsapi.WTSQuerySessionInformationW(None, wt.DWORD(-1 & 0xFFFFFFFF), 25, ctypes.byref(buf), ctypes.byref(size)):
            return None
        flags = ctypes.c_long.from_address(buf.value + 16).value
        wtsapi.WTSFreeMemory(buf)
    except Exception:
        return None
    if flags == 0:
        return ("Bhanu's laptop is locked, so nothing on its screen can be read or done; "
                "tell him to unlock it and ask again.")
    return None


def _ui_window(name: str):
    """(window dict, error) for a window the accessibility tools may use."""
    if (locked := _locked()):
        return None, locked
    hits = _find_windows(name)
    if not hits:
        return None, f"No open window matches {name!r}. Use list_windows to see what's open."
    win = hits[0]
    if _MESSAGING.search(win["title"]) or _MESSAGING.search(win["process"]):
        return None, f"Refused: {win['title']!r} is a messaging window; the twin doesn't read or act in those."
    # A minimized window exposes only its title bar; restore it so its contents can be read.
    win["restored"] = bool(user32.IsIconic(win["hwnd"]))
    if win["restored"]:
        user32.ShowWindow(win["hwnd"], 9)  # SW_RESTORE
        time.sleep(0.6)
    return win, None


def _ui_root(win):
    """The part of the window to read: for a browser, the page itself (its document element),
    leaving out the tab strip, address bar and extension buttons; otherwise the whole window."""
    import uiautomation as auto
    root = auto.ControlFromHandle(win["hwnd"])
    if win["process"].lower() in BROWSERS:
        docs = [c for c, _ in auto.WalkControl(root, maxDepth=12) if c.ControlTypeName == "DocumentControl"]
        named = [c for c in docs if c.Name and c.Name in win["title"]]
        if named or docs:
            return (named or docs)[0]
    return root


def _ui_elements(win, limit: int = 250, depth: int = 30) -> list[tuple]:
    """(kind, name, control) for the window's named elements in reading order."""
    import uiautomation as auto
    root = _ui_root(win)
    out, seen = [], set()
    for control, _ in auto.WalkControl(root, maxDepth=depth):
        kind = _UI_KINDS.get(control.ControlTypeName)
        if not kind:
            continue
        name = " ".join((control.Name or "").split())
        if not name or len(name) > 300 or (kind, name) in seen and kind == "text":
            continue
        seen.add((kind, name))
        out.append((kind, name, control))
        if len(out) >= limit:
            break
    return out


@server.tool()
def ui_read(window: str, contains: str = "") -> str:
    """Read a window's labelled elements as text: buttons, links, tabs, list items, text boxes and
    visible text, in order. Use it to see what's on screen without a screenshot, e.g. the links on a
    course page in Chrome or the prices on a shop page, then ui_click or ui_type by name.
    `contains` keeps only elements whose name includes it. Not for messaging windows."""
    import uiautomation as auto
    win, err = _ui_window(window)
    if err:
        return err
    with auto.UIAutomationInitializerInThread():
        elements = _ui_elements(win)
        q = _norm(contains)
        lines = [f"- {kind}: {name}" for kind, name, _ in elements if not q or q in _norm(name)]
    if not lines:
        return (f"{win['title']!r} shows no labelled elements" + (f" containing {contains!r}" if q else "")
                + ". It may not support accessibility; screen control would be needed.")
    head = f"{win['title']!r}, {len(lines)} element(s)" + (" (reading stopped at 250 elements)" if len(elements) >= 250 else "")
    if win.get("restored"):
        head += " (it was minimized; restored it to read it)"
    return head + ":\n" + "\n".join(lines[:250])


def _ui_target(window: str, element: str, kinds: set[str]):
    """(window, control, kind, name, error) for the single best match of `element` in `window`."""
    win, err = _ui_window(window)
    if err:
        return None, None, None, None, err
    q = _norm(element)
    elements = [e for e in _ui_elements(win) if e[0] in kinds]
    # When one name labels several elements (a menu entry and its link), act on the most direct one.
    rank = {"link": 0, "button": 1, "tab": 2, "menu item": 3, "checkbox": 4, "radio button": 5}
    exact = sorted((e for e in elements if _norm(e[1]) == q), key=lambda e: rank.get(e[0], 9))
    partial = [e for e in elements if q and q in _norm(e[1])]
    hits = exact or partial
    if not hits:
        return win, None, None, None, f"No {'/'.join(sorted(kinds))} named like {element!r} in {win['title']!r}. Try ui_read."
    if len(hits) > 1 and not exact and len({_norm(n) for _, n, _ in hits}) > 1:
        names = "; ".join(f"{k} '{n}'" for k, n, _ in hits[:8])
        return win, None, None, None, f"Several elements match {element!r}: {names}. Say which."
    kind, name, control = hits[0]
    if _IRREVERSIBLE.search(name):
        return win, None, None, None, (f"Refused: {kind} '{name}' looks irreversible. Deleting goes through "
                                       "delete_files; other irreversible actions are for Bhanu to do himself.")
    return win, control, kind, name, None


@server.tool()
def ui_click(window: str, element: str) -> str:
    """Click a button, link, tab, list item, menu item or checkbox in a window by its name, as read
    by ui_read (e.g. window "SPeCTRUM", element "Lecture 1"). Refuses elements that look
    irreversible (delete, submit, send, pay...) and messaging windows."""
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        win, control, kind, name, err = _ui_target(window, element, _ACTIONABLE)
        if err:
            return err
        _focus(win["hwnd"])
        how = None
        # Element classes expose only the patterns their type usually has, so look them up by name.
        for getter, act, label in (("GetInvokePattern", lambda p: p.Invoke(), "invoked"),
                                   ("GetTogglePattern", lambda p: p.Toggle(), "toggled"),
                                   ("GetSelectionItemPattern", lambda p: p.Select(), "selected")):
            try:
                pattern = getattr(control, getter)() if hasattr(control, getter) else None
                if pattern:
                    act(pattern)
                    how = label
                    break
            except Exception:
                continue
        if how is None:
            control.Click(simulateMove=False)
            how = "clicked"
    time.sleep(0.8)
    return f"{how.capitalize()} {kind} '{name}' in {win['title']!r}. The window is now titled {_window_title(win['hwnd'])!r}."


@server.tool()
def ui_type(window: str, element: str, text: str, press_enter: bool = False) -> str:
    """Type `text` into a text box in a window, found by its name as read by ui_read (e.g. a search
    box). Replaces what the box held. `press_enter` presses Enter afterwards, e.g. to run a
    search. Never into password boxes or messaging windows."""
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        win, control, kind, name, err = _ui_target(window, element, {"text box", "dropdown"})
        if err:
            return err
        try:
            if control.IsPassword:
                return "Refused: that's a password box."
        except Exception:
            pass
        _focus(win["hwnd"])
        try:
            control.GetValuePattern().SetValue(text)
        except Exception:
            control.SetFocus()
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            auto.SendKeys(text.replace("{", "{{}").replace("}", "{}}"), interval=0, waitTime=0.05)
        if press_enter:
            control.SetFocus()
            auto.SendKeys("{Enter}", waitTime=0.3)
    return f"Typed into {kind} '{name}' in {win['title']!r}" + (" and pressed Enter." if press_enter else ".")


# --- Screen control (step 2d-iii, layer 3): look at a window, then click, type or scroll ---------
# For what the accessibility tree can't reach: canvas and game content, apps with poor labels,
# visual questions. A look is a screenshot of one window, never the whole screen and never a
# messaging window, so other people's messages don't reach the model. Before every click the
# element under the point is read through UI Automation, and irreversible-looking elements and
# password boxes are refused, so deleting still goes only through delete_files and its confirm
# card. Coordinates are those of the window's last look.

_LOOK_MAX = 1366  # longest side of the image sent to the model, in pixels
_looks: dict[int, dict] = {}  # hwnd -> {"rect": (left, top, right, bottom), "scale": float}


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


_gdi32 = ctypes.windll.gdi32
user32.GetWindowDC.restype = wt.HDC
user32.GetWindowDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
_gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = wt.HGDIOBJ
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wt.HDC]


def _window_rect(hwnd) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _capture(hwnd):
    """(PIL image, rect) of one window as it draws itself, even where other windows cover it
    (PrintWindow with PW_RENDERFULLCONTENT). Falls back to grabbing that part of the screen when the
    window can't draw itself (some games), after bringing it to the front."""
    from PIL import Image as PILImage, ImageGrab
    rect = _window_rect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    hdc = user32.GetWindowDC(hwnd)
    mem = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = _gdi32.SelectObject(mem, bmp)
    try:
        ok = user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
        info = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                                biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        _gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
    finally:
        _gdi32.SelectObject(mem, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
    img = PILImage.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    if not ok or img.getextrema() in (((0, 0), (0, 0), (0, 0)),):
        _focus(hwnd)
        time.sleep(0.3)
        img = ImageGrab.grab(bbox=rect, all_screens=True)
    return img, rect


@server.tool()
def screen_look(window: str):
    """Take a screenshot of one window, for what ui_read can't see: games and other canvas content,
    images, charts, apps whose buttons have no names, or "what does this look like". Then act with
    screen_click / screen_type / screen_scroll using x, y in this image. Prefer ui_read first: it is
    cheaper and exact. Never the whole screen, never messaging windows."""
    from mcp.server.mcpserver import Image
    win, err = _ui_window(window)
    if err:
        return err
    img, rect = _capture(win["hwnd"])
    w, h = img.size
    scale = min(1.0, _LOOK_MAX / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)))
    _looks[win["hwnd"]] = {"rect": rect, "scale": scale}
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    note = (f"{win['title']!r} ({win['process']}), {img.size[0]} x {img.size[1]} pixels. Use x, y in this image "
            f"with screen_click.") + (" It was minimized; restored it." if win.get("restored") else "")
    return [note, Image(data=out.getvalue(), format="jpeg")]


def _screen_point(win, x: float, y: float):
    """Screen pixel for image point (x, y) of the window's last look, or an error sentence."""
    look = _looks.get(win["hwnd"])
    if not look:
        return None, f"Look at {win['title']!r} with screen_look first."
    if _window_rect(win["hwnd"]) != look["rect"]:
        return None, f"{win['title']!r} has moved or resized since the last look; look again first."
    left, top, right, bottom = look["rect"]
    sx, sy = left + round(x / look["scale"]), top + round(y / look["scale"])
    if not (left <= sx < right and top <= sy < bottom):
        return None, f"({x}, {y}) is outside the window's image."
    return (sx, sy), None


def _target_check(win, sx: int, sy: int) -> tuple[str, str | None]:
    """(description of the element under the point, refusal or None). Refuses points another window
    covers, irreversible-looking elements (or their button/link/menu parents) and password boxes."""
    import uiautomation as auto
    under = user32.GetAncestor(user32.WindowFromPoint(POINT(sx, sy)), 2)  # GA_ROOT
    if under != win["hwnd"]:
        return "", f"Another window covers that point ({_window_title(under)!r}); bring {win['title']!r} forward and look again."
    with auto.UIAutomationInitializerInThread():
        el = auto.ControlFromPoint(sx, sy)
        # Chromium builds its accessibility tree on first request; an unnamed element in a browser
        # may be a page that isn't described yet, so ask once more before trusting it.
        if win["process"].lower() in BROWSERS and el and not (el.Name or "").strip():
            time.sleep(0.4)
            el = auto.ControlFromPoint(sx, sy)
        chain, c = [], el
        for _ in range(4):  # the element and the controls it sits in, up to its button or link
            if not c:
                break
            chain.append((c.ControlTypeName, " ".join((c.Name or "").split())[:80]))
            if c.ControlTypeName in ("WindowControl", "PaneControl", "DocumentControl"):
                break
            c = c.GetParentControl()
        try:
            password = el.ControlTypeName == "EditControl" and bool(el.IsPassword)
        except Exception:
            password = False
    for _, name in chain:
        if _IRREVERSIBLE.search(name):
            return name, (f"Refused: that point is on '{name}', which looks irreversible. Deleting goes through "
                          "delete_files; other irreversible actions are for Bhanu to do himself.")
    if password:
        return "password box", "Refused: that's a password box."
    kind, name = chain[0] if chain else ("", "")
    return (f"{_UI_KINDS.get(kind, kind.replace('Control', '').lower())} '{name}'" if name else "an unlabelled spot"), None


def _mouse(flags: int, data: int = 0) -> None:
    m = INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(dwFlags=flags, mouseData=ctypes.c_ulong(data & 0xFFFFFFFF).value)))
    user32.SendInput(1, ctypes.byref(m), ctypes.sizeof(INPUT))


def _at_point(sx: int, sy: int, action) -> None:
    """Move the cursor to (sx, sy), do `action`, and put the cursor back where Bhanu left it."""
    back = POINT()
    user32.GetCursorPos(ctypes.byref(back))
    user32.SetCursorPos(sx, sy)
    time.sleep(0.05)
    try:
        action()
    finally:
        time.sleep(0.05)
        user32.SetCursorPos(back.x, back.y)


@server.tool()
def screen_click(window: str, x: float, y: float, double: bool = False) -> str:
    """Click at x, y of the window's last screen_look image (double-click with `double`). Refuses
    points on elements that look irreversible (delete, submit, send, pay...), password boxes and
    messaging windows. Look again afterwards to see the result."""
    win, err = _ui_window(window)
    if err:
        return err
    point, err = _screen_point(win, x, y)
    if err:
        return err
    if not _focus(win["hwnd"]):
        return f"Windows didn't let me bring {win['title']!r} to the front, so nothing was clicked."
    what, refusal = _target_check(win, *point)
    if refusal:
        return refusal
    clicks = 2 if double else 1
    _at_point(*point, lambda: [(_mouse(0x0002), _mouse(0x0004)) for _ in range(clicks)])  # LEFTDOWN, LEFTUP
    time.sleep(0.6)
    return (f"{'Double-clicked' if double else 'Clicked'} {what} at ({x:g}, {y:g}) in {win['title']!r}. "
            f"The window is now titled {_window_title(win['hwnd'])!r}; look again to see the result.")


@server.tool()
def screen_type(window: str, text: str, press_enter: bool = False) -> str:
    """Type `text` where the keyboard focus is in a window, e.g. after screen_click on a text field
    (ui_type is better when the field has a name). `press_enter` presses Enter afterwards. Never
    into password boxes or messaging windows."""
    import uiautomation as auto
    win, err = _ui_window(window)
    if err:
        return err
    if not _focus(win["hwnd"]) or user32.GetForegroundWindow() != win["hwnd"]:
        return f"Windows didn't let me bring {win['title']!r} to the front, so nothing was typed."
    with auto.UIAutomationInitializerInThread():
        focused = auto.GetFocusedControl()
        try:
            if focused and focused.ControlTypeName == "EditControl" and focused.IsPassword:
                return "Refused: the focus is on a password box."
        except Exception:
            pass
        auto.SendKeys(text.replace("{", "{{}").replace("}", "{}}"), interval=0, waitTime=0.05)
        if press_enter:
            auto.SendKeys("{Enter}", waitTime=0.3)
    return f"Typed into {win['title']!r}" + (" and pressed Enter." if press_enter else ".")


@server.tool()
def screen_scroll(window: str, direction: str = "down", amount: int = 5) -> str:
    """Scroll a window up or down by `amount` notches (about three lines each), with the mouse wheel
    over the middle of the window. Look again afterwards."""
    win, err = _ui_window(window)
    if err:
        return err
    d = direction.strip().lower()
    if d not in ("up", "down"):
        return "direction must be up or down."
    amount = max(1, min(int(amount), 30))
    if not _focus(win["hwnd"]):
        return f"Windows didn't let me bring {win['title']!r} to the front, so nothing was scrolled."
    left, top, right, bottom = _window_rect(win["hwnd"])
    delta = 120 * amount * (1 if d == "up" else -1)
    _at_point((left + right) // 2, (top + bottom) // 2, lambda: _mouse(0x0800, delta))  # MOUSEEVENTF_WHEEL
    time.sleep(0.4)
    return f"Scrolled {d} {amount} notches in {win['title']!r}; look again to see the result."

# --- File picker: fill a website's or app's Windows "Open" box (step 2 of the NotebookLM plan) ---

_UPLOADS = Path(os.environ["LOCALAPPDATA"]) / "student-twin" / "uploads"
_MAX_PICK = 50
user32.GetWindow.restype = wt.HWND
user32.GetParent.restype = wt.HWND
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _name_box(dialog):
    """The dialog's "File name" edit box (inside the control with id 1148), or None."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if _class_name(hwnd) == "Edit":
            parent = user32.GetParent(hwnd)
            if 1148 in (user32.GetDlgCtrlID(parent), user32.GetDlgCtrlID(user32.GetParent(parent))):
                found.append(hwnd)
                return False
        return True

    user32.EnumChildWindows(dialog, each, 0)
    return found[0] if found else None


def _file_dialogs() -> list[dict]:
    """Open file dialogs on screen, each with the window that opened it."""
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _class_name(hwnd) == "#32770":
            box = _name_box(hwnd)
            if box:
                owner = user32.GetWindow(hwnd, 4) or hwnd  # GW_OWNER
                out.append({"hwnd": hwnd, "box": box, "title": _window_title(hwnd),
                            "owner": _window_title(owner), "process": _process_name(owner)})
        return True

    user32.EnumWindows(each, 0)
    return out


def _accepts_many(dialog) -> bool | None:
    """Whether the Open box allows several files (its file list's CanSelectMultiple), None if unknown.
    A one-file box given several names silently takes the first (seen in Chrome, 23 Oct 2026)."""
    import uiautomation as auto
    try:
        with auto.UIAutomationInitializerInThread():
            for c, _ in auto.WalkControl(auto.ControlFromHandle(dialog), maxDepth=8):
                if c.ControlTypeName == "ListControl" and c.Name == "Items View":
                    return bool(c.GetSelectionPattern().CanSelectMultiple)
    except Exception:
        pass
    return None


def _dialog_submit(dialog, box, text: str) -> None:
    user32.SendMessageW(box, 0x000C, 0, ctypes.c_wchar_p(text))  # WM_SETTEXT
    user32.PostMessageW(dialog, 0x0111, 1, 0)  # WM_COMMAND IDOK: the Open button


def _stage(files: list[Path]) -> Path:
    """One folder holding all `files` (the Open box selects several files only within one folder):
    hard links, so no space is used and the originals are untouched; a copy if linking fails."""
    import shutil
    if _UPLOADS.exists():
        for old in _UPLOADS.iterdir():  # yesterday's batches are no longer needed
            if time.time() - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)
    folder = _UPLOADS / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    for f in files:
        dest = folder / f.name
        n = 2
        while dest.exists():
            dest = folder / f"{f.stem} ({n}){f.suffix}"
            n += 1
        try:
            os.link(f, dest)
        except OSError:
            shutil.copy2(f, dest)
    return folder


@server.tool()
async def pick_files(folder: str, pattern: str = "*", recursive: bool = False, names: str = "", window: str = "",
                     ctx: Context | None = None) -> str:
    """Fill an open Windows "Open"/upload box with files and press Open, so a website (e.g. NotebookLM's
    "Add source" upload) or app receives them. First make the site open its file box (ui_click or
    screen_click on its upload button), then call this. `folder`: a path or spoken name ("Thermal
    Physics" in his course folder works by name); `pattern` like "*.pdf"; `recursive` takes files from
    subfolders too (course folders keep one subfolder per section); `names`: specific file names
    separated by ";" instead of a pattern. `window`: the window that opened the box, if several are
    open. Up to 50 files. This hands files to the website: only for files Bhanu asked to add, to the
    site he named. Never for messaging or email windows. On Telegram it asks him with Confirm/Cancel
    buttons first (he can't see the screen); on the widget it doesn't."""
    base = _resolve_folder(folder)
    if isinstance(base, str):
        return base
    if names:
        files, missing = [], []
        for n in [n.strip() for n in names.split(";") if n.strip()]:
            hit = base / n if (base / n).is_file() else next(iter(sorted(base.rglob(n))), None) if recursive else None
            (files if hit else missing).append(hit or n)
        if missing:
            return f"Not found in {base}: {'; '.join(missing)}. Nothing was picked."
    else:
        files, complete = _matches(base, pattern, recursive, None, time_limit=_SHORT_SEARCH_S)
        files = sorted(files)
        if not complete:
            return f"Searching {base} took too long; name a narrower folder. Nothing was picked."
    files = [f for f in files if not f.name.endswith((".part", ".tmp")) and not f.name.startswith("~$")]
    if not files:
        return f"No files match {pattern!r} in {base}" + (" or its subfolders" if recursive else "") + ". Nothing was picked."
    if len(files) > _MAX_PICK:
        return f"That's {len(files)} files; the most at once is {_MAX_PICK}. Narrow it with a pattern or a subfolder."
    if (locked := _locked()):
        return locked
    dialogs = _file_dialogs()
    if window:
        q = _norm(window)
        dialogs = [d for d in dialogs if q in _norm(d["owner"]) or q in _norm(d["process"])]
    if not dialogs:
        return ("No file Open box is open" + (f" for {window!r}" if window else "") +
                ". Click the site's upload button first (ui_click or screen_click), then call pick_files.")
    if len(dialogs) > 1:
        return "Several Open boxes are open: " + "; ".join(f"{d['title']!r} from {d['owner']!r}" for d in dialogs) + ". Say which window."
    d = dialogs[0]
    if _MESSAGING.search(d["owner"]) or _MESSAGING.search(d["process"]):
        return f"Refused: the Open box belongs to {d['owner']!r}, a messaging or email window; Bhanu sends files there himself."
    if len(files) > 1 and _accepts_many(d["hwnd"]) is False:
        return (f"The Open box of {d['owner']!r} takes one file at a time, and {len(files)} match. Nothing was "
                "picked; name one file, or pick them one by one.")
    import confirm
    names_shown = [f.name for f in files[:8]] + ([f"…and {len(files) - 8} more"] if len(files) > 8 else [])
    decision = await confirm.ask_where(ctx, f"Upload {len(files)} file(s) from {base.name} to {d['owner']}?",
                                       names_shown, action="Upload", widget_too=False)
    if decision != "confirm":
        return confirm.OUTCOME.get(decision, "Nothing was picked.") + " The Open box is still open."
    where = files[0].parent if len({f.parent for f in files}) == 1 else _stage(files)
    shown = [f.name for f in files] if where == files[0].parent else sorted(p.name for p in where.iterdir())
    if len(shown) == 1:
        _dialog_submit(d["hwnd"], d["box"], str(where / shown[0]))
    else:
        _dialog_submit(d["hwnd"], d["box"], str(where))  # navigate into the folder first
        time.sleep(1.0)
        if not user32.IsWindow(d["hwnd"]):
            return "The Open box closed when its folder was set; nothing was picked. Open it again and retry."
        _dialog_submit(d["hwnd"], d["box"], " ".join(f'"{n}"' for n in shown))
    for _ in range(25):
        time.sleep(0.2)
        if not user32.IsWindow(d["hwnd"]) or not user32.IsWindowVisible(d["hwnd"]):
            size = sum(f.stat().st_size for f in files)
            return (f"Picked {len(files)} file(s) ({_size(size)}) from {base} in the Open box of {d['owner']!r}; "
                    "the box closed, so the site has them. Its page shows any upload progress or rejected types.")
    return (f"The Open box of {d['owner']!r} is still open after choosing the files; it may be showing an "
            "error (a file type the site doesn't accept, or a single-file box). Look at it with screen_look.")


@server.tool()
def send_screenshot(window: str) -> str:
    """Save a screenshot of one window so it can be sent to Bhanu on Telegram, where he can't see the
    laptop: put the returned MEDIA line on its own line in the reply. Use after a request that changed
    something on screen. Not on the widget (he sees the screen), never messaging windows."""
    win, err = _ui_window(window)
    if err:
        return err
    img, _ = _capture(win["hwnd"])
    w, h = img.size
    scale = min(1.0, 1600 / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)))
    # Hermes sends MEDIA files only from its own cache folders; screenshots/ is one of them.
    folder = Path(os.environ.get("HERMES_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hermes") / "cache" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("twin-*.jpg"):  # keep the folder from growing: a day is plenty
        if time.time() - old.stat().st_mtime > 86400:
            old.unlink(missing_ok=True)
    out = folder / f"twin-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
    img.convert("RGB").save(out, format="JPEG", quality=82)
    return f"Screenshot of {win['title']!r} saved. Put this line, alone, in your reply:\nMEDIA:{out}"


if __name__ == "__main__":
    server.run("stdio")
