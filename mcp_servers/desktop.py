"""Student Twin desktop tools: direct, reversible actions on this computer (slice 2, step 2d-i).

An MCP server Hermes starts over stdio. Enabled for the desktop widget and the local CLI only,
never for Telegram (platform_toolsets.telegram includes `no_mcp`).

Every tool here is reversible: opening things, media controls, moving windows. Irreversible
actions (deleting, uninstalling, paying, submitting) are not in this server; they go through the
confirm card (step 2d-ii).

Per-user settings (course pages, default music) come from %LOCALAPPDATA%\\student-twin\\config.json.
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
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer

CONFIG_PATH = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin") / "config.json"

server = MCPServer(
    "desktop",
    instructions=(
        "Direct actions on Bhanu's Windows computer. Prefer these over screen control: they are fast "
        "and reliable. For weather or general facts use web search instead."
    ),
)

# Coordinates in this process are physical pixels on every monitor.
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
user32 = ctypes.windll.user32
psapi_k32 = ctypes.windll.kernel32


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# --- Opening things ---------------------------------------------------------------------------
@server.tool()
def open_url(url: str) -> str:
    """Open a web page in the default browser. Only http and https addresses are allowed."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"Refused: only http(s) web addresses can be opened, not {url!r}."
    os.startfile(url.strip())
    return f"Opened {url.strip()} in the browser."


@server.tool()
def open_portal() -> str:
    """Open Bhanu's university portal (SPeCTRUM, UM's Moodle site) in the browser."""
    portal = _config().get("portal")
    if not portal:
        return "No portal is configured in config.json."
    os.startfile(portal["url"])
    return f"Opened {portal['name']}."


@server.tool()
def open_course(course: str) -> str:
    """Open a course's page on the university portal, where its notes and slides are posted.
    `course` is a course code such as "SIF1014" or part of a course name such as "thermal physics"."""
    courses = _config().get("courses", {})
    q = _norm(course).replace(" ", "")
    for code, info in courses.items():
        if q == code.lower() or q in code.lower():
            os.startfile(info["url"])
            return f"Opened {code} {info['name']} on the portal."
    by_name = {code: _norm(info["name"]) for code, info in courses.items()}
    hits = [code for code, name in by_name.items() if _norm(course) and _norm(course) in name]
    if len(hits) == 1:
        info = courses[hits[0]]
        os.startfile(info["url"])
        return f"Opened {hits[0]} {info['name']} on the portal."
    known = ", ".join(f"{c} ({i['name']})" for c, i in courses.items())
    return f"No single course matches {course!r}. Courses: {known}."


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
    """Open an installed app by name, e.g. "Spotify", "VS Code", "Notepad", "Chrome".
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
        os.startfile(f"shell:AppsFolder\\{app['AppID']}")
        return f"Opened {app['Name']}."
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
async def play_music(what: str = "") -> str:
    """Start Bhanu's music in Spotify. With no argument (or "my playlist", "my library") it plays his
    Liked Songs from the start of the list."""
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


def _match_window(query: str):
    aliases = {"vs code": "code.exe", "vscode": "code.exe", "visual studio code": "code.exe",
               "chrome": "chrome.exe", "edge": "msedge.exe", "spotify": "spotify.exe"}
    q = query.strip().lower()
    wins = _windows()
    proc = aliases.get(q)
    hits = [w for w in wins if proc and w["process"].lower() == proc]
    hits = hits or [w for w in wins if q in w["title"].lower()]
    hits = hits or [w for w in wins if q in w["process"].lower()]
    return hits[0] if hits else None


def _place(hwnd, rect):
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE: un-maximise and un-minimise first
    user32.SetWindowPos(hwnd, None, rect[0], rect[1], rect[2], rect[3], 0x0004 | 0x0040)  # NOZORDER|SHOWWINDOW


@server.tool()
def arrange_windows(left: str, right: str) -> str:
    """Put two windows side by side, each filling half of the screen. `left` and `right` are part of
    a window title (e.g. "PyTorch") or a program ("VS Code", "Chrome", "Spotify").
    Open a page first with open_url if it isn't open yet."""
    lw, rw = _match_window(left), _match_window(right)
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
    return f"Arranged: {lw['title']} on the left, {rw['title']} on the right."


if __name__ == "__main__":
    server.run("stdio")
