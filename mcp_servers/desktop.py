"""Student Twin desktop tools: direct, reversible actions on this computer (slice 2, step 2d-i).

An MCP server Hermes starts over stdio. Enabled for the desktop widget and the local CLI only,
never for Telegram (platform_toolsets.telegram includes `no_mcp`).

Every tool here is reversible: opening things, media controls, moving, focusing and closing
windows (closing asks the program, as its X button does, so it can still offer to save). Irreversible
actions (deleting, uninstalling, paying, submitting) are not in this server; they go through the
confirm card (step 2d-ii).

Course contents come from the portal's Moodle API through moodle.py (read-only allowlist).
Per-user settings (course pages, browser profiles, default music, home city) come from
%LOCALAPPDATA%\\student-twin\\config.json.
Runs in the isolated student-twin venv: mcp, winrt-Windows.Media.Control, uiautomation.
"""
import asyncio
import ctypes
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

from mcp.server.mcpserver import MCPServer

CONFIG_PATH = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin") / "config.json"

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


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


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


def _match_course(course: str) -> tuple[str, dict] | str:
    """(code, info) for a course code ("SIF1014") or part of its name ("thermal physics"), or an
    error sentence listing the courses."""
    courses = _config().get("courses", {})
    q = _norm(course).replace(" ", "")
    for code, info in courses.items():
        if q and (q == code.lower() or q in code.lower()):
            return code, info
    hits = [code for code, info in courses.items() if _norm(course) and _norm(course) in _norm(info["name"])]
    if len(hits) == 1:
        return hits[0], courses[hits[0]]
    known = ", ".join(f"{c} ({i['name']})" for c, i in courses.items())
    return f"No single course matches {course!r}. Courses: {known}."


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


def _moodle():
    from moodle import Moodle  # imported lazily: only the course tools need it
    portal = _config().get("portal", {})
    global _moodle_client
    if _moodle_client is None or _moodle_client.site != portal.get("url", "").rstrip("/"):
        _moodle_client = Moodle(portal["url"], portal.get("token_credential", "student-twin/spectrum-token"))
    return _moodle_client


_moodle_client = None


def _course_contents(course: str):
    """(code, info, sections) or an error sentence."""
    match = _match_course(course)
    if isinstance(match, str):
        return match
    code, info = match
    course_id = parse_qs(urlparse(info["url"]).query).get("id", [None])[0]
    if not course_id or not _config().get("portal", {}).get("url"):
        return f"{code} has no portal course id or the portal isn't configured."
    from moodle import MoodleError
    try:
        return code, info, _moodle().course_contents(int(course_id))
    except MoodleError as exc:
        return f"Couldn't read {code} from the portal: {exc}."


@server.tool()
def course_materials(course: str) -> str:
    """List what is posted in a course on the university portal: lecture notes, slides, files,
    folders, links, assignments and quizzes, section by section. Read-only. Use it to answer what is
    in a course or to find the exact name of an item before open_course_item."""
    got = _course_contents(course)
    if isinstance(got, str):
        return got
    code, info, sections = got
    lines = [f"{code} {info['name']}:"]
    for sec in sections:
        lines.append(f"## {sec['name']}")
        for m in sec["modules"]:
            files = f" (files: {', '.join(m['files'][:5])}{', …' if len(m['files']) > 5 else ''})" if m["files"] else ""
            lines.append(f"- [{m['type']}] {m['name']}{files}")
    text = "\n".join(lines)
    return text if len(text) <= 6000 else text[:6000] + "\n… (list cut short)"


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
        tests.append(lambda w: any(word in _norm(w["title"]).split() or word in w["process"].lower() for word in words))
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
    `name` is part of the tab's page title, and it works on the tab showing in front of its window)."""
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
        if len(hits) > 1:
            return "Several windows match; say which to close: " + "; ".join(w["title"] for w in hits[:8]) + "."
        user32.PostMessageW(win["hwnd"], 0x0010, 0, 0)  # WM_CLOSE
        return f"Asked {win['title']} to close."
    if act == "minimize":
        user32.ShowWindow(win["hwnd"], 6)  # SW_MINIMIZE
        return f"Minimized {win['title']}."
    if act in ("maximize", "restore"):
        user32.ShowWindow(win["hwnd"], 3 if act == "maximize" else 9)  # SW_MAXIMIZE / SW_RESTORE
    focused = _focus(win["hwnd"])
    done = {"focus": "Brought {} to the front", "maximize": "Maximized {}", "restore": "Restored {}"}[act]
    return done.format(win["title"]) + ("." if focused else ", but Windows didn't let me bring it to the front.")


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


if __name__ == "__main__":
    server.run("stdio")
