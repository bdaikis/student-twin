"""Deadlines (slice 3): one store for everything Bhanu has to hand in or sit, wherever it comes from.

Sources: SPeCTRUM (the dashboard timeline, assignments with his submission status, quizzes with his
attempts), dates read from the course-information forms, and deadlines he adds himself. SPeCTRUM
lists none of the tests, which is why the other two exist.

The store is a SQLite file in the twin's data folder: one row per item plus a history of changes
(new, moved, removed, status), which is what lets the twin notice a new or moved deadline.
Reading SPeCTRUM goes through moodle.py's read-only allowlist; nothing here writes to the portal.
"""
import html
import re
import sqlite3
import time
from datetime import datetime, timedelta

import timetable
from common import DATA, config, course_id, match_course, moodle

DB = DATA / "deadlines.db"
REFRESH_EVERY = 10 * 60  # seconds; the tools refresh from SPeCTRUM at most this often
DONE = {"submitted", "attempted"}  # statuses that mean nothing is left to do


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            key TEXT PRIMARY KEY,      -- spectrum:assign:<id>, spectrum:quiz:<id>, spectrum:<module>:<id>, form:..., own:...
            source TEXT NOT NULL,      -- spectrum | course form | added
            course TEXT NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,        -- assignment | quiz | forum | test | other
            opens INTEGER,             -- unix time, or NULL
            due INTEGER,               -- unix time, or NULL
            precision TEXT NOT NULL DEFAULT 'time',  -- time | day | week
            status TEXT NOT NULL DEFAULT 'open',     -- open | started | submitted | attempted | not open
            url TEXT,
            note TEXT,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            removed INTEGER            -- unix time it disappeared from SPeCTRUM, or NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL, at INTEGER NOT NULL, change TEXT NOT NULL, old TEXT, new TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def _clean(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def _courses() -> dict[int, str]:
    """{portal course id: course code} for the courses in the per-user config."""
    out = {}
    for code, info in (config().get("courses") or {}).items():
        cid = course_id(info)
        if cid:
            out[cid] = code
    return out


# --- Reading SPeCTRUM ---------------------------------------------------------------------------
def _from_spectrum(now: int) -> dict[str, dict]:
    """Every current deadline on SPeCTRUM, keyed like the store. Read-only."""
    m = moodle()
    courses = _courses()
    params = {f"courseids[{i}]": cid for i, cid in enumerate(courses)}
    items: dict[str, dict] = {}

    for c in m.call("mod_assign_get_assignments", **params).get("courses", []):
        code = courses.get(c["id"], c.get("shortname", "?"))
        for a in c.get("assignments", []):
            if not a.get("duedate") or a["duedate"] < now - 30 * 86400:
                continue
            status = "open"
            try:
                last = (m.call("mod_assign_get_submission_status", assignid=a["id"]).get("lastattempt") or {})
                sub = last.get("submission") or last.get("teamsubmission") or {}
                status = {"submitted": "submitted", "draft": "started"}.get(sub.get("status"), "open")
            except Exception:
                pass
            if a.get("allowsubmissionsfromdate") and a["allowsubmissionsfromdate"] > now and status == "open":
                status = "not open"
            items[f"spectrum:assign:{a['id']}"] = dict(
                source="spectrum", course=code, title=_clean(a["name"]), kind="assignment",
                opens=a.get("allowsubmissionsfromdate") or None, due=a["duedate"], precision="time",
                status=status, url=f"{m.site}/mod/assign/view.php?id={a['cmid']}", note=None)

    for q in m.call("mod_quiz_get_quizzes_by_courses", **params).get("quizzes", []):
        close = q.get("timeclose") or 0
        if not close or close < now - 30 * 86400:
            continue
        status = "open" if (q.get("timeopen") or 0) <= now else "not open"
        try:
            attempts = m.call("mod_quiz_get_user_attempts", quizid=q["id"], status="all").get("attempts", [])
            if any(a.get("state") == "finished" for a in attempts):
                status = "attempted"
            elif attempts:
                status = "started"
        except Exception:
            pass
        items[f"spectrum:quiz:{q['id']}"] = dict(
            source="spectrum", course=courses.get(q["course"], "?"), title=_clean(q["name"]), kind="quiz",
            opens=q.get("timeopen") or None, due=close, precision="time", status=status,
            url=f"{m.site}/mod/quiz/view.php?id={q['coursemodule']}", note=None)

    # The dashboard timeline adds what the two above don't cover (forum posts due, lessons, ...).
    events = m.call("core_calendar_get_action_events_by_timesort", timesortfrom=now - 30 * 86400,
                    timesortto=now + 200 * 86400, limitnum=50).get("events", [])
    for e in events:
        module, inst = e.get("modulename") or "other", e.get("instance")
        if module in ("assign", "quiz") or not inst:
            continue
        key = f"spectrum:{module}:{inst}"
        title = re.sub(r"\s+is due$", "", _clean(e["name"]))
        items[key] = dict(
            source="spectrum", course=courses.get((e.get("course") or {}).get("id"), "?"), title=title,
            kind="forum" if module == "forum" else "other", opens=None, due=e["timesort"], precision="time",
            status="open", url=e.get("url"), note=None)
    return items


def refresh(force: bool = False) -> list[dict]:
    """Bring the store up to date with SPeCTRUM. Returns the changes recorded: dicts with key,
    change (new | moved | status | removed), old, new and the item. At most every REFRESH_EVERY
    seconds unless forced."""
    now = int(time.time())
    db = _connect()
    try:
        last = db.execute("SELECT value FROM meta WHERE name='refreshed'").fetchone()
        if not force and last and now - int(last[0]) < REFRESH_EVERY:
            return []
        fresh = _from_spectrum(now)
        changes = []
        old = {r["key"]: dict(r) for r in db.execute("SELECT * FROM items WHERE source='spectrum'")}
        for key, it in fresh.items():
            prev = old.get(key)
            if prev is None:
                db.execute("""INSERT INTO items (key, source, course, title, kind, opens, due, precision, status, url,
                              note, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (key, it["source"], it["course"], it["title"], it["kind"], it["opens"], it["due"],
                            it["precision"], it["status"], it["url"], it["note"], now, now))
                changes.append({"key": key, "change": "new", "old": None, "new": it["due"], "item": it})
                continue
            if prev["removed"]:
                changes.append({"key": key, "change": "new", "old": None, "new": it["due"], "item": it})
            if prev["due"] != it["due"]:
                changes.append({"key": key, "change": "moved", "old": prev["due"], "new": it["due"], "item": it})
            if prev["status"] != it["status"]:
                changes.append({"key": key, "change": "status", "old": prev["status"], "new": it["status"], "item": it})
            db.execute("""UPDATE items SET course=?, title=?, kind=?, opens=?, due=?, status=?, url=?, last_seen=?,
                          removed=NULL WHERE key=?""",
                       (it["course"], it["title"], it["kind"], it["opens"], it["due"], it["status"], it["url"], now, key))
        for key, prev in old.items():
            # Gone from SPeCTRUM while still in the future: worth saying. Past items just age out.
            if key not in fresh and not prev["removed"] and (prev["due"] or 0) > now:
                db.execute("UPDATE items SET removed=? WHERE key=?", (now, key))
                changes.append({"key": key, "change": "removed", "old": prev["due"], "new": None, "item": prev})
        for c in changes:
            db.execute("INSERT INTO history (key, at, change, old, new) VALUES (?,?,?,?,?)",
                       (c["key"], now, c["change"], None if c["old"] is None else str(c["old"]),
                        None if c["new"] is None else str(c["new"])))
        db.execute("INSERT OR REPLACE INTO meta (name, value) VALUES ('refreshed', ?)", (str(now),))
        db.commit()
        return changes
    finally:
        db.close()


# --- Deadlines he adds, and dates from the course forms ------------------------------------------
_TIME = re.compile(r"(?i)\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b(?!\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))")


def parse_when(text: str, course: str | None = None) -> tuple[datetime, str] | str:
    """(local datetime, precision) from "27 Oct", "Tue 27 Oct 9am", "tomorrow 14:00", "next monday",
    or "week 8"; an error sentence otherwise. With no time and a class of `course` that day, the
    class's start time is used."""
    t = " ".join(text.lower().split())
    m = re.fullmatch(r"week\s+(\d{1,2})", t)
    if m:
        week = int(m[1])
        for p in (config().get("semester") or {}).get("periods", []):
            if "first_week" in p:
                start = datetime.strptime(p["from"], "%Y-%m-%d")
                end = datetime.strptime(p["to"], "%Y-%m-%d")
                monday = start + timedelta(days=7 * (week - p["first_week"]))
                if start <= monday <= end:
                    return monday.replace(tzinfo=timetable.LOCAL) + timedelta(days=6, hours=23, minutes=59), "week"
        return f"Week {week} isn't in the semester calendar."
    tm = None
    for m in _TIME.finditer(t):
        if m[2] or m[3]:  # "14:00", "9am"; a bare number is part of the date
            tm = m
    day_text = (t[:tm.start()] + t[tm.end():]).strip() if tm else t
    # "Tue 27 Oct": the weekday is redundant next to a date (and parse_day reads one or the other).
    day_text = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+(?=\d)", "", day_text)
    day = timetable.parse_day(day_text or "today")
    if isinstance(day, str):
        return f"Couldn't read the date in {text!r}: {day}"
    if tm:
        hour = int(tm[1]) % 12 + (12 if (tm[3] or "").lower() == "pm" else 0) if tm[3] else int(tm[1])
        return day.replace(hour=hour, minute=int(tm[2] or 0), second=0, microsecond=0), "time"
    if course:
        try:
            for b in timetable.on_day(day):
                if b.code == course and b.activity != "LAB":
                    return b.start, "time"
        except RuntimeError:
            pass
    return day.replace(hour=23, minute=59, second=0, microsecond=0), "day"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def add(course: str, title: str, when: str, note: str = "", source: str = "added", kind: str = "test") -> str:
    match = match_course(course)
    if isinstance(match, str):
        return match
    code = match[0]
    parsed = parse_when(when, code)
    if isinstance(parsed, str):
        return parsed
    due, precision = parsed
    key = f"{'form' if source == 'course form' else 'own'}:{code}:{_slug(title)}"
    now = int(time.time())
    db = _connect()
    try:
        prev = db.execute("SELECT due FROM items WHERE key=?", (key,)).fetchone()
        db.execute("""INSERT OR REPLACE INTO items (key, source, course, title, kind, opens, due, precision, status,
                      url, note, first_seen, last_seen) VALUES (?,?,?,?,?,NULL,?,?,'open',NULL,?,
                      COALESCE((SELECT first_seen FROM items WHERE key=?), ?), ?)""",
                   (key, source, code, title.strip(), kind, int(due.timestamp()), precision, note or None, key, now, now))
        db.execute("INSERT INTO history (key, at, change, old, new) VALUES (?,?,?,?,?)",
                   (key, now, "moved" if prev else "new", str(prev[0]) if prev else None, str(int(due.timestamp()))))
        db.commit()
    finally:
        db.close()
    return f"{'Updated' if prev else 'Added'}: {code} {title.strip()}, {describe_due(int(due.timestamp()), precision)}."


def remove(course: str, title: str) -> str:
    """Remove a deadline he added (or one pre-loaded from a course form). SPeCTRUM's own can't be."""
    match = match_course(course)
    if isinstance(match, str):
        return match
    code = match[0]
    want = set(_slug(title).split("-"))
    db = _connect()
    try:
        rows = [r for r in db.execute("SELECT key, title FROM items WHERE course=? AND source!='spectrum'", (code,))
                if want <= set(_slug(r["title"]).split("-"))]
        if not rows:
            return f"No deadline you added for {code} matches {title!r}. SPeCTRUM's own deadlines can't be removed here."
        if len(rows) > 1:
            return "Several match: " + "; ".join(r["title"] for r in rows) + ". Say which."
        db.execute("DELETE FROM items WHERE key=?", (rows[0]["key"],))
        db.execute("INSERT INTO history (key, at, change, old, new) VALUES (?,?,?,?,NULL)",
                   (rows[0]["key"], int(time.time()), "removed", rows[0]["title"]))
        db.commit()
        return f"Removed {code} {rows[0]['title']}."
    finally:
        db.close()


# --- Answering --------------------------------------------------------------------------------
def describe_due(due: int, precision: str, now: datetime | None = None) -> str:
    now = now or timetable.now()
    d = datetime.fromtimestamp(due, timetable.LOCAL)
    if precision == "week":
        monday = d - timedelta(days=6, hours=23, minutes=59)
        return f"in the week of {monday:%a %d %b} (no exact day given)"
    days = (d.date() - now.date()).days
    when = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days, f"{d:%a %d %b}")
    if days not in (0, 1, -1) and 1 < days < 7:
        when = f"{d:%A} {d:%d %b}"
    return f"{when}" + ("" if precision == "day" else f" {d:%H:%M}")


def upcoming(days: int = 7, course: str = "", include_done: bool = False) -> list[dict]:
    """Items due from 14 days ago (overdue) up to `days` ahead, soonest first."""
    now = int(time.time())
    db = _connect()
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM items WHERE removed IS NULL AND due IS NOT NULL AND due BETWEEN ? AND ? ORDER BY due",
            (now - 14 * 86400, now + days * 86400))]
    finally:
        db.close()
    if course:
        m = match_course(course)
        if not isinstance(m, str):
            rows = [r for r in rows if r["course"] == m[0]]
    return [r for r in rows if include_done or r["status"] not in DONE or r["due"] >= now]


def line(r: dict, now: datetime | None = None) -> str:
    now = now or timetable.now()
    status = {"submitted": "submitted ✓", "attempted": "attempted ✓", "started": "started, not submitted",
              "open": "", "not open": "not open yet"}.get(r["status"], r["status"])
    if r["source"] == "spectrum" and r["kind"] in ("assignment",) and r["status"] == "open":
        status = "not submitted"
    if r["kind"] == "quiz" and r["status"] == "open":
        status = "not attempted"
    window = ""
    if r["kind"] == "quiz" and r.get("opens"):
        o = datetime.fromtimestamp(r["opens"], timetable.LOCAL)
        window = f" (opens {o:%a %d %b %H:%M})" if o > now else ""
    src = {"course form": " [from the course form]", "added": " [added by you]"}.get(r["source"], "")
    note = f" ({r['note']})" if r.get("note") else ""
    verb = {"quiz": "closes", "test": "on"}.get(r["kind"], "due")
    return (f"{r['course']} {r['title']}{note}: {verb} {describe_due(r['due'], r['precision'], now)}{window}"
            + (f" — {status}" if status else "") + src)
