"""Shared by the twin's MCP servers: per-user config, course matching and the portal client.

Per-user settings live outside the repo in %LOCALAPPDATA%\\student-twin\\config.json (or under
STUDENT_TWIN_HOME): the portal URL, the courses' portal pages, browser profiles, music, home city
and protected folders.
"""
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
CONFIG_PATH = DATA / "config.json"


def _onedrive_folder() -> str | None:
    """OneDrive's folder from its own registry entry, for processes started without the user's
    environment (Hermes passes MCP servers only the variables listed in its config)."""
    import winreg
    for key in (r"Software\Microsoft\OneDrive", r"Software\Microsoft\OneDrive\Accounts\Personal"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value = winreg.QueryValueEx(k, "UserFolder")[0]
                if value:
                    return value
        except OSError:
            continue
    return None


def expand_path(text: str) -> Path:
    """A config path with environment variables expanded ("%OneDrive%/University"), as an absolute
    path. %OneDrive% is filled in from the registry when the variable is missing. Raises ValueError
    rather than return a path with an unexpanded variable, which would otherwise become a relative
    folder named "%OneDrive%" (it did, 25 Oct 2026)."""
    if re.search(r"%OneDrive(Consumer)?%", text, re.I) and not os.environ.get("OneDrive"):
        folder = _onedrive_folder()
        if folder:
            text = re.sub(r"%OneDrive(Consumer)?%", lambda _: folder, text, flags=re.I)
    expanded = os.path.expandvars(text)
    if re.search(r"%[^%\/]+%|\$\{?\w+", expanded) or not Path(expanded).expanduser().is_absolute():
        raise ValueError(f"the configured path {text!r} could not be expanded to an absolute path")
    return Path(expanded).expanduser()


def config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Statuses that count as attended on SPeCTRUM (English and Malay labels).
PRESENT = re.compile(r"(?i)(?<!not )(?<!tidak )\b(present|hadir)\b")


def session_for(block, activities: list[dict]) -> dict | None:
    """The SPeCTRUM session listed for a timetable class (`timetable.ClassBlock`) among a course's
    attendance activities: same date and start time; or, if that course lists exactly one session
    that day, that one."""
    from datetime import datetime
    day = []
    for act in activities:
        for s in act.get("sessions", []):
            m = re.match(r"\w{3} (\d{1,2} \w{3} \d{4})\s+(\d{1,2})(?::(\d\d))?\s*([AP]M)", s["when"])
            if not m:
                continue
            try:
                date = datetime.strptime(m[1], "%d %b %Y").date()
            except ValueError:
                continue
            if date != block.start.date():
                continue
            hour = int(m[2]) % 12 + (12 if m[4] == "PM" else 0)
            day.append((s, (hour, int(m[3] or 0))))
    exact = [s for s, hm in day if hm == (block.start.hour, block.start.minute)]
    if exact:
        return exact[0]
    return day[0][0] if len(day) == 1 else None


# SPeCTRUM lists a session in the app's view only around its own time (the 8-9 AM session of
# 19 Oct 2026 was listed at 08:20 and gone by 14:39), so every status seen is kept here and
# attendance_today falls back to it.
SEEN = DATA / "attendance_seen.json"


def _load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember_sessions(code: str, activities: list[dict]) -> None:
    """Keep the status of every marked session in `activities` (from Moodle.attendance)."""
    from datetime import datetime
    seen = _load_seen()
    changed = False
    for act in activities:
        for s in act.get("sessions", []):
            key = f"{code}|{s['when']}"
            if s["status"] and seen.get(key, {}).get("status") != s["status"]:
                seen[key] = {"status": s["status"], "seen": datetime.now().strftime("%Y-%m-%d %H:%M")}
                changed = True
    if changed:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEN.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen, indent=1), encoding="utf-8")
        tmp.replace(SEEN)


def remembered_session(code: str, block) -> dict | None:
    """A status seen earlier for this class: from the kept statuses, or from attendance pages saved
    that day in attendance_snapshots/. Adds "seen" ("2026-10-19 08:20")."""
    from moodle import _sessions
    sessions = [{"when": k.split("|", 1)[1], "description": "", "open": False, **v}
                for k, v in _load_seen().items() if k.startswith(code + "|")]
    for f in sorted((DATA / "attendance_snapshots").glob(f"{block.start:%Y%m%d}-*-{code}-*.json")):
        try:
            page = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hhmm = f.name[9:13]
        for s in _sessions(page.get("html", "")):
            if s["status"]:
                sessions.append({**s, "seen": f"{block.start:%Y-%m-%d} {hhmm[:2]}:{hhmm[2:]}"})
    marked = [s for s in sessions if s.get("status")]
    return session_for(block, [{"sessions": marked}]) if marked else None


def _points(rows: dict) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", rows.get("Points over taken sessions", ""))
    return (int(m[1]), int(m[2])) if m else None


def totals_verdict(block, activities: list[dict]) -> tuple[bool | None, str]:
    """(present?, sentence) for a class whose session SPeCTRUM no longer lists, from its totals now
    against those the reminder job saved when the class started. present is None when unknown."""
    now_rows = {str(a["cmid"]): dict(a["rows"]) for a in activities}
    try:
        state = json.loads((DATA / "reminders_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    before = state.get(f"{block.code}-{block.start.isoformat()}", {}).get("before") or {}
    for cmid, old in before.items():
        new = now_rows.get(cmid)
        if not new:
            continue
        taken = int(new.get("Taken sessions", 0)) - int(old.get("Taken sessions", 0))
        p0, p1 = _points(old), _points(new)
        if taken == 1 and p0 and p1:
            got, out_of = p1[0] - p0[0], p1[1] - p0[1]
            present = got == out_of
            verdict = "counted as present" if present else f"recorded with {got} of {out_of} points (absent or late)"
            return present, (f"{verdict}, inferred from SPeCTRUM's totals ({old['Taken sessions']} → "
                             f"{new['Taken sessions']} sessions taken since the class started); it no longer "
                             f"lists the session itself.")
        if taken == 0:
            return False, ("not taken on SPeCTRUM: no session was added to the totals since the class started, "
                           "so either the lecturer hasn't recorded it or there was no SPeCTRUM register.")
    rows = dict(activities[0]["rows"]) if activities else {}
    totals = (f"{rows.get('Taken sessions', '?')} of {rows.get('Total number of sessions', '?')} sessions taken, "
              f"{rows.get('Points over taken sessions', '?')} points")
    return None, (f"SPeCTRUM no longer lists this session and no earlier reading was saved, so its status isn't "
                  f"known. Totals: {totals}.")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_course(course: str) -> tuple[str, dict] | str:
    """(code, info) for a course code ("SIF1014") or part of its name ("thermal physics"), or an
    error sentence listing the courses."""
    courses = config().get("courses", {})
    q = norm(course).replace(" ", "")
    for code, info in courses.items():
        if q and (q == code.lower() or q in code.lower()):
            return code, info
    name = norm(course)
    hits = [code for code, info in courses.items() if name and name in norm(info["name"])]
    if not hits and name.endswith("s"):  # "maths" for Mathematical Methods
        hits = [code for code, info in courses.items() if name[:-1] in norm(info["name"])]
    if len(hits) == 1:
        return hits[0], courses[hits[0]]
    known = ", ".join(f"{c} ({i['name']})" for c, i in courses.items())
    return f"No single course matches {course!r}. Courses: {known}."


def is_announcement_forum(forum: dict) -> bool:
    """A course's built-in Announcements (news) forum, or an ordinary forum a lecturer named for
    announcements: SIF1017 has no news forum and posts in "Announcements (PLEASE READ)"."""
    return forum.get("type") == "news" or bool(re.search(r"(?i)announce|pengumuman", forum.get("name", "")))


def post_text(message: str) -> str:
    """A forum post's HTML as one line of plain text."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", message or "")).split())


def announcement_posts(courses: dict[int, str], days: int = 14) -> list[tuple[int, str, str, dict]]:
    """(posted, course code, forum name, discussion) for the posts of the last `days` days in the
    announcement forums of `courses` ({portal course id: code}), newest first. Raises MoodleError."""
    if not courses:
        return []
    m = moodle()
    since = int(time.time()) - max(days, 1) * 86400
    forums = [f for f in m.call("mod_forum_get_forums_by_courses",
                                **{f"courseids[{i}]": c for i, c in enumerate(courses)})
              if is_announcement_forum(f)]
    posts = []
    for f in forums:
        for d in m.call("mod_forum_get_forum_discussions", forumid=f["id"], sortorder=1,
                        perpage=10).get("discussions", []):
            at = d.get("created") or d.get("timemodified") or 0
            if at >= since:
                posts.append((at, courses.get(f["course"], "?"), f["name"], d))
    return sorted(posts, key=lambda p: -p[0])


# --- Which day a post is about ------------------------------------------------------------------
# Lecturers post class changes in English and Malay: "Tuesday, 10/11/2026 is an online class ya",
# "Tomorrow's class shall be conducted online", "This week will be a face-to-face class".
RELATIVE_DAYS = (("today", 0), ("tonight", 0), ("tomorrow", 1), ("hari ini", 0), ("malam ini", 0),
                 ("esok", 1), ("besok", 1))
_MONTHS = ("jan|january|januari", "feb|february|februari", "mar|march|mac", "apr|april", "may|mei",
           "jun|june", "jul|july|julai", "aug|august|ogos", "sep|sept|september", "oct|october|oktober",
           "nov|november", "dec|december|disember")
_MONTH = "|".join(_MONTHS)
_WEEKDAYS = ("mon|monday|isnin", "tue|tues|tuesday|selasa", "wed|wednesday|rabu",
             "thu|thur|thurs|thursday|khamis", "fri|friday|jumaat", "saturday|sabtu", "sunday|ahad")
_ONLINE = re.compile(r"(?i)\bonline\b|google meet|meet\.google|\bzoom\b|voov|microsoft teams|teams\.microsoft|"
                     r"webex|dalam talian|atas talian")
_IN_PERSON = re.compile(r"(?i)face[- ]to[- ]face|physical (?:class|lecture)|in[- ]person|offline|bersemuka|"
                        r"kelas fizikal")
_ABOUT_CLASS = re.compile(r"(?i)\b(class|classes|lecture|tutorial|kelas|kuliah|venue|room|dewan|bilik)\b"
                          r"|online|cancel|postpone|batal|tangguh")
_MEETING = re.compile(r"(?i)(?:https?://)?(?:meet\.google\.com|[\w.-]*zoom\.us|voovmeeting\.com|"
                      r"teams\.microsoft\.com|teams\.live\.com|[\w.-]*webex\.com)/[^\s<>\"')]+")


def _month(word: str) -> int:
    return next(i + 1 for i, names in enumerate(_MONTHS) if re.fullmatch(names, word, re.I))


def dates_in(text: str, posted: date) -> set[date]:
    """Calendar dates written in a post: 10/11, 10/11/2026, 2026/11/06, 6 Nov, 5th November,
    November 5. Day before month, as in Malaysia. A missing year is the post's own, or the next
    one for a date more than two months before the post. Times ("3.30pm", "10-11am") are not dates,
    and a bare "3/4" counts only if it falls within two weeks before to two months after the post
    (a maths post may write fractions)."""
    found = set()

    def add(year, month, day, near):
        try:
            when = date(int(year) if year else posted.year, int(month), int(day))
            if not year and (posted - when).days > 60:
                when = when.replace(year=when.year + 1)
        except ValueError:  # 31/2, or 29 Feb in the following year
            return
        if near and not -14 <= (when - posted).days <= 60:
            return
        found.add(when)

    def take(pattern, order, near=False):
        nonlocal text
        for m in re.finditer(pattern, text, re.I):
            y, mo, d = (m[i] if i else None for i in order)
            if y and len(y) == 2:
                y = "20" + y
            add(y, mo if mo.isdigit() else _month(mo), d, near)
        text = re.sub(pattern, " ", text, flags=re.I)

    take(r"(?<!\d)(20\d\d)[/.-](\d{1,2})[/.-](\d{1,2})(?!\d)", (1, 2, 3))
    take(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d|\d\d)(?!\d)", (3, 2, 1))
    take(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])(?!\s*[ap]\.?m)", (0, 2, 1), near=True)
    take(rf"(?<![\d/])(\d{{1,2}})(?:st|nd|rd|th|hb)?\s+(?:of\s+)?({_MONTH})\b(?:\s+(20\d\d))?", (3, 2, 1))
    take(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(20\d\d))?", (3, 1, 2))
    return found


def post_about_day(text: str, posted: datetime, day: date) -> bool:
    """Whether a post (subject and text) is about `day`. A post that writes a date is about the
    dates it writes. Otherwise: "today"/"tomorrow" counted from the post's own date, then a weekday
    named within the week after the post, then "this week"/"next week", and last, a post from that
    same day that talks about a class (online, cancelled, a room)."""
    p = posted.date()
    gap = (day - p).days
    if gap < 0:
        return False
    written = dates_in(text, p)
    if written:
        return day in written
    relative = [n for w, n in RELATIVE_DAYS if re.search(rf"(?i)\b{w}\b", text)]
    if relative:
        return gap in relative
    if re.search(rf"(?i)\b(?:{'|'.join(_WEEKDAYS)})\b", text):  # names a weekday: that one only
        return gap <= 7 and bool(re.search(rf"(?i)\b(?:{_WEEKDAYS[day.weekday()]})\b", text))
    if re.search(r"(?i)\bthis week\b|minggu ini", text) and p.isocalendar()[:2] == day.isocalendar()[:2]:
        return True
    if re.search(r"(?i)\bnext week\b|minggu depan", text) and \
            (p + timedelta(days=7)).isocalendar()[:2] == day.isocalendar()[:2]:
        return True
    return gap == 0 and bool(_ABOUT_CLASS.search(text))


def class_announcements(block, posts: list[tuple[int, str, str, dict]]) -> tuple[str, list[str]]:
    """Where a timetable class (`timetable.ClassBlock`) is, and lines on the announcements about
    its day from `posts` (announcement_posts). The newest post that says online or in person (not
    both, as a poll does) decides where the class is (SIF1017's lecturer moved Tue 10 Nov online by
    announcement; the timetable kept DKFIZIK) and is quoted in full; up to two others by title."""
    where = "online" if block.online else block.location
    about = []
    for at, code, _forum, d in posts:
        posted = datetime.fromtimestamp(at, block.start.tzinfo)
        subject, text = d.get("subject", "").strip(), post_text(d.get("message", ""))
        if code == block.code and post_about_day(f"{subject} {text}", posted, block.start.date()):
            about.append((posted, d.get("userfullname", "").strip(" .") or "the lecturer", subject, text))
    if not about:
        return where, []
    modes = [(a, bool(_ONLINE.search(f"{a[2]} {a[3]}"))) for a in about
             if bool(_ONLINE.search(f"{a[2]} {a[3]}")) != bool(_IN_PERSON.search(f"{a[2]} {a[3]}"))]
    main = modes[0][0] if modes else about[0]
    if modes and modes[0][1] and not block.online:
        where = f"online, per the announcement below (the timetable says {block.location})"
    elif modes and not modes[0][1] and block.online:
        where = "in person, per the announcement below (the timetable says online)"
    posted, author, subject, text = main
    shown = text[:280] + ("…" if len(text) > 280 else "")
    links = [u for u in dict.fromkeys(_MEETING.findall(text)) if u not in shown]
    lines = [f"Announcement, {posted:%a %d %b %H:%M}, {author}: {subject}"
             + (f"{'' if subject[-1:] in '.!?' else '.'} {shown}" if shown else "")
             + (f" Link: {' '.join(links)}" if links else "")]
    lines += [f"Also: {p:%a %d %b %H:%M}, {s}" for p, _, s, _ in [a for a in about if a is not main][:2]]
    return where, lines


def course_id(info: dict) -> int | None:
    value = parse_qs(urlparse(info.get("url", "")).query).get("id", [None])[0]
    return int(value) if value else None


_client = None


def moodle():
    from moodle import Moodle  # imported lazily: only the portal tools need it
    global _client
    portal = config().get("portal", {})
    if _client is None or _client.site != portal.get("url", "").rstrip("/"):
        _client = Moodle(portal["url"], portal.get("token_credential", "student-twin/spectrum-token"))
    return _client


def course_contents(course: str):
    """(code, info, sections) or an error sentence."""
    match = match_course(course)
    if isinstance(match, str):
        return match
    code, info = match
    cid = course_id(info)
    if not cid or not config().get("portal", {}).get("url"):
        return f"{code} has no portal course id or the portal isn't configured."
    from moodle import MoodleError
    try:
        return code, info, moodle().course_contents(cid)
    except MoodleError as exc:
        return f"Couldn't read {code} from the portal: {exc}."
