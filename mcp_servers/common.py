"""Shared by the twin's MCP servers: per-user config, course matching and the portal client.

Per-user settings live outside the repo in %LOCALAPPDATA%\\student-twin\\config.json (or under
STUDENT_TWIN_HOME): the portal URL, the courses' portal pages, browser profiles, music, home city
and protected folders.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
CONFIG_PATH = DATA / "config.json"


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
    hits = [code for code, info in courses.items() if norm(course) and norm(course) in norm(info["name"])]
    if len(hits) == 1:
        return hits[0], courses[hits[0]]
    known = ", ".join(f"{c} ({i['name']})" for c, i in courses.items())
    return f"No single course matches {course!r}. Courses: {known}."


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
